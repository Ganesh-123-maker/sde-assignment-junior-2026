# AI Usage Disclosure

## Tools used
Muse (Meta's personal AI agent, model Muse Spark), with a delegated
builder agent that did the implementation work in this session. No other AI
tools were used.

## What I used them for
- Scaffolding the solution package (`solution/normalize.py`, `db.py`,
  `ingest.py`, `report.py`, `__main__.py`) from the assignment spec.
- Writing the first draft of the 55-test pytest suite (synthetic fixtures
  for phone/date/decimal/enum parsing, rejection order, duplicate merging,
  effective disposition, buckets, orphan re-linking, idempotency, PII).
- Running the verification flows (seeds 42/7/123, re-ingest and
  one-command-vs-two diffs, invariant checks, PII grep, large-scale run)
  and drafting `solution/README.md`, `SUBMISSION.md`, and this file.

## What I wrote or decided myself
The assignment choice (Assignment A, not B) and every spec call in the
`SUBMISSION.md` decisions table are mine / my parent agent's, given to the
builder as explicit instructions before any code was written — in
particular: the HMAC-SHA256 + pepper PII design (hash, not encryption, with
the stated limit that a future name-display feature would need
Fernet/KMS); SQL aggregation with window functions/CTEs rather than
Python-side aggregation; drop fingerprinting by SHA-256 over sorted
(relative path, bytes); orphan re-linking to a fixpoint; the
`event_id`-descending tie-break; and the verification plan (fresh seeds,
orphan counts after drop_1 vs drop_2, byte-identical re-ingest diffs).
The builder implemented those decisions; I checked the results against the
spec and the verification numbers.

## Where the AI was wrong or unhelpful
At least one real example, found by the test suite (not invented):

1. **SQLite date modifier bug (real, in the builder's first draft of
   `report.py`).** The daily-overview query used
   `date(initiated_at, '+5:30 hours')` to convert UTC to IST. SQLite
   silently returns NULL for that modifier — `HH:MM` is not a valid
   modifier format — so every `daily_overview.csv` date would have come
   out empty on the graded fresh seed. The test
   `test_report_formats_ist_and_masking` caught it (date `''`, all calls
   collapsed into one row). I verified the behaviour directly in SQLite
   (`'+5:30 hours'` → NULL, `'+330 minutes'` → correct date) and fixed
   the query to `'+330 minutes'`. After the fix all 55 tests pass.
   Lesson recorded in `solution/BUILD_NOTES.md`: SQLite date modifiers
   fail silently with NULL instead of erroring, so IST conversion needs an
   explicit test with a 22:00Z timestamp (which rolls to the next IST
   date).

2. **Two test bugs that were mine/the builder's, not the code's** (also
   caught by running the suite, and fixed in the tests, not the code):
   - the rejection-order test expected 2 field warnings but "Hindi" as a
     language is a third invalid optional field (bad date + bad premium +
     bad language = 3);
   - the duplicate-merge fixture gave the duplicate row a blank required
     `customer_name`, so the row is correctly *rejected* before dedupe and
     never merges. The fixture was fixed, and the behaviour is now covered
     by an explicit test (dup row with blank required field → rejected,
     merge count unchanged).

## How I verified AI-generated code
- Read the implementation files (`normalize.py`, `db.py`, `ingest.py`,
  `report.py`) against the spec line by line before running anything.
- The 55-test suite above (synthetic fixtures, not just generator output).
- Manual verification runs on generated data, seeds 42, 7 and 123:
  ingest drop_1 → report; ingest drop_2 → report; re-ingest drop_1 and
  diff all five report files (byte-identical); fresh DB ingesting both
  drops in one command and diff (byte-identical); invariant arithmetic
  (`lead_rows_total = rejected + merged + created`, batch totals = lead
  rows, daily vs lead call sums); orphan counts after drop_1 vs drop_2
  (1123 → 462 on seed 42); and a byte-level PII grep of the real DB file
  (0 hits for 502 names, 2017 raw phones, 1961 canonical numbers).
- Large-scale run (`--scale large`, ~140k leads / ~600k events) timed and
  reported in `SUBMISSION.md`.
