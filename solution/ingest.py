"""Ingestion: leads (CSV) and events (JSONL) into the SQLite store.

Order of work inside one drop: lead files first (so events in the same drop
can link immediately), then event files, then an orphan re-link pass.
Re-ingesting a drop whose fingerprint is already recorded is a no-op.
"""
from __future__ import annotations

import csv
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .db import Store
from .normalize import (
    normalise_header,
    normalise_phone,
    parse_date,
    parse_decimal,
    parse_enum,
    parse_timestamp,
    utc_iso,
)


# --------------------------------------------------------------------------
# drop fingerprint
# --------------------------------------------------------------------------

def drop_fingerprint(drop: Path) -> str:
    """SHA-256 over sorted (relative path, file bytes) of the whole drop."""
    h = hashlib.sha256()
    files = sorted(
        (p for p in drop.rglob("*") if p.is_file()),
        key=lambda p: str(p.relative_to(drop)),
    )
    for p in files:
        rel = str(p.relative_to(drop)).encode("utf-8")
        data = p.read_bytes()
        h.update(rel + b"\0" + str(len(data)).encode() + b"\0" + data)
    return h.hexdigest()


# --------------------------------------------------------------------------
# lead ingestion
# --------------------------------------------------------------------------

def _parse_field(field: dict, raw: str | None):
    """Return (normalised value, ok). ``raw`` is None when missing/empty."""
    if raw is None:
        return None, True
    ftype = field["type"]
    if ftype == "string":
        return raw, True
    if ftype == "phone":
        v = normalise_phone(raw)
        return v, v is not None
    if ftype == "date":
        v = parse_date(raw)
        return v, v is not None
    if ftype == "decimal":
        v = parse_decimal(raw)
        return v, v is not None
    if ftype == "enum":
        v = parse_enum(raw, field.get("values", []))
        return v, v is not None
    return raw, True


def _field_map(header: list[str], fields: list[dict]) -> dict[str, int]:
    """Map field name -> column index. First column wins on duplicates."""
    wanted = {}
    for f in fields:
        wanted[normalise_header(f["name"])] = f["name"]
        for alias in f.get("aliases", []):
            wanted.setdefault(normalise_header(alias), f["name"])
    mapping: dict[str, int] = {}
    for idx, col in enumerate(header):
        fname = wanted.get(normalise_header(col))
        if fname is not None and fname not in mapping:
            mapping[fname] = idx
    return mapping


def ingest_lead_file(store: Store, path: Path, workflow_id: str, schema: dict) -> None:
    """Ingest one lead CSV. One transaction for the whole file."""
    fields = schema["fields"]
    by_name = {f["name"]: f for f in fields}
    ref_field = next((f["name"] for f in fields if f.get("reference")), None)
    batch_id = path.stem
    conn = store.conn
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            return  # empty file: no header, no rows
        colmap = _field_map(header, fields)
        row_number = 0
        with conn:  # one transaction per file
            for raw_row in reader:
                if all(c.strip() == "" for c in raw_row):
                    continue  # blank row: skipped, not counted
                row_number += 1
                store.bump("lead_rows_total")
                cells = list(raw_row) + [""] * max(0, len(header) - len(raw_row))
                values: dict[str, str | None] = {}
                reason = None
                warnings = 0
                for field in fields:
                    fname = field["name"]
                    idx = colmap.get(fname)
                    raw = None
                    if idx is not None:
                        v = cells[idx].strip()
                        raw = v if v != "" else None
                    if raw is None:
                        if field.get("required"):
                            reason = f"missing_required:{fname}"
                            break
                        values[fname] = None
                        continue
                    value, ok = _parse_field(field, raw)
                    if not ok:
                        if fname == "phone":
                            reason = "invalid_phone"
                            break
                        # No other rejection reason exists in the spec:
                        # a required non-phone field that is present but
                        # invalid (no such field in the shipped schemas)
                        # is treated like an optional invalid field.
                        warnings += 1
                        values[fname] = None
                        continue
                    values[fname] = value
                if reason is not None:
                    conn.execute(
                        """INSERT OR REPLACE INTO rejections
                           (workflow_id, batch_id, row_number, reason)
                           VALUES (?, ?, ?, ?)""",
                        (workflow_id, batch_id, row_number, reason),
                    )
                    store.bump("lead_rows_rejected")
                    continue
                if warnings:
                    store.bump("field_warnings", warnings)
                _upsert_lead(store, workflow_id, batch_id, values, by_name, ref_field)


def _upsert_lead(store, workflow_id, batch_id, values, by_name, ref_field):
    phone_field = next(
        (f["name"] for f in by_name.values() if f["type"] == "phone"), "phone"
    )
    name_field = next(
        (f["name"] for f in by_name.values() if f.get("pii") and f["type"] == "string"),
        "customer_name",
    )
    phone = values[phone_field]  # identity field; always non-null here
    phone_hash = store.hash_phone(phone)
    name = values.get(name_field) or ""
    existing = store.conn.execute(
        "SELECT * FROM leads WHERE workflow_id = ? AND phone_hash = ?",
        (workflow_id, phone_hash),
    ).fetchone()
    attrs = {
        fname: v
        for fname, v in values.items()
        if v is not None
        and not by_name[fname].get("pii")
        and fname != ref_field
    }
    if existing is None:
        store.conn.execute(
            """INSERT INTO leads
               (workflow_id, phone_hash, phone_last4, name_hash, batch_id,
                external_ref, attrs_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                workflow_id,
                phone_hash,
                phone[-4:],
                store.hash_name(name),
                batch_id,
                values.get(ref_field) or "",
                json.dumps(attrs, sort_keys=True),
            ),
        )
        return
    # Duplicate: overwrite only fields whose new value is non-null.
    store.bump("duplicate_lead_rows_merged")
    merged_attrs = json.loads(existing["attrs_json"])
    merged_attrs.update(attrs)
    new_name_hash = store.hash_name(name) if values.get(name_field) else existing["name_hash"]
    new_ref = values.get(ref_field) or existing["external_ref"]
    store.conn.execute(
        """UPDATE leads SET name_hash = ?, external_ref = ?, attrs_json = ?
           WHERE workflow_id = ? AND phone_hash = ?""",
        (
            new_name_hash,
            new_ref,
            json.dumps(merged_attrs, sort_keys=True),
            workflow_id,
            phone_hash,
        ),
    )


def apply_defaults(store: Store, schemas: dict[str, dict]) -> None:
    """Apply schema defaults to fields still null, after merging."""
    for workflow_id, schema in schemas.items():
        defaults = {
            f["name"]: f["default"] for f in schema["fields"] if "default" in f
        }
        if not defaults:
            continue
        rows = store.conn.execute(
            "SELECT phone_hash, attrs_json FROM leads WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchall()
        with store.conn:
            for row in rows:
                attrs = json.loads(row["attrs_json"])
                changed = False
                for fname, default in defaults.items():
                    if attrs.get(fname) is None:
                        attrs[fname] = default
                        changed = True
                if changed:
                    store.conn.execute(
                        """UPDATE leads SET attrs_json = ?
                           WHERE workflow_id = ? AND phone_hash = ?""",
                        (json.dumps(attrs, sort_keys=True), workflow_id, row["phone_hash"]),
                    )


def ingest_lead_files(store: Store, drop: Path, schemas: dict[str, dict]) -> None:
    leads_dir = drop / "leads"
    if not leads_dir.is_dir():
        return
    for wf_dir in sorted(leads_dir.iterdir()):
        if not wf_dir.is_dir():
            continue
        schema = schemas.get(wf_dir.name)
        if schema is None:
            print(
                f"warning: unknown workflow directory '{wf_dir.name}' "
                f"(no schema file); skipping its lead files",
                file=sys.stderr,
            )
            continue
        for path in sorted(wf_dir.glob("*.csv")):
            ingest_lead_file(store, path, wf_dir.name, schema)
    apply_defaults(store, schemas)


# --------------------------------------------------------------------------
# event ingestion
# --------------------------------------------------------------------------

def _disposition_code(payload) -> str | None:
    if isinstance(payload, dict):
        code = payload.get("disposition_code")
        if isinstance(code, str) and code.strip():
            return code.strip()
    return None


def _sanitise_payload(payload) -> dict:
    """Payload stored in the ledger; never let a phone leak in via payload."""
    if not isinstance(payload, dict):
        return {}
    return {k: v for k, v in payload.items() if k.lower() not in ("phone", "mobile")}


class _Linker:
    """In-memory indexes for linking events to leads, refreshed per drop."""

    def __init__(self, store: Store):
        self.store = store
        self.refresh_leads()
        self.call_lead: dict[tuple[str, str], str] = {}
        for r in store.conn.execute(
            """SELECT workflow_id, call_id, lead_phone_hash FROM events
               WHERE lead_phone_hash IS NOT NULL"""
        ):
            self.call_lead.setdefault((r["workflow_id"], r["call_id"]), r["lead_phone_hash"])
        self.seen: set[str] = {
            r["event_id"] for r in store.conn.execute("SELECT event_id FROM seen_event_ids")
        }
        self.dispositions: set[tuple[str, str]] = {
            (r["workflow_id"], r["code"])
            for r in store.conn.execute("SELECT workflow_id, code FROM dispositions")
        }

    def refresh_leads(self) -> None:
        self.lead_phone_hashes: set[tuple[str, str]] = {
            (r["workflow_id"], r["phone_hash"])
            for r in self.store.conn.execute(
                "SELECT workflow_id, phone_hash FROM leads"
            )
        }

    def resolve(self, event: dict):
        """Return (lead_phone_hash, phone_hash) or (None, phone_hash|None)."""
        wf = event.get("workflow_id")
        if not isinstance(wf, str) or not wf:
            return None, None
        etype = event.get("event_type") or ""
        if etype == "disposition.override":
            call_id = event.get("call_id")
            if not isinstance(call_id, str) or not call_id:
                return None, None
            lead = self.call_lead.get((wf, call_id))
            return lead, None
        phone = event.get("phone")
        if phone is None or (isinstance(phone, str) and phone.strip() == ""):
            return None, None
        canonical = normalise_phone(str(phone))
        if canonical is None:
            return None, None
        ph = self.store.hash_phone(canonical)
        if (wf, ph) in self.lead_phone_hashes:
            return ph, ph
        return None, ph


def _insert_event(store, linker, event, occurred, lead_hash, phone_hash):
    payload = _sanitise_payload(event.get("payload"))
    unknown = 0
    etype = event.get("event_type") or ""
    if etype.startswith("disposition."):
        code = _disposition_code(payload)
        wf = event.get("workflow_id")
        if code is None or (wf, code) not in linker.dispositions:
            unknown = 1
            store.bump("unknown_disposition_events")
    store.conn.execute(
        """INSERT INTO events
           (event_id, event_type, workflow_id, call_id, phone_hash,
            lead_phone_hash, occurred_at, payload_json, unknown_disposition)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            event["event_id"],
            etype,
            event.get("workflow_id") or "",
            event.get("call_id") if isinstance(event.get("call_id"), str) else "",
            phone_hash,
            lead_hash,
            utc_iso(occurred),
            json.dumps(payload, sort_keys=True),
            unknown,
        ),
    )
    call_id = event.get("call_id")
    if lead_hash and isinstance(call_id, str) and call_id:
        linker.call_lead.setdefault((event["workflow_id"], call_id), lead_hash)


def ingest_event_file(store: Store, linker: _Linker, path: Path) -> None:
    conn = store.conn
    with open(path, encoding="utf-8") as f:
        with conn:  # one transaction per file
            for line in f:
                if line.strip() == "":
                    continue  # blank line: not counted
                store.bump("events_total")
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    store.bump("malformed_events")
                    continue
                if not isinstance(event, dict):
                    store.bump("malformed_events")
                    continue
                event_id = event.get("event_id")
                if not isinstance(event_id, str) or event_id.strip() == "":
                    store.bump("malformed_events")
                    continue
                if event_id in linker.seen:
                    store.bump("duplicate_events_dropped")
                    continue
                linker.seen.add(event_id)
                conn.execute(
                    "INSERT OR IGNORE INTO seen_event_ids (event_id) VALUES (?)",
                    (event_id,),
                )
                occurred = parse_timestamp(event.get("occurred_at"))
                if occurred is None:
                    # No usable timestamp: cannot place the event in any
                    # report. Counted as malformed (spec's malformed class
                    # is JSON/event_id; this never occurs in generated data).
                    store.bump("malformed_events")
                    continue
                lead_hash, phone_hash = linker.resolve(event)
                if lead_hash is None:
                    conn.execute(
                        """INSERT INTO quarantine
                           (event_id, event_type, workflow_id, call_id,
                            phone_hash, occurred_at, payload_json)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (
                            event_id,
                            event.get("event_type") or "",
                            event.get("workflow_id") if isinstance(event.get("workflow_id"), str) else "",
                            event.get("call_id") if isinstance(event.get("call_id"), str) else None,
                            phone_hash,
                            utc_iso(occurred),
                            json.dumps(_sanitise_payload(event.get("payload")), sort_keys=True),
                        ),
                    )
                    continue
                _insert_event(store, linker, event, occurred, lead_hash, phone_hash)


def relink_orphans(store: Store, linker: _Linker) -> int:
    """Move quarantined events to the ledger once their lead exists.

    Iterates to a fixpoint: an override may resolve only after its call's
    initiated event has itself been re-linked. Returns events moved.
    """
    moved_total = 0
    while True:
        rows = store.conn.execute("SELECT * FROM quarantine ORDER BY event_id").fetchall()
        if not rows:
            return moved_total
        moved = 0
        with store.conn:
            for row in rows:
                event = {
                    "event_id": row["event_id"],
                    "event_type": row["event_type"],
                    "workflow_id": row["workflow_id"],
                    "call_id": row["call_id"],
                    "phone": None,
                }
                lead_hash = None
                if row["event_type"] == "disposition.override":
                    if row["call_id"]:
                        lead_hash = linker.call_lead.get(
                            (row["workflow_id"], row["call_id"])
                        )
                elif row["phone_hash"]:
                    if (row["workflow_id"], row["phone_hash"]) in linker.lead_phone_hashes:
                        lead_hash = row["phone_hash"]
                if lead_hash is None:
                    continue
                occurred = parse_timestamp(row["occurred_at"])
                # occurred_at in quarantine is already stored as UTC ISO.
                event["payload"] = json.loads(row["payload_json"])
                _insert_event(store, linker, event, occurred, lead_hash, row["phone_hash"])
                store.conn.execute(
                    "DELETE FROM quarantine WHERE event_id = ?", (row["event_id"],)
                )
                moved += 1
        moved_total += moved
        if moved == 0:
            return moved_total


def ingest_event_files(store: Store, drop: Path) -> None:
    events_dir = drop / "events"
    if not events_dir.is_dir():
        return
    linker = _Linker(store)
    for path in sorted(events_dir.glob("*.jsonl")):
        ingest_event_file(store, linker, path)
    relink_orphans(store, linker)


# --------------------------------------------------------------------------
# drop entry point
# --------------------------------------------------------------------------

def ingest_drop(store: Store, drop: Path, schemas: dict[str, dict]) -> bool:
    """Ingest one drop. Returns False if it was already ingested."""
    drop = Path(drop)
    fingerprint = drop_fingerprint(drop)
    row = store.conn.execute(
        "SELECT 1 FROM ingested_drops WHERE fingerprint = ?", (fingerprint,)
    ).fetchone()
    if row is not None:
        print(f"drop {drop} already ingested; no changes made")
        return False
    ingest_lead_files(store, drop, schemas)
    ingest_event_files(store, drop)
    # Leads may have arrived after events in this same drop were read, and
    # relinking also runs inside ingest_event_files; run once more so a
    # lead-first/orphan-second ordering inside the drop cannot strand an
    # event. Cheap when the quarantine is empty.
    linker = _Linker(store)
    relink_orphans(store, linker)
    store.conn.execute(
        "INSERT INTO ingested_drops (fingerprint, drop_name, ingested_at)"
        " VALUES (?, ?, ?)",
        (
            fingerprint,
            drop.name,
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ),
    )
    store.conn.commit()
    return True
