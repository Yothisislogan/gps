"""Tests for the POI ingest, load and export jobs.

No network, no database, no osmium: every job is exercised through its pure
functions plus small synthetic files, which is also how they should be debugged
when a nightly build goes wrong at 02:15.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar

import pytest

from pipeline.common.io import write_geojsonseq
from pipeline.pois.export_geojson import (
    gazetteer_document,
    iter_from_file,
    to_index_document,
    to_tile_feature,
)
from pipeline.pois.fetch_osm_pois import (
    build_record,
    iter_records,
    normalize_phone,
    polygon_centroid,
)
from pipeline.pois.fetch_overture import build_query, record_from_row
from pipeline.pois.load_pois import _params, _source_pairs

MANAGUA = (12.1150, -86.2504)


def osm_feature(**tags) -> dict:
    return {
        "type": "Feature",
        "id": tags.pop("_id", "n1"),
        "geometry": {"type": "Point", "coordinates": [MANAGUA[1], MANAGUA[0]]},
        "properties": tags,
    }


class TestPhoneNormalisation:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("+50525522522", "+50525522522"),
            ("2552-2522", "+50525522522"),
            ("2552 2522", "+50525522522"),
            ("8888 7777", "+50588887777"),
            ("(505) 2552 2522", "+50525522522"),
            ("505 7777 8888", "+50577778888"),
            ("+50625522522", "+50625522522"),  # a Costa Rican number in a border town
            ("+50525522522;+50588887777", "+50525522522"),  # first of a list
        ],
    )
    def test_normalises(self, raw: str, expected: str):
        # Local tagging drops the country code about half the time, which breaks
        # both tel: links and wa.me links.
        assert normalize_phone(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "no tiene", "12", "llamar al local"])
    def test_rejects_non_numbers(self, raw: str | None):
        assert normalize_phone(raw) is None


class TestPolygonCentroid:
    def test_square(self):
        square = [[[-86.2, 12.1], [-86.1, 12.1], [-86.1, 12.2], [-86.2, 12.2], [-86.2, 12.1]]]
        lon, lat = polygon_centroid(square)
        assert (lon, lat) == pytest.approx((-86.15, 12.15), abs=1e-6)

    def test_l_shape_is_not_the_bbox_centre(self):
        shape = [
            [[0.0, 0.0], [2.0, 0.0], [2.0, 1.0], [1.0, 1.0], [1.0, 2.0], [0.0, 2.0], [0.0, 0.0]]
        ]
        lon, lat = polygon_centroid(shape)
        assert lon == pytest.approx(lat, abs=1e-9)  # symmetric about the diagonal
        assert lon < 1.0

    def test_degenerate_ring_falls_back_to_the_mean(self):
        # Badly drawn buildings really do have zero area; dividing by it would
        # take the whole nightly build down.
        line = [[[-86.2, 12.1], [-86.1, 12.1], [-86.2, 12.1]]]
        lon, lat = polygon_centroid(line)
        assert lat == pytest.approx(12.1)
        assert -86.2 <= lon <= -86.1

    def test_single_point_ring(self):
        assert polygon_centroid([[[-86.2, 12.1]]]) == (-86.2, 12.1)


class TestOsmRecords:
    def test_restaurant(self):
        record = build_record(
            osm_feature(
                amenity="restaurant",
                name="Restaurante El Zaguán",
                phone="2552-2522",
                cuisine="nicaraguan;grill",
                opening_hours="Mo-Sa 12:00-22:00",
            )
        )
        assert record is not None
        assert record["category"] == "restaurante"
        assert record["phone"] == "+50525522522"
        assert record["cuisine"] == ["nicaraguan", "grill"]
        assert record["source"] == "osm"

    def test_alternative_names_are_collected(self):
        record = build_record(
            osm_feature(
                amenity="fuel", name="Puma Km 9", old_name="Esso Km 9", **{"name:es": "Puma Km 9"}
            )
        )
        assert record is not None
        # old_name matters more here than elsewhere: people navigate by what a
        # place used to be called long after the sign changes.
        assert "Esso Km 9" in record["name_alt"]
        assert "Puma Km 9" not in record["name_alt"]

    def test_disused_prefix_marks_the_place_closed(self):
        record = build_record(
            osm_feature(
                name="Restaurante La Casona",
                amenity="restaurant",
                **{"disused:amenity": "restaurant"},
            )
        )
        assert record is not None
        assert record["status"] == "closed"

    def test_unnamed_places_are_skipped(self):
        assert build_record(osm_feature(amenity="restaurant")) is None

    def test_untaxonomised_tags_are_skipped(self):
        assert build_record(osm_feature(name="Algo", man_made="surveillance")) is None

    def test_points_outside_nicaragua_are_skipped(self):
        feature = osm_feature(amenity="restaurant", name="Paris Brasserie")
        feature["geometry"]["coordinates"] = [2.35, 48.85]
        assert build_record(feature) is None

    def test_polygon_features_use_the_centroid(self):
        feature = {
            "type": "Feature",
            "id": "w9",
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [[-86.2, 12.1], [-86.1, 12.1], [-86.1, 12.2], [-86.2, 12.2], [-86.2, 12.1]]
                ],
            },
            "properties": {"shop": "supermarket", "name": "La Colonia"},
        }
        record = build_record(feature)
        assert record is not None
        assert record["lat"] == pytest.approx(12.15, abs=1e-4)
        assert record["lon"] == pytest.approx(-86.15, abs=1e-4)

    def test_address_falls_back_to_street_and_number(self):
        record = build_record(
            osm_feature(
                amenity="pharmacy",
                name="Farmacia Xolotlán",
                **{"addr:street": "Pista Juan Pablo II", "addr:housenumber": "120"},
            )
        )
        assert record is not None
        assert record["address_text"] == "Pista Juan Pablo II 120"

    def test_iter_records_writes_features_and_can_clip_to_the_circle(self, tmp_path: Path):
        source = tmp_path / "osm_pois.geojsonseq"
        far = osm_feature(amenity="restaurant", name="Lejos", _id="n2")
        far["geometry"]["coordinates"] = [-83.77, 12.0]  # Bluefields: outside the circle
        write_geojsonseq(source, [osm_feature(amenity="restaurant", name="Cerca"), far])

        assert len(list(iter_records(source))) == 2
        inside = list(iter_records(source, circle_only=True))
        assert len(inside) == 1
        assert inside[0]["properties"]["name"] == "Cerca"
        assert inside[0]["geometry"]["coordinates"] == [MANAGUA[1], MANAGUA[0]]


class TestOvertureRecords:
    LEGACY_COLUMNS: ClassVar[set[str]] = {
        "id",
        "geometry",
        "categories",
        "confidence",
        "websites",
        "socials",
        "phones",
        "brand",
        "addresses",
        "names",
        "sources",
        "operating_status",
        "bbox",
    }
    NEW_COLUMNS: ClassVar[set[str]] = (LEGACY_COLUMNS - {"categories"}) | {
        "basic_category",
        "taxonomy",
    }

    def test_query_uses_the_legacy_column_while_it_exists(self):
        query = build_query("2026-08-19.0", self.LEGACY_COLUMNS)
        assert "categories.primary AS legacy_category" in query
        assert "NULL AS tax_primary" in query

    def test_query_survives_the_categories_removal(self):
        # The 2026-09 quarterly release drops `categories`; selecting it would
        # fail the whole job rather than degrade.
        query = build_query("2026-09-23.0", self.NEW_COLUMNS)
        assert "categories.primary" not in query
        assert "taxonomy.primary AS tax_primary" in query
        assert "basic_category" in query

    def test_query_filters_to_nicaragua(self):
        # The bounding box bleeds into Honduras and Costa Rica, so the country
        # filter is not optional.
        query = build_query("2026-08-19.0", self.LEGACY_COLUMNS)
        assert "addresses[1].country = 'NI'" in query

    def test_record_from_row(self):
        record = record_from_row(
            {
                "id": "08f2ab",
                "name": "El Zaguan",
                "confidence": 0.9,
                "lat": 11.9303,
                "lon": -85.9552,
                "phones": '["+50525522522"]',
                "websites": "[]",
                "socials": '["https://www.facebook.com/elzaguan", "https://instagram.com/zaguan"]',
                "addr_freeform": "Calle Corrales",
                "addr_locality": "Granada",
                "src_dataset": "meta",
                "src_license": "CDLA-Permissive-2.0",
                "legacy_category": "restaurant",
                "operating_status": "open",
            }
        )
        assert record is not None
        assert record["category"] == "restaurante"
        assert record["gers_id"] == "08f2ab"
        assert record["facebook"].endswith("elzaguan")
        assert record["instagram"].endswith("zaguan")
        assert record["status"] == "open"
        # The licence travels with the row so the published dataset stays
        # attributable per source.
        assert record["overture_license"] == "CDLA-Permissive-2.0"

    def test_bare_leaf_category_resolves(self):
        record = record_from_row(
            {"id": "x", "name": "Bomba", "lat": 12.1, "lon": -86.2, "tax_primary": "gas_station"}
        )
        assert record is not None
        assert record["category"] == "gasolinera"

    def test_unknown_category_keeps_the_place(self):
        # An uncategorised restaurant is still a real business; the admin queue
        # can classify it later.
        record = record_from_row(
            {"id": "x", "name": "Algo", "lat": 12.1, "lon": -86.2, "tax_primary": "quantum_widget"}
        )
        assert record is not None
        assert record["category"] == "otro"

    def test_closed_status(self):
        record = record_from_row(
            {
                "id": "x",
                "name": "Cerrado",
                "lat": 12.1,
                "lon": -86.2,
                "operating_status": "closed_permanently",
            }
        )
        assert record is not None
        assert record["status"] == "closed"

    @pytest.mark.parametrize(
        "row",
        [
            {"id": "x", "name": "", "lat": 12.1, "lon": -86.2},
            {"id": "x", "name": "Sin punto", "lat": None, "lon": -86.2},
        ],
    )
    def test_unusable_rows_are_skipped(self, row: dict):
        assert record_from_row(row) is None


class TestExport:
    ROW: ClassVar[dict] = {
        "id": "abc",
        "name": "Vulcanización El Rayo",
        "category": "vulcanizacion",
        "lat": 12.12,
        "lon": -86.27,
        "verified_at": "2026-01-01",
        "phone": "+50588887777",
        "popularity": 0.5,
        "name_alt": ["El Rayo"],
    }

    def test_tile_feature_shape(self):
        feature = to_tile_feature(self.ROW)
        assert feature["geometry"]["coordinates"] == [-86.27, 12.12]
        assert feature["properties"]["category"] == "vulcanizacion"
        assert feature["properties"]["verified"] is True
        # The tippecanoe extension is what keeps the categories drivers need
        # alive when the densest cells are thinned.
        assert feature["tippecanoe"]["minzoom"] >= 0
        assert feature["tippecanoe"]["maxzoom"] == 14

    def test_tile_features_omit_volatile_fields(self):
        # Phone numbers and hours change far more often than tiles are rebuilt,
        # so they belong on the card, not baked into every tile.
        properties = to_tile_feature(self.ROW)["properties"]
        for volatile in ("phone", "opening_hours", "website", "facebook"):
            assert volatile not in properties

    def test_rank_prefers_verified_places_with_contact_details(self):
        bare = dict(self.ROW, verified_at=None, phone=None, popularity=0.0)
        assert (
            to_tile_feature(self.ROW)["properties"]["rank"]
            < to_tile_feature(bare)["properties"]["rank"]
        )

    def test_index_document_shape(self):
        document = to_index_document(self.ROW)
        assert document["id"] == "poi:abc"
        assert document["kind"] == "poi"
        assert document["_geo"] == {"lat": 12.12, "lng": -86.27}
        # Accent-folded copy so "vulcanizacion" matches "Vulcanización".
        assert "vulcanizacion" in document["name_norm"]
        assert document["verified"] is True

    def test_gazetteer_document(self):
        document = gazetteer_document(
            {
                "id": "g1",
                "name": "Rotonda El Güegüense",
                "name_alt": ["Plaza España"],
                "kind": "rotonda",
                "lat": 12.1352,
                "lon": -86.2807,
                "former": False,
                "popularity": 0.9,
                "city": "Managua",
            }
        )
        assert document["id"] == "landmark:g1"
        assert document["kind"] == "landmark"
        assert "gueguense" in document["name_norm"]
        assert "espana" in document["name_norm"]

    def test_iter_from_file_flattens_geometry(self, tmp_path: Path):
        path = tmp_path / "merged.geojsonseq"
        write_geojsonseq(
            path,
            [
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [-86.27, 12.12]},
                    "properties": {"name": "X", "category": "otro", "source_id": "s1"},
                }
            ],
        )
        rows = list(iter_from_file(path))
        assert rows[0]["lat"] == 12.12
        assert rows[0]["lon"] == -86.27
        assert rows[0]["id"] == "s1"

    def test_iter_from_file_skips_unusable_rows(self, tmp_path: Path):
        path = tmp_path / "merged.geojsonseq"
        write_geojsonseq(
            path,
            [
                {"type": "Feature", "geometry": None, "properties": {"name": "Sin punto"}},
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [-86.2, 12.1]},
                    "properties": {"category": "otro"},
                },
            ],
        )
        assert list(iter_from_file(path)) == []


class TestLoadParams:
    def test_maps_properties_onto_sql_parameters(self):
        params = _params(
            {
                "name": "Fritanga La Fe",
                "category": "fritanga",
                "lat": 12.1415,
                "lon": -86.1682,
                "sources": {"osm_id": "n1", "overture_gers_id": "08f"},
                "confidence": 0.9,
            }
        )
        assert params["gers_id"] == "08f"
        assert params["osm_id"] == "n1"
        assert params["in_circle"] is True
        assert json.loads(params["sources"])["osm_id"] == "n1"

    def test_flags_places_outside_the_curation_circle(self):
        params = _params({"name": "Bluefields", "category": "otro", "lat": 12.0, "lon": -83.77})
        assert params["in_circle"] is False

    def test_sources_may_arrive_as_a_json_string(self):
        params = _params(
            {
                "name": "X",
                "category": "otro",
                "lat": 12.1,
                "lon": -86.2,
                "sources": '{"osm_id": "n5"}',
            }
        )
        assert params["osm_id"] == "n5"

    def test_source_pairs(self):
        assert _source_pairs({"sources": {"osm_id": "n1", "overture_gers_id": "08f"}}) == [
            ("osm", "n1"),
            ("overture", "08f"),
        ]
        assert _source_pairs({"sources": {}}) == []


def test_conflation_preserves_each_overture_license():
    from pipeline.pois.conflate import conflate
    from pipeline.pois.load_pois import _source_license

    records = [
        {
            "source": "overture",
            "source_id": str(i),
            "name": "Cafe",
            "lat": 12.1,
            "lon": -86.2,
            "overture_license": license,
        }
        for i, license in enumerate(("Apache-2.0", "CDLA-Permissive-2.0"))
    ]
    result = conflate(
        records,
        decisions=[{"left_key": "overture:0", "right_key": "overture:1", "decision": "merge"}],
    )
    props = result.merged[0].as_feature()["properties"]
    assert _source_license(props, "overture", "0") == "Apache-2.0"
    assert _source_license(props, "overture", "1") == "CDLA-Permissive-2.0"
    with pytest.raises(ValueError, match="Missing Overture source license"):
        _source_license({}, "overture", "unknown")
