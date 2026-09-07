"""Snapshot moderation before conflation; acknowledge only completed publication."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from common.config import get_settings
from pipeline.common.io import atomic_write_text


def snapshot(dsn: str, path: Path) -> None:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        rows = conn.execute(
            "SELECT id, left_key, right_key, decision, decided_at, published_at "
            "FROM poi_match_queue WHERE decision <> 'pending' ORDER BY id"
        ).fetchall()
    atomic_write_text(path, json.dumps(rows, default=str))


def load_decisions(path: Path) -> list[dict]:
    rows = json.loads(path.read_text())
    if not isinstance(rows, list):
        raise ValueError("Decision snapshot must be a list")
    return rows


def enqueue(dsn: str, path: Path) -> None:
    import psycopg

    rows = json.loads(path.read_text())["rows"]
    with psycopg.connect(dsn) as conn:
        for row in rows:
            left, right = sorted((row["left_key"], row["right_key"]))
            conn.execute(
                "INSERT INTO poi_match_queue (left_key,right_key,score,name_score,distance_m,category_ok) "
                "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (left_key,right_key) DO UPDATE SET "
                "score=EXCLUDED.score,name_score=EXCLUDED.name_score,"
                "distance_m=EXCLUDED.distance_m,category_ok=EXCLUDED.category_ok",
                (
                    left,
                    right,
                    row["score"],
                    row["name_score"],
                    row["distance_m"],
                    row["category_ok"],
                ),
            )


def mark_published(dsn: str, snapshot_path: Path, export_path: Path) -> None:
    import psycopg

    # A decision is not published if one side was absent/closed in the export.
    exported = {
        json.loads(line)["properties"]["id"]
        for line in export_path.read_text().splitlines()
        if line.strip()
    }
    with psycopg.connect(dsn) as conn:
        for row in load_decisions(snapshot_path):
            ids = []
            for key in (row["left_key"], row["right_key"]):
                source, source_id = key.split(":", 1)
                found = conn.execute(
                    "SELECT poi_id FROM poi_source WHERE source=%s AND source_id=%s",
                    (source, source_id),
                ).fetchone()
                ids.append(str(found[0]) if found and found[0] else None)
            if not all(poi_id in exported for poi_id in ids):
                continue
            if (ids[0] == ids[1]) != (row["decision"] == "merge"):
                raise RuntimeError(f"Decision {row['id']} does not match published identities")
            conn.execute(
                "UPDATE poi_match_queue SET published_at=now() WHERE id=%s AND decision=%s "
                "AND decided_at=%s::timestamptz AND published_at IS NULL",
                (row["id"], row["decision"], row["decided_at"]),
            )


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("snapshot", "enqueue", "published", "pending"))
    parser.add_argument("--dsn", default=settings.database_url)
    parser.add_argument(
        "--snapshot", type=Path, default=settings.data_dir / "exports" / "decisions.json"
    )
    parser.add_argument(
        "--queue", type=Path, default=settings.data_dir / "exports" / "review_queue.json"
    )
    parser.add_argument(
        "--export", type=Path, default=settings.data_dir / "exports" / "pois.geojsonseq"
    )
    args = parser.parse_args()
    if args.action == "snapshot":
        snapshot(args.dsn, args.snapshot)
    elif args.action == "enqueue":
        enqueue(args.dsn, args.queue)
    elif args.action == "published":
        mark_published(args.dsn, args.snapshot, args.export)
    else:
        print(
            "yes"
            if any(not row.get("published_at") for row in load_decisions(args.snapshot))
            else "no"
        )


if __name__ == "__main__":
    main()
