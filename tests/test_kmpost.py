"""Corpus and geometry tests for the km-post geocoder.

The strings are written the way Nicaraguans write them — "km12", "Km 9½", "kilómetro
nueve y medio", accents dropped at random — and the geometry tests run against synthetic
carreteras with lengths known to the metre, so a wrong along-distance shows up as a number
rather than as a plausible-looking pin.
"""

from __future__ import annotations

import json

import pytest

from common.geo import (
    NICARAGUA_BBOX,
    destination_point,
    haversine_m,
    initial_bearing_deg,
    interpolate_along,
    line_length_m,
)
from common.models import GeocodeMethod
from common.text import normalize
from pipeline.geocode.kmpost import (
    CARRETERAS_PATH,
    SIDE_OFFSET_M,
    Highway,
    all_highways,
    highway_by_key,
    load_highways,
    looks_like_kmpost,
    main,
    match_highway,
    parse,
    resolve,
)

#: Managua's historic Km 0, as docs/carreteras.csv records it for the radial carreteras.
KM0 = (12.1544, -86.2733)


def straight_line(
    spacing_m: float,
    *,
    segments: int = 30,
    bearing_deg: float = 135.0,
    start: tuple[float, float] = KM0,
) -> list[list[float]]:
    """A synthetic centreline of ``segments`` equal hops, as GeoJSON ``[lon, lat]`` pairs.

    ``spacing_m`` is how much *geometry* each signed kilometre gets: 1000 m models a road
    whose OSM length matches its signs, 1080 m the 8 %-too-long line the calibration tests
    have to correct.
    """
    lat, lon = start
    points = [[lon, lat]]
    for _ in range(segments):
        lat, lon = destination_point(lat, lon, bearing_deg, spacing_m)
        points.append([lon, lat])
    return points


def lookup(coords):
    """A ``geometry_lookup`` that answers with one line whatever the key."""

    def _lookup(_highway_key: str):
        return coords

    return _lookup


# --------------------------------------------------------------------------- #
# docs/carreteras.csv
# --------------------------------------------------------------------------- #


class TestRegistry:
    def test_ships_the_carreteras_people_quote(self):
        keys = {highway.highway_key for highway in all_highways()}
        assert {
            "carretera_a_masaya",
            "carretera_norte",
            "carretera_sur",
            "carretera_nueva_a_leon",
            "carretera_vieja_a_leon",
            "carretera_a_tipitapa",
            "carretera_masaya_granada",
            "carretera_a_el_crucero",
            "carretera_a_ticuantepe",
            "panamericana_sur",
            "carretera_a_el_rama",
            "carretera_a_matagalpa",
            "pista_juan_pablo_ii",
            "pista_suburbana",
            "pista_de_la_resistencia",
        } <= keys

    def test_aliases_are_stored_normalised(self):
        for highway in all_highways():
            for alias in highway.aliases:
                assert normalize(alias) == alias, f"{highway.highway_key}: {alias!r}"

    def test_no_row_claims_the_bare_word_carretera(self):
        # It appears in half the addresses in the country and identifies nothing.
        for highway in all_highways():
            assert "carretera" not in highway.aliases

    def test_aliases_are_unique_across_rows(self):
        seen: dict[str, str] = {}
        for highway in all_highways():
            for alias in highway.aliases:
                assert alias not in seen, (
                    f"{alias!r} claimed by {seen.get(alias)} and {highway.highway_key}"
                )
                seen[alias] = highway.highway_key

    def test_km0_is_in_nicaragua_or_absent(self):
        min_lon, min_lat, max_lon, max_lat = NICARAGUA_BBOX
        for highway in all_highways():
            if highway.km0 is None:
                # Only the urban pistas, which carry no signed chainage.
                assert highway.highway_key.startswith("pista")
                continue
            lat, lon = highway.km0
            assert min_lat <= lat <= max_lat and min_lon <= lon <= max_lon

    def test_radial_carreteras_share_the_managua_origin(self):
        masaya = highway_by_key("carretera_a_masaya")
        norte = highway_by_key("carretera_norte")
        assert masaya is not None and norte is not None
        assert masaya.km0 == norte.km0 == KM0

    def test_refs_are_only_filled_where_claimed(self):
        refs = {h.highway_key: h.osm_ref for h in all_highways() if h.osm_ref}
        assert refs == {
            "carretera_a_masaya": "NIC-4",
            "carretera_norte": "NIC-1",
            "panamericana_sur": "NIC-2",
        }

    def test_unknown_key(self):
        assert highway_by_key("carretera_a_narnia") is None

    def test_header_is_the_highway_table_shape(self):
        header = CARRETERAS_PATH.read_text(encoding="utf-8")
        line = next(
            row for row in header.splitlines() if row.strip() and not row.lstrip().startswith("#")
        )
        assert line == "highway_key,display_name,aliases,osm_ref,km0_lat,km0_lon,note"

    @pytest.mark.parametrize(
        ("row", "problem"),
        [
            ("Carretera_Mala,X,carretera mala,,,,", "highway_key"),
            ("ok_key,,carretera ok,,,,", "display_name is empty"),
            ("ok_key,X,Carretera OK,,,,", "not normalised"),
            ("ok_key,X,carretera,,,,", "identifies no highway"),
            ("ok_key,X,,,,,", "no aliases"),
            ("ok_key,X,carretera ok,NIC4,,,", "not a NIC-"),
            ("ok_key,X,carretera ok,,12.1,,", "both be set"),
            ("ok_key,X,carretera ok,,48.0,-86.2,", "outside Nicaragua"),
        ],
    )
    def test_malformed_rows_fail_the_load(self, tmp_path, row, problem):
        path = tmp_path / f"carreteras_{abs(hash(row))}.csv"
        path.write_text(
            "highway_key,display_name,aliases,osm_ref,km0_lat,km0_lon,note\n" + row + "\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match=problem):
            load_highways(path)

    def test_duplicate_alias_across_rows_fails(self, tmp_path):
        path = tmp_path / "carreteras_dup.csv"
        path.write_text(
            "highway_key,display_name,aliases,osm_ref,km0_lat,km0_lon,note\n"
            "uno,Uno,carretera uno,,,,\n"
            "dos,Dos,carretera uno,,,,\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="already claimed"):
            load_highways(path)

    def test_missing_file_is_fatal(self, tmp_path):
        with pytest.raises(ValueError, match="cannot read highway registry"):
            load_highways(tmp_path / "nope.csv")


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


class TestParseKm:
    @pytest.mark.parametrize(
        ("text", "km"),
        [
            ("Km 12.5", 12.5),
            ("km 9½", 9.5),
            ("Km 9 1/2", 9.5),
            ("kilometro 12", 12.0),
            ("Kilómetro 12", 12.0),
            ("km12", 12.0),
            ("KM 3", 3.0),
            ("Km. 7", 7.0),
            ("km 12,5", 12.5),
            ("Km-12", 12.0),  # the hyphen is a separator; there is no negative chainage
            ("km doce", 12.0),
            ("kilometro nueve y medio", 9.5),
            ("km 9 y medio", 9.5),
            ("km veinticinco", 25.0),
            ("kilometros 14", 14.0),
            ("km 0", 0.0),
        ],
    )
    def test_quantity_forms(self, text, km):
        parsed = parse(text)
        assert parsed is not None
        assert parsed.km == pytest.approx(km)

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "   ",
            "supermercado la colonia",
            "2 km al sur de la rotonda",  # a relative-address hop, not a km post
            "300 mts al norte",
            "km 2024 carretera norte",  # a year, not a mojón
            "kilometraje del vehiculo",
            "kmart",
        ],
    )
    def test_negatives(self, text):
        assert parse(text) is None

    def test_raw_is_kept(self):
        parsed = parse("  Km 12.5   Carretera a Masaya ")
        assert parsed is not None
        assert parsed.raw == "Km 12.5 Carretera a Masaya"


class TestParseHighway:
    @pytest.mark.parametrize(
        ("text", "key", "km"),
        [
            ("Km 12.5 Carretera a Masaya", "carretera_a_masaya", 12.5),
            ("km 9½ carretera norte, mano derecha", "carretera_norte", 9.5),
            ("Km 5 Carretera Norte", "carretera_norte", 5.0),
            ("Carretera a Masaya km 14", "carretera_a_masaya", 14.0),
            ("km 14 carretera masaya mano derecha", "carretera_a_masaya", 14.0),
            ("km12 carretera nueva a leon", "carretera_nueva_a_leon", 12.0),
            ("Km 22 Ctra. a Masaya", "carretera_a_masaya", 22.0),
            ("KILÓMETRO 9 Y MEDIO CARRETERA NUEVA A LEÓN", "carretera_nueva_a_leon", 9.5),
            ("km 4 carretera masaya-granada", "carretera_masaya_granada", 4.0),
            ("km 18 carretera a la concha", "carretera_a_ticuantepe", 18.0),
            ("km 292 carretera al rama", "carretera_a_el_rama", 292.0),
            ("Km 111 Panamericana Sur", "panamericana_sur", 111.0),
            ("km 14 masaya", "carretera_a_masaya", 14.0),
        ],
    )
    def test_highway_is_resolved(self, text, key, km):
        parsed = parse(text)
        assert parsed is not None
        assert parsed.highway_key == key
        assert parsed.km == pytest.approx(km)

    def test_highway_text_keeps_the_users_spelling(self):
        parsed = parse("Km 22 Ctra. a Masaya")
        assert parsed is not None
        assert parsed.highway_text == "Ctra. a Masaya"

    def test_unknown_highway_parses_without_a_key(self):
        # The carretera to San Marcos is real and not in the registry; the parse still
        # succeeds so the API can log the name as one to add.
        parsed = parse("Km 8 carretera a San Marcos")
        assert parsed is not None
        assert parsed.highway_key is None
        assert parsed.highway_text == "carretera a San Marcos"

    def test_side_hint_is_not_part_of_the_highway_name(self):
        parsed = parse("km 8 carretera a San Marcos, mano izquierda")
        assert parsed is not None
        assert parsed.highway_text == "carretera a San Marcos"
        assert parsed.side_hint == "izquierda"

    @pytest.mark.parametrize(
        ("text", "side"),
        [
            ("km 14 carretera a masaya mano derecha", "derecha"),
            ("Km 14 Carretera a Masaya, Mano Izquierda", "izquierda"),
            ("km 9 carretera norte lado derecho", "derecha"),
            ("km 9 carretera norte a mano izquierda", "izquierda"),
            ("km 9 carretera norte", None),
        ],
    )
    def test_side_hint(self, text, side):
        parsed = parse(text)
        assert parsed is not None
        assert parsed.side_hint == side

    def test_render(self):
        parsed = parse("km 12,5 ctra masaya mano derecha")
        assert parsed is not None
        assert parsed.render() == "Km 12.5 Carretera a Masaya, mano derecha"


class TestMatchHighway:
    @pytest.mark.parametrize(
        ("text", "key"),
        [
            ("Carretera a Masaya", "carretera_a_masaya"),
            ("carretera masaya", "carretera_a_masaya"),
            ("CTRA A MASAYA", "carretera_a_masaya"),
            ("ctra. a masaya", "carretera_a_masaya"),
            ("Carretera Nueva a León", "carretera_nueva_a_leon"),
            ("nueva a leon", "carretera_nueva_a_leon"),
            ("Carretera Vieja a León", "carretera_vieja_a_leon"),
            ("vieja a leon", "carretera_vieja_a_leon"),
            ("Panamericana Norte", "carretera_norte"),
            ("panamericana sur", "panamericana_sur"),
            ("Pista Juan Pablo II", "pista_juan_pablo_ii"),
            ("la resistencia", "pista_de_la_resistencia"),
            ("Carretera Masaya-Granada", "carretera_masaya_granada"),
            ("carretera a el crucero", "carretera_a_el_crucero"),
        ],
    )
    def test_aliases(self, text, key):
        assert match_highway(text) == key

    def test_accent_free_typing_matches(self):
        assert match_highway("carretera nueva a leon") == match_highway("Carretera Nueva a León")

    def test_longest_alias_wins(self):
        # "carretera masaya" is also an alias, of a different road.
        assert match_highway("carretera masaya granada") == "carretera_masaya_granada"

    @pytest.mark.parametrize(
        "text", ["", "carretera", "la carretera", "carretera nueva", "colonia"]
    )
    def test_non_matches(self, text):
        assert match_highway(text) is None

    def test_does_not_match_inside_a_longer_word(self):
        assert match_highway("masayana") is None


class TestGate:
    @pytest.mark.parametrize(
        "text",
        [
            "Km 12.5 Carretera a Masaya",
            "km 9½ carretera norte",
            "Carretera a Masaya km 14",
            "km 14 masaya",
            "kilometro 9 y medio carretera nueva a leon",
            "Km 8 carretera a San Marcos",  # unknown carretera, but clearly a km post
            "km12 ctra sur",
        ],
    )
    def test_fires(self, text):
        assert looks_like_kmpost(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "km 12 de mi casa",
            "Restaurante Km 5",
            "km 12",
            "2 km al sur de la rotonda",
            "de la rotonda el gueguense 2c al lago",
            "supermercado la colonia",
            "km 2024 carretera norte",
            "farmacia",
        ],
    )
    def test_stays_quiet(self, text):
        assert looks_like_kmpost(text) is False


# --------------------------------------------------------------------------- #
# Measuring along the carretera
# --------------------------------------------------------------------------- #


class TestAlongLine:
    def test_line_fixture_has_the_length_it_claims(self):
        coords = straight_line(1000.0)
        assert line_length_m(coords) == pytest.approx(30_000.0, abs=1.0)

    def test_km_lands_at_the_right_distance(self):
        coords = straight_line(1000.0)
        candidates = resolve(parse("Km 12.5 Carretera a Masaya"), lookup(coords))
        assert len(candidates) == 1
        candidate = candidates[0]
        expected = interpolate_along(coords, 12_500.0)
        assert haversine_m(candidate.lat, candidate.lon, *expected) < 1.0
        assert haversine_m(candidate.lat, candidate.lon, *KM0) == pytest.approx(12_500.0, abs=20.0)

    def test_candidate_shape(self):
        coords = straight_line(1000.0)
        candidate = resolve(parse("Km 12.5 Carretera a Masaya"), lookup(coords))[0]
        assert candidate.method is GeocodeMethod.KMPOST
        assert candidate.label == "Km 12.5 Carretera a Masaya"
        assert 0.0 <= candidate.confidence <= 1.0
        assert candidate.notes

    def test_reversed_geometry_is_oriented_to_km0(self):
        # PostGIS hands back whatever direction the ways were merged in; a backwards
        # carretera would put Km 5 where Km 25 belongs.
        coords = straight_line(1000.0)
        backwards = list(reversed(coords))
        candidate = resolve(parse("km 12 carretera a masaya"), lookup(backwards))[0]
        expected = interpolate_along(coords, 12_000.0)
        assert haversine_m(candidate.lat, candidate.lon, *expected) < 1.0

    def test_unknown_highway_returns_nothing(self):
        coords = straight_line(1000.0)
        assert resolve(parse("Km 8 carretera a San Marcos"), lookup(coords)) == []

    def test_missing_geometry_returns_nothing(self):
        assert resolve(parse("Km 8 carretera a masaya"), lookup(None)) == []
        assert resolve(parse("Km 8 carretera a masaya"), lookup([])) == []
        assert resolve(parse("Km 8 carretera a masaya"), lookup([[-86.27, 12.15]])) == []

    def test_no_parse_returns_nothing(self):
        assert resolve(None, lookup(straight_line(1000.0))) == []

    def test_km_beyond_the_mapped_end_clamps(self):
        coords = straight_line(1000.0)  # 30 km of carretera
        candidate = resolve(parse("km 40 carretera a masaya"), lookup(coords))[0]
        assert candidate.lat == pytest.approx(coords[-1][1])
        assert candidate.lon == pytest.approx(coords[-1][0])
        assert any("fuera del tramo" in note for note in candidate.notes)
        assert candidate.confidence < 0.3


class TestCalibration:
    """The signs are the authority; OSM geometry is 8 % too long in these fixtures."""

    @staticmethod
    def stretched():
        """A carretera whose OSM line runs 1 080 m for every signed kilometre."""
        return straight_line(1080.0)

    @staticmethod
    def mojones(coords, *kms):
        """Mojones placed where the *signs* are: km k sits at 1.08·k km of geometry."""
        return [(km, *interpolate_along(coords, km * 1080.0)) for km in kms]

    def test_uncalibrated_geometry_is_wrong_by_the_stretch(self):
        coords = self.stretched()
        candidate = resolve(parse("km 15 carretera a masaya"), lookup(coords))[0]
        signed = interpolate_along(coords, 15 * 1080.0)
        # 8 % of 15 km: the pin lands 1.2 km short of the sign.
        assert haversine_m(candidate.lat, candidate.lon, *signed) == pytest.approx(1200, abs=20)

    def test_two_mojones_correct_the_stretch(self):
        coords = self.stretched()
        candidate = resolve(
            parse("km 15 carretera a masaya"), lookup(coords), self.mojones(coords, 10, 20)
        )[0]
        signed = interpolate_along(coords, 15 * 1080.0)
        assert haversine_m(candidate.lat, candidate.lon, *signed) < 5.0
        assert any("Calibrado entre mojones" in note for note in candidate.notes)

    def test_calibration_accepts_mappings(self):
        coords = self.stretched()
        rows = [{"km": km, "lat": lat, "lon": lon} for km, lat, lon in self.mojones(coords, 10, 20)]
        candidate = resolve(parse("km 15 carretera a masaya"), lookup(coords), rows)[0]
        signed = interpolate_along(coords, 15 * 1080.0)
        assert haversine_m(candidate.lat, candidate.lon, *signed) < 5.0

    @pytest.mark.parametrize("km", [5, 25])
    def test_extrapolation_outside_the_calibrated_range(self, km):
        coords = self.stretched()
        candidate = resolve(
            parse(f"km {km} carretera a masaya"), lookup(coords), self.mojones(coords, 10, 20)
        )[0]
        signed = interpolate_along(coords, km * 1080.0)
        assert haversine_m(candidate.lat, candidate.lon, *signed) < 5.0
        assert any("Extrapolado" in note for note in candidate.notes)

    def test_single_mojon_anchors_the_scale_through_the_origin(self):
        coords = self.stretched()
        candidate = resolve(
            parse("km 15 carretera a masaya"), lookup(coords), self.mojones(coords, 20)
        )[0]
        signed = interpolate_along(coords, 15 * 1080.0)
        assert haversine_m(candidate.lat, candidate.lon, *signed) < 5.0

    def test_a_mojon_off_the_carretera_is_ignored(self):
        # A mis-keyed kmpost row (a sign from another highway) must not drag the answer.
        coords = self.stretched()
        candidate = resolve(
            parse("km 15 carretera a masaya"), lookup(coords), [(10.0, 12.5, -86.9)]
        )[0]
        uncalibrated = resolve(parse("km 15 carretera a masaya"), lookup(coords))[0]
        assert candidate.lat == pytest.approx(uncalibrated.lat)
        assert any("Sin mojones" in note for note in candidate.notes)

    def test_non_monotonic_mojones_are_dropped(self):
        coords = self.stretched()
        good = self.mojones(coords, 10, 20)
        # A sign read as Km 15 but standing before the Km 10 mojón: keeping it would
        # invert the whole segment.
        liar = (15.0, *interpolate_along(coords, 5 * 1080.0))
        candidate = resolve(parse("km 18 carretera a masaya"), lookup(coords), [*good, liar])[0]
        signed = interpolate_along(coords, 18 * 1080.0)
        assert haversine_m(candidate.lat, candidate.lon, *signed) < 5.0

    def test_implausible_scale_falls_back_to_raw_geometry(self):
        coords = self.stretched()
        # Two mojones 1 signed km apart but 10 km of geometry apart: one of them is a
        # misreading, so the segment is unusable and the geometry is trusted 1:1.
        anchors = [
            (10.0, *interpolate_along(coords, 10_800.0)),
            (11.0, *interpolate_along(coords, 20_800.0)),
        ]
        candidate = resolve(parse("km 12 carretera a masaya"), lookup(coords), anchors)[0]
        expected = interpolate_along(coords, 20_800.0 + 1_000.0)
        assert haversine_m(candidate.lat, candidate.lon, *expected) < 5.0


class TestSideOfRoad:
    """Nicaragua drives on the right, and km posts are quoted outbound from Managua."""

    @pytest.mark.parametrize(
        ("bearing_deg", "side", "expected_offset_bearing_deg"),
        [
            (0.0, "derecha", 90.0),  # heading north -> right is east
            (0.0, "izquierda", 270.0),
            (90.0, "derecha", 180.0),  # heading east -> right is south
            (90.0, "izquierda", 0.0),
            (180.0, "derecha", 270.0),
            (270.0, "izquierda", 180.0),
        ],
    )
    def test_offset_direction(self, bearing_deg, side, expected_offset_bearing_deg):
        coords = straight_line(1000.0, bearing_deg=bearing_deg)
        centre = interpolate_along(coords, 12_000.0)
        candidate = resolve(parse(f"km 12 carretera a masaya mano {side}"), lookup(coords))[0]
        offset_m = haversine_m(candidate.lat, candidate.lon, *centre)
        assert offset_m == pytest.approx(SIDE_OFFSET_M, abs=0.5)
        bearing = initial_bearing_deg(centre[0], centre[1], candidate.lat, candidate.lon)
        assert bearing == pytest.approx(expected_offset_bearing_deg, abs=1.0)

    def test_no_side_hint_stays_on_the_centreline(self):
        coords = straight_line(1000.0)
        candidate = resolve(parse("km 12 carretera a masaya"), lookup(coords))[0]
        assert haversine_m(candidate.lat, candidate.lon, *interpolate_along(coords, 12_000.0)) < 0.5

    def test_side_offset_is_configurable(self):
        coords = straight_line(1000.0, bearing_deg=0.0)
        centre = interpolate_along(coords, 12_000.0)
        candidate = resolve(
            parse("km 12 carretera a masaya mano derecha"), lookup(coords), side_offset_m=30.0
        )[0]
        assert haversine_m(candidate.lat, candidate.lon, *centre) == pytest.approx(30.0, abs=0.5)

    def test_side_is_reported_in_the_notes(self):
        coords = straight_line(1000.0)
        candidate = resolve(parse("km 12 carretera a masaya mano derecha"), lookup(coords))[0]
        assert any("Mano derecha" in note for note in candidate.notes)


class TestConfidence:
    @staticmethod
    def stretched():
        return straight_line(1080.0)

    def score(self, query, calibration=None):
        coords = self.stretched()
        return resolve(parse(query), lookup(coords), calibration)[0].confidence

    def test_ordering(self):
        coords = self.stretched()
        anchors = [(km, *interpolate_along(coords, km * 1080.0)) for km in (10, 20)]
        bracketed = self.score("km 15 carretera a masaya", anchors)
        extrapolated = self.score("km 26 carretera a masaya", anchors)
        uncalibrated = self.score("km 15 carretera a masaya")
        clamped = self.score("km 40 carretera a masaya")
        assert bracketed > extrapolated > uncalibrated > clamped

    def test_a_km_post_never_claims_certainty(self):
        coords = self.stretched()
        anchors = [(km, *interpolate_along(coords, km * 1080.0)) for km in (14, 15)]
        # The best possible case: mojones one kilometre either side of the query.
        assert self.score("km 14.5 carretera a masaya", anchors) <= 0.9

    def test_a_wide_bracket_scores_lower_than_a_narrow_one(self):
        coords = self.stretched()
        narrow = [(km, *interpolate_along(coords, km * 1080.0)) for km in (14, 16)]
        wide = [(km, *interpolate_along(coords, km * 1080.0)) for km in (2, 28)]
        assert self.score("km 15 carretera a masaya", narrow) > self.score(
            "km 15 carretera a masaya", wide
        )

    def test_extrapolating_further_scores_lower(self):
        coords = self.stretched()
        anchors = [(km, *interpolate_along(coords, km * 1080.0)) for km in (5, 10)]
        near = self.score("km 12 carretera a masaya", anchors)
        far = self.score("km 26 carretera a masaya", anchors)
        assert near > far

    def test_unverified_origin_costs_confidence(self):
        # A line that starts nowhere near the row's km 0: the chainage origin is a guess.
        far_away = straight_line(1000.0, start=(11.0, -85.0))
        anchored = resolve(parse("km 12 carretera a masaya"), lookup(straight_line(1000.0)))[0]
        floating = resolve(parse("km 12 carretera a masaya"), lookup(far_away))[0]
        assert floating.confidence < anchored.confidence
        assert any("Origen del kilometraje sin verificar" in n for n in floating.notes)

    def test_pista_without_signed_chainage_scores_low(self):
        coords = straight_line(1000.0, segments=8)
        candidate = resolve(parse("km 3 pista juan pablo ii"), lookup(coords))[0]
        assert candidate.confidence < 0.55

    def test_highway_outside_the_registry_still_resolves_with_an_injected_row(self):
        # The API can hand in a row the CSV does not carry yet (a new carretera in the
        # highway table); it resolves, but scores below a registered one.
        coords = straight_line(1000.0)
        injected = Highway(
            highway_key="carretera_a_masaya",
            display_name="Carretera de Prueba",
            aliases=("carretera de prueba",),
            osm_ref=None,
            km0_lat=None,
            km0_lon=None,
            note="",
            row_order=0,
        )
        candidate = resolve(parse("km 12 carretera a masaya"), lookup(coords), highway=injected)[0]
        assert candidate.label == "Km 12 Carretera de Prueba"
        assert candidate.confidence < 0.55


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


class TestCli:
    def test_list_highways(self, capsys):
        assert main(["--list-highways"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert {row["highway_key"] for row in payload} >= {"carretera_a_masaya", "carretera_norte"}

    def test_parse_only(self, capsys):
        assert main(["Km 12.5 Carretera a Masaya"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["parsed"]["km"] == 12.5
        assert payload["parsed"]["highway_key"] == "carretera_a_masaya"
        assert payload["candidates"] == []

    def test_resolve_with_geometry(self, tmp_path, capsys):
        coords = straight_line(1000.0)
        geometry = tmp_path / "masaya.geojson"
        geometry.write_text(
            json.dumps(
                {
                    "type": "Feature",
                    "properties": {},
                    "geometry": {"type": "LineString", "coordinates": coords},
                }
            ),
            encoding="utf-8",
        )
        calibration = tmp_path / "mojones.json"
        calibration.write_text(
            json.dumps([[10, *interpolate_along(coords, 10_000.0)]]), encoding="utf-8"
        )
        code = main(
            [
                "Km 12.5 Carretera a Masaya",
                "--geometry",
                str(geometry),
                "--calibration",
                str(calibration),
            ]
        )
        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        assert len(payload["candidates"]) == 1
        candidate = payload["candidates"][0]
        assert (
            haversine_m(candidate["lat"], candidate["lon"], *interpolate_along(coords, 12_500.0))
            < 5.0
        )
        assert candidate["method"] == "kmpost"

    def test_not_a_kmpost_exits_non_zero(self):
        assert main(["supermercado la colonia"]) == 1

    def test_no_arguments_exits_non_zero(self):
        assert main([]) == 2

    def test_missing_geometry_file_exits_non_zero(self, tmp_path):
        assert main(["Km 5 carretera norte", "--geometry", str(tmp_path / "nope.geojson")]) == 2
