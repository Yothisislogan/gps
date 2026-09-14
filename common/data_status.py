"""Read operational metadata without confusing imports with source verification."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def read_status(path: Path) -> dict[str, Any]:
    """Missing, invalid or unreadable metadata is unknown, never fresh."""
    try:
        payload = json.loads(path.read_text())
        if (
            isinstance(payload, dict)
            and payload.get("schema_version") == 1
            and payload.get("status") in ("running", "failed", "succeeded", "unchanged", "imported")
        ):
            return payload
    except (OSError, ValueError):
        pass
    return {"status": "unknown"}


def data_status(directory: Path) -> dict[str, Any]:
    return {
        "sources": {name: read_status(directory / f"{name}.json") for name in ("osm", "overture")},
        "pipelines": {
            name: read_status(directory / f"{name}.json")
            for name in ("nightly", "monthly_overture")
        },
    }
