# Assignment A — Interaction Ledger & Reporting Pipeline

Stdlib-only Python 3.11+ (developed on 3.12.3, SQLite 3.45.1). No new
dependencies; `pytest>=8.0` (already in the root `requirements.txt`) is the
only thing needed for tests.

## How to run

From the repo root:

```bash
# 1. Generate data (any seed; the grader uses a fresh one)
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data

# 2. Ingest, one drop at a time (or several at once, in order)
python -m solution ingest data/drop_1 [--db au.db]
python -m solution ingest data/drop_2 [--db au.db]
python -m solution ingest data/drop_1 data/drop_2 [--db au.db]

# 3. Report (defaults: --db au.db --out reports/)
python -m solution report --db au.db --out reports/
```

## How to test

```bash
python -m pytest solution/tests -q        # 55 tests, ~0.3s
```

## Architecture

```
solution/
├── __main__.py    CLI: `ingest` / `report` subcommands
├── normalize.py   pure parsers: phone, date, decimal, enum, timestamps
├── db.py          SQLite schema, HMAC-SHA256 PII hashing, pepper handling
├── ingest.py      lead CSVs, event JSONL, orphan re-linking, idempotency
├── report.py      SQL aggregation (CTEs + window functions) -> 5 files
└── tests/         pytest suite on small synthetic fixtures
```

### Database schema (SQLite, WAL mode, one transaction per file)

| Table | Purpose |
|---|---|
| `leads` | One row per `(workflow_id, phone_hash)` — PK on that pair. `phone_last4`, `name_hash` (HMAC), `batch_id` (first file's stem, permanent), `external_ref` (the schema's `reference` field), `attrs_json` (all other normalised fields: dates as ISO, decimals as 2dp strings, enums lower-case). |
| `events` | Append-only ledger. `event_id` PK, `event_type`, `workflow_id`, `call_id`, `phone_hash`, `lead_phone_hash` (the link), `occurred_at` (UTC ISO), `payload_json` (phone keys stripped), `unknown_disposition` flag. Overrides never edit/delete anything — they are just more rows. |
| `quarantine` | Orphan events (`event_id` PK) with the raw fields needed to re-link: `workflow_id`, `call_id`, `phone_hash`, `occurred_at`, `payload_json`. |
| `seen_event_ids` | `event_id` PK — explicit, persistent dedupe across all drops. |
| `ingested_drops` | Drop fingerprint (SHA-256 over sorted relative-path + bytes of every file in the drop) as PK. Re-ingesting a recorded fingerprint changes nothing. |
| `counters` | Cumulative counts: `lead_rows_total`, `lead_rows_rejected`, `duplicate_lead_rows_merged`, `field_warnings`, `events_total`, `malformed_events`, `duplicate_events_dropped`, `unknown_disposition_events`. |
| `rejections` | `(workflow_id, batch_id, row_number, reason)` — `rejections.csv` and `rejections_by_reason` derive from this, so reports are reproducible from stored data alone. |
| `dispositions` | The `dispositions.csv` catalogue, upserted at open, so the SQL aggregation can join `is_terminal` / `is_success`. |

Indexes: `leads(workflow_id, phone_hash)`, `events(workflow_id, call_id)`,
`events(workflow_id, lead_phone_hash)`, `events(event_type)`,
`quarantine(workflow_id, call_id)`, `quarantine(workflow_id, phone_hash)`.

### PII at rest

Fields marked `"pii": true` (`customer_name`, `phone`) never appear in
plaintext in the DB file — verified by grepping the DB bytes for every
generated name/phone (see Verification below).

- Phone → HMAC-SHA256 hex of the canonical `+91XXXXXXXXXX`, keyed by a
  per-deployment pepper, plus `phone_last4` in a separate column (the
  masked report needs only the last 4).
- Name → HMAC-SHA256 hex of the trimmed name. No report ever needs the
  name back, so a keyed hash is sufficient and avoids any crypto
  dependency. **Limit:** a future feature that must *display* a name would
  need reversible encryption (Fernet / KMS envelope encryption); the hash
  cannot be reversed. Documented here and in `SUBMISSION.md`.
- Raw event JSON is never stored; only the sanitised payload (any
  `phone`/`mobile` keys stripped defensively).
- Pepper: `AU_PII_KEY` env var if set, else an auto-generated key file next
  to the DB (`<db>.pii.key`, mode `0600`, never committed — see
  `solution/.gitignore`). The pepper must stay stable for a deployment: a
  different pepper yields different hashes and leads/events stop matching.

### Idempotency

Fingerprint = SHA-256 over sorted `(relative path, file bytes)` of all files
under the drop directory. The fingerprint is recorded in `ingested_drops`
only after the drop ingests cleanly; a re-ingest is detected up front and
is a complete no-op (no counter changes, no duplicate merges, no report
changes). Verified byte-identical reports in tests and on real data.

### Orphan re-linking

Events are processed in order: malformed → duplicate → orphan → unknown
disposition. An event that cannot link is quarantined, never counted in any
report. After every lead ingest (and after event ingest within a drop) the
quarantine is re-attempted to a **fixpoint**: a direct `(workflow, phone)`
match, plus overrides whose `call_id` now resolves to a lead — the loop
repeats because an override may resolve only after its call's initiated
event has itself been re-linked. `data_quality.json: orphan_events` is the
live quarantine count, i.e. the final state.

### Derived facts (SQL, `report.py`)

One CTE chain: `initiated` (earliest `call.initiated` per
`(workflow_id, call_id)`), `connected` / `telef_failed` (pre-aggregated
per-call sets), `calls` (effective disposition via a per-call scalar
subquery ordered override-first, `occurred_at DESC`, `event_id DESC`),
`call_disp` (join to the catalogue for `is_terminal` / `is_success`),
`lead_calls`, `lead_bucket`, `recent_disp`
(most recent call with a disposition, by initiated time, tie-break
`call_id DESC`). Python only formats: IST dates/timestamps, the 4-decimal
connectivity rate, the `+91******1234` mask, booleans.

## Decisions (see SUBMISSION.md for the full table)

- Batch with zero leads gets **no** `batch_summary.csv` row (spec: only
  batches with ≥ 1 lead).
- Effective-disposition tie-break: `event_id` descending (documented).
  `current_disposition` tie-break: initiated time, then `call_id` descending.
- Unknown-workflow lead directory: skipped with a stderr warning, no crash.
- Unparseable event timestamp (never occurs in generated data): counted as
  malformed and skipped.
- A duplicate lead row with a blank *required* field is rejected, not
  merged — rejection is decided before dedupe, per the spec's field walk.
- Defaults apply after merging, to fields still null.
- Rejected rows are never leads, so their events stay orphaned (e.g. calls
  to a bad-phone lead can never link).

## Verification (seed 42, small; full numbers in SUBMISSION.md)

- `python -m pytest solution/tests -q` → **55 passed**.
- Ingest drop_1 → report; ingest drop_2 → report; re-ingest drop_1 →
  byte-identical reports; fresh DB ingesting both drops in one command →
  byte-identical reports.
- Invariants on seeds 42/7/123: `lead_rows_total = rejected + merged +
  created`; batch totals = lead rows; daily `total_calls` sum = lead
  `total_calls` sum.
- Orphans: seed 42, 1123 after drop_1 → 462 after drop_2 (re-linked once
  leads arrived; the remainder are calls to never-uploaded numbers and to
  rejected leads).
- PII grep on the real DB: 0 hits for 502 names, 2017 raw phones, 1961
  canonical `+91…` numbers.
- Large scale (~140k leads, ~600k events): timings in SUBMISSION.md.
