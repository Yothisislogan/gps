"""Tests for POI conflation: blocking, scoring, the three decision bands and field precedence.

Everything here is pure — no database, no network.  The awkward cases are the Nicaraguan ones:
chain gas stations 60 m apart on opposite sides of a highway, the same pharmacy brand repeated
across Managua, and one business spelled three different ways by three sources.
"""

from __future__ import annotations

import random
from dataclasses import replace
from typing import Any

import pytest

from common.geo import destination_point, haversine_m
from pipeline.pois import conflate as conf
from pipeline.pois.conflate import (
    AUTO_MERGE_SCORE,
    BLOCK_RADIUS_M,
    NAME_ONLY_MAX_MERGE_DISTANCE_M,
    REVIEW_SCORE,
    ConflationResult,
    Decision,
    MergedPoi,
    PoiRecord,
    candidate_pairs,
    chain_keys_for,
    conflate,
    decide,
    merge_records,
    score_pair,
)

MANAGUA = (12.1300, -86.2500)


def rec(**kwargs: Any) -> PoiRecord:
    """Terse record builder with sane defaults for the fields a test does not care about."""
    base: dict[str, Any] = {
        "source": "osm",
        "source_id": "n1",
        "name": "Sin nombre",
        "lat": MANAGUA[0],
        "lon": MANAGUA[1],
        "category": "restaurante",
    }
    base.update(kwargs)
    return PoiRecord.from_mapping(base)


def offset(lat: float, lon: float, metres: float, bearing_deg: float = 90.0) -> tuple[float, float]:
    """A point ``metres`` away — used to build exact-distance fixtures."""
    return destination_point(lat, lon, bearing_deg, metres)


def pair_score(distance_m: float, **kwargs: Any) -> conf.MatchScore:
    """Score two otherwise-identical records placed ``distance_m`` apart."""
    left = rec(source="osm", source_id="n1", **kwargs)
    lat, lon = offset(left.lat, left.lon, distance_m)
    right = rec(source="overture", source_id="o1", lat=lat, lon=lon, **kwargs)
    return score_pair(left, right)


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #


def test_record_key_matches_the_match_queue_format() -> None:
    assert rec(source="overture", source_id="08f2ab").key == "overture:08f2ab"


def test_from_mapping_ignores_unknown_keys_and_coerces_lists() -> None:
    record = PoiRecord.from_mapping(
        {
            "source": "osm",
            "source_id": 12345,
            "name": "Pulpería La Esquina",
            "lat": "12.13",
            "lon": "-86.25",
            "name_alt": ["La Esquina"],
            "cuisine": None,
            "raw": {"anything": "at all"},
        }
    )
    assert record.source_id == "12345"
    assert record.lat == pytest.approx(12.13)
    assert record.name_alt == ("La Esquina",)
    assert record.cuisine == ()


@pytest.mark.parametrize("missing", ["source", "source_id", "name", "lat", "lon"])
def test_from_mapping_requires_the_core_fields(missing: str) -> None:
    data: dict[str, Any] = {
        "source": "osm",
        "source_id": "n1",
        "name": "X",
        "lat": 12.0,
        "lon": -86.0,
    }
    data.pop(missing)
    with pytest.raises(ValueError, match=missing):
        PoiRecord.from_mapping(data)


# --------------------------------------------------------------------------- #
# Blocking
# --------------------------------------------------------------------------- #


def test_point_just_inside_the_radius_is_a_candidate() -> None:
    left = rec(source_id="a")
    lat, lon = offset(left.lat, left.lon, 74.0)
    right = rec(source="overture", source_id="b", lat=lat, lon=lon)
    assert list(candidate_pairs([left, right])) == [(0, 1)]


def test_point_just_outside_the_radius_is_not_a_candidate() -> None:
    left = rec(source_id="a")
    lat, lon = offset(left.lat, left.lon, 76.0)
    right = rec(source="overture", source_id="b", lat=lat, lon=lon)
    assert list(candidate_pairs([left, right])) == []


def test_blocking_works_across_bucket_boundaries_in_every_direction() -> None:
    # A 74 m neighbour must be found whatever the bearing, including the diagonals where a
    # naive single-cell lookup would miss it.
    left = rec(source_id="a")
    for bearing in range(0, 360, 15):
        lat, lon = offset(left.lat, left.lon, 74.0, bearing)
        right = rec(source="overture", source_id="b", lat=lat, lon=lon)
        assert list(candidate_pairs([left, right])) == [(0, 1)], bearing


def test_blocking_matches_a_brute_force_scan() -> None:
    rng = random.Random(20260904)
    records = [
        rec(
            source="osm" if index % 2 else "overture",
            source_id=str(index),
            lat=MANAGUA[0] + rng.uniform(-0.002, 0.002),
            lon=MANAGUA[1] + rng.uniform(-0.002, 0.002),
        )
        for index in range(250)
    ]
    grid = set(candidate_pairs(records))
    brute = {
        (i, j)
        for i in range(len(records))
        for j in range(i + 1, len(records))
        if haversine_m(records[i].lat, records[i].lon, records[j].lat, records[j].lon)
        <= BLOCK_RADIUS_M
    }
    assert grid == brute
    assert brute, "fixture should produce some real candidate pairs"


def test_blocking_yields_each_pair_once_and_ordered() -> None:
    records = [rec(source_id=str(index)) for index in range(4)]  # all at the same point
    pairs = list(candidate_pairs(records))
    assert pairs == sorted(set(pairs))
    assert all(i < j for i, j in pairs)
    assert len(pairs) == 6


def test_blocking_handles_degenerate_inputs() -> None:
    assert list(candidate_pairs([])) == []
    assert list(candidate_pairs([rec()])) == []


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #


def test_score_is_bounded_and_components_are_reported() -> None:
    score = pair_score(10.0, name="Restaurante El Zaguán")
    assert 0.0 <= score.score <= 1.0
    assert score.name_score == pytest.approx(1.0)
    assert score.distance_m == pytest.approx(10.0, abs=0.5)
    assert set(score.as_dict()) >= {"score", "name_score", "distance_score", "category_score"}


def test_score_decreases_monotonically_with_distance() -> None:
    scores = [pair_score(float(d), name="Hotel Camino Real").score for d in range(0, 76, 5)]
    assert scores == sorted(scores, reverse=True)
    assert scores[0] > scores[-1]


def test_score_increases_with_name_similarity() -> None:
    def at(name_a: str, name_b: str) -> float:
        left = rec(source="osm", source_id="a", name=name_a)
        lat, lon = offset(left.lat, left.lon, 10.0)
        right = rec(source="overture", source_id="b", name=name_b, lat=lat, lon=lon)
        return score_pair(left, right).score

    identical = at("Ferretería Jenny", "Ferretería Jenny")
    close = at("Ferretería Jenny", "Ferreteria Jeny")
    unrelated = at("Ferretería Jenny", "Panadería Norma")
    assert identical > close > unrelated


def test_name_matching_ignores_accents_case_and_generic_prefixes() -> None:
    left = rec(source="osm", source_id="a", name="Cafetín El Güegüense")
    right = rec(source="overture", source_id="b", name="EL GUEGUENSE", lat=left.lat, lon=left.lon)
    assert score_pair(left, right).name_score == pytest.approx(1.0)


def test_word_order_does_not_matter_but_an_extra_word_costs() -> None:
    def name_score(a: str, b: str) -> float:
        left = rec(source="osm", source_id="a", name=a)
        right = rec(source="overture", source_id="b", name=b, lat=left.lat, lon=left.lon)
        return score_pair(left, right).name_score

    assert name_score("Restaurante El Zaguán", "El Zaguán Restaurante") == pytest.approx(1.0)
    # A branch suffix must not score as a perfect match, or every Farmacia Xolotlán merges.
    assert name_score("Farmacia Xolotlán", "Farmacia Xolotlán Bello Horizonte") < 0.9


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("restaurante", "restaurante", 1.0),
        ("restaurante", "fritanga", 0.6),  # same group: comida
        ("restaurante", "gasolinera", 0.0),
        ("restaurante", None, 0.5),
        (None, None, 0.5),
        ("restaurante", "otro", 0.5),
        ("restaurante", "no_existe_en_el_csv", 0.5),
    ],
)
def test_category_agreement(left: str | None, right: str | None, expected: float) -> None:
    a = rec(source="osm", source_id="a", name="X", category=left)
    b = rec(source="overture", source_id="b", name="X", category=right, lat=a.lat, lon=a.lon)
    assert score_pair(a, b).category_score == pytest.approx(expected)


# --------------------------------------------------------------------------- #
# Decision bands
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1.0, Decision.MERGE),
        (AUTO_MERGE_SCORE, Decision.MERGE),
        (AUTO_MERGE_SCORE - 0.001, Decision.REVIEW),
        (0.70, Decision.REVIEW),
        (REVIEW_SCORE, Decision.REVIEW),
        (REVIEW_SCORE - 0.001, Decision.SEPARATE),
        (0.0, Decision.SEPARATE),
    ],
)
def test_decision_bands(value: float, expected: Decision) -> None:
    assert decide(value) is expected


def test_bands_end_to_end_on_realistic_pairs() -> None:
    same_place = pair_score(12.0, name="Hotel Los Robles")
    assert decide(same_place) is Decision.MERGE

    maybe = score_pair(
        rec(source="osm", source_id="a", name="Comedor Doña María", category="comedor"),
        rec(
            source="overture",
            source_id="b",
            name="Comedor Doña Mariana",
            category="comedor",
            lat=offset(*MANAGUA, 35.0)[0],
            lon=offset(*MANAGUA, 35.0)[1],
        ),
    )
    assert decide(maybe) is Decision.REVIEW

    different = score_pair(
        rec(source="osm", source_id="a", name="Fritanga La Fe", category="fritanga"),
        rec(
            source="overture",
            source_id="b",
            name="Pulpería La Esquina",
            category="pulperia",
            lat=offset(*MANAGUA, 11.0)[0],
            lon=offset(*MANAGUA, 11.0)[1],
        ),
    )
    assert decide(different) is Decision.SEPARATE


# --------------------------------------------------------------------------- #
# The chain-store trap
# --------------------------------------------------------------------------- #


def puma(source: str, source_id: str, lat: float, lon: float, name: str) -> PoiRecord:
    return rec(
        source=source,
        source_id=source_id,
        name=name,
        category="gasolinera",
        brand="Puma",
        lat=lat,
        lon=lon,
    )


def test_two_puma_stations_across_the_highway_are_not_the_same_poi() -> None:
    # 60 m apart, identical brand, identical category: the naive score says merge.
    left = puma("osm", "n1", *MANAGUA, name="Gasolinera Puma")
    lat, lon = offset(left.lat, left.lon, 60.0)
    right = puma("osm", "n2", lat, lon, name="Gasolinera Puma")
    score = score_pair(left, right, chain_keys=chain_keys_for([left, right]))
    assert score.chain is True
    assert score.capped_by == "chain_distance_guard"
    assert decide(score) is Decision.SEPARATE


def test_the_same_puma_station_from_two_sources_still_merges() -> None:
    left = puma("osm", "n1", *MANAGUA, name="Gasolinera Puma Bello Horizonte")
    lat, lon = offset(left.lat, left.lon, 8.0)
    right = puma("overture", "o1", lat, lon, name="Puma Bello Horizonte")
    assert (
        decide(score_pair(left, right, chain_keys=chain_keys_for([left, right]))) is Decision.MERGE
    )


def test_farmacia_xolotlan_branches_stay_separate() -> None:
    # The same brand appears dozens of times across Managua; two nearby branches are two POIs.
    records = [
        rec(
            source="overture",
            source_id=f"o{index}",
            name="Farmacia Xolotlán",
            category="farmacia",
            lat=MANAGUA[0] + 0.01 * index,
            lon=MANAGUA[1],
        )
        for index in range(4)
    ]
    keys = chain_keys_for(records)
    assert "xolotlan" in keys

    left = records[0]
    lat, lon = offset(left.lat, left.lon, 65.0)
    right = rec(
        source="overture",
        source_id="o9",
        name="FARMACIA XOLOTLAN",
        category="farmacia",
        lat=lat,
        lon=lon,
    )
    assert decide(score_pair(left, right, chain_keys=keys)) is Decision.SEPARATE


def test_a_brand_seen_twice_is_not_treated_as_a_chain() -> None:
    # One unique business contributed by two sources must not look like a chain.
    left = rec(source="osm", source_id="n1", name="Restaurante El Zaguán")
    right = rec(source="overture", source_id="o1", name="El Zaguan", lat=left.lat, lon=left.lon)
    assert chain_keys_for([left, right]) == conf.KNOWN_CHAIN_KEYS
    assert score_pair(left, right, chain_keys=chain_keys_for([left, right])).chain is False


def test_no_pair_auto_merges_on_name_alone_across_a_large_distance() -> None:
    for distance in range(int(NAME_ONLY_MAX_MERGE_DISTANCE_M) + 1, int(BLOCK_RADIUS_M) + 1):
        score = pair_score(float(distance), name="Hotel Camino Real")
        assert decide(score) is not Decision.MERGE, distance


def test_distance_cap_fires_when_the_decay_would_otherwise_allow_a_merge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Flatten the decay so the raw score clears 0.85 at 60 m; the guard must still bite.
    monkeypatch.setattr(conf, "DISTANCE_DECAY_M", 100_000.0)
    score = pair_score(60.0, name="Hotel Camino Real")
    assert score.capped_by == "name_only_distance"
    assert decide(score) is Decision.REVIEW


def test_two_rows_from_the_same_source_never_auto_merge() -> None:
    left = rec(source="osm", source_id="n1", name="Pulpería Doña Chepa", category="pulperia")
    right = rec(
        source="osm",
        source_id="n2",
        name="Pulpería Doña Chepa",
        category="pulperia",
        lat=left.lat,
        lon=left.lon,
    )
    score = score_pair(left, right)
    assert score.same_source is True
    assert score.capped_by == "same_source"
    assert decide(score) is Decision.REVIEW


# --------------------------------------------------------------------------- #
# Merge precedence
# --------------------------------------------------------------------------- #


def osm_row() -> PoiRecord:
    return rec(
        source="osm",
        source_id="node/1",
        name="Restaurante El Zaguán",
        category="restaurante",
        lat=11.9302,
        lon=-85.9553,
        address_text="Calle El Arsenal, Granada",
        phone="+50522222222",
        opening_hours="Mo-Su 11:00-22:00",
        website="https://osm.example/zaguan",
    )


def overture_row() -> PoiRecord:
    return rec(
        source="overture",
        source_id="08f2ab",
        gers_id="08f2ab00000000",
        name="EL ZAGUAN",
        category="restaurante",
        lat=11.9310,
        lon=-85.9560,
        phone="+50525522522",
        facebook="https://facebook.com/elzaguan",
        instagram="https://instagram.com/elzaguan",
        opening_hours="Mo-Sa 12:00-23:00",
        website="https://overture.example/zaguan",
        price_level=2,
        status="open",
    )


@pytest.mark.parametrize("reverse", [False, True])
def test_field_precedence_osm_position_overture_contacts(reverse: bool) -> None:
    # Asserted both ways round: the outcome must depend on the precedence table, not on the
    # order the cluster happened to be assembled in.
    members = [osm_row(), overture_row()]
    merged = merge_records(list(reversed(members)) if reverse else members)

    assert (merged.lat, merged.lon) == (11.9302, -85.9553)  # OSM points are hand-placed
    assert merged.field_sources["position"] == "osm"
    assert merged.name == "Restaurante El Zaguán"
    assert merged.field_sources["name"] == "osm"
    assert merged.address_text == "Calle El Arsenal, Granada"

    assert merged.phone == "+50525522522"  # Overture wins phone/socials/hours
    assert merged.field_sources["phone"] == "overture"
    assert merged.facebook == "https://facebook.com/elzaguan"
    assert merged.opening_hours == "Mo-Sa 12:00-23:00"
    assert merged.website == "https://overture.example/zaguan"
    assert merged.price_level == 2
    assert merged.status == "open"


def test_survey_wins_every_field() -> None:
    survey = rec(
        source="survey",
        source_id="sv-1",
        name="El Zaguán",
        category="restaurante",
        lat=11.9300,
        lon=-85.9550,
        phone="+50588880000",
        opening_hours="Mo-Su 11:30-22:30",
        status="open",
    )
    merged = merge_records([osm_row(), overture_row(), survey])
    assert (merged.lat, merged.lon) == (11.9300, -85.9550)
    assert merged.name == "El Zaguán"
    assert merged.phone == "+50588880000"
    assert merged.opening_hours == "Mo-Su 11:30-22:30"
    assert set(merged.field_sources.values()) >= {"survey"}
    assert merged.field_sources["position"] == "survey"
    assert merged.field_sources["phone"] == "survey"


def test_unverified_status_is_treated_as_no_opinion() -> None:
    # The schema default must never overwrite a source that actually knows.
    osm = rec(source="osm", source_id="n1", name="X", status="unverified")
    overture = rec(source="overture", source_id="o1", name="X", status="closed")
    assert merge_records([osm, overture]).status == "closed"


def test_merge_keeps_the_overture_gers_id_for_the_monthly_refresh() -> None:
    merged = merge_records([osm_row(), overture_row()])
    assert merged.sources == {"osm_id": "node/1", "overture_gers_id": "08f2ab00000000"}
    assert merged.gers_id == "08f2ab00000000"
    assert merged.source_keys == ("osm:node/1", "overture:08f2ab")


def test_merge_collects_alternate_spellings_without_duplicating_the_chosen_name() -> None:
    merged = merge_records([osm_row(), overture_row()])
    assert merged.name == "Restaurante El Zaguán"
    assert "EL ZAGUAN" in merged.name_alt
    assert merged.name not in merged.name_alt


def test_merge_is_deterministic_regardless_of_member_order() -> None:
    members = [osm_row(), overture_row()]
    first = merge_records(members)
    second = merge_records(list(reversed(members)))
    assert first == second


def test_merge_of_a_single_record_is_that_record() -> None:
    merged = merge_records([osm_row()])
    assert merged.key == "osm:node/1"
    assert merged.name == "Restaurante El Zaguán"
    assert merged.sources == {"osm_id": "node/1"}


def test_merge_requires_at_least_one_record() -> None:
    with pytest.raises(ValueError, match="at least one"):
        merge_records([])


def test_merged_confidence_rises_with_independent_agreement() -> None:
    single = merge_records([osm_row()])
    both = merge_records([osm_row(), overture_row()])
    assert both.confidence > single.confidence


def test_merged_feature_uses_geojson_lon_lat_order() -> None:
    feature = merge_records([osm_row()]).as_feature()
    assert feature["geometry"]["coordinates"] == [-85.9553, 11.9302]
    assert feature["properties"]["category"] == "restaurante"


def test_merged_category_falls_back_to_otro() -> None:
    merged = merge_records([rec(source="osm", source_id="n1", name="X", category=None)])
    assert merged.category == "otro"


# --------------------------------------------------------------------------- #
# End to end
# --------------------------------------------------------------------------- #


def synthetic_corpus() -> list[PoiRecord]:
    """Twelve records covering every outcome the conflator has to produce."""
    puma_lat, puma_lon = 12.1200, -86.2200
    puma_b = offset(puma_lat, puma_lon, 5.0)
    puma_far = offset(puma_lat, puma_lon, 60.0, bearing_deg=0.0)

    farm_lat, farm_lon = 12.1300, -86.2500
    farm_survey = offset(farm_lat, farm_lon, 6.0)
    farm_other = offset(farm_lat, farm_lon, 60.0, bearing_deg=180.0)

    comedor_lat, comedor_lon = 12.1400, -86.2400
    comedor_b = offset(comedor_lat, comedor_lon, 35.0)

    barrio_lat, barrio_lon = 12.1000, -86.3000
    pulperia = offset(barrio_lat, barrio_lon, 11.0)

    return [
        rec(
            source="osm",
            source_id="node/1",
            name="Restaurante El Zaguán",
            category="restaurante",
            lat=11.9302,
            lon=-85.9553,
            city="Granada",
        ),
        rec(
            source="overture",
            source_id="ov-zaguan",
            gers_id="08f2ab",
            name="El Zaguan",
            category="restaurante",
            lat=11.9303,
            lon=-85.9552,
            phone="+50525522522",
            facebook="https://facebook.com/elzaguan",
        ),
        rec(
            source="osm",
            source_id="node/2",
            name="Gasolinera Puma Bello Horizonte",
            category="gasolinera",
            brand="Puma",
            lat=puma_lat,
            lon=puma_lon,
        ),
        rec(
            source="overture",
            source_id="ov-puma-a",
            name="Puma Bello Horizonte",
            category="gasolinera",
            brand="Puma",
            lat=puma_b[0],
            lon=puma_b[1],
        ),
        rec(
            source="osm",
            source_id="node/3",
            name="Gasolinera Puma Villa Fontana",
            category="gasolinera",
            brand="Puma",
            lat=puma_far[0],
            lon=puma_far[1],
        ),
        rec(
            source="overture",
            source_id="ov-farm-a",
            name="Farmacia Xolotlán",
            category="farmacia",
            lat=farm_lat,
            lon=farm_lon,
        ),
        rec(
            source="survey",
            source_id="sv-farm",
            name="Farmacia Xolotlán",
            category="farmacia",
            lat=farm_survey[0],
            lon=farm_survey[1],
            phone="+50522334455",
            opening_hours="Mo-Sa 08:00-20:00",
            status="open",
        ),
        rec(
            source="overture",
            source_id="ov-farm-b",
            name="Farmacia Xolotlán",
            category="farmacia",
            lat=farm_other[0],
            lon=farm_other[1],
        ),
        rec(
            source="osm",
            source_id="node/6",
            name="Comedor Doña María",
            category="comedor",
            lat=comedor_lat,
            lon=comedor_lon,
        ),
        rec(
            source="overture",
            source_id="ov-comedor",
            name="Comedor Doña Mariana",
            category="comedor",
            lat=comedor_b[0],
            lon=comedor_b[1],
        ),
        rec(
            source="osm",
            source_id="node/4",
            name="Fritanga La Fe",
            category="fritanga",
            lat=barrio_lat,
            lon=barrio_lon,
        ),
        rec(
            source="osm",
            source_id="node/5",
            name="Pulpería La Esquina",
            category="pulperia",
            lat=pulperia[0],
            lon=pulperia[1],
        ),
    ]


EXPECTED_CLUSTERS = {
    frozenset({"osm:node/1", "overture:ov-zaguan"}),  # same restaurant, two sources
    frozenset({"osm:node/2", "overture:ov-puma-a"}),  # same Puma forecourt, two sources
    frozenset({"osm:node/3"}),  # Puma across the road: separate
    frozenset({"overture:ov-farm-a", "survey:sv-farm"}),  # survey confirms the Overture row
    frozenset({"overture:ov-farm-b"}),  # another Xolotlán branch
    frozenset({"osm:node/6"}),  # Doña María / Doña Mariana: queued
    frozenset({"overture:ov-comedor"}),
    frozenset({"osm:node/4"}),  # fritanga next to a pulpería
    frozenset({"osm:node/5"}),
}


def test_conflate_end_to_end_produces_the_expected_merges() -> None:
    result = conflate(synthetic_corpus())
    assert isinstance(result, ConflationResult)
    assert {frozenset(poi.source_keys) for poi in result.merged} == EXPECTED_CLUSTERS
    assert result.stats["input_records"] == 12
    assert result.stats["merged_pois"] == 9
    assert result.stats["auto_merged_pairs"] == 3
    assert result.stats["review_pairs"] == 1
    assert result.stats["clusters_with_multiple_sources"] == 3


def test_conflate_end_to_end_merges_the_right_fields() -> None:
    merged = {poi.key: poi for poi in conflate(synthetic_corpus()).merged}

    zaguan = merged["osm:node/1"]
    assert zaguan.name == "Restaurante El Zaguán"
    assert zaguan.phone == "+50525522522"
    assert zaguan.city == "Granada"
    assert zaguan.gers_id == "08f2ab"
    assert (zaguan.lat, zaguan.lon) == (11.9302, -85.9553)

    farmacia = merged["survey:sv-farm"]
    assert farmacia.field_sources["position"] == "survey"
    assert farmacia.phone == "+50522334455"
    assert farmacia.status == "open"
    assert farmacia.opening_hours == "Mo-Sa 08:00-20:00"


def test_conflate_queues_only_the_uncertain_pair() -> None:
    result = conflate(synthetic_corpus())
    assert len(result.review) == 1
    queued = result.review[0]
    assert {queued.left.key, queued.right.key} == {"osm:node/6", "overture:ov-comedor"}
    row = queued.as_queue_row()
    assert row["decision"] == "pending"
    assert REVIEW_SCORE <= row["score"] < AUTO_MERGE_SCORE
    assert "nombre" in row["explanation_es"] and "distancia" in row["explanation_es"]


def test_conflate_is_order_independent() -> None:
    corpus = synthetic_corpus()
    shuffled = corpus[:]
    random.Random(7).shuffle(shuffled)
    first = conflate(corpus)
    second = conflate(shuffled)
    assert [poi.as_feature() for poi in first.merged] == [poi.as_feature() for poi in second.merged]
    assert first.stats == second.stats


def test_conflate_accepts_plain_dicts(sample_pois: list[dict[str, Any]]) -> None:
    # The shared fixture is the shape the DB loader hands over.
    result = conflate(sample_pois)
    assert result.stats["merged_pois"] == 2
    zaguan = next(poi for poi in result.merged if "Zagu" in poi.name)
    assert zaguan.phone == "+50525522522"
    assert zaguan.facebook == "https://facebook.com/elzaguan"
    assert isinstance(zaguan, MergedPoi)


def test_conflate_of_an_empty_corpus_is_empty() -> None:
    result = conflate([])
    assert result.merged == ()
    assert result.stats["input_records"] == 0


def test_chain_guard_explanation_is_spanish() -> None:
    left = puma("osm", "n1", *MANAGUA, name="Gasolinera Puma")
    lat, lon = offset(left.lat, left.lon, 60.0)
    right = puma("overture", "o1", lat, lon, name="Puma")
    explanation = score_pair(left, right, chain_keys=chain_keys_for([left, right])).explain_es()
    assert "cadena" in explanation
    assert "sucursales distintas" in explanation


# --------------------------------------------------------------------------- #
# CLI (filesystem only: no database, no network, no external binary)
# --------------------------------------------------------------------------- #


def write_geojsonseq_fixture(
    path: Any, records: list[PoiRecord], *, rs_prefix: bool = False
) -> None:
    """Write records as the GeoJSON-seq the fetch jobs produce."""
    import json

    lines = []
    for record in records:
        feature = {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [record.lon, record.lat]},
            "properties": {
                "source": record.source,
                "source_id": record.source_id,
                "name": record.name,
                "category": record.category,
                "phone": record.phone,
                "gers_id": record.gers_id,
            },
        }
        prefix = "\x1e" if rs_prefix else ""
        lines.append(prefix + json.dumps(feature, ensure_ascii=False))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_cli_conflates_two_geojsonseq_inputs(tmp_path: Any) -> None:
    import json

    osm_path = tmp_path / "osm.geojsonl"
    overture_path = tmp_path / "overture.geojsonl"
    osm = osm_row()
    # Move the Overture row inside the blocking radius; osm_row/overture_row sit ~120 m apart
    # so the precedence tests exercise merge_records without the blocker in the way.
    nearby = offset(osm.lat, osm.lon, 12.0)
    overture = replace(overture_row(), lat=nearby[0], lon=nearby[1])
    write_geojsonseq_fixture(osm_path, [osm])
    # osmium emits RFC 8142 records with a record-separator byte; the reader must cope.
    write_geojsonseq_fixture(overture_path, [overture], rs_prefix=True)

    output = tmp_path / "pois.geojsonl"
    queue = tmp_path / "queue.json"
    exit_code = conf.main(
        [
            "--input",
            str(osm_path),
            "--input",
            str(overture_path),
            "--output",
            str(output),
            "--queue",
            str(queue),
            "--log-level",
            "WARNING",
        ]
    )
    assert exit_code == 0

    features = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert len(features) == 1
    assert features[0]["geometry"]["coordinates"] == [-85.9553, 11.9302]
    assert features[0]["properties"]["phone"] == "+50525522522"
    assert json.loads(queue.read_text(encoding="utf-8"))["stats"]["input_records"] == 2


def test_cli_returns_non_zero_when_an_input_is_missing(tmp_path: Any) -> None:
    assert (
        conf.main(["--input", str(tmp_path / "nope.geojsonl"), "--output", str(tmp_path / "o")])
        == 1
    )


def test_cli_help_builds() -> None:
    assert conf.build_parser().format_help()


class TestHumanDecisions:
    def test_explicit_merge_outside_blocking_radius(self):
        a = rec(source_id="a", name="Cafe")
        b = rec(source="overture", source_id="b", name="Other", lat=13.0)
        result = conflate(
            [a, b], decisions=[{"left_key": a.key, "right_key": b.key, "decision": "merge"}]
        )
        assert len(result.merged) == 1
        assert set(result.merged[0].source_keys) == {a.key, b.key}

    def test_separation_blocks_transitive_auto_merge(self):
        records = [rec(source_id=str(i), name="Cafe", phone="+50588888888") for i in range(3)]
        result = conflate(
            records,
            decisions=[
                {"left_key": records[0].key, "right_key": records[2].key, "decision": "separate"}
            ],
        )
        assert all(
            not {records[0].key, records[2].key} <= set(p.source_keys) for p in result.merged
        )

    def test_conflicting_human_edges_fail_instead_of_overriding_a_separation(self):
        records = [rec(source_id=str(i)) for i in range(3)]
        decisions = [
            {"left_key": records[a].key, "right_key": records[b].key, "decision": d}
            for a, b, d in [(0, 1, "merge"), (1, 2, "merge"), (0, 2, "separate")]
        ]
        with pytest.raises(ValueError, match="conflict"):
            conflate(records, decisions=decisions)

    def test_decision_survives_changed_upstream_attributes_and_input_order(self):
        a = rec(source_id="a", name="Cafe")
        b = rec(source="overture", source_id="b", name="Cafe", phone="+50588888888")
        decisions = [{"left_key": a.key, "right_key": b.key, "decision": "separate"}]
        for records in ([a, b], [replace(b, name="Cafe nuevo"), a]):
            assert len(conflate(records, decisions=decisions).merged) == 2
