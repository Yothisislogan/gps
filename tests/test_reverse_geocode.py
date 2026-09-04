"""Tests for reverse relative geocoding — a pin in, a Nicaraguan address out.

The headline property is the round trip: whatever
:func:`pipeline.geocode.reverse.describe` prints must survive
:func:`pipeline.geocode.relative_address.parse` and resolve back to
approximately the point it started from.  "Approximately" is load-bearing and
quantified below — an address that says "2 cuadras" is *supposed* to lose the
difference between 160 m and two nominal blocks, because that is the difference
a person walking it will also lose.

Distances here are written as multiples of :func:`common.geo.cuadra_length_m`
rather than as round metre counts.  The block length is a prior that is meant to
be recalibrated from the street graph, and a test that hard-codes "200 m is two
cuadras" would start failing for a reason that has nothing to do with this
module.
"""

from __future__ import annotations

import math
import statistics

import pytest

from common.geo import (
    CITY_ORIENTATION,
    VARA_M,
    cuadra_length_m,
    haversine_m,
    local_projection,
    resolve_direction,
)
from pipeline.geocode.relative_address import LandmarkMatch, parse, resolve
from pipeline.geocode.reverse import (
    CUADRA_TOLERANCE_FRACTION,
    DEAD_ZONE_M,
    METRE_STEP_FAR_M,
    VARA_TOLERANCE_M,
    choose_landmark,
    describe,
    landmark_lookup,
    reverse,
    round_trip_error_m,
)

#: One block, in metres, as the rest of the stack currently believes it.
CUADRA = cuadra_length_m("Managua")

# --------------------------------------------------------------------------- #
# Tolerance
# --------------------------------------------------------------------------- #

#: The largest error one axis can pick up, derived from the module's own
#: constants rather than typed in, so a recalibrated block length moves the
#: bound instead of breaking the test:
#:
#: * a component quantised to a block is off by at most 12 % of a block;
#: * a component quantised to a vara step is off by at most the vara window;
#: * a component quoted in metres is off by at most half the rounding step;
#: * a component under the dead zone is dropped and loses all of itself.
_MAX_AXIS_ERROR_M = max(
    DEAD_ZONE_M,
    VARA_TOLERANCE_M,
    CUADRA_TOLERANCE_FRACTION * CUADRA,
    METRE_STEP_FAR_M / 2,
)

#: Worst-case round-trip drift, in metres.  The two axes are quantised
#: independently, so the vector error is bounded by the diagonal of that square
#: (~17 m today); the extra metre absorbs the disagreement between the
#: equirectangular frame the delta is split in and the spherical walk that puts
#: it back together.  It is not metre-exact by construction and must not be:
#: block quantisation is the feature, not an error.
ROUND_TRIP_TOLERANCE_M = math.hypot(_MAX_AXIS_ERROR_M, _MAX_AXIS_ERROR_M) + 1.0

#: The grid's *mean* error must stay far below the worst case; a change that
#: quietly degrades the typical answer while staying inside the bound would
#: otherwise pass unnoticed.
ROUND_TRIP_MEAN_TOLERANCE_M = 10.0


# --------------------------------------------------------------------------- #
# Fixtures: synthetic but plausible Managua/Granada landmarks
# --------------------------------------------------------------------------- #

GUEGUENSE = LandmarkMatch(
    id="gz-gue",
    name="Rotonda El Güegüense",
    lat=12.1352,
    lon=-86.2807,
    city="Managua",
    kind="rotonda",
    popularity=0.9,
)
HUEMBES = LandmarkMatch(
    id="gz-hue",
    name="Mercado Roberto Huembes",
    lat=12.1200,
    lon=-86.2450,
    city="Managua",
    kind="mercado",
    popularity=0.85,
)
GRANADA = LandmarkMatch(
    id="gz-gra",
    name="Parque Central de Granada",
    lat=11.9299,
    lon=-85.9560,
    city="Granada",
    kind="parque",
    popularity=0.8,
)
CINE_CABRERA = LandmarkMatch(
    id="gz-cine",
    name="Cine Cabrera",
    lat=12.1543,
    lon=-86.2733,
    city="Managua",
    kind="edificio",
    popularity=0.4,
    former=True,
)

ALL_LANDMARKS = [GUEGUENSE, HUEMBES, GRANADA, CINE_CABRERA]


def offset_point(landmark: LandmarkMatch, east_m: float, north_m: float) -> tuple[float, float]:
    """A point ``east_m``/``north_m`` from a landmark, as (lat, lon).

    Uses the same local frame the module decomposes in, so a test that asks for
    "two blocks north" gets a point the code should describe as exactly that —
    the test states the intent, not a coordinate someone eyeballed.
    """
    _, to_lonlat = local_projection(landmark.lat, landmark.lon)
    return to_lonlat(east_m, north_m)


def only(landmark: LandmarkMatch) -> list[LandmarkMatch]:
    return [landmark]


def hops(landmark: LandmarkMatch, east_m: float, north_m: float, **kwargs):
    """The offsets ``reverse`` produces for a point relative to one landmark."""
    lat, lon = offset_point(landmark, east_m, north_m)
    address = reverse(lat, lon, only(landmark), **kwargs)
    assert address is not None
    return address.offsets


# --------------------------------------------------------------------------- #
# Quantisation
# --------------------------------------------------------------------------- #


class TestCuadraQuantisation:
    def test_whole_blocks(self):
        (hop,) = hops(GUEGUENSE, 0.0, -2 * CUADRA)
        assert (hop.quantity, hop.unit) == (2.0, "cuadra")
        assert hop.distance_m == pytest.approx(2 * CUADRA)

    def test_block_and_a_half(self):
        # "cuadra y media" is said as readily as "dos cuadras"; quarter blocks
        # are not said at all, which is why the grid is 0.5c.
        (hop,) = hops(GUEGUENSE, 1.5 * CUADRA, 0.0)
        assert (hop.quantity, hop.unit) == (1.5, "cuadra")

    def test_snaps_within_tolerance(self):
        # Eight metres past the corner is not a new unit: the listener counts
        # corners, not metres.
        (hop,) = hops(GUEGUENSE, 0.0, CUADRA + 8.0)
        assert (hop.quantity, hop.unit) == (1.0, "cuadra")
        assert hop.distance_m == pytest.approx(CUADRA)

    def test_outside_tolerance_falls_back_to_metres(self):
        # A fifth of a block past the corner is too far to call it one, so it is
        # quoted in metres rather than pretending to a block count.
        (hop,) = hops(GUEGUENSE, 0.0, CUADRA * 1.2)
        assert hop.unit == "metro"

    def test_tolerance_boundary(self):
        window = CUADRA_TOLERANCE_FRACTION * CUADRA
        inside = hops(GUEGUENSE, 0.0, CUADRA + window - 0.5)[0]
        outside = hops(GUEGUENSE, 0.0, CUADRA + window + 3.0)[0]
        assert inside.unit == "cuadra"
        assert outside.unit == "metro"


class TestVaraSnapping:
    @pytest.mark.parametrize("step", [25, 50, 75])
    def test_exact_steps(self, step: int):
        (hop,) = hops(GUEGUENSE, 0.0, -step * VARA_M)
        assert hop.unit == "vara"
        assert hop.quantity == pytest.approx(float(step))

    def test_near_step_snaps(self):
        # 60 m is 71.8 varas; nobody says that, everybody says 75.
        (hop,) = hops(GUEGUENSE, 60.0, 0.0)
        assert (hop.quantity, hop.unit) == (75.0, "vara")
        assert hop.distance_m == pytest.approx(75 * VARA_M)

    def test_off_step_prefers_metres(self):
        # 52 m sits between 50 and 75 varas and is well short of a block, so it
        # stays in metres rather than being rounded into a unit it is not.
        (hop,) = hops(GUEGUENSE, 52.0, 0.0)
        assert hop.unit == "metro"
        assert hop.quantity == pytest.approx(50.0)

    def test_a_hundred_varas_is_a_cuadra_not_a_vara_count(self):
        # The colonial block *is* 100 varas, so the two ladders would otherwise
        # fight over the same distance.  From a block up, people count blocks.
        (hop,) = hops(GUEGUENSE, 0.0, 100 * VARA_M)
        assert hop.unit == "cuadra"
        assert hop.quantity == pytest.approx(1.0)

    def test_ladders_do_not_overlap(self):
        # Sweep the whole sub-block range: nothing below a block is ever called
        # a cuadra, and nothing at or above one is ever called varas.
        for metres in range(int(DEAD_ZONE_M) + 1, int(6 * CUADRA)):
            offsets = hops(GUEGUENSE, 0.0, float(metres))
            assert len(offsets) == 1
            hop = offsets[0]
            if hop.unit == "vara":
                assert hop.distance_m < CUADRA
            elif hop.unit == "cuadra":
                assert hop.quantity >= 1.0


class TestMetreFallback:
    def test_rounds_to_spoken_precision(self):
        (hop,) = hops(GUEGUENSE, 33.0, 0.0)
        assert (hop.quantity, hop.unit) == (35.0, "metro")

    def test_rounds_to_ten_past_a_hundred(self):
        (hop,) = hops(GUEGUENSE, 0.0, 4.25 * CUADRA)
        assert hop.unit == "metro"
        assert hop.quantity % METRE_STEP_FAR_M == 0.0


class TestDeadZone:
    def test_tiny_component_is_not_worth_saying(self):
        # 8 m east is GPS noise and a doorway's width; a hop for it would be
        # noise dressed as precision.
        (hop,) = hops(GUEGUENSE, 8.0, -2 * CUADRA)
        assert hop.direction_text == "al sur"

    def test_point_on_the_landmark_has_no_offsets(self):
        address = reverse(GUEGUENSE.lat, GUEGUENSE.lon, only(GUEGUENSE))
        assert address is not None
        assert address.offsets == []
        assert address.total_distance_m == 0.0
        assert address.render() == "De la Rotonda El Güegüense"
        assert round_trip_error_m(GUEGUENSE.lat, GUEGUENSE.lon, only(GUEGUENSE)) == pytest.approx(
            0.0, abs=1.0
        )


# --------------------------------------------------------------------------- #
# Hop order and direction signs
# --------------------------------------------------------------------------- #


class TestHopOrder:
    def test_north_south_before_east_west(self):
        first, second = hops(GUEGUENSE, CUADRA, 2 * CUADRA)
        assert first.bearing_deg == 0.0
        assert second.bearing_deg == 90.0
        assert first.direction_text == "al lago"
        assert second.direction_text == "arriba"

    def test_order_holds_when_the_east_west_leg_is_longer(self):
        first, second = hops(GUEGUENSE, -4 * CUADRA, CUADRA)
        assert first.bearing_deg == 0.0  # north/south is still spoken first
        assert second.bearing_deg == 270.0

    def test_at_most_two_hops(self):
        assert len(hops(GUEGUENSE, 3 * CUADRA, -3 * CUADRA)) == 2

    def test_single_hop_keeps_the_longer_leg(self):
        (hop,) = hops(GUEGUENSE, -CUADRA, -3 * CUADRA, max_hops=1)
        assert (hop.quantity, hop.bearing_deg) == (3.0, 180.0)


class TestDirectionSigns:
    """Nicaragua is all positive latitude and all negative longitude.

    A flipped sign is the classic bug in this function: "east" means a *less*
    negative longitude, and it is very easy to write ``lon0 - lon`` and produce
    an address that is a perfect mirror image of the truth.
    """

    @pytest.mark.parametrize(
        ("east_blocks", "north_blocks", "expected"),
        [
            (0, 2, ["al lago"]),
            (0, -2, ["al sur"]),
            (2, 0, ["arriba"]),
            (-2, 0, ["abajo"]),
            (2, 2, ["al lago", "arriba"]),
            (-2, -2, ["al sur", "abajo"]),
        ],
    )
    def test_quadrants(self, east_blocks: int, north_blocks: int, expected: list[str]):
        offsets = hops(GUEGUENSE, east_blocks * CUADRA, north_blocks * CUADRA)
        assert [offset.direction_text for offset in offsets] == expected

    def test_higher_latitude_is_the_lake_not_the_hills(self):
        lat, lon = GUEGUENSE.lat + 0.002, GUEGUENSE.lon
        assert lat > GUEGUENSE.lat
        assert "al lago" in (describe(lat, lon, only(GUEGUENSE)) or "")

    def test_less_negative_longitude_is_arriba(self):
        lat, lon = GUEGUENSE.lat, GUEGUENSE.lon + 0.002
        assert lon > GUEGUENSE.lon  # -86.2787 > -86.2807: eastward, still negative
        assert "arriba" in (describe(lat, lon, only(GUEGUENSE)) or "")

    def test_more_negative_longitude_is_abajo(self):
        lat, lon = GUEGUENSE.lat, GUEGUENSE.lon - 0.002
        assert "abajo" in (describe(lat, lon, only(GUEGUENSE)) or "")

    def test_all_fixtures_are_in_the_nicaraguan_quadrant(self):
        for landmark in ALL_LANDMARKS:
            assert landmark.lat > 0
            assert landmark.lon < 0


# --------------------------------------------------------------------------- #
# Registers
# --------------------------------------------------------------------------- #


class TestRegisters:
    def test_colloquial_is_the_default(self):
        lat, lon = offset_point(GUEGUENSE, -CUADRA, -2 * CUADRA)
        assert describe(lat, lon, only(GUEGUENSE)) == (
            "De la Rotonda El Güegüense, 2c al sur, 1c abajo"
        )

    def test_formal_register(self):
        lat, lon = offset_point(GUEGUENSE, -CUADRA, -2 * CUADRA)
        assert describe(lat, lon, only(GUEGUENSE), style="formal") == (
            "De la Rotonda El Güegüense, 2c al sur, 1c al oeste"
        )

    def test_registers_agree_on_geometry(self):
        lat, lon = offset_point(GUEGUENSE, 3 * CUADRA, 4 * CUADRA)
        colloquial = reverse(lat, lon, only(GUEGUENSE))
        formal = reverse(lat, lon, only(GUEGUENSE), style="formal")
        assert colloquial is not None and formal is not None
        assert [o.bearing_deg for o in colloquial.offsets] == [
            o.bearing_deg for o in formal.offsets
        ]
        assert [o.distance_m for o in colloquial.offsets] == [o.distance_m for o in formal.offsets]

    @pytest.mark.parametrize("style", ["colloquial", "formal"])
    def test_both_registers_round_trip(self, style: str):
        lat, lon = offset_point(HUEMBES, -3 * CUADRA, 2 * CUADRA)
        error = round_trip_error_m(lat, lon, ALL_LANDMARKS, style=style)
        assert error is not None
        assert error <= ROUND_TRIP_TOLERANCE_M

    def test_unknown_style_is_rejected(self):
        with pytest.raises(ValueError, match="unknown style"):
            describe(GUEGUENSE.lat, GUEGUENSE.lon, only(GUEGUENSE), style="pirate")  # type: ignore[arg-type]

    def test_more_than_two_hops_is_rejected(self):
        with pytest.raises(ValueError, match="max_hops"):
            describe(GUEGUENSE.lat, GUEGUENSE.lon, only(GUEGUENSE), max_hops=3)


class TestGeographicRegisterIsLocal:
    """ "Al lago" is north in Managua and east in Granada.

    Emitting it for the wrong town writes an address that is ninety degrees off
    the moment anyone resolves it, so the word is only used where it agrees with
    the direction meant.
    """

    def test_managua_says_al_lago(self):
        lat, lon = offset_point(GUEGUENSE, 0.0, 2 * CUADRA)
        assert describe(lat, lon, only(GUEGUENSE)) == "De la Rotonda El Güegüense, 2c al lago"

    def test_granada_falls_back_to_the_cardinal_word(self):
        # Lake Cocibolca is east of the Parque Central — Calle La Calzada runs
        # down to it — so "al lago" there would mean the wrong ninety degrees.
        assert CITY_ORIENTATION["granada"]["lago"] == 90.0
        lat, lon = offset_point(GRANADA, 0.0, 2 * CUADRA)
        assert describe(lat, lon, only(GRANADA)) == "Del Parque Central de Granada, 2c al norte"

    def test_the_solar_pair_is_national(self):
        # "arriba"/"abajo" follow the sunrise everywhere, so they survive the
        # city check in both towns.
        for landmark in (GUEGUENSE, GRANADA):
            lat, lon = offset_point(landmark, 2 * CUADRA, 0.0)
            assert "arriba" in (describe(lat, lon, only(landmark)) or "")

    def test_an_explicit_city_overrides_the_landmark(self):
        lat, lon = offset_point(GUEGUENSE, 0.0, 2 * CUADRA)
        assert "al norte" in (describe(lat, lon, only(GUEGUENSE), city="Granada") or "")

    @pytest.mark.parametrize("city", sorted(CITY_ORIENTATION))
    @pytest.mark.parametrize("style", ["colloquial", "formal"])
    def test_every_emitted_word_means_what_we_meant(self, city: str, style: str):
        """Property: the word printed resolves, in that town, to the bearing used.

        This is the invariant the whole feature rests on — it is what makes the
        output re-parseable rather than merely plausible.
        """
        for east_blocks, north_blocks in ((2, 3), (-2, -3), (2, -3), (-2, 3)):
            offsets = hops(
                GUEGUENSE,
                east_blocks * CUADRA,
                north_blocks * CUADRA,
                city=city,
                style=style,
            )
            for offset in offsets:
                assert resolve_direction(offset.direction_text, city) == offset.bearing_deg


# --------------------------------------------------------------------------- #
# Landmark choice
# --------------------------------------------------------------------------- #


def landmark_at(
    pin: tuple[float, float],
    name: str,
    kind: str,
    popularity: float,
    east_m: float,
    north_m: float,
    *,
    former: bool = False,
) -> LandmarkMatch:
    """A landmark placed relative to a pin, for scoring tests."""
    _, to_lonlat = local_projection(*pin)
    lat, lon = to_lonlat(east_m, north_m)
    return LandmarkMatch(
        id=name,
        name=name,
        lat=lat,
        lon=lon,
        city="Managua",
        kind=kind,
        popularity=popularity,
        former=former,
    )


PIN = (12.1352, -86.2807)


class TestLandmarkChoice:
    def test_well_known_beats_merely_closest(self):
        # The spoken address has to work for the person reading it: a rotonda
        # everyone can picture at 900 m is a better anchor than a colegio at
        # 200 m that only its own neighbours have heard of.
        rotonda = landmark_at(PIN, "Rotonda El Güegüense", "rotonda", 0.9, 636.0, 636.0)
        colegio = landmark_at(PIN, "Colegio Bautista", "colegio", 0.25, 141.0, 141.0)
        choice = choose_landmark(*PIN, [colegio, rotonda])
        assert choice is not None
        assert choice.match.name == "Rotonda El Güegüense"
        assert choice.distance_m == pytest.approx(900.0, abs=5.0)

    def test_adjacent_landmark_beats_a_famous_but_distant_one(self):
        # The other side of the same trade-off: once the reader can see the
        # thing from the door, fame stops mattering.
        colegio = landmark_at(PIN, "Colegio Bautista", "colegio", 0.25, 21.0, 21.0)
        rotonda = landmark_at(PIN, "Rotonda Universitaria", "rotonda", 0.9, 778.0, 778.0)
        choice = choose_landmark(*PIN, [rotonda, colegio])
        assert choice is not None
        assert choice.match.name == "Colegio Bautista"

    def test_prefers_a_clean_block_count(self):
        # Two equally famous rotondas at nearly the same distance: the one that
        # lands on whole cuadras wins, because "2c al sur" is an address and
        # "155 m al sur" is a survey reading.
        clean = landmark_at(PIN, "Rotonda Santo Domingo", "rotonda", 0.8, 0.0, 2 * CUADRA)
        messy = landmark_at(PIN, "Rotonda Jean Paul Genie", "rotonda", 0.8, 0.0, -(2 * CUADRA - 13))
        choice = choose_landmark(*PIN, [messy, clean])
        assert choice is not None
        assert choice.match.name == "Rotonda Santo Domingo"
        assert choice.offsets[0].unit == "cuadra"

    def test_popularity_falls_back_to_the_kind(self):
        # A gazetteer row nobody has scored yet is still a rotonda, and the
        # builder's per-kind default says how often rotondas anchor addresses.
        rotonda = LandmarkMatch(
            id="r", name="Rotonda Sin Puntaje", lat=PIN[0], lon=PIN[1], kind="rotonda"
        )
        colegio = LandmarkMatch(
            id="c", name="Colegio Sin Puntaje", lat=PIN[0], lon=PIN[1], kind="colegio"
        )
        choice = choose_landmark(*PIN, [colegio, rotonda])
        assert choice is not None
        assert choice.match.id == "r"

    def test_out_of_range_landmarks_are_ignored(self):
        far = landmark_at(PIN, "Rotonda Centroamérica", "rotonda", 0.95, 0.0, 2_000.0)
        near = landmark_at(PIN, "Colegio Bautista", "colegio", 0.25, 0.0, 300.0)
        choice = choose_landmark(*PIN, [far, near])
        assert choice is not None
        assert choice.match.name == "Colegio Bautista"

    def test_range_is_configurable(self):
        far = landmark_at(PIN, "Rotonda Centroamérica", "rotonda", 0.95, 0.0, 2_000.0)
        assert choose_landmark(*PIN, [far]) is None
        assert choose_landmark(*PIN, [far], max_landmark_distance_m=2_500.0) is not None

    def test_score_is_a_quality_in_the_unit_interval(self):
        choice = choose_landmark(*offset_point(GUEGUENSE, 100.0, 100.0), ALL_LANDMARKS)
        assert choice is not None
        assert 0.0 <= choice.score <= 1.0

    def test_reported_error_matches_the_real_round_trip(self):
        lat, lon = offset_point(GUEGUENSE, 137.0, -212.0)
        choice = choose_landmark(lat, lon, only(GUEGUENSE))
        measured = round_trip_error_m(lat, lon, only(GUEGUENSE))
        assert choice is not None and measured is not None
        assert choice.error_m == pytest.approx(measured, abs=1.0)


class TestFormerLandmarks:
    def test_a_ghost_landmark_loses_to_a_living_one(self):
        # "Donde fue el Cine Cabrera" is perfectly clear to a Managua native and
        # useless to the tourist reading the share sheet, so it only wins when
        # nothing else is in range.
        cine = landmark_at(PIN, "Cine Cabrera", "edificio", 0.4, CUADRA, 0.0, former=True)
        mercado = landmark_at(PIN, "Mercado Oriental", "mercado", 0.8, 0.0, 6 * CUADRA)
        choice = choose_landmark(*PIN, [cine, mercado])
        assert choice is not None
        assert choice.match.name == "Mercado Oriental"

    def test_a_ghost_landmark_beats_no_landmark(self):
        cine = landmark_at(PIN, "Cine Cabrera", "edificio", 0.4, CUADRA, 0.0, former=True)
        assert describe(*PIN, [cine]) == "Donde fue el Cine Cabrera, 1c abajo"

    def test_ghost_landmarks_are_flagged_for_the_resolver(self):
        # The "donde fue" prefix is not decoration: it is what makes the parser
        # set former_landmark, which is what makes the resolver look in the
        # gazetteer's former rows instead of the live ones.
        lat, lon = offset_point(CINE_CABRERA, 0.0, -2 * CUADRA)
        address = reverse(lat, lon, [CINE_CABRERA])
        assert address is not None
        assert address.former_landmark is True
        reparsed = parse(address.render())
        assert reparsed is not None
        assert reparsed.former_landmark is True
        assert reparsed.landmark_query == "Cine Cabrera"

    def test_ghost_landmark_round_trips(self):
        lat, lon = offset_point(CINE_CABRERA, CUADRA, -2 * CUADRA)
        error = round_trip_error_m(lat, lon, [CINE_CABRERA])
        assert error is not None
        assert error <= ROUND_TRIP_TOLERANCE_M

    def test_lookup_prefers_former_rows(self):
        lookup = landmark_lookup(ALL_LANDMARKS)
        assert [m.id for m in lookup("Cine Cabrera", True)] == ["gz-cine"]
        assert lookup("Rotonda El Güegüense", False)[0].id == "gz-gue"
        assert lookup("no existe", False) == []


# --------------------------------------------------------------------------- #
# Nothing in range
# --------------------------------------------------------------------------- #


class TestNoLandmark:
    def test_empty_gazetteer(self):
        assert reverse(*PIN, []) is None
        assert describe(*PIN, []) is None
        assert round_trip_error_m(*PIN, []) is None

    def test_everything_out_of_range(self):
        # Better no address than one anchored on something 5 km away: the reader
        # would trust it.
        far = landmark_at(PIN, "Rotonda Centroamérica", "rotonda", 0.95, 0.0, 5_000.0)
        assert reverse(*PIN, [far]) is None
        assert describe(*PIN, [far]) is None
        assert round_trip_error_m(*PIN, [far]) is None


# --------------------------------------------------------------------------- #
# Sentence shape
# --------------------------------------------------------------------------- #


class TestLandmarkPhrase:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("Rotonda El Güegüense", "De la Rotonda El Güegüense"),
            ("Mercado Oriental", "Del Mercado Oriental"),
            ("Parque Las Piedrecitas", "Del Parque Las Piedrecitas"),
            ("Universidad Centroamericana", "De la Universidad Centroamericana"),
            ("Terminal de Buses", "De la Terminal de Buses"),
            ("Estación de Bomberos", "De la Estación de Bomberos"),
            ("El Zaguán", "Del Zaguán"),
            ("La Curva", "De la Curva"),
        ],
    )
    def test_leading_article(self, name: str, expected: str):
        landmark = LandmarkMatch(id="x", name=name, lat=GUEGUENSE.lat, lon=GUEGUENSE.lon)
        address = reverse(GUEGUENSE.lat, GUEGUENSE.lon, [landmark])
        assert address is not None
        assert address.render() == expected

    def test_article_can_be_switched_off(self):
        lat, lon = offset_point(GUEGUENSE, 0.0, -2 * CUADRA)
        assert describe(lat, lon, only(GUEGUENSE), with_article=False) == (
            "Rotonda El Güegüense, 2c al sur"
        )

    def test_query_keeps_the_gazetteer_name(self):
        # Reverse geocoding knows exactly which row it used, so the query does
        # not have to be re-derived from the display text the way the parser has
        # to derive it.
        lat, lon = offset_point(GUEGUENSE, 0.0, -2 * CUADRA)
        address = reverse(lat, lon, only(GUEGUENSE))
        assert address is not None
        assert address.landmark_query == "Rotonda El Güegüense"
        assert address.raw == address.render()

    def test_modifiers_are_carried_verbatim(self):
        lat, lon = offset_point(GUEGUENSE, 0.0, -2 * CUADRA)
        address = reverse(lat, lon, only(GUEGUENSE), modifiers=["portón negro"])
        assert address is not None
        assert address.render() == "De la Rotonda El Güegüense, 2c al sur, portón negro"
        reparsed = parse(address.render())
        assert reparsed is not None
        assert reparsed.modifiers == ["portón negro"]
        assert len(reparsed.offsets) == 1  # a modifier never becomes geometry


# --------------------------------------------------------------------------- #
# The round trip
# --------------------------------------------------------------------------- #


class TestRoundTrip:
    def test_grid_around_every_landmark(self):
        """parse(describe(p)) must resolve back to p, within the block quantisation.

        The grid deliberately includes the ugly cases — points that land between
        blocks, points closer to a different landmark than the one they were
        generated around, and the Granada landmark whose "al lago" points
        somewhere else entirely — because those are where a sign error, a wrong
        block length or a mis-chosen direction word would show up.
        """
        errors: list[float] = []
        for landmark in ALL_LANDMARKS:
            for east_m in range(-600, 601, 75):
                for north_m in range(-600, 601, 75):
                    lat, lon = offset_point(landmark, float(east_m), float(north_m))
                    error = round_trip_error_m(lat, lon, ALL_LANDMARKS)
                    assert error is not None, f"no address for {lat},{lon}"
                    assert error <= ROUND_TRIP_TOLERANCE_M, (
                        f"{describe(lat, lon, ALL_LANDMARKS)!r} drifted {error:.1f} m"
                    )
                    errors.append(error)
        assert len(errors) == len(ALL_LANDMARKS) * 17 * 17
        assert statistics.mean(errors) <= ROUND_TRIP_MEAN_TOLERANCE_M

    def test_round_trip_uses_the_real_resolver(self):
        # Not a shortcut that hands back the landmark we started from: the
        # string goes through parse() and resolve() exactly as the /api/geocode
        # path would run it.
        lat, lon = offset_point(HUEMBES, -2 * CUADRA, 4 * CUADRA)
        text = describe(lat, lon, ALL_LANDMARKS)
        assert text == "Del Mercado Roberto Huembes, 4c al lago, 2c abajo"
        reparsed = parse(text)
        assert reparsed is not None
        candidates = resolve(reparsed, landmark_lookup(ALL_LANDMARKS))
        assert candidates
        assert haversine_m(lat, lon, candidates[0].lat, candidates[0].lon) <= (
            ROUND_TRIP_TOLERANCE_M
        )

    def test_dropped_hop_costs_at_most_the_dead_zone(self):
        lat, lon = offset_point(GUEGUENSE, 9.0, -2 * CUADRA)
        error = round_trip_error_m(lat, lon, only(GUEGUENSE))
        assert error is not None
        assert 8.0 <= error <= DEAD_ZONE_M  # the unspoken 9 m, and nothing else

    def test_granada_round_trips_despite_the_lake_being_east(self):
        # The forward path re-derives bearings from the town; if this module had
        # written "al lago" for north here, the pin would come back a block and
        # a half away in the wrong direction.
        for east_blocks, north_blocks in ((3, -1), (-3, 1), (0, 4), (4, 0)):
            lat, lon = offset_point(GRANADA, east_blocks * CUADRA, north_blocks * CUADRA)
            error = round_trip_error_m(lat, lon, [GRANADA])
            assert error is not None
            assert error <= ROUND_TRIP_TOLERANCE_M
