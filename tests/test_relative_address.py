"""Corpus tests for the Nicaraguan relative-address parser.

The strings here are written the way Nicaraguans actually write them — mixed
abbreviations, missing accents, inconsistent commas, fractions in words — because
a parser that only handles tidy input solves nothing.
"""

from __future__ import annotations

import math

import pytest

from common.geo import VARA_M, destination_point, haversine_m
from common.models import GeocodeMethod
from pipeline.geocode.relative_address import (
    LandmarkMatch,
    looks_like_relative_address,
    parse,
    resolve,
    unit_to_metres,
)

ROTONDA_GUEGUENSE = LandmarkMatch(
    id="gz-1", name="Rotonda El Güegüense", lat=12.1352, lon=-86.2807, score=0.95, city="Managua"
)
CINE_CABRERA = LandmarkMatch(
    id="gz-2", name="Cine Cabrera", lat=12.1543, lon=-86.2733, score=0.8, former=True
)


def lookup_one(match: LandmarkMatch):
    def _lookup(query: str, former: bool):
        return [match]

    return _lookup


def no_landmarks(query: str, former: bool):
    return []


class TestUnits:
    def test_cuadra_is_a_block(self):
        # 100 varas of colonial platting, ~84 m — not a round 100 m.
        assert unit_to_metres(2, "cuadra") == 168.0
        assert unit_to_metres(0.5, "cuadra", city="Granada") == 42.0

    def test_vara(self):
        assert unit_to_metres(75, "vara") == pytest.approx(75 * VARA_M)
        assert unit_to_metres(75, "vara") == pytest.approx(62.7, abs=0.1)

    def test_metre_and_kilometre(self):
        assert unit_to_metres(30, "metro") == 30.0
        assert unit_to_metres(1.5, "kilometro") == 1500.0

    def test_unknown_unit_raises(self):
        with pytest.raises(ValueError, match="unknown unit"):
            unit_to_metres(1, "legua")


class TestParseLandmark:
    @pytest.mark.parametrize(
        ("raw", "expected_query"),
        [
            ("De la Rotonda El Güegüense, 2c al sur", "Rotonda El Güegüense"),
            ("Del Colonial Los Robles 1c al lago", "Colonial Los Robles"),
            ("Desde la Iglesia El Calvario, 1c abajo", "Iglesia El Calvario"),
            ("Metrocentro 2c al sur", "Metrocentro"),
            ("de la UCA 3 cuadras arriba", "UCA"),
            ("frente al Colegio Centroamérica", "Colegio Centroamérica"),
            ("contiguo a la Farmacia Xolotlán, 1c arriba", "Farmacia Xolotlán"),
        ],
    )
    def test_landmark_query_strips_prepositions(self, raw: str, expected_query: str):
        parsed = parse(raw)
        assert parsed is not None
        assert parsed.landmark_query == expected_query

    def test_display_text_keeps_the_original_spelling(self):
        parsed = parse("De la Rotonda El Güegüense, 2c al sur")
        assert parsed is not None
        assert "Güegüense" in parsed.landmark_text

    @pytest.mark.parametrize("raw", ["", "   ", ",,,"])
    def test_empty_input_returns_none(self, raw: str):
        assert parse(raw) is None

    def test_landmark_without_offsets_still_parses(self):
        parsed = parse("Iglesia El Calvario, portón negro")
        assert parsed is not None
        assert parsed.offsets == []
        assert parsed.modifiers == ["portón negro"]


class TestParseOffsets:
    def test_two_hop_address(self):
        parsed = parse("De la Rotonda El Güegüense, 2 cuadras al sur, 1 cuadra abajo")
        assert parsed is not None
        assert [(o.quantity, o.unit, o.bearing_deg) for o in parsed.offsets] == [
            (2.0, "cuadra", 180.0),
            (1.0, "cuadra", 270.0),
        ]
        assert parsed.total_distance_m == pytest.approx(3 * 84.0)

    @pytest.mark.parametrize(
        ("raw", "quantity", "unit", "bearing"),
        [
            ("Metrocentro 1c al lago", 1.0, "cuadra", 0.0),
            ("Metrocentro 2c. al sur", 2.0, "cuadra", 180.0),
            ("Metrocentro 3 cuadras arriba", 3.0, "cuadra", 90.0),
            ("Metrocentro 75 vrs abajo", 75.0, "vara", 270.0),
            ("Metrocentro 75 varas al oeste", 75.0, "vara", 270.0),
            ("Metrocentro 30 metros hacia el este", 30.0, "metro", 90.0),
            ("Metrocentro 30 mts al norte", 30.0, "metro", 0.0),
            ("Metrocentro media cuadra arriba", 0.5, "cuadra", 90.0),
            ("Metrocentro 1/2 c al sur", 0.5, "cuadra", 180.0),
            ("Metrocentro 1½ c al sur", 1.5, "cuadra", 180.0),
            ("Metrocentro dos cuadras al lago", 2.0, "cuadra", 0.0),
            ("Metrocentro una cuadra a la montaña", 1.0, "cuadra", 180.0),
            ("Metrocentro cinco cuadras al oriente", 5.0, "cuadra", 90.0),
            ("Metrocentro 2.5 km al sur", 2.5, "kilometro", 180.0),
        ],
    )
    def test_single_offset_shapes(self, raw: str, quantity: float, unit: str, bearing: float):
        parsed = parse(raw)
        assert parsed is not None, raw
        assert len(parsed.offsets) == 1, raw
        offset = parsed.offsets[0]
        assert offset.quantity == pytest.approx(quantity)
        assert offset.unit == unit
        assert offset.bearing_deg == bearing

    def test_direction_before_quantity(self):
        parsed = parse("De la Rotonda Centroamérica al sur 2 cuadras")
        assert parsed is not None
        assert len(parsed.offsets) == 1
        assert parsed.offsets[0].bearing_deg == 180.0
        assert parsed.offsets[0].quantity == 2.0

    def test_parenthetical_gloss_supplies_the_direction(self):
        # From MapaNica's own event notice.
        parsed = parse("Busto José Martí, 30 metros hacia el este (arriba)")
        assert parsed is not None
        assert parsed.offsets[0].bearing_deg == 90.0
        assert parsed.offsets[0].distance_m == 30.0

    def test_offset_without_a_direction_is_dropped_from_geometry(self):
        parsed = parse("Metrocentro 200 metros")
        assert parsed is not None
        assert parsed.offsets == []
        assert parsed.total_distance_m == 0.0

    @pytest.mark.parametrize(
        ("raw", "quantity"),
        [
            ("Metrocentro media cuadra al sur", 0.5),
            ("Metrocentro 1 cuadra y media al sur", 1.5),
            ("Metrocentro cuadra y media al sur", 1.5),
            ("Metrocentro al sur 1 cuadra y media", 1.5),
            ("Metrocentro al sur cuadra y media", 1.5),
        ],
    )
    def test_fractions_on_either_side_of_the_unit(self, raw: str, quantity: float):
        # "media cuadra" is half a block; "cuadra y media" is one and a half.
        parsed = parse(raw)
        assert parsed is not None, raw
        assert len(parsed.offsets) == 1, raw
        assert parsed.offsets[0].quantity == pytest.approx(quantity)
        assert parsed.offsets[0].bearing_deg == 180.0

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # The ambiguity that makes this parser interesting: the same words in
            # two orders bind the direction to different quantities.
            ("Metrocentro 2 cuadras al sur 1 cuadra abajo", [(2.0, 180.0), (1.0, 270.0)]),
            ("Metrocentro al sur 2c y al lago 1c", [(2.0, 180.0), (1.0, 0.0)]),
            ("Metrocentro 2c al sur y 1c abajo", [(2.0, 180.0), (1.0, 270.0)]),
        ],
    )
    def test_direction_binds_to_the_right_quantity(self, raw: str, expected: list):
        parsed = parse(raw)
        assert parsed is not None
        assert [(o.quantity, o.bearing_deg) for o in parsed.offsets] == expected

    def test_three_hops_in_order(self):
        parsed = parse("Del Hospital Bautista 1c al lago, 2c arriba, 25 vrs al sur")
        assert parsed is not None
        assert [o.bearing_deg for o in parsed.offsets] == [0.0, 90.0, 180.0]
        assert parsed.offsets[2].distance_m == pytest.approx(25 * VARA_M)


class TestFormerLandmarks:
    @pytest.mark.parametrize(
        "raw",
        [
            "Donde fue el Cine Cabrera, media cuadra arriba",
            "donde fue el Banco Popular 1c al sur",
            "De donde era la Pepsi, 2c abajo",
            "Del antiguo Hospital El Retiro 1c al lago",
            "Donde estuvo el Arbolito, 1c arriba",
        ],
    )
    def test_flagged_as_former(self, raw: str):
        parsed = parse(raw)
        assert parsed is not None
        assert parsed.former_landmark is True

    def test_marker_is_stripped_from_the_query(self):
        parsed = parse("Donde fue el Cine Cabrera, media cuadra arriba")
        assert parsed is not None
        assert parsed.landmark_query == "Cine Cabrera"
        assert "Donde fue" in parsed.landmark_text  # display keeps it

    def test_present_landmark_is_not_flagged(self):
        parsed = parse("Del Cine Cabrera 1c arriba")
        assert parsed is not None
        assert parsed.former_landmark is False


class TestModifiersAndSide:
    def test_modifiers_are_kept_verbatim(self):
        parsed = parse(
            "De la Rotonda El Güegüense, 2c al sur, 1c abajo, casa esquinera, portón negro"
        )
        assert parsed is not None
        assert "casa esquinera" in parsed.modifiers
        assert "portón negro" in parsed.modifiers

    @pytest.mark.parametrize(
        ("raw", "side"),
        [
            ("Km 12 carretera a Masaya, mano derecha", "derecha"),
            ("Del Colegio 1c al sur, mano izquierda", "izquierda"),
            ("Del Colegio 1c al sur", None),
        ],
    )
    def test_side_hint(self, raw: str, side: str | None):
        parsed = parse(raw)
        assert parsed is not None
        assert parsed.side_hint == side

    def test_trailing_direction_is_not_a_modifier(self):
        parsed = parse("Metrocentro, 2c, al sur")
        assert parsed is not None
        assert "al sur" not in parsed.modifiers


class TestRender:
    def test_round_trips_to_a_readable_string(self):
        parsed = parse("De la Rotonda El Güegüense, 2 cuadras al sur, 75 vrs abajo")
        assert parsed is not None
        rendered = parsed.render()
        # The display form keeps the natural Nicaraguan phrasing, article and all.
        assert rendered.startswith("De la Rotonda El Güegüense")
        assert "2c al sur" in rendered
        assert "75 vrs abajo" in rendered


class TestGate:
    @pytest.mark.parametrize(
        "raw",
        [
            "De la Rotonda El Güegüense, 2c al sur",
            "Metrocentro 1c al lago",
            "donde fue el Cine Cabrera",
            "frente al Colegio Centroamérica",
            "contiguo a la Farmacia Xolotlán",
        ],
    )
    def test_recognises_addresses(self, raw: str):
        assert looks_like_relative_address(raw) is True

    @pytest.mark.parametrize(
        "raw",
        ["", "restaurante el zaguan", "Metrocentro", "farmacia", "gasolinera uno", "12.14,-86.16"],
    )
    def test_rejects_plain_searches(self, raw: str):
        assert looks_like_relative_address(raw) is False


class TestResolve:
    def test_walks_the_offsets_from_the_landmark(self):
        parsed = parse("De la Rotonda El Güegüense, 2 cuadras al sur, 1 cuadra abajo")
        assert parsed is not None
        candidates = resolve(parsed, lookup_one(ROTONDA_GUEGUENSE))
        assert len(candidates) == 1
        candidate = candidates[0]

        expected_lat, expected_lon = destination_point(
            *destination_point(ROTONDA_GUEGUENSE.lat, ROTONDA_GUEGUENSE.lon, 180.0, 168.0),
            270.0,
            84.0,
        )
        assert candidate.lat == pytest.approx(expected_lat, abs=1e-6)
        assert candidate.lon == pytest.approx(expected_lon, abs=1e-6)
        assert candidate.method == GeocodeMethod.RELATIVE
        assert candidate.landmark_id == "gz-1"

    def test_south_then_west_lands_southwest_of_the_landmark(self):
        parsed = parse("De la Rotonda El Güegüense, 2c al sur, 1c abajo")
        assert parsed is not None
        candidate = resolve(parsed, lookup_one(ROTONDA_GUEGUENSE))[0]
        assert candidate.lat < ROTONDA_GUEGUENSE.lat, "al sur must decrease latitude"
        assert candidate.lon < ROTONDA_GUEGUENSE.lon, "abajo (west) must decrease longitude"
        assert haversine_m(
            ROTONDA_GUEGUENSE.lat, ROTONDA_GUEGUENSE.lon, candidate.lat, candidate.lon
        ) == pytest.approx(math.hypot(168.0, 84.0), abs=2.0)

    def test_no_landmark_match_yields_no_candidates(self):
        parsed = parse("De la Rotonda Inexistente, 2c al sur")
        assert parsed is not None
        assert resolve(parsed, no_landmarks) == []

    def test_candidates_are_sorted_by_confidence(self):
        weak = LandmarkMatch(id="a", name="Rotonda A", lat=12.10, lon=-86.20, score=0.4)
        strong = LandmarkMatch(id="b", name="Rotonda B", lat=12.11, lon=-86.21, score=0.9)

        def lookup(query: str, former: bool):
            return [weak, strong]

        parsed = parse("De la Rotonda X, 1c al sur")
        assert parsed is not None
        candidates = resolve(parsed, lookup)
        assert [c.landmark_id for c in candidates] == ["b", "a"]
        assert candidates[0].confidence > candidates[1].confidence

    def test_limit_is_respected(self):
        many = [
            LandmarkMatch(
                id=str(i), name=f"R{i}", lat=12.1 + i / 100, lon=-86.2, score=0.9 - i / 10
            )
            for i in range(5)
        ]
        parsed = parse("De la Rotonda X, 1c al sur")
        assert parsed is not None
        assert len(resolve(parsed, lambda q, f: many, limit=2)) == 2

    def test_city_overrides_the_cuadra_length(self, monkeypatch: pytest.MonkeyPatch):
        from common import geo

        monkeypatch.setitem(geo.CUADRA_M, "granada", 80.0)  # a deliberately odd value
        parsed = parse("Del Parque Central, 2c al sur")
        assert parsed is not None
        candidate = resolve(parsed, lookup_one(ROTONDA_GUEGUENSE), city="Granada")[0]
        assert candidate.relative is not None
        assert candidate.relative.offsets[0].distance_m == pytest.approx(160.0)

    def test_snapping_moves_the_pin_and_is_reported(self):
        parsed = parse("De la Rotonda El Güegüense, 1c al sur")
        assert parsed is not None
        candidate = resolve(
            parsed, lookup_one(ROTONDA_GUEGUENSE), snap=lambda lat, lon: (lat + 0.0001, lon)
        )[0]
        assert candidate.snapped_to_road is True

    def test_a_failing_snapper_does_not_break_geocoding(self):
        def broken(lat: float, lon: float):
            raise ConnectionError("valhalla down")

        parsed = parse("De la Rotonda El Güegüense, 1c al sur")
        assert parsed is not None
        candidates = resolve(parsed, lookup_one(ROTONDA_GUEGUENSE), snap=broken)
        assert len(candidates) == 1
        assert candidates[0].snapped_to_road is False

    def test_confidence_is_bounded_and_pessimistic(self):
        parsed = parse("De la Rotonda El Güegüense, 1c al sur")
        assert parsed is not None
        candidate = resolve(parsed, lookup_one(ROTONDA_GUEGUENSE))[0]
        assert 0.0 <= candidate.confidence <= 1.0
        assert candidate.confidence <= ROTONDA_GUEGUENSE.score * 1.05

    def test_long_walks_are_penalised(self):
        near = parse("De la Rotonda X, 1c al sur")
        far = parse("De la Rotonda X, 20c al sur")
        assert near is not None and far is not None
        near_conf = resolve(near, lookup_one(ROTONDA_GUEGUENSE))[0].confidence
        far_conf = resolve(far, lookup_one(ROTONDA_GUEGUENSE))[0].confidence
        assert far_conf < near_conf

    def test_former_query_matching_a_standing_landmark_is_penalised(self):
        standing = LandmarkMatch(id="x", name="Cine Cabrera", lat=12.15, lon=-86.27, score=0.9)
        parsed = parse("Donde fue el Cine Cabrera, 1c arriba")
        assert parsed is not None
        penalised = resolve(parsed, lookup_one(standing))[0]
        rewarded = resolve(parsed, lookup_one(CINE_CABRERA))[0]
        assert penalised.confidence < 0.9
        assert "ya no existe" in " ".join(penalised.notes)
        assert "histórico" in " ".join(rewarded.notes)

    def test_landmark_only_addresses_score_lower_than_walked_ones(self):
        bare = parse("Iglesia El Calvario")
        walked = parse("Iglesia El Calvario, 1c al sur")
        assert bare is not None and walked is not None
        bare_conf = resolve(bare, lookup_one(ROTONDA_GUEGUENSE))[0].confidence
        walked_conf = resolve(walked, lookup_one(ROTONDA_GUEGUENSE))[0].confidence
        assert bare_conf < walked_conf

    def test_granada_addresses_walk_east_to_the_lake(self):
        # Lake Cocibolca is east of Granada's Parque Central. Resolving this
        # with Managua's orientation would send the pin a block and a half north
        # instead — a real address landing in the wrong barrio.
        parque = LandmarkMatch(
            id="gz-granada",
            name="Parque Central de Granada",
            lat=11.9299,
            lon=-85.9560,
            score=0.95,
            city="Granada",
        )
        parsed = parse("Del Parque Central, 2c al lago", city="Granada")
        assert parsed is not None
        assert parsed.offsets[0].bearing_deg == 90.0

        candidate = resolve(parsed, lookup_one(parque), city="Granada")[0]
        assert candidate.lon > parque.lon, "al lago in Granada means east"
        assert candidate.lat == pytest.approx(parque.lat, abs=1e-4)

    def test_the_resolver_relearns_the_city_from_the_landmark(self):
        # The parser usually runs before the city is known — the gazetteer match
        # is what reveals it — so resolve() must re-derive the bearing.
        parque = LandmarkMatch(
            id="gz-granada", name="Parque Central", lat=11.9299, lon=-85.9560, city="Granada"
        )
        parsed = parse("Del Parque Central, 2c al lago")  # parsed with no city
        assert parsed is not None
        assert parsed.offsets[0].bearing_deg == 0.0  # Managua default

        candidate = resolve(parsed, lookup_one(parque), city="Granada")[0]
        assert candidate.relative is not None
        assert candidate.relative.offsets[0].bearing_deg == 90.0
        assert candidate.lon > parque.lon

    def test_an_unlisted_town_is_penalised_and_says_why(self):
        somewhere = LandmarkMatch(
            id="x", name="Iglesia", lat=13.0, lon=-85.0, score=0.9, city="Bluefields"
        )
        geographic = resolve(parse("De la Iglesia, 2c al lago"), lookup_one(somewhere))[0]
        cardinal = resolve(parse("De la Iglesia, 2c al sur"), lookup_one(somewhere))[0]
        assert geographic.confidence < cardinal.confidence
        assert any("Managua" in note for note in geographic.notes)

    def test_lookup_receives_the_former_flag(self):
        seen: list[tuple[str, bool]] = []

        def lookup(query: str, former: bool):
            seen.append((query, former))
            return [CINE_CABRERA]

        parsed = parse("Donde fue el Cine Cabrera, 1c arriba")
        assert parsed is not None
        resolve(parsed, lookup)
        assert seen == [("Cine Cabrera", True)]
