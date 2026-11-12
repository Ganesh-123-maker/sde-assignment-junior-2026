# Submission

## Candidate
- **Name:** Barmavath Ganesh
- **Assignment chosen:** A (Data)
- **Time spent (honest estimate):** ~30 minutes of session time on
  12 Nov 2026 (14:27–14:55 IST), build + tests + verification in one
  sitting. The large-scale ingest wall-clock (~11 min, mostly
  unattended I/O wait) is inside that window. The build was done by a
  delegated builder agent (Muse) working from my spec; I reviewed the
  spec decisions and the verification numbers.

## How to run
```bash
# from a clean clone, repo root; Python 3.11+ stdlib only, no install needed
# (only pytest is needed for tests; it is already in requirements.txt)
python -m pytest solution/tests -q

# generate data (any seed; evaluation uses a fresh one)
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data

# ingest (one drop at a time, or several in one command, in order)
python -m solution ingest data/drop_1 [--db au.db]
python -m solution ingest data/drop_2 [--db au.db]
python -m solution ingest data/drop_1 data/drop_2 [--db au.db]

# report (defaults: --db au.db --out reports/)
python -m solution report --db au.db --out reports/
```

## Design
SQLite (WAL mode), one transaction per file, `executemany` batches.
`solution/normalize.py` holds pure parsers (phone, date, decimal, enum,
timestamps); `solution/db.py` the schema + PII hashing; `solution/ingest.py`
lead/event ingestion, orphan re-linking, idempotency; `solution/report.py`
SQL aggregation; `solution/__main__.py` the CLI. Full design in
`solution/README.md`.

Schema (all in `solution/db.py`):

- **`leads`** — PK `(workflow_id, phone_hash)`. `phone_last4`, `name_hash`
  (HMAC-SHA256), `batch_id` (first file's stem, permanent), `external_ref`
  (the schema's `reference` field), `attrs_json` (all other normalised
  fields: dates ISO, decimals 2dp strings, enums lower-case).
- **`events`** — append-only ledger, `event_id` PK. `event_type`,
  `workflow_id`, `call_id`, `phone_hash`, `lead_phone_hash` (the link),
  `occurred_at` (UTC ISO), `payload_json` (phone keys stripped),
  `unknown_disposition` flag. Overrides are new rows; nothing is ever
  edited or deleted.
- **`quarantine`** — orphan events (`event_id` PK) with the fields needed
  to re-link: `workflow_id`, `call_id`, `phone_hash`, `occurred_at`,
  `payload_json`.
- **`seen_event_ids`** — `event_id` PK, explicit persistent dedupe.
- **`ingested_drops`** — drop fingerprint (SHA-256 over sorted relative
  path + bytes of every file in the drop) as PK.
- **`counters`** — cumulative counts for `data_quality.json`.
- **`rejections`** — `(workflow_id, batch_id, row_number, reason)`;
  `rejections.csv` and `rejections_by_reason` derive from it.
- **`dispositions`** — the `dispositions.csv` catalogue, upserted at open
  so the SQL aggregation can join `is_terminal` / `is_success`.

Indexes on `leads(workflow_id, phone_hash)`,
`events(workflow_id, call_id)`, `events(workflow_id, lead_phone_hash)`,
`events(event_type)`, `quarantine(workflow_id, call_id)` and
`quarantine(workflow_id, phone_hash)`.

PII: phone → HMAC-SHA256 hex of the canonical `+91XXXXXXXXXX` keyed by a
per-deployment pepper, plus `phone_last4` as a separate column; name →
HMAC-SHA256 hex of the trimmed name. Pepper from `AU_PII_KEY` if set, else
an auto-generated `<db>.pii.key` file (mode 0600) next to the DB, reused on
later runs. No report needs a name back, so a keyed hash is sufficient and
avoids a crypto dependency; a future feature that must *display* a name
would need Fernet/KMS envelope encryption instead (the hash cannot be
reversed). Raw event JSON is never stored; only the sanitised payload.

Reports are one SQL CTE chain (`initiated` → `disp` → `eff` via
`ROW_NUMBER()` ordered override-first, `occurred_at DESC`, `event_id DESC`
→ `calls` → `lead_calls` → `lead_bucket` → `recent_disp`); Python only
formats IST dates/timestamps, the 4-decimal rate, the `+91******1234` mask
and booleans.

## Decisions & assumptions

| Decision | Why | Alternative considered |
|---|---|---|
| Batch with zero leads gets no `batch_summary.csv` row | My call — the spec doesn't state it; every generated batch has leads, so this never arises on generated data | Emit a zero row
| Effective-disposition tie-break: `event_id` descending | My call — the spec is silent on ties; deterministic across runs | `occurred_at` only, then insertion order — non-deterministic across runs |
| `current_disposition` tie-break: initiated time, then `call_id` descending | My call — the spec is silent on ties; descending matches the `event_id` DESC convention elsewhere | `call_id` ascending |
| Unknown-workflow lead directory: skip with stderr warning, no crash | Spec: skip, do not crash | Fail the whole ingest — would lose a good drop to one stray dir |
| Unparseable event timestamp: count as malformed, skip | No usable time means the event cannot appear in any report; never occurs in generated data | Store with NULL time and exclude from call logic — leaves a row that can never be reported |
| Duplicate lead row with a blank *required* field is rejected, not merged | Rejection is decided by the schema-order field walk, before dedupe; a "valid row" is what merges | Merge it anyway — would silently accept rows the spec says to reject |
| Required non-phone field present but invalid (no such field in shipped schemas): warning + null, like an optional field | The spec defines only two rejection reasons (`missing_required:*`, `invalid_phone`); inventing a third reason would break the diff | Reject with an invented `invalid_<field>` reason |
| PII: HMAC-SHA256 hash, not encryption | No report needs the name/phone back; hash is stdlib-only and sufficient for matching | Fernet — needs a dependency and key management for no benefit here |
| Pepper in `<db>.pii.key` (0600) or `AU_PII_KEY` | Stable per deployment, no secret in code or DB | Pepper in the DB — defeats the purpose; pepper in git — worse |
| Drop fingerprint over sorted (relpath, bytes) of all files | Any content or filename change = a different drop; re-ingest is a provable no-op | Fingerprint only file sizes/mtimes — mtimes change on copy |
| Orphan re-link runs after lead ingest *and* after event ingest, to a fixpoint | An override may resolve only after its call's initiated event re-links; a same-drop lead/event ordering must not strand events | Re-link only on lead ingest — misses overrides whose initiated event arrives in the same drop |
| `lead_rows_total = rejected + created + merged` arithmetic | Follows from the spec's counting rules; used as a verification invariant | — |

## Failure modes considered
- **Re-ingest / duplicate delivery:** drop fingerprint no-op; `event_id`
  dedupe is persistent across drops (incl. orphans and unknown-disposition
  events, which still occupy their `event_id`).
- **Events before leads:** quarantined, re-linked to a fixpoint on every
  lead ingest; `orphan_events` in `data_quality.json` is the live
  quarantine count (final state).
- **Bad input:** malformed JSON / non-object / missing `event_id` counted
  and skipped; blank CSV rows and blank JSONL lines not counted; unknown
  workflow dirs warned and skipped; unknown disposition codes flagged in
  the ledger and excluded from effective disposition.
- **Crash mid-drop:** one transaction per file, so a crash loses at most
  the in-flight file — **but** the drop fingerprint is recorded only after
  the whole drop succeeds, so a re-ingest after a mid-drop crash would
  re-process the already-ingested files of that drop. Counters would move
  (events would be caught by `seen_event_ids` and counted as duplicates).
  **Not handled:** fully atomic drop ingest / resume-from-checkpoint.
  A production version would record per-file fingerprints.
- **Lost pepper file:** hashes change, leads/events stop matching; only
  recovery is a fresh DB. **Not handled:** pepper backup/rotation.
- **Clock/timezones:** naive timestamps are IST; all report dates are IST
  dates; timestamps stored as UTC. A real bug here (SQLite `'+5:30 hours'`
  modifier returning NULL) was caught by tests — see AI_USAGE.md.
- **Scale:** large-scale run timed (see Results); the report is fast
  after two planner fixes (31s), but ingest wall-clock is slow on this
  VM (drop_1 2m53s, drop_2 7m21s — mostly I/O wait, not CPU). In-memory
  `seen` set and lead index are built per drop — fine at 600k events,
  would need chunking past ~10M. Batching the per-line counter UPDATEs
  and ledger INSERTs (`executemany`, flush counters per file) is the
  known next optimisation.
- **Not handled:** Postgres (SQLite chosen deliberately), concurrent
  writers (single-process CLI), schema evolution, events with a
  `workflow_id` that has no schema (orphaned by lookup failure, which is
  the spec-consistent outcome).

## Testing
- `python -m pytest solution/tests -q` → **55 passed** (~0.3s), all on
  small synthetic fixtures built in-test (not only generator output).
- Covered: phone (all 6 formats + 7 invalid), dates (all 4 formats +
  impossible), decimal (prefixes, commas, negative, junk), enum case,
  header alias mapping + reordered/renamed columns + extra columns +
  duplicate-column first-wins, rejection order (schema order, first
  failure wins), duplicate merging (overwrite only non-null, batch sticks,
  blank-required-field dup rejected), defaults after merge, effective
  disposition (override beats set, latest wins, unknown ignored, tie-break
  `event_id` DESC), buckets (all 4, incl. all-telephony-failed vs mixed),
  call without `initiated` not counted, orphan re-linking across drops
  incl. the override chain, malformed/duplicate/unknown counts,
  idempotent re-ingest (byte-identical reports), one-command vs two-command
  ingest (byte-identical), PII (no plaintext name/phone bytes in the DB),
  report formats (4-decimal rate, masked phone, IST `last_call_at`, sort
  orders, IST date rollover at 22:00Z).
- Not tested: Postgres, concurrent ingest, crash-mid-drop recovery,
  multi-GB scale beyond the large fixture.

## Results
Seed 42 (small, 2,057 lead rows, 8,745 event lines, 1,892 leads):
- `lead_rows_total` 2057 = rejected 108 + merged 57 + created 1892 ✓;
  batch totals = 1892 ✓; daily `total_calls` 2536 = lead `total_calls` 2536 ✓.
- Orphans: **1123 after drop_1 → 462 after drop_2**. The drop is real
  re-linking (events whose leads arrived in drop_2). The remaining 462 are
  explicable, not a leak: calls to numbers never uploaded (the generator
  adds ~10 per workflow) plus calls belonging to rejected leads (e.g. a
  bad-phone lead's calls can never link — the phone is invalid).
- Re-ingest drop_1 → byte-identical reports; fresh DB, both drops in one
  command → byte-identical reports.
- PII grep of `au.db`: **0 hits** for 502 names, 2017 raw phone strings,
  1961 canonical `+91…` numbers.
- Small-scale ingest: drop_1 ~0.6s, drop_2 ~0.7s (this VM).

Fresh seeds 7 and 123 (same flow, all invariants held, re-ingest identical):
- Seed 7: rows 2065, rejected 114, merged 65, leads 1886, events 8834,
  malformed 8, dup events 265, unknown 11, warnings 133,
  orphans 1207 → 479.
- Seed 123: rows 2048, rejected 98, merged 48, leads 1902, events 9125,
  malformed 8, dup events 241, unknown 16, warnings 125,
  orphans 1242 → 450.

Large scale (seed 42, `--scale large`, 143,990 lead rows, 632,602 event
lines, 132,903 leads):
- Generation: 20s.
- Ingest drop_1: **2m53s**; ingest drop_2: **7m21s**; report: **31s**.
  Total ≈ 10m45s — the under-2-minutes stretch goal is **missed** on
  ingest wall-clock on this VM. The ingest is not CPU-bound (user CPU
  only ~28s + ~40s for the two ingests; the rest is I/O wait / CPU
  throttling on this shared 1.5-CPU VM), and the per-row `execute()` calls
  (~3 per event line: counter UPDATE + seen INSERT + ledger INSERT) are
  the obvious next fix: batch counters in memory and flush per file, and
  `executemany` the ledger/seen inserts.
- The report itself was the bigger story: the first version ran 6+
  minutes CPU-bound without writing a file (see AI_USAGE.md — two real
  planner bugs, correlated EXISTS and a window-join planned as a per-call
  full scan). After both fixes the full large report runs in **31s**
  (batch query alone 22.8s).
- Large invariants held: arithmetic True (7097 + 3990 + 132903 =
  143990), batch totals = 132903 leads, daily `total_calls` 182733 =
  lead `total_calls` 182733. Orphans 34,424 (same causes as small scale,
  at scale: never-uploaded numbers + rejected-lead calls).

Surprising in the data: the orphan count after drop_1 is very large
(~55% of drop_1 events) because the generator assigns events to drops by
*time* (cutoff 7 Oct) while leads arrive by *batch* — so most drop_1
events belong to batch_003 leads that only arrive in drop_2. The pipeline
handles this exactly as the spec requires (re-link on lead arrival).

## If I had another day
1. Per-file fingerprints + resume, so a crash mid-drop is recoverable
   instead of re-processing the drop's finished files.
2. Pepper backup/rotation story (KMS-backed) and a documented re-key
   procedure.
3. A `report --check` mode that asserts the invariants
   (`lead_rows_total = rejected + merged + created`, daily vs lead call
   sums) and fails loudly instead of trusting them.
4. Production note (100k leads/day): scheduled drop polling, dead-letter
   queue for malformed lines with the raw line preserved (not in the DB),
   backfill by drop fingerprint, Postgres with the same schema, and a
   daily reconciliation job diffing report totals against the ledger.
5. Property-based tests (Hypothesis) over the phone/date/decimal parsers.
