#!/usr/bin/env python3
"""Validate the exact curated files consumed by the pipeline. Missing files fail."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pipeline.geocode.kmpost import load_highways  # noqa: E402
from pipeline.pois.taxonomy import load_taxonomy  # noqa: E402
from pipeline.qa.golden_routes import load_cases  # noqa: E402


def main() -> int:
    checks = [
        ("docs/taxonomy.csv", lambda path: len(load_taxonomy(path))),
        ("docs/carreteras.csv", lambda path: len(load_highways(path))),
        ("docs/golden_routes.json", lambda path: len(load_cases(path)[0])),
    ]
    failed = False
    for path, count in checks:
        try:
            size = count(REPO_ROOT / path)
            if not size:
                raise ValueError("no entries")
            print(f"ok   {path}: {size} entries")
        except (OSError, ValueError, TypeError) as exc:
            print(f"FAIL {path}: {exc}")
            failed = True
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
