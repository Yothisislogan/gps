"""Real PostGIS checks; CI supplies a dedicated disposable database."""

import json
import os
from pathlib import Path

import pytest

from pipeline.pois.conflate import conflate
from pipeline.pois.load_pois import load
from pipeline.pois.review import mark_published, snapshot

pytestmark = pytest.mark.docker
DSN = os.environ.get("NICANAV_TEST_DSN", "")


@pytest.fixture
def db():
    if not DSN:
        pytest.skip("requires NICANAV_TEST_DSN")
    import psycopg
    from psycopg.conninfo import conninfo_to_dict

    assert conninfo_to_dict(DSN).get("dbname") == "nicanav_test", (
        "Only disposable test database allowed"
    )
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        for path in sorted((Path(__file__).resolve().parents[1] / "db/migrations").glob("*.sql")):
            conn.execute(path.read_text())
        yield conn


def records():
    return [
        {
            "source": s,
            "overture_license": "Apache-2.0" if s == "overture" else None,
            "source_id": str(i),
            "name": "Cafe",
            "category": "cafe",
            "lat": 12.1,
            "lon": -86.2,
        }
        for i, s in enumerate(("osm", "overture"))
    ]


def test_merge_preserves_links_related_records_and_reimports(db, tmp_path):
    items = records()
    load([p.as_feature() for item in items for p in conflate([item]).merged], DSN)
    ids = [r[0] for r in db.execute("SELECT id FROM poi ORDER BY id").fetchall()]
    db.execute("UPDATE poi SET verified_at=now(), name='Reviewed cafe' WHERE id=%s", (ids[0],))
    db.execute(
        "INSERT INTO poi_photo(poi_id,url,license) VALUES (%s,'https://example.com/photo','CC0')",
        (ids[1],),
    )
    db.execute(
        "INSERT INTO poi_match_queue(left_key,right_key,score,decision,decided_at) VALUES ('osm:0','overture:1',0.7,'merge',now())"
    )
    path = tmp_path / "decisions.json"
    snapshot(DSN, path)
    decision = json.loads(path.read_text())
    merged = [p.as_feature() for p in conflate(items, decisions=decision).merged]
    load(merged, DSN)
    load(merged, DSN)
    assert db.execute("SELECT count(*) FROM poi").fetchone()[0] == 1
    assert db.execute("SELECT id,name FROM poi").fetchone() == (ids[0], "Reviewed cafe")
    assert (
        db.execute("SELECT poi_id FROM poi_redirect WHERE old_id=%s", (ids[1],)).fetchone()[0]
        == ids[0]
    )
    assert db.execute("SELECT poi_id FROM poi_photo").fetchone()[0] == ids[0]
    assert db.execute("SELECT published_at FROM poi_match_queue").fetchone()[0] is None
    export = tmp_path / "pois.jsonl"
    export.write_text(json.dumps({"properties": {"id": str(ids[0])}}))
    mark_published(DSN, path, export)
    assert db.execute("SELECT published_at FROM poi_match_queue").fetchone()[0] is not None


def test_separate_splits_previous_cluster_and_remains_separate(db):
    items = records()
    load(
        [
            p.as_feature()
            for p in conflate(
                items,
                decisions=[{"left_key": "osm:0", "right_key": "overture:1", "decision": "merge"}],
            ).merged
        ],
        DSN,
    )
    separated = [
        p.as_feature()
        for p in conflate(
            items,
            decisions=[{"left_key": "osm:0", "right_key": "overture:1", "decision": "separate"}],
        ).merged
    ]
    load(separated, DSN)
    load(separated, DSN)
    assert db.execute("SELECT count(*) FROM poi").fetchone()[0] == 2
    assert db.execute("SELECT count(DISTINCT poi_id) FROM poi_source").fetchone()[0] == 2


def test_failed_load_rolls_back_earlier_clusters(db):
    features = [p.as_feature() for p in conflate(records()[:1]).merged]
    bad = json.loads(json.dumps(features[0]))
    bad["properties"]["source_keys"] = ["unsupported:bad"]
    with pytest.raises(ValueError):
        load([*features, bad], DSN)
    assert db.execute("SELECT count(*) FROM poi").fetchone()[0] == 0
