"""Atomic refresh evidence. A successful import is not a verified business."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from common.data_status import read_status
from pipeline.common.io import atomic_write_text


def timestamp() -> str:
    return datetime.now(UTC).isoformat()


def save(path: Path, payload: dict) -> None:
    atomic_write_text(path, json.dumps({**payload, "schema_version": 1}, indent=2) + "\n")


def update_run(path: Path, event: str, stage: str = "", scope: str = "full") -> None:
    previous = read_status(path)
    now = timestamp()
    if event == "start":
        payload = {
            "status": "running",
            "started_at": now,
            "scope": scope,
            "last_full_success_at": previous.get("last_full_success_at"),
            "last_success_sources": previous.get("last_success_sources", {}),
        }
    else:
        payload = previous
        payload.update(status=event, stage=stage, updated_at=now)
        if event in {"succeeded", "failed", "unchanged"}:
            payload["finished_at"] = now
        if event == "succeeded":
            payload["last_success_sources"] = {
                name: read_status(path.parent / f"{name}.json") for name in ("osm", "overture")
            }
        if event == "succeeded" and payload.get("scope") == "full":
            payload["last_full_success_at"] = now
    save(path, payload)


def overture_already_applied(directory: Path) -> bool:
    run = read_status(directory / "monthly_overture.json")
    source = read_status(directory / "overture.json")
    sources = run.get("last_success_sources") or {}
    if not isinstance(sources, dict) or not isinstance(sources.get("overture"), dict):
        return False
    applied = sources["overture"].get("sha256")
    return bool(
        run.get("status") in ("succeeded", "unchanged")
        and applied
        and applied == source.get("sha256")
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("name", choices=("nightly", "monthly_overture", "osm"))
    parser.add_argument(
        "event", choices=("start", "running", "succeeded", "failed", "unchanged", "source", "check")
    )
    parser.add_argument("--stage", default="")
    parser.add_argument("--scope", default="full", choices=("full", "partial"))
    parser.add_argument("--source-timestamp")
    args = parser.parse_args()
    path = args.directory / f"{args.name}.json"
    if args.event == "check":
        print("yes" if overture_already_applied(args.directory) else "no")
        return
    if args.event == "source":
        # Header timestamp is upstream evidence, not the file's local mtime.
        source_time = args.source_timestamp or None
        if source_time:
            datetime.fromisoformat(source_time.replace("Z", "+00:00"))
        save(
            path,
            {"status": "imported", "source_timestamp": source_time, "imported_at": timestamp()},
        )
    else:
        update_run(path, args.event, args.stage, args.scope)


if __name__ == "__main__":
    main()
