"""Report generation: aggregation in SQL, Python only for formatting.

All five files are derived from the stored ledger; nothing is recomputed
from the raw drops. Dates are IST dates; timestamps are rendered in IST.
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))

CALLS_CTE = """
WITH initiated AS (
    SELECT workflow_id, call_id, lead_phone_hash,
           MIN(occurred_at) AS initiated_at
    FROM events
    WHERE event_type = 'call.initiated'
    GROUP BY workflow_id, call_id
),
-- Effective disposition per call, resolved by a per-call scalar subquery.
-- (An earlier version joined a window-function result back to `initiated`;
-- SQLite planned that join as a full scan of the window output per call —
-- O(calls x dispositions) — which took 6+ minutes on the large fixture.
-- The subquery below probes the (workflow_id, call_id) index and sorts
-- only that call's handful of disposition events, so it is linear.)
calls AS (
    SELECT i.workflow_id, i.call_id, i.lead_phone_hash, i.initiated_at,
        CASE WHEN c.call_id IS NOT NULL THEN 1 ELSE 0 END AS connected,
        CASE WHEN t.call_id IS NOT NULL THEN 1 ELSE 0 END AS telef_failed,
        (SELECT json_extract(e.payload_json, '$.disposition_code')
         FROM events e
         WHERE e.workflow_id = i.workflow_id AND e.call_id = i.call_id
           AND e.event_type IN ('disposition.set', 'disposition.override')
           AND e.unknown_disposition = 0
         ORDER BY CASE WHEN e.event_type = 'disposition.override'
                       THEN 1 ELSE 0 END DESC,
                  e.occurred_at DESC, e.event_id DESC
         LIMIT 1) AS eff_code
    FROM initiated i
    LEFT JOIN connected c
      ON c.workflow_id = i.workflow_id AND c.call_id = i.call_id
    LEFT JOIN telef_failed t
      ON t.workflow_id = i.workflow_id AND t.call_id = i.call_id
),
call_disp AS (
    SELECT c.*, d.is_terminal, d.is_success
    FROM calls c
    LEFT JOIN dispositions d
      ON d.workflow_id = c.workflow_id AND d.code = c.eff_code
),
-- Pre-aggregated per-call flags. (A correlated EXISTS here makes SQLite
-- probe the event_type index once per call — O(calls x events) on large
-- data; these single scans are linear instead.)
connected AS (
    SELECT workflow_id, call_id FROM events
    WHERE event_type = 'call.connected' GROUP BY workflow_id, call_id
),
telef_failed AS (
    SELECT workflow_id, call_id FROM events
    WHERE event_type = 'call.telephony_failed' GROUP BY workflow_id, call_id
),
lead_calls AS (
    SELECT workflow_id, lead_phone_hash,
        COUNT(*) AS total_calls,
        SUM(connected) AS connected_calls,
        MAX(initiated_at) AS last_initiated,
        SUM(CASE WHEN is_success = 1 THEN 1 ELSE 0 END) AS success_calls,
        SUM(CASE WHEN is_terminal = 1 THEN 1 ELSE 0 END) AS terminal_calls,
        SUM(CASE WHEN telef_failed = 1 THEN 1 ELSE 0 END) AS tf_calls
    FROM call_disp
    GROUP BY workflow_id, lead_phone_hash
),
lead_bucket AS (
    SELECT l.workflow_id, l.phone_hash, l.batch_id, l.external_ref,
           l.phone_last4,
           COALESCE(lc.total_calls, 0) AS total_calls,
           COALESCE(lc.connected_calls, 0) AS connected_calls,
           lc.last_initiated,
           COALESCE(lc.success_calls, 0) AS success_calls,
           CASE
               WHEN COALESCE(lc.total_calls, 0) = 0 THEN 'yet_to_reach_out'
               WHEN COALESCE(lc.terminal_calls, 0) > 0 THEN 'completed'
               WHEN COALESCE(lc.tf_calls, 0) = lc.total_calls
                   THEN 'in_progress_telephony_failed'
               ELSE 'in_progress_dialed'
           END AS bucket
    FROM leads l
    LEFT JOIN lead_calls lc
      ON lc.workflow_id = l.workflow_id AND lc.lead_phone_hash = l.phone_hash
),
recent_disp AS (
    SELECT workflow_id, lead_phone_hash, eff_code
    FROM (
        SELECT c.workflow_id, c.lead_phone_hash, c.eff_code,
               ROW_NUMBER() OVER (
                   PARTITION BY c.workflow_id, c.lead_phone_hash
                   ORDER BY c.initiated_at DESC, c.call_id DESC
               ) AS rn
        FROM call_disp c
        WHERE c.eff_code IS NOT NULL
    )
    WHERE rn = 1
)
"""


def _ist_iso(utc_iso: str | None) -> str:
    """UTC ISO string -> IST ISO-8601, e.g. 2026-10-14T17:55:24+05:30."""
    if not utc_iso:
        return ""
    dt = datetime.fromisoformat(utc_iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).isoformat(timespec="seconds")


def _write_csv(path: Path, header: list[str], rows) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)


def generate_reports(store, out_dir: str | Path) -> list[Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    conn = store.conn
    written: list[Path] = []

    # 1. batch_summary.csv — only batches with >= 1 lead get a row.
    rows = conn.execute(
        CALLS_CTE
        + """
SELECT workflow_id, batch_id, COUNT(*) AS total,
       SUM(CASE WHEN bucket = 'yet_to_reach_out' THEN 1 ELSE 0 END),
       SUM(CASE WHEN bucket = 'in_progress_telephony_failed' THEN 1 ELSE 0 END),
       SUM(CASE WHEN bucket = 'in_progress_dialed' THEN 1 ELSE 0 END),
       SUM(CASE WHEN bucket = 'completed' THEN 1 ELSE 0 END)
FROM lead_bucket
GROUP BY workflow_id, batch_id
ORDER BY workflow_id, batch_id
"""
    ).fetchall()
    _write_csv(
        out / "batch_summary.csv",
        ["workflow_id", "batch_id", "total_leads", "yet_to_reach_out",
         "in_progress_telephony_failed", "in_progress_dialed", "completed"],
        [tuple(r) for r in rows],
    )
    written.append(out / "batch_summary.csv")

    # 2. daily_overview.csv — one row per workflow per IST date with >= 1 call.
    rows = conn.execute(
        CALLS_CTE
        + """
SELECT workflow_id,
       date(initiated_at, '+330 minutes') AS ist_date,
       COUNT(*) AS total_calls,
       SUM(connected) AS connected_calls,
       COUNT(DISTINCT CASE WHEN is_success = 1 THEN lead_phone_hash END)
           AS success_leads
FROM call_disp
GROUP BY workflow_id, ist_date
ORDER BY workflow_id, ist_date
"""
    ).fetchall()
    _write_csv(
        out / "daily_overview.csv",
        ["workflow_id", "date", "total_calls", "connected_calls",
         "connectivity_rate", "success_leads"],
        [
            (r["workflow_id"], r["ist_date"], r["total_calls"], r["connected_calls"],
             f"{(r['connected_calls'] / r['total_calls']):.4f}" if r["total_calls"] else "0.0000",
             r["success_leads"])
            for r in rows
        ],
    )
    written.append(out / "daily_overview.csv")

    # 3. lead_report.csv — one row per lead.
    rows = conn.execute(
        CALLS_CTE
        + """
SELECT lb.workflow_id, lb.external_ref, lb.batch_id, lb.phone_last4,
       lb.bucket, lb.total_calls, lb.connected_calls, lb.last_initiated,
       rd.eff_code, lb.success_calls
FROM lead_bucket lb
LEFT JOIN recent_disp rd
  ON rd.workflow_id = lb.workflow_id AND rd.lead_phone_hash = lb.phone_hash
ORDER BY lb.workflow_id, lb.external_ref
"""
    ).fetchall()
    _write_csv(
        out / "lead_report.csv",
        ["workflow_id", "external_ref", "batch_id", "phone_masked", "bucket",
         "total_calls", "connected_calls", "last_call_at",
         "current_disposition", "ever_success"],
        [
            (r["workflow_id"], r["external_ref"], r["batch_id"],
             f"+91******{r['phone_last4']}", r["bucket"], r["total_calls"],
             r["connected_calls"], _ist_iso(r["last_initiated"]),
             r["eff_code"] or "",
             "true" if r["success_calls"] > 0 else "false")
            for r in rows
        ],
    )
    written.append(out / "lead_report.csv")

    # 4. rejections.csv — no PII.
    rows = conn.execute(
        """SELECT workflow_id, batch_id, row_number, reason FROM rejections
           ORDER BY workflow_id, batch_id, row_number"""
    ).fetchall()
    _write_csv(
        out / "rejections.csv",
        ["workflow_id", "batch_id", "row_number", "reason"],
        [tuple(r) for r in rows],
    )
    written.append(out / "rejections.csv")

    # 5. data_quality.json — keys in exactly this order.
    counters = store.counters()
    by_reason = {
        r["reason"]: r["c"]
        for r in conn.execute(
            "SELECT reason, COUNT(*) AS c FROM rejections GROUP BY reason"
        )
    }
    dq = {
        "lead_rows_total": counters["lead_rows_total"],
        "lead_rows_rejected": counters["lead_rows_rejected"],
        "rejections_by_reason": {k: by_reason[k] for k in sorted(by_reason)},
        "duplicate_lead_rows_merged": counters["duplicate_lead_rows_merged"],
        "field_warnings": counters["field_warnings"],
        "events_total": counters["events_total"],
        "malformed_events": counters["malformed_events"],
        "duplicate_events_dropped": counters["duplicate_events_dropped"],
        "orphan_events": store.orphan_count(),
        "unknown_disposition_events": counters["unknown_disposition_events"],
    }
    with open(out / "data_quality.json", "w", encoding="utf-8") as f:
        json.dump(dq, f, indent=2)
        f.write("\n")
    written.append(out / "data_quality.json")
    return written
