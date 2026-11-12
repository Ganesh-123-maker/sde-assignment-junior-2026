"""SQLite store: schema, PII hashing (HMAC-SHA256 + pepper), counters.

PII design
----------
Fields marked ``"pii": true`` (``customer_name``, ``phone``) never appear in
plaintext in the DB file:

* ``phone`` -> HMAC-SHA256 hex of the canonical ``+91XXXXXXXXXX``, keyed by
  a per-deployment pepper, plus ``phone_last4`` (4 digits) in a separate
  column — the masked report only needs the last 4.
* ``customer_name`` -> HMAC-SHA256 hex of the trimmed name. No report ever
  needs the name back, so a keyed hash is sufficient and avoids any crypto
  dependency. Limit, stated honestly: a future feature that must *display*
  a name would need to switch to reversible encryption (Fernet / KMS
  envelope encryption); the hash cannot be reversed.

Pepper: ``AU_PII_KEY`` env var if set, else an auto-generated key file next
to the DB (``<db>.pii.key``, mode 0600, never committed). The pepper must
stay stable for a deployment: a different pepper yields different hashes,
so leads/events would no longer match. Re-ingesting after losing the key
file therefore requires starting from a fresh DB.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from pathlib import Path

COUNTER_NAMES = [
    "lead_rows_total",
    "lead_rows_rejected",
    "duplicate_lead_rows_merged",
    "field_warnings",
    "events_total",
    "malformed_events",
    "duplicate_events_dropped",
    "unknown_disposition_events",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS ingested_drops (
    fingerprint TEXT PRIMARY KEY,
    drop_name TEXT NOT NULL,
    ingested_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS dispositions (
    workflow_id TEXT NOT NULL,
    code TEXT NOT NULL,
    is_terminal INTEGER NOT NULL,
    is_success INTEGER NOT NULL,
    PRIMARY KEY (workflow_id, code)
);
CREATE TABLE IF NOT EXISTS leads (
    workflow_id TEXT NOT NULL,
    phone_hash TEXT NOT NULL,
    phone_last4 TEXT NOT NULL,
    name_hash TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    external_ref TEXT NOT NULL,
    attrs_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (workflow_id, phone_hash)
);
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    phone_hash TEXT,
    lead_phone_hash TEXT,
    occurred_at TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    unknown_disposition INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS quarantine (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    call_id TEXT,
    phone_hash TEXT,
    occurred_at TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS seen_event_ids (
    event_id TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS rejections (
    workflow_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    row_number INTEGER NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY (workflow_id, batch_id, row_number)
);
CREATE INDEX IF NOT EXISTS idx_leads_wf_phone ON leads (workflow_id, phone_hash);
CREATE INDEX IF NOT EXISTS idx_events_call ON events (workflow_id, call_id);
CREATE INDEX IF NOT EXISTS idx_events_lead ON events (workflow_id, lead_phone_hash);
CREATE INDEX IF NOT EXISTS idx_events_type ON events (event_type);
CREATE INDEX IF NOT EXISTS idx_quarantine_call ON quarantine (workflow_id, call_id);
CREATE INDEX IF NOT EXISTS idx_quarantine_phone ON quarantine (workflow_id, phone_hash);
"""


def get_pepper(db_path: Path) -> bytes:
    """Return the deployment pepper, creating the key file if needed."""
    env = os.environ.get("AU_PII_KEY")
    if env:
        return env.encode("utf-8")
    key_path = Path(str(db_path) + ".pii.key")
    if key_path.exists():
        return key_path.read_bytes().strip()
    pepper = secrets.token_hex(32).encode("utf-8")
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, pepper + b"\n")
    finally:
        os.close(fd)
    return pepper


class Store:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.pepper = get_pepper(self.db_path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        for name in COUNTER_NAMES:
            self.conn.execute(
                "INSERT OR IGNORE INTO counters (name, value) VALUES (?, 0)",
                (name,),
            )
        self.conn.commit()

    # -- PII hashing ------------------------------------------------------
    def hash_phone(self, canonical_phone: str) -> str:
        return hmac.new(
            self.pepper, canonical_phone.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def hash_name(self, name: str) -> str:
        return hmac.new(
            self.pepper, name.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    # -- counters ---------------------------------------------------------
    def bump(self, name: str, n: int = 1) -> None:
        self.conn.execute(
            "UPDATE counters SET value = value + ? WHERE name = ?", (n, name)
        )

    def counter(self, name: str) -> int:
        row = self.conn.execute(
            "SELECT value FROM counters WHERE name = ?", (name,)
        ).fetchone()
        return row["value"] if row else 0

    def counters(self) -> dict[str, int]:
        return {
            r["name"]: r["value"]
            for r in self.conn.execute("SELECT name, value FROM counters")
        }

    def orphan_count(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) AS c FROM quarantine"
        ).fetchone()["c"]

    def close(self) -> None:
        self.conn.close()


def load_dispositions(store: Store, csv_path: Path) -> None:
    """Upsert the disposition catalogue (needed for SQL-side aggregation)."""
    import csv

    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    store.conn.executemany(
        """INSERT INTO dispositions (workflow_id, code, is_terminal, is_success)
           VALUES (?, ?, ?, ?)
           ON CONFLICT (workflow_id, code) DO UPDATE
           SET is_terminal = excluded.is_terminal,
               is_success = excluded.is_success""",
        [
            (
                r["workflow_id"],
                r["code"],
                1 if r["is_terminal"].strip().lower() == "true" else 0,
                1 if r["is_success"].strip().lower() == "true" else 0,
            )
            for r in rows
        ],
    )
    store.conn.commit()


def load_schemas(schema_dir: Path) -> dict[str, dict]:
    """Load every ``<workflow>.json`` schema, keyed by workflow_id."""
    schemas = {}
    for path in sorted(schema_dir.glob("*.json")):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        schemas[data["workflow_id"]] = data
    return schemas


def find_data_dir() -> Path:
    """Locate ``assignment-a-data`` from the repo root or the cwd."""
    here = Path(__file__).resolve().parent.parent  # repo root
    for cand in (here / "assignment-a-data", Path.cwd() / "assignment-a-data"):
        if (cand / "schemas").is_dir():
            return cand
    raise FileNotFoundError(
        "assignment-a-data/ not found next to the repo root or in the cwd"
    )
