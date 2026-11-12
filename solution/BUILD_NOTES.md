# Build notes (Assignment A)

Log kept while building, so the AI_USAGE "where AI was wrong" section is
factual. Times are IST, 12 Nov 2026.

- 14:27 Build started. Read spec, schemas, generator. Installed pytest
  (pip --user --break-system-packages; system pip is externally managed).
- 14:35 Wrote solution package: normalize.py, db.py, ingest.py, report.py,
  __main__.py, .gitignore. Wrote 55-test suite (synthetic fixtures).
- 14:40 First test run: 52 passed, 3 failed. Real bugs found by tests:
  1. **SQLite modifier bug (code, real):** `date(occurred_at, '+5:30 hours')`
     returns NULL — SQLite does not accept `HH:MM` in a modifier. Verified
     directly: `'+5:30 hours'` -> NULL, `'+330 minutes'` -> correct date.
     Fixed in report.py (`'+330 minutes'`). This would have silently
     emptied every `daily_overview.csv` date on the graded fresh seed.
  2. **Test expectation bug (mine, not code):** "Hindi" as a language is a
     third field warning, not just bad date + bad premium — expected 2,
     actual 3. Fixed the test to 3.
  3. **Test fixture bug (mine, not code):** duplicate row had a blank
     required `customer_name`, so it is *rejected* (rejection runs before
     dedupe), not merged. Fixed the fixture; added an explicit test that a
     dup row with a blank required field is rejected and does not count as
     a merge.
- 14:42 Re-run: 55 passed.
- Large-scale run (seed 42, ~140k leads / ~632k event lines): generation
  20s; ingest drop_1 2m53s, drop_2 7m21s (user CPU only ~28s/40s — the
  rest is I/O wait / CPU throttling on this shared VM, not query work).
  TWO MORE REAL PERFORMANCE BUGS found while timing the report:
  4. **Correlated EXISTS was O(calls x events) (code, real):** the first
     `calls` CTE used `EXISTS (... event_type='call.connected')` per
     call; SQLite planned it as a probe of the event_type index per call
     and the report ran 6+ min CPU-bound without writing a single file.
     First fix: pre-aggregate `connected` / `telef_failed` as GROUP BY
     sets and LEFT JOIN them.
  5. **Window-function join planned as a per-call full scan (code,
     real):** even after fix 4, the batch query did not finish in 120s.
     EXPLAIN QUERY PLAN showed the `eff` window result joined back to
     `initiated` via `SCAN (subquery) LEFT-JOIN` — a full scan of the
     ~150k-row window output per initiated call. Fix: resolve the
     effective disposition with a per-call scalar subquery (probes the
     (workflow_id, call_id) index, sorts only that call's few disposition
     events), then join the catalogue once for is_terminal/is_success.
     After both fixes the large batch query was re-timed (see below).
- (large report timing + final numbers appended below)
- Large report AFTER the two planner fixes: full report 30.8s
  (batch_summary query alone 22.8s); invariants held on the large DB
  (arith True, 132,903 leads, daily_calls == lead_calls == 182,733).
  Ingest wall-clock stays the bottleneck (drop_1 2m53s, drop_2 7m21s;
  user CPU only ~28s/~40s — I/O wait/throttling on this VM).
- One more editing slip of my own, caught instantly by the suite: while
  restructuring the report CTE I left the old `calls` CTE in place
  alongside the new one — `sqlite3.OperationalError: duplicate WITH
  table name: calls`, 12 tests failed; removed the stale block and all
  55 passed again, small reports byte-identical afterwards.
