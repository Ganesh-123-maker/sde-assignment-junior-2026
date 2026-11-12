"""Assignment A — Interaction Ledger & Reporting Pipeline.

Usage (from the repo root):
    python -m solution ingest data/drop_1 [--db au.db]
    python -m solution ingest data/drop_2 [--db au.db]
    python -m solution ingest data/drop_1 data/drop_2 [--db au.db]
    python -m solution report --db au.db --out reports/

Defaults: database ``au.db`` in the current directory, reports in ``reports/``.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .db import Store, find_data_dir, load_dispositions, load_schemas
from .ingest import ingest_drop
from .report import generate_reports


def _open_store(db_path: str) -> tuple[Store, dict]:
    data_dir = find_data_dir()
    store = Store(db_path)
    schemas = load_schemas(data_dir / "schemas")
    load_dispositions(store, data_dir / "schemas" / "dispositions.csv")
    return store, schemas


def cmd_ingest(args) -> int:
    store, schemas = _open_store(args.db)
    try:
        for drop in args.drops:
            drop_path = Path(drop)
            if not drop_path.is_dir():
                print(f"error: drop directory not found: {drop}", file=sys.stderr)
                return 1
            ingest_drop(store, drop_path, schemas)
        return 0
    finally:
        store.close()


def cmd_report(args) -> int:
    store, _schemas = _open_store(args.db)
    try:
        written = generate_reports(store, args.out)
        for p in written:
            print(f"wrote {p}")
        return 0
    finally:
        store.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="solution",
        description="Assignment A: Interaction Ledger & Reporting Pipeline",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="ingest one or more drops, in order")
    p_ingest.add_argument("drops", nargs="+", help="drop directories, e.g. data/drop_1")
    p_ingest.add_argument("--db", default="au.db", help="SQLite DB path (default: au.db)")
    p_ingest.set_defaults(func=cmd_ingest)

    p_report = sub.add_parser("report", help="write the five report files")
    p_report.add_argument("--db", default="au.db", help="SQLite DB path (default: au.db)")
    p_report.add_argument("--out", default="reports/", help="report directory (default: reports/)")
    p_report.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
