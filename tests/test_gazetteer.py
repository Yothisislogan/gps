"""Tests for the landmark gazetteer: the curated seed and the build job.

Two very different things are checked here.

The **seed CSV** is data, and a wrong row in it is worse than a crash: every
relative address anchored on a landmark inherits that landmark's coordinate, so a
misplaced rotonda quietly moves a whole neighbourhood.  Those tests are therefore
about plausibility — inside Nicaragua, inside the curation circle, no two rows
claiming the same name in the same city, every ghost landmark explained.

The **build job** is code, and it is tested with tiny synthetic GeoJSON-seq files
under ``tmp_path``.  No database, no network, no osmium.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from common.geo import CURATION_RADIUS_M, NICARAGUA_BBOX, distance_from_mga_m, haversine_m
from common.text import name_key, normalize
from pipeline.geocode.gazetteer_build import (
    DEFAULT_SEED_PATH,
    GAZETTEER_KINDS,
    GazetteerStats,
    Landmark,
    build_gazetteer,
    default_popularity,
    export_geojson,
    geometry_centroid,
    kind_from_tags,
    landmark_from_osm_feature,
    load_aliases,
    load_osm,
    load_seed,
    main,
    merge_landmarks,
    summarize,
)

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def raw_seed_rows() -> list[dict[str, str]]:
    """Parse the seed CSV with the stdlib alone, bypassing load_seed's validation."""
    with open(DEFAULT_SEED_PATH, encoding="utf-8") as handle:
        rows = (line for line in handle if not line.lstrip().startswith("#"))
        return [row for row in csv.DictReader(rows) if (row.get("name") or "").strip()]


def write_geojsonseq(path: Path, features: list[dict]) -> Path:
    """Minimal writer for the synthetic OSM exports the tests feed to load_osm."""
    path.write_text(
        "\n".join(json.dumps(feature, ensure_ascii=False) for feature in features) + "\n",
        encoding="utf-8",
    )
    return path


def osm_feature(tags: dict, geometry: dict, osm_id: str = "node/1") -> dict:
    return {
        "type": "Feature",
        "properties": {**tags, "@id": osm_id},
        "geometry": geometry,
    }


def point(lat: float, lon: float) -> dict:
    """GeoJSON point — note the [lon, lat] order the format demands."""
    return {"type": "Point", "coordinates": [lon, lat]}


# --------------------------------------------------------------------------- #
# docs/gazetteer_seed.csv
# --------------------------------------------------------------------------- #


class TestSeedFile:
    def test_it_exists_and_has_a_useful_number_of_rows(self):
        rows = raw_seed_rows()
        # Below ~90 the gazetteer does not cover Managua's spoken landmarks; the
        # upper bound is a reminder that this file is curated, not scraped.
        assert 90 <= len(rows) <= 140

    def test_every_row_parses(self):
        landmarks = load_seed(DEFAULT_SEED_PATH)
        assert len(landmarks) == len(raw_seed_rows())
        assert all(isinstance(entry, Landmark) for entry in landmarks)
        assert all(entry.source == "seed" and entry.source_id for entry in landmarks)

    def test_coordinates_are_inside_nicaragua(self):
        min_lon, min_lat, max_lon, max_lat = NICARAGUA_BBOX
        for row in raw_seed_rows():
            lat, lon = float(row["lat"]), float(row["lon"])
            assert min_lat <= lat <= max_lat, row["name"]
            assert min_lon <= lon <= max_lon, row["name"]
            # (lat, lon) not (lon, lat): Nicaragua is north of the equator and
            # west of Greenwich, so a swapped pair is caught here.
            assert lat > 0 > lon, row["name"]

    def test_everything_is_inside_or_near_the_curation_circle(self):
        for row in raw_seed_rows():
            distance = distance_from_mga_m(float(row["lat"]), float(row["lon"]))
            assert distance <= CURATION_RADIUS_M, f"{row['name']} is {distance / 1000:.1f} km out"

    def test_no_duplicate_name_and_city(self):
        seen: dict[tuple[str, str], str] = {}
        for row in raw_seed_rows():
            key = (name_key(row["name"]), normalize(row.get("city") or ""))
            assert key not in seen, f"{row['name']} duplicates {seen.get(key)}"
            seen[key] = row["name"]

    def test_kinds_are_canonical(self):
        for row in raw_seed_rows():
            assert row["kind"] in GAZETTEER_KINDS, f"{row['name']}: {row['kind']}"

    def test_popularity_is_a_probability(self):
        for row in raw_seed_rows():
            raw = (row.get("popularity") or "").strip()
            if raw:
                assert 0.0 <= float(raw) <= 1.0, row["name"]

    def test_every_former_landmark_is_explained(self):
        formers = [entry for entry in load_seed(DEFAULT_SEED_PATH) if entry.former]
        # The "donde fue" list is the differentiator; a thin one is a bug.
        assert len(formers) >= 15
        for entry in formers:
            assert entry.note, entry.name

    def test_the_landmarks_managua_actually_uses_are_present(self):
        keys = {key for entry in load_seed(DEFAULT_SEED_PATH) for key in entry.match_keys}
        for spoken in (
            "Rotonda El Güegüense",
            "Rotonda Metrocentro",
            "Rotonda Centroamérica",
            "Rotonda Jean Paul Genie",
            "Metrocentro",
            "Mercado Huembes",
            "Mercado Oriental",
            "Mercado Israel Lewites",
            "Mercado Mayoreo",
            "Galerías Santo Domingo",
            "Plaza Inter",
            "Camino de Oriente",
            "UCA",
            "Cine Cabrera",
            "Parque Central de Granada",
            "La Merced",
            "Mirador de Catarina",
        ):
            assert name_key(spoken) in keys, spoken

    def test_alt_names_are_clean(self):
        for entry in load_seed(DEFAULT_SEED_PATH):
            assert all(alt.strip() for alt in entry.name_alt)
            assert normalize(entry.name) not in {normalize(alt) for alt in entry.name_alt}

    def test_rejects_a_former_row_with_no_note(self, tmp_path: Path):
        bad = tmp_path / "seed.csv"
        bad.write_text(
            "name,name_alt,kind,lat,lon,city,former,era,popularity,note\n"
            "Cine Fantasma,,edificio,12.15,-86.27,Managua,true,,0.4,\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="needs a note"):
            load_seed(bad)

    def test_rejects_a_coordinate_outside_nicaragua(self, tmp_path: Path):
        bad = tmp_path / "seed.csv"
        bad.write_text(
            "name,name_alt,kind,lat,lon,city,former,era,popularity,note\n"
            "Rotonda Fantasma,,rotonda,48.85,2.35,Managua,false,,0.4,\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="outside Nicaragua"):
            load_seed(bad)

    def test_rejects_an_unknown_kind(self, tmp_path: Path):
        bad = tmp_path / "seed.csv"
        bad.write_text(
            "name,name_alt,kind,lat,lon,city,former,era,popularity,note\n"
            "Algo,,glorieta,12.15,-86.27,Managua,false,,0.4,\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="unknown kind"):
            load_seed(bad)


# --------------------------------------------------------------------------- #
# OSM tags -> kind
# --------------------------------------------------------------------------- #


class TestKindFromTags:
    @pytest.mark.parametrize(
        ("tags", "expected"),
        [
            ({"junction": "roundabout", "highway": "primary", "name": "Rotonda X"}, "rotonda"),
            ({"highway": "primary", "name": "Rotonda Universitaria"}, "rotonda"),
            ({"highway": "traffic_signals", "name": "Semáforos del Zumen"}, "semaforo"),
            ({"amenity": "marketplace", "name": "Mercado Oriental"}, "mercado"),
            ({"amenity": "place_of_worship", "name": "Iglesia El Calvario"}, "iglesia"),
            ({"amenity": "school", "name": "Escuela Rubén Darío"}, "colegio"),
            ({"amenity": "university", "name": "UCA"}, "universidad"),
            ({"amenity": "hospital", "name": "Hospital Bautista"}, "hospital"),
            ({"amenity": "fuel", "name": "Puma"}, "gasolinera"),
            ({"shop": "mall", "name": "Metrocentro"}, "centro_comercial"),
            ({"tourism": "hotel", "name": "Hotel Camino Real"}, "hotel"),
            ({"tourism": "viewpoint", "name": "Mirador de Catarina"}, "mirador"),
            ({"tourism": "museum", "name": "Museo Nacional"}, "edificio"),
            ({"leisure": "park", "name": "Parque Central"}, "parque"),
            ({"leisure": "stadium", "name": "Estadio Nacional"}, "estadio"),
            ({"historic": "monument", "name": "Monumento a Sandino"}, "monumento"),
            ({"aeroway": "aerodrome", "name": "Augusto C. Sandino"}, "aeropuerto"),
            ({"natural": "volcano", "name": "Volcán Masaya"}, "volcan"),
            ({"natural": "water", "name": "Laguna de Tiscapa"}, "laguna"),
            ({"man_made": "bridge", "name": "Puente El Edén"}, "puente"),
        ],
    )
    def test_mapping(self, tags: dict, expected: str):
        assert kind_from_tags(tags, tags.get("name", "")) == expected

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("Reparto Los Robles", "reparto"),
            ("Colonia Centroamérica", "colonia"),
            ("Residencial Las Colinas", "residencial"),
            ("Barrio Riguero", "barrio"),
            ("Altamira D'Este", "barrio"),
        ],
    )
    def test_place_kind_comes_from_the_first_word_of_the_name(self, name: str, expected: str):
        # OSM tags every one of these place=neighbourhood; the Nicaraguan class
        # word is only in the name.
        assert kind_from_tags({"place": "neighbourhood", "name": name}, name) == expected

    def test_roundabout_beats_the_highway_rule(self):
        # A roundabout way also carries highway=*, and the highway branch returns
        # None for anything not named "rotonda" — order matters.
        tags = {"junction": "roundabout", "highway": "secondary", "name": "La Virgen"}
        assert kind_from_tags(tags, "La Virgen") == "rotonda"

    @pytest.mark.parametrize(
        "tags",
        [
            {"highway": "residential", "name": "Calle 27 de Mayo"},
            {"highway": "milestone", "name": "Km 12"},
            {"amenity": "bench"},
            {"place": "city", "name": "Managua"},
            {"name": "Sin categoría"},
            {},
        ],
    )
    def test_unmapped_features_are_skipped(self, tags: dict):
        assert kind_from_tags(tags, tags.get("name", "")) is None


class TestDefaultPopularity:
    def test_rotondas_and_markets_outrank_schools(self):
        assert default_popularity("rotonda") > default_popularity("colegio")
        assert default_popularity("mercado") > default_popularity("colegio")
        assert default_popularity("colegio") <= 0.3

    def test_a_generic_state_school_sinks_further(self):
        assert default_popularity("colegio", "Escuela Rubén Darío") < default_popularity("colegio")

    def test_everything_is_a_probability(self):
        for kind in GAZETTEER_KINDS:
            assert 0.0 <= default_popularity(kind) <= 1.0


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #


class TestGeometryCentroid:
    def test_point_passes_through_as_lat_lon(self):
        assert geometry_centroid(point(12.1246, -86.2686)) == (12.1246, -86.2686)

    def test_closed_way_is_a_roundabout_and_uses_its_area_centroid(self):
        # A rotonda mapped as a closed way: the address anchor is the middle of
        # the circle, not a point on its kerb.
        ring = [
            [-86.2686, 12.1246],
            [-86.2680, 12.1246],
            [-86.2680, 12.1252],
            [-86.2686, 12.1252],
            [-86.2686, 12.1246],
        ]
        lat, lon = geometry_centroid({"type": "LineString", "coordinates": ring})
        assert lat == pytest.approx(12.1249, abs=1e-4)
        assert lon == pytest.approx(-86.2683, abs=1e-4)

    def test_open_way_uses_the_midpoint_along_its_length(self):
        line = [[-86.27, 12.12], [-86.27, 12.13], [-86.27, 12.14]]
        lat, lon = geometry_centroid({"type": "LineString", "coordinates": line})
        assert lat == pytest.approx(12.13, abs=1e-4)
        assert lon == pytest.approx(-86.27, abs=1e-6)

    def test_polygon_centroid(self):
        polygon = {
            "type": "Polygon",
            "coordinates": [
                [
                    [-86.27, 12.12],
                    [-86.26, 12.12],
                    [-86.26, 12.13],
                    [-86.27, 12.13],
                    [-86.27, 12.12],
                ]
            ],
        }
        lat, lon = geometry_centroid(polygon)
        assert lat == pytest.approx(12.125, abs=1e-4)
        assert lon == pytest.approx(-86.265, abs=1e-4)

    def test_multipolygon_uses_the_largest_ring(self):
        big = [
            [-86.27, 12.12],
            [-86.26, 12.12],
            [-86.26, 12.13],
            [-86.27, 12.13],
            [-86.27, 12.12],
        ]
        sliver = [
            [-86.10, 12.30],
            [-86.0999, 12.30],
            [-86.0999, 12.3001],
            [-86.10, 12.30],
        ]
        lat, lon = geometry_centroid(
            {"type": "MultiPolygon", "coordinates": [[sliver], [big]]},
        )
        assert lat == pytest.approx(12.125, abs=1e-3)
        assert lon == pytest.approx(-86.265, abs=1e-3)

    def test_degenerate_and_missing_geometries_do_not_raise(self):
        assert geometry_centroid(None) is None
        assert geometry_centroid({"type": "Point", "coordinates": []}) is None
        assert geometry_centroid({"type": "Polygon", "coordinates": []}) is None
        assert geometry_centroid({"type": "Curve", "coordinates": [1, 2]}) is None
        doubled = {"type": "LineString", "coordinates": [[-86.27, 12.12], [-86.27, 12.12]]}
        assert geometry_centroid(doubled) == pytest.approx((12.12, -86.27))

    def test_geometry_collection_takes_the_first_usable_member(self):
        collection = {
            "type": "GeometryCollection",
            "geometries": [{"type": "Polygon", "coordinates": []}, point(12.1, -86.2)],
        }
        assert geometry_centroid(collection) == (12.1, -86.2)


# --------------------------------------------------------------------------- #
# OSM features -> landmarks
# --------------------------------------------------------------------------- #


class TestLoadOsm:
    def test_converts_a_named_roundabout(self):
        feature = osm_feature(
            {"junction": "roundabout", "highway": "primary", "name": "Rotonda Centroamérica"},
            point(12.1094, -86.2545),
            osm_id="way/42",
        )
        landmark = landmark_from_osm_feature(feature)
        assert landmark is not None
        assert landmark.name == "Rotonda Centroamérica"
        assert landmark.kind == "rotonda"
        assert (landmark.lat, landmark.lon) == (12.1094, -86.2545)
        assert landmark.source == "osm"
        assert landmark.osm_id == "way/42"
        assert landmark.former is False

    def test_spanish_name_wins_and_the_other_becomes_an_alias(self):
        feature = osm_feature(
            {"amenity": "marketplace", "name": "Mercado Roberto Huembes", "name:es": "Huembes"},
            point(12.1214, -86.2445),
        )
        landmark = landmark_from_osm_feature(feature)
        assert landmark is not None
        assert landmark.name == "Huembes"
        assert "Mercado Roberto Huembes" in landmark.name_alt

    def test_unnamed_and_unmapped_features_are_counted_not_crashed(self, tmp_path: Path):
        path = write_geojsonseq(
            tmp_path / "osm.geojsonl",
            [
                osm_feature({"junction": "roundabout"}, point(12.1, -86.2)),
                osm_feature({"highway": "residential", "name": "Calle X"}, point(12.1, -86.2)),
                osm_feature({"amenity": "marketplace", "name": "Sin geometría"}, {}),
                osm_feature({"amenity": "marketplace", "name": "Mercado Ok"}, point(12.1, -86.2)),
            ],
        )
        stats = GazetteerStats()
        landmarks = list(load_osm([path], stats=stats))
        assert [entry.name for entry in landmarks] == ["Mercado Ok"]
        assert stats.osm_features == 4
        assert stats.osm_used == 1
        assert stats.osm_skipped["sin_nombre"] == 1
        assert stats.osm_skipped["sin_categoria"] == 1
        assert stats.osm_skipped["sin_geometria"] == 1

    def test_reads_the_record_separator_flavour_osmium_emits(self, tmp_path: Path):
        path = tmp_path / "osm.geojsonseq"
        feature = osm_feature({"shop": "mall", "name": "Metrocentro"}, point(12.1246, -86.2686))
        path.write_text("\x1e" + json.dumps(feature) + "\n", encoding="utf-8")
        assert [entry.name for entry in load_osm([path])] == ["Metrocentro"]


# --------------------------------------------------------------------------- #
# Merge precedence
# --------------------------------------------------------------------------- #


SEED_ROTONDA = Landmark(
    name="Rotonda Rubén Darío",
    kind="rotonda",
    lat=12.1281,
    lon=-86.2681,
    name_alt=["Rotonda Metrocentro"],
    city="Managua",
    popularity=0.98,
    source="seed",
    source_id="rotonda-ruben-dario-managua",
)
OSM_ROTONDA = Landmark(
    name="Rotonda Metrocentro",
    kind="referencia",
    lat=12.1285,
    lon=-86.2679,
    name_alt=["Rotonda Rubén Darío"],
    source="osm",
    source_id="way/7",
    osm_id="way/7",
)


class TestMergePrecedence:
    def test_seed_wins_the_name_and_kind_osm_wins_the_position(self):
        merged = merge_landmarks(SEED_ROTONDA, OSM_ROTONDA)
        assert merged.name == "Rotonda Rubén Darío"
        assert merged.kind == "rotonda"
        assert (merged.lat, merged.lon) == (OSM_ROTONDA.lat, OSM_ROTONDA.lon)
        assert merged.source == "seed"
        assert merged.source_id == "rotonda-ruben-dario-managua"
        assert merged.osm_id == "way/7"
        assert merged.popularity == 0.98

    def test_the_same_holds_with_the_arguments_reversed(self):
        forwards = merge_landmarks(SEED_ROTONDA, OSM_ROTONDA)
        backwards = merge_landmarks(OSM_ROTONDA, SEED_ROTONDA)
        assert (backwards.name, backwards.kind) == (forwards.name, forwards.kind)
        assert (backwards.lat, backwards.lon) == (forwards.lat, forwards.lon)

    def test_a_former_seed_row_keeps_its_own_position(self):
        # OSM cannot map a cinema the 1972 earthquake took down, so a nearby OSM
        # object with the same name is a coincidence, not better evidence.
        ghost = Landmark(
            name="Cine Cabrera",
            kind="edificio",
            lat=12.1543,
            lon=-86.2733,
            city="Managua",
            former=True,
            era="hasta 1972",
            note="cine del centro viejo",
            source="seed",
            source_id="cine-cabrera-managua",
        )
        impostor = Landmark(
            name="Cine Cabrera",
            kind="edificio",
            lat=12.1560,
            lon=-86.2750,
            source="osm",
            source_id="node/9",
            osm_id="node/9",
        )
        merged = merge_landmarks(ghost, impostor)
        assert (merged.lat, merged.lon) == (12.1543, -86.2733)
        assert merged.former is True
        assert merged.era == "hasta 1972"

    def test_alt_names_are_unioned_and_the_loser_name_is_kept(self):
        merged = merge_landmarks(SEED_ROTONDA, OSM_ROTONDA)
        alts = {normalize(alt) for alt in merged.name_alt}
        assert normalize("Rotonda Metrocentro") in alts
        assert normalize(merged.name) not in alts

    def test_a_user_alias_never_outranks_the_seed(self):
        alias = Landmark(
            name="La rotonda del Metro",
            kind="referencia",
            lat=12.1290,
            lon=-86.2670,
            source="user",
            source_id="user-la-rotonda-del-metro",
            popularity=0.3,
        )
        merged = merge_landmarks(SEED_ROTONDA, alias)
        assert merged.name == "Rotonda Rubén Darío"
        assert (merged.lat, merged.lon) == (SEED_ROTONDA.lat, SEED_ROTONDA.lon)
        assert "La rotonda del Metro" in merged.name_alt


# --------------------------------------------------------------------------- #
# De-duplication
# --------------------------------------------------------------------------- #


class TestBuildAndDeduplicate:
    def test_the_seed_and_its_osm_twin_become_one_row(self):
        entries, stats = build_gazetteer(seed=[SEED_ROTONDA], osm=[OSM_ROTONDA])
        assert len(entries) == 1
        assert stats.merged_duplicates == 1
        assert entries[0].name == "Rotonda Rubén Darío"
        assert (entries[0].lat, entries[0].lon) == (OSM_ROTONDA.lat, OSM_ROTONDA.lon)

    def test_order_does_not_change_the_result(self):
        first, _ = build_gazetteer(seed=[SEED_ROTONDA], osm=[OSM_ROTONDA])
        # Feed the same two records with the seed arriving second.
        second, _ = build_gazetteer(osm=[OSM_ROTONDA], aliases=[], seed=[SEED_ROTONDA])
        assert [(e.name, e.lat, e.lon) for e in first] == [(e.name, e.lat, e.lon) for e in second]

    def test_two_osm_copies_of_one_market_collapse(self):
        copies = [
            Landmark(name="Mercado Oriental", kind="mercado", lat=12.1500, lon=-86.2585),
            Landmark(
                name="Mercado Oriental",
                kind="mercado",
                lat=12.1503,
                lon=-86.2588,
                name_alt=["El Oriental"],
            ),
        ]
        entries, stats = build_gazetteer(osm=copies)
        assert len(entries) == 1
        assert stats.merged_duplicates == 1
        assert "El Oriental" in entries[0].name_alt

    def test_the_same_name_far_apart_stays_two_rows(self):
        far = [
            Landmark(name="Iglesia San Juan", kind="iglesia", lat=12.1500, lon=-86.2585),
            Landmark(name="Iglesia San Juan", kind="iglesia", lat=11.9299, lon=-85.9560),
        ]
        entries, stats = build_gazetteer(osm=far)
        assert len(entries) == 2
        assert stats.merged_duplicates == 0

    def test_the_same_name_in_two_cities_stays_two_rows(self):
        # 200 m apart on purpose: only the city guard can separate these.
        pair = [
            Landmark(
                name="Parque Central", kind="parque", lat=11.9299, lon=-85.9560, city="Granada"
            ),
            Landmark(
                name="Parque Central", kind="parque", lat=11.9301, lon=-85.9578, city="Masaya"
            ),
        ]
        entries, _ = build_gazetteer(seed=pair)
        assert len(entries) == 2

    def test_a_nickname_collision_across_kinds_does_not_merge(self):
        # "Linda Vista" is both a barrio and a mall 120 m apart.  They match only
        # through an alternative name, so the kind guard has to keep them apart.
        pair = [
            Landmark(
                name="Centro Comercial Linda Vista",
                kind="centro_comercial",
                lat=12.1400,
                lon=-86.2905,
                name_alt=["Linda Vista"],
                city="Managua",
            ),
            Landmark(
                name="Barrio Linda Vista",
                kind="barrio",
                lat=12.1395,
                lon=-86.2915,
                name_alt=["Linda Vista"],
                city="Managua",
            ),
        ]
        entries, stats = build_gazetteer(seed=pair)
        assert len(entries) == 2
        assert stats.merged_duplicates == 0

    def test_merge_radius_is_respected(self):
        pair = [
            Landmark(name="Rotonda X", kind="rotonda", lat=12.1500, lon=-86.2585),
            Landmark(name="Rotonda X", kind="rotonda", lat=12.1530, lon=-86.2585),
        ]
        assert haversine_m(12.1500, -86.2585, 12.1530, -86.2585) == pytest.approx(333, abs=15)
        assert len(build_gazetteer(osm=pair, merge_radius_m=200.0)[0]) == 2
        assert len(build_gazetteer(osm=pair, merge_radius_m=500.0)[0]) == 1

    def test_the_real_seed_has_no_self_collisions(self):
        entries, stats = build_gazetteer(seed=load_seed(DEFAULT_SEED_PATH))
        assert stats.merged_duplicates == 0
        assert len(entries) == stats.seed_rows

    def test_entries_come_back_most_useful_first(self):
        entries, _ = build_gazetteer(seed=load_seed(DEFAULT_SEED_PATH))
        popularity = [entry.resolved_popularity() for entry in entries]
        assert popularity == sorted(popularity, reverse=True)


class TestAliases:
    def test_an_alias_on_a_known_landmark_becomes_an_alt_name(self, tmp_path: Path):
        path = tmp_path / "aliases.csv"
        path.write_text(
            "text,lat,lon,city\nRotonda del Metrocentro,12.1283,-86.2680,Managua\n",
            encoding="utf-8",
        )
        aliases = load_aliases(path)
        assert len(aliases) == 1
        entries, _ = build_gazetteer(seed=[SEED_ROTONDA], aliases=aliases)
        assert len(entries) == 1
        # The alias blocks on the same name key as the curated row's nickname,
        # so it folds in as another spelling instead of becoming a rival pin.
        assert "Rotonda del Metrocentro" in entries[0].name_alt
        assert entries[0].name == "Rotonda Rubén Darío"

    def test_an_alias_nobody_knows_becomes_its_own_row(self, tmp_path: Path):
        path = tmp_path / "aliases.geojsonl"
        write_geojsonseq(
            path,
            [
                {
                    "type": "Feature",
                    "properties": {"text": "Donde fue la venta de Doña Chepa", "city": "Managua"},
                    "geometry": point(12.0800, -86.3000),
                }
            ],
        )
        aliases = load_aliases(path)
        entries, _ = build_gazetteer(seed=[SEED_ROTONDA], aliases=aliases)
        assert len(entries) == 2
        new = next(entry for entry in entries if entry.source == "user")
        assert new.kind == "referencia"
        assert new.resolved_popularity() == pytest.approx(0.3)

    def test_an_alias_without_a_coordinate_is_skipped(self, tmp_path: Path):
        path = tmp_path / "aliases.csv"
        path.write_text("text,lat,lon\nSin punto,,\n", encoding="utf-8")
        assert load_aliases(path) == []


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #


class TestExport:
    def test_writes_valid_geojson_in_lon_lat_order(self, tmp_path: Path):
        out = tmp_path / "gazetteer.geojson"
        entries, _ = build_gazetteer(seed=[SEED_ROTONDA])
        export_geojson(entries, out)

        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["type"] == "FeatureCollection"
        assert len(payload["features"]) == 1
        feature = payload["features"][0]
        assert feature["type"] == "Feature"
        lon, lat = feature["geometry"]["coordinates"]
        assert lon == pytest.approx(-86.2681)
        assert lat == pytest.approx(12.1281)
        assert lon < 0 < lat  # GeoJSON is [lon, lat]; Nicaragua is (12, -86)

        properties = feature["properties"]
        assert properties["name"] == "Rotonda Rubén Darío"
        assert properties["kind"] == "rotonda"
        assert properties["former"] is False
        assert 0.0 <= properties["popularity"] <= 1.0
        assert properties["source"] == "seed"

    def test_the_whole_seed_round_trips(self, tmp_path: Path):
        out = tmp_path / "gazetteer.geojson"
        entries, _ = build_gazetteer(seed=load_seed(DEFAULT_SEED_PATH))
        export_geojson(entries, out)
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert len(payload["features"]) == len(entries)
        for feature in payload["features"]:
            lon, lat = feature["geometry"]["coordinates"]
            assert distance_from_mga_m(lat, lon) <= CURATION_RADIUS_M
            assert feature["properties"]["kind"] in GAZETTEER_KINDS

    def test_no_temporary_file_is_left_behind(self, tmp_path: Path):
        out = tmp_path / "gazetteer.geojson"
        entries, _ = build_gazetteer(seed=[SEED_ROTONDA])
        export_geojson(entries, out)
        assert not (tmp_path / "gazetteer.geojson.tmp").exists()


class TestSummary:
    def test_it_reports_counts_by_kind_and_ghosts(self):
        entries, stats = build_gazetteer(seed=load_seed(DEFAULT_SEED_PATH))
        text = summarize(entries, stats)
        assert f"{len(entries)} landmarks" in text
        assert "former" in text
        assert "rotonda" in text
        assert "merged as duplicates: 0" in text


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


class TestCli:
    def test_end_to_end_seed_plus_osm(self, tmp_path: Path):
        osm_path = write_geojsonseq(
            tmp_path / "osm.geojsonl",
            [
                osm_feature(
                    {"junction": "roundabout", "name": "Rotonda Metrocentro"},
                    point(12.1285, -86.2679),
                    osm_id="way/7",
                ),
                osm_feature(
                    {"amenity": "school", "name": "Escuela Nueva"},
                    point(12.1000, -86.2000),
                    osm_id="node/8",
                ),
            ],
        )
        out = tmp_path / "gazetteer.geojson"
        code = main(
            [
                "--seed",
                str(DEFAULT_SEED_PATH),
                "--osm",
                str(osm_path),
                "--out",
                str(out),
                "--log-level",
                "WARNING",
            ]
        )
        assert code == 0
        payload = json.loads(out.read_text(encoding="utf-8"))
        names = {feature["properties"]["name"] for feature in payload["features"]}
        assert "Escuela Nueva" in names
        # The OSM roundabout folded into the curated row rather than doubling it.
        assert "Rotonda Metrocentro" not in names
        assert "Rotonda Rubén Darío" in names
        rotonda = next(
            feature
            for feature in payload["features"]
            if feature["properties"]["name"] == "Rotonda Rubén Darío"
        )
        assert rotonda["geometry"]["coordinates"] == [-86.2679, 12.1285]

    def test_missing_input_exits_non_zero(self, tmp_path: Path):
        assert (
            main(["--seed", str(tmp_path / "nope.csv"), "--out", str(tmp_path / "o.geojson")]) == 2
        )

    def test_no_sources_at_all_exits_non_zero(self, tmp_path: Path):
        assert main(["--seed", "", "--out", str(tmp_path / "o.geojson")]) == 2
