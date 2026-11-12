"""Tests for Assignment A. Small synthetic fixtures built in-test (tmp dirs),
not only generator output. Run:  python -m pytest solution/tests -q
"""
from __future__ import annotations

import csv
import io
import json
import sqlite3
from pathlib import Path

import pytest

from solution.db import Store, find_data_dir, load_dispositions, load_schemas
from solution.ingest import ingest_drop
from solution.normalize import (
    normalise_phone,
    parse_date,
    parse_decimal,
    parse_enum,
)
from solution.report import generate_reports

DATA_DIR = find_data_dir()
SCHEMAS = load_schemas(DATA_DIR / "schemas")
MOTOR = "motor_renewal"


# --------------------------------------------------------------------------
# unit: normalisation
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("9876543210", "+919876543210"),
    ("+91 98765 43210", "+919876543210"),
    ("09876543210", "+919876543210"),
    ("91-98765-43210", "+919876543210"),
    ("(+91) 9876543210", "+919876543210"),
    ("+919876543210", "+919876543210"),
])
def test_phone_all_six_formats(raw, expected):
    assert normalise_phone(raw) == expected


@pytest.mark.parametrize("raw", [
    "987654321",            # 9 digits
    "5876543210",           # bad prefix
    "022-2000-1000",        # landline
    "N/A",
    "+1 415 555 0134",      # US number
    "",
    "abcdefghij",
])
def test_phone_invalid(raw):
    assert normalise_phone(raw) is None


@pytest.mark.parametrize("raw,expected", [
    ("2026-12-01", "2026-12-01"),
    ("01/12/2026", "2026-12-01"),
    ("01-12-2026", "2026-12-01"),
    ("01-Dec-2026", "2026-12-01"),
    ("01-dec-2026", "2026-12-01"),
    ("01-DEC-2026", "2026-12-01"),
])
def test_dates_all_formats(raw, expected):
    assert parse_date(raw) == expected


@pytest.mark.parametrize("raw", ["31/02/2026", "2026-13-01", "soon", "TBD", "32-01-2026"])
def test_dates_impossible(raw):
    assert parse_date(raw) is None


@pytest.mark.parametrize("raw,expected", [
    ("12000", "12000.00"),
    ("12000.1", "12000.10"),
    ("₹12,000", "12000.00"),
    ("Rs. 12000", "12000.00"),
    ("Rs 12000.555", "12000.56"),
    ("INR 12000.1", "12000.10"),
    ("inr 5,000", "5000.00"),
    ("₹1,234.56", "1234.56"),
])
def test_decimal_formats(raw, expected):
    assert parse_decimal(raw) == expected


@pytest.mark.parametrize("raw", ["-5", "N/A", "-", "twelve thousand", "", "₹-100", "12,34,56x"])
def test_decimal_invalid(raw):
    assert parse_decimal(raw) is None


def test_enum_case_insensitive():
    assert parse_enum("EN", ["en", "hi"]) == "en"
    assert parse_enum(" Hi ", ["en", "hi"]) == "hi"
    assert parse_enum("Hindi", ["en", "hi"]) is None
    assert parse_enum("english", ["en", "hi"]) is None


# --------------------------------------------------------------------------
# fixtures: build drops in tmp dirs
# --------------------------------------------------------------------------

def write_lead_csv(path: Path, header, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            if r is None:
                f.write("," * (len(header) - 1) + "\n")  # blank row
            else:
                w.writerow(r)


def make_drop(root: Path, name="drop_1", leads=None, events=None):
    """leads: {(workflow, filename): (header, rows)}; events: [json lines]."""
    drop = root / name
    for (wf, fn), (header, rows) in (leads or {}).items():
        write_lead_csv(drop / "leads" / wf / fn, header, rows)
    if events is not None:
        edir = drop / "events"
        edir.mkdir(parents=True, exist_ok=True)
        with open(edir / "events_0001.jsonl", "w", encoding="utf-8") as f:
            for line in events:
                f.write(line if line.endswith("\n") else line + "\n")
    return drop


def ev(eid, etype, call, phone=None, ts="2026-10-03T10:01:00Z",
       wf=MOTOR, payload=None):
    e = {"event_id": eid, "event_type": etype, "workflow_id": wf,
         "call_id": call, "occurred_at": ts, "payload": payload or {}}
    if phone is not None:
        e["phone"] = phone
    return json.dumps(e)


MOTOR_HEADER = ["customer_name", "phone", "policy_number", "policy_expiry",
                "premium_amount", "preferred_language"]


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    load_dispositions(s, DATA_DIR / "schemas" / "dispositions.csv")
    yield s
    s.close()


def ingest(store, drop):
    return ingest_drop(store, drop, SCHEMAS)


def reports(store, tmp_path, name="rep"):
    out = tmp_path / name
    generate_reports(store, out)
    return out


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def data_quality(out):
    return json.loads((out / "data_quality.json").read_text())


# --------------------------------------------------------------------------
# leads
# --------------------------------------------------------------------------

def test_header_alias_reordered_extra_columns_and_blank_rows(store, tmp_path):
    # batch_003 style: renamed, reordered, extra column, blank row.
    header = ["Language", "Insured Name", "Policy No", "Mobile No",
              "Expiry Date", "Premium", "agent_notes"]
    rows = [
        ["hi", "Aarav Sharma", "MOT/2026/000001", "9876543210", "01-Dec-2026",
         "₹12,000", "VIP"],
        None,  # blank row: not counted
        ["", "Meera Iyer", "MOT/2026/000002", "+91 9123456780", "2026-11-02",
         "5000", ""],
    ]
    drop = make_drop(tmp_path, leads={(MOTOR, "batch_003.csv"): (header, rows)})
    assert ingest(store, drop) is True
    dq = data_quality(reports(store, tmp_path))
    assert dq["lead_rows_total"] == 2
    assert dq["lead_rows_rejected"] == 0
    assert dq["field_warnings"] == 0
    leads = store.conn.execute(
        "SELECT external_ref, attrs_json FROM leads ORDER BY external_ref"
    ).fetchall()
    assert [r["external_ref"] for r in leads] == ["MOT/2026/000001", "MOT/2026/000002"]
    attrs = json.loads(leads[0]["attrs_json"])
    assert attrs["policy_expiry"] == "2026-12-01"
    assert attrs["premium_amount"] == "12000.00"
    assert attrs["preferred_language"] == "hi"
    assert "agent_notes" not in attrs  # extra column ignored
    # default applied: second row had blank language -> en
    assert json.loads(leads[1]["attrs_json"])["preferred_language"] == "en"


def test_rejection_order_schema_order_first_failure_wins(store, tmp_path):
    rows = [
        # missing name (first field) AND bad phone -> missing_required:customer_name
        ["", "12345", "MOT/2026/000001", "", "", ""],
        # valid name, bad phone, missing ref -> invalid_phone (phone before ref)
        ["Rohan Rao", "5876543210", "", "", "", ""],
        # valid name+phone, missing ref -> missing_required:policy_number
        ["Rohan Rao", "9876543210", "", "", "", ""],
        # bad date + bad premium + bad language (all optional) -> 3 warnings
        ["Sneha Das", "9123456780", "MOT/2026/000002", "31/02/2026", "N/A", "Hindi"],
    ]
    drop = make_drop(tmp_path, leads={(MOTOR, "batch_001.csv"): (MOTOR_HEADER, rows)})
    ingest(store, drop)
    dq = data_quality(reports(store, tmp_path))
    assert dq["lead_rows_total"] == 4
    assert dq["lead_rows_rejected"] == 3
    assert dq["field_warnings"] == 3  # bad date + bad premium + bad language
    assert dq["rejections_by_reason"] == {
        "invalid_phone": 1,
        "missing_required:customer_name": 1,
        "missing_required:policy_number": 1,
    }
    rej = read_csv(reports(store, tmp_path) / "rejections.csv")
    assert [(r["row_number"], r["reason"]) for r in rej] == [
        ("1", "missing_required:customer_name"),
        ("2", "invalid_phone"),
        ("3", "missing_required:policy_number"),
    ]


def test_duplicate_merge_overwrite_only_non_null_batch_sticks(store, tmp_path):
    rows1 = [["Aarav Sharma", "9876543210", "MOT/2026/000001", "2026-12-01",
              "12000", ""]]
    # Duplicate row: same identity, blank expiry (kept), new premium/language.
    # NOTE: the name must be present — a dup row with a blank *required*
    # field is rejected, not merged (rejection runs before dedupe).
    rows2 = [["Aarav Sharma", "91-98765-43210", "MOT/2026/000001", "", "15000.5", "hi"],
             ["Zoya Khan", "9123456780", "MOT/2026/000002", "", "", ""]]
    d1 = make_drop(tmp_path, "drop_1", {(MOTOR, "batch_001.csv"): (MOTOR_HEADER, rows1)})
    d2 = make_drop(tmp_path, "drop_2", {(MOTOR, "batch_002.csv"): (MOTOR_HEADER, rows2)})
    ingest(store, d1)
    ingest(store, d2)
    dq = data_quality(reports(store, tmp_path))
    assert dq["duplicate_lead_rows_merged"] == 1
    assert dq["lead_rows_total"] == 3
    # A later row for the same identity with a blank required field is
    # rejected (not merged): rejection is decided before dedupe.
    d3 = make_drop(tmp_path, "drop_3", {(MOTOR, "batch_003.csv"): (MOTOR_HEADER, [
        ["", "9876543210", "MOT/2026/000001", "", "", ""]])})
    ingest(store, d3)
    dq = data_quality(reports(store, tmp_path))
    assert dq["duplicate_lead_rows_merged"] == 1
    assert dq["lead_rows_rejected"] == 1
    lead = store.conn.execute(
        "SELECT batch_id, external_ref, attrs_json FROM leads WHERE phone_last4='3210'"
    ).fetchone()
    assert lead["batch_id"] == "batch_001"  # first file's batch sticks
    attrs = json.loads(lead["attrs_json"])
    assert attrs["premium_amount"] == "15000.50"  # overwritten (non-null wins)
    assert attrs["policy_expiry"] == "2026-12-01"  # blank in dup -> kept
    assert attrs["preferred_language"] == "hi"  # explicit value beats default
    # name hash unchanged by the dup row (its name cell was blank)
    assert store.hash_name("Aarav Sharma") == store.conn.execute(
        "SELECT name_hash FROM leads WHERE phone_last4='3210'").fetchone()["name_hash"]


def test_duplicate_column_first_wins(store, tmp_path):
    header = ["phone", "Mobile No", "customer_name", "policy_number"]
    rows = [["9876543210", "9111111111", "Aarav Sharma", "MOT/2026/000001"]]
    drop = make_drop(tmp_path, leads={(MOTOR, "batch_001.csv"): (header, rows)})
    ingest(store, drop)
    lead = store.conn.execute("SELECT phone_last4 FROM leads").fetchone()
    assert lead["phone_last4"] == "3210"  # first phone column wins


def test_unknown_workflow_dir_skipped(store, tmp_path, capsys):
    drop = make_drop(tmp_path, leads={
        ("mystery_wf", "batch_001.csv"): (MOTOR_HEADER, [
            ["Aarav Sharma", "9876543210", "MOT/2026/000001", "", "", ""]]),
        (MOTOR, "batch_001.csv"): (MOTOR_HEADER, [
            ["Meera Iyer", "9123456780", "MOT/2026/000002", "", "", ""]]),
    })
    ingest(store, drop)
    assert "unknown workflow" in capsys.readouterr().err
    assert store.conn.execute("SELECT COUNT(*) c FROM leads").fetchone()["c"] == 1


# --------------------------------------------------------------------------
# events: disposition, buckets, orphans, dedupe
# --------------------------------------------------------------------------

def _lead_drop(tmp_path, name, phones):
    rows = [[f"Person {i}", p, f"MOT/2026/{i:06d}", "", "", ""]
            for i, p in enumerate(phones, start=1)]
    return make_drop(tmp_path, name, {(MOTOR, "batch_001.csv"): (MOTOR_HEADER, rows)})


def test_effective_disposition_override_beats_set_latest_wins(store, tmp_path):
    d1 = _lead_drop(tmp_path, "drop_1", ["9876543210"])
    evs = [
        ev("e1", "call.initiated", "c1", "9876543210"),
        ev("e2", "call.connected", "c1", "9876543210"),
        ev("e3", "disposition.set", "c1", "9876543210",
           ts="2026-10-03T11:00:00Z", payload={"disposition_code": "callback_requested"}),
        # later set wins over earlier set
        ev("e4", "disposition.set", "c1", "9876543210",
           ts="2026-10-03T12:00:00Z", payload={"disposition_code": "payment_link_sent"}),
        # override beats any set, however late the set is
        ev("e5", "disposition.override", "c1",
           ts="2026-10-03T11:30:00Z",
           payload={"disposition_code": "renewed", "overridden_by": "sup1"}),
        # unknown-code set is ignored (never an effective disposition)
        ev("e6", "disposition.set", "c1", "9876543210",
           ts="2026-10-03T13:00:00Z", payload={"disposition_code": "RENEWD"}),
    ]
    d2 = make_drop(tmp_path, "drop_2", events=evs)
    ingest(store, d1)
    ingest(store, d2)
    out = reports(store, tmp_path)
    lr = read_csv(out / "lead_report.csv")
    assert lr[0]["current_disposition"] == "renewed"
    assert lr[0]["ever_success"] == "true"
    assert lr[0]["bucket"] == "completed"
    assert data_quality(out)["unknown_disposition_events"] == 1


def test_effective_disposition_unknown_override_ignored(store, tmp_path):
    d1 = _lead_drop(tmp_path, "drop_1", ["9876543210"])
    evs = [
        ev("e1", "call.initiated", "c1", "9876543210"),
        ev("e2", "disposition.set", "c1", "9876543210",
           ts="2026-10-03T11:00:00Z", payload={"disposition_code": "callback_requested"}),
        ev("e3", "disposition.override", "c1",
           ts="2026-10-03T12:00:00Z",
           payload={"disposition_code": "no_such_code", "overridden_by": "sup1"}),
    ]
    d2 = make_drop(tmp_path, "drop_2", events=evs)
    ingest(store, d1)
    ingest(store, d2)
    lr = read_csv(reports(store, tmp_path) / "lead_report.csv")
    assert lr[0]["current_disposition"] == "callback_requested"  # set survives
    assert data_quality(reports(store, tmp_path))["unknown_disposition_events"] == 1


def test_override_tie_break_event_id_descending(store, tmp_path):
    d1 = _lead_drop(tmp_path, "drop_1", ["9876543210"])
    evs = [
        ev("e1", "call.initiated", "c1", "9876543210"),
        ev("e9", "disposition.override", "c1", ts="2026-10-03T12:00:00Z",
           payload={"disposition_code": "not_interested", "overridden_by": "s1"}),
        ev("e2", "disposition.override", "c1", ts="2026-10-03T12:00:00Z",
           payload={"disposition_code": "renewed", "overridden_by": "s1"}),
    ]
    d2 = make_drop(tmp_path, "drop_2", events=evs)
    ingest(store, d1)
    ingest(store, d2)
    lr = read_csv(reports(store, tmp_path) / "lead_report.csv")
    assert lr[0]["current_disposition"] == "not_interested"  # e9 > e2 at same time


def test_buckets_all_four(store, tmp_path):
    phones = ["9876543210", "9123456780", "9988776655", "9000011111"]
    d1 = _lead_drop(tmp_path, "drop_1", phones)
    evs = [
        # lead 2: one call, telephony failed only -> in_progress_telephony_failed
        ev("e1", "call.initiated", "c2", "9123456780"),
        ev("e2", "call.telephony_failed", "c2", "9123456780",
           payload={"reason": "sip_503"}),
        # lead 3: one failed + one dialled (no answer) -> in_progress_dialed
        ev("e3", "call.initiated", "c3", "9988776655"),
        ev("e4", "call.telephony_failed", "c3", "9988776655",
           payload={"reason": "sip_503"}),
        ev("e5", "call.initiated", "c4", "9988776655"),
        ev("e6", "call.ended", "c4", "9988776655",
           payload={"duration_sec": 0, "end_reason": "no_answer"}),
        # lead 4: connected + terminal disposition -> completed
        ev("e7", "call.initiated", "c5", "9000011111"),
        ev("e8", "call.connected", "c5", "9000011111"),
        ev("e9", "disposition.set", "c5", "9000011111",
           ts="2026-10-03T11:00:00Z", payload={"disposition_code": "renewed"}),
    ]
    d2 = make_drop(tmp_path, "drop_2", events=evs)
    ingest(store, d1)
    ingest(store, d2)
    lr = read_csv(reports(store, tmp_path) / "lead_report.csv")
    by_ref = {r["external_ref"]: r for r in lr}
    assert by_ref["MOT/2026/000001"]["bucket"] == "yet_to_reach_out"
    assert by_ref["MOT/2026/000002"]["bucket"] == "in_progress_telephony_failed"
    assert by_ref["MOT/2026/000003"]["bucket"] == "in_progress_dialed"
    assert by_ref["MOT/2026/000004"]["bucket"] == "completed"


def test_call_without_initiated_not_counted(store, tmp_path):
    d1 = _lead_drop(tmp_path, "drop_1", ["9876543210"])
    evs = [  # connected + disposition but no initiated: no call at all
        ev("e1", "call.connected", "c1", "9876543210"),
        ev("e2", "disposition.set", "c1", "9876543210",
           payload={"disposition_code": "renewed"}),
    ]
    d2 = make_drop(tmp_path, "drop_2", events=evs)
    ingest(store, d1)
    ingest(store, d2)
    out = reports(store, tmp_path)
    assert read_csv(out / "lead_report.csv")[0]["total_calls"] == "0"
    assert read_csv(out / "daily_overview.csv") == []


def test_orphan_relink_across_drops_including_override_chain(store, tmp_path):
    # drop_1: events for a lead that does not exist yet (incl. an override
    # whose call's initiated event is itself orphaned).
    evs = [
        ev("e1", "call.initiated", "c1", "9876543210"),
        ev("e2", "call.connected", "c1", "9876543210"),
        ev("e3", "disposition.set", "c1", "9876543210",
           ts="2026-10-03T11:00:00Z", payload={"disposition_code": "callback_requested"}),
        ev("e4", "disposition.override", "c1", ts="2026-10-03T12:00:00Z",
           payload={"disposition_code": "renewed", "overridden_by": "s1"}),
        ev("e5", "call.initiated", "c9", "9000099999"),  # never uploaded
    ]
    d1 = make_drop(tmp_path, "drop_1", events=evs)
    ingest(store, d1)
    assert data_quality(reports(store, tmp_path))["orphan_events"] == 5
    # drop_2: the lead arrives; all 4 of its events must re-link.
    d2 = _lead_drop(tmp_path, "drop_2", ["9876543210"])
    ingest(store, d2)
    out = reports(store, tmp_path)
    assert data_quality(out)["orphan_events"] == 1  # only the never-uploaded one
    lr = read_csv(out / "lead_report.csv")
    assert lr[0]["total_calls"] == "1"
    assert lr[0]["current_disposition"] == "renewed"  # override resolved
    assert lr[0]["bucket"] == "completed"


def test_malformed_duplicate_unknown_counts_and_events_total(store, tmp_path):
    d1 = _lead_drop(tmp_path, "drop_1", ["9876543210"])
    lines = [
        ev("e1", "call.initiated", "c1", "9876543210"),
        '{"event_id": "e2", "event_type": "call.connected", ',  # truncated JSON
        "not json at all",
        "42",  # valid JSON, not an object
        json.dumps({"event_type": "call.initiated"}),  # no event_id
        ev("e1", "call.initiated", "c1", "9876543210"),  # duplicate, same drop
        "",  # blank line: not counted
        "   ",
    ]
    d2 = make_drop(tmp_path, "drop_2", events=lines)
    ingest(store, d1)
    ingest(store, d2)
    dq = data_quality(reports(store, tmp_path))
    assert dq["events_total"] == 6  # blank lines excluded
    assert dq["malformed_events"] == 4
    assert dq["duplicate_events_dropped"] == 1
    # duplicate of e1 in a later drop also counts as duplicate, not a new event
    d3 = make_drop(tmp_path, "drop_3", events=[ev("e1", "call.initiated", "c1", "9876543210")])
    ingest(store, d3)
    dq = data_quality(reports(store, tmp_path))
    assert dq["events_total"] == 7
    assert dq["duplicate_events_dropped"] == 2


def test_idempotent_reingest_and_one_command_vs_two(store, tmp_path):
    d1 = _lead_drop(tmp_path, "drop_1", ["9876543210", "9123456780"])
    evs = [
        ev("e1", "call.initiated", "c1", "9876543210"),
        ev("e2", "call.connected", "c1", "9876543210"),
        ev("e3", "disposition.set", "c1", "9876543210",
           ts="2026-10-03T11:00:00Z", payload={"disposition_code": "renewed"}),
    ]
    d2 = make_drop(tmp_path, "drop_2", events=evs)
    ingest(store, d1)
    ingest(store, d2)
    out1 = reports(store, tmp_path, "rep1")
    snap1 = {p.name: p.read_bytes() for p in out1.iterdir()}
    assert ingest(store, d1) is False  # already ingested: no-op
    assert ingest(store, d2) is False
    out2 = reports(store, tmp_path, "rep2")
    snap2 = {p.name: p.read_bytes() for p in out2.iterdir()}
    assert snap1 == snap2

    # Fresh DB, both drops in a single ingest call -> identical reports.
    store2 = Store(tmp_path / "test2.db")
    load_dispositions(store2, DATA_DIR / "schemas" / "dispositions.csv")
    try:
        ingest_drop(store2, d1, SCHEMAS)
        ingest_drop(store2, d2, SCHEMAS)
        out3 = reports(store2, tmp_path, "rep3")
        snap3 = {p.name: p.read_bytes() for p in out3.iterdir()}
        assert snap1 == snap3
    finally:
        store2.close()


def test_pii_never_plaintext_in_db(store, tmp_path):
    d1 = _lead_drop(tmp_path, "drop_1", ["9876543210"])
    evs = [ev("e1", "call.initiated", "c1", "+91 98765 43210")]
    d2 = make_drop(tmp_path, "drop_2", events=evs)
    ingest(store, d1)
    ingest(store, d2)
    store.close()
    raw = (tmp_path / "test.db").read_bytes()
    assert b"9876543210" not in raw
    assert b"98765 43210" not in raw
    assert b"Person 1" not in raw
    assert b"+919876543210" not in raw
    # but the hash + last4 are there
    assert b"3210" in raw


def test_report_formats_ist_and_masking(store, tmp_path):
    d1 = _lead_drop(tmp_path, "drop_1", ["9876543210", "9123456780"])
    evs = [
        # 22:00 UTC 3 Oct = 03:30 IST 4 Oct -> call date is 2026-10-04
        ev("e1", "call.initiated", "c1", "9876543210", ts="2026-10-03T22:00:00Z"),
        ev("e2", "call.connected", "c1", "9876543210", ts="2026-10-03T22:01:00Z"),
        ev("e3", "disposition.set", "c1", "9876543210",
           ts="2026-10-03T22:02:00Z", payload={"disposition_code": "renewed"}),
        # naive timestamp means IST: 10:01 IST 3 Oct stays 3 Oct
        ev("e4", "call.initiated", "c2", "9123456780", ts="2026-10-03 10:01:00"),
        # offset timestamp: 10:00 +05:30 = 04:30 UTC
        ev("e5", "call.initiated", "c3", "9123456780", ts="2026-10-04T10:00:00+05:30"),
    ]
    d2 = make_drop(tmp_path, "drop_2", events=evs)
    ingest(store, d1)
    ingest(store, d2)
    out = reports(store, tmp_path)
    daily = read_csv(out / "daily_overview.csv")
    assert [(r["date"], r["total_calls"], r["connected_calls"],
             r["connectivity_rate"], r["success_leads"]) for r in daily] == [
        ("2026-10-03", "1", "0", "0.0000", "0"),
        ("2026-10-04", "2", "1", "0.5000", "1"),
    ]
    lr = read_csv(out / "lead_report.csv")
    assert lr[0]["phone_masked"] == "+91******3210"
    assert lr[0]["last_call_at"] == "2026-10-04T03:30:00+05:30"
    assert lr[1]["last_call_at"] == "2026-10-04T10:00:00+05:30"
    bs = read_csv(out / "batch_summary.csv")
    assert bs[0]["total_leads"] == "2"
    assert bs[0]["completed"] == "1"
    assert bs[0]["yet_to_reach_out"] == "0"
