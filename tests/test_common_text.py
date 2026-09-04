"""Tests for the shared text normalisation used by the geocoder and conflator."""

from __future__ import annotations

import pytest

from common.text import (
    name_key,
    normalize,
    parse_spanish_number,
    strip_accents,
    strip_generic_prefix,
    tokenize,
)


class TestNormalisation:
    def test_strips_accents_including_enye(self):
        assert strip_accents("Güegüense") == "Gueguense"
        assert strip_accents("Niquinohomo Ñandaime") == "Niquinohomo Nandaime"

    def test_normalize_folds_case_accents_and_punctuation(self):
        assert normalize("Rotonda Güegüense, 2c. al Sur") == "rotonda gueguense 2c al sur"

    def test_normalize_is_idempotent(self):
        once = normalize("De la Iglesia El Calvario, 1½ c. abajo")
        assert normalize(once) == once

    def test_tokenize_drops_empties(self):
        assert tokenize("  Km 12.5   Carretera a Masaya ") == [
            "km",
            "12",
            "5",
            "carretera",
            "a",
            "masaya",
        ]


class TestBusinessNames:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Restaurante El Zaguán", "zaguan"),
            ("Bar y Restaurante El Zaguán", "zaguan"),
            ("Comedor Doña Pilar", "dona pilar"),
            ("Pulpería La Esquina", "esquina"),
        ],
    )
    def test_strip_generic_prefix(self, raw: str, expected: str):
        assert strip_generic_prefix(raw) == expected

    def test_generic_only_name_survives(self):
        # "Fritanga" on its own is the whole name; stripping to nothing would
        # make every unnamed fritanga collide with every other one.
        assert strip_generic_prefix("Fritanga") == "fritanga"
        assert name_key("Fritanga") == "fritanga"

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            ("Bar y Restaurante El Zaguán", "El Zaguan Restaurante"),
            ("Fritanga La Fe", "La Fe"),
            ("Hotel Colonial", "Colonial"),
            ("Farmacia Xolotlán", "XOLOTLAN farmacia"),
        ],
    )
    def test_name_key_collides_for_same_business(self, left: str, right: str):
        assert name_key(left) == name_key(right)

    def test_name_key_separates_different_businesses(self):
        assert name_key("Restaurante La Casona") != name_key("Restaurante El Zaguán")


class TestSpanishNumbers:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("2", 2.0),
            ("2.5", 2.5),
            ("12,5", 12.5),  # comma decimal, as written on Nicaraguan signage
            ("9½", 9.5),
            ("1 1/2", 1.5),
            ("1/2", 0.5),
            ("media", 0.5),
            ("medio", 0.5),
            ("una", 1.0),
            ("dos", 2.0),
            ("veinticinco", 25.0),
            ("treinta y cinco", 35.0),
            ("dos y media", 2.5),
            ("ciento cincuenta", 150.0),
            ("setenta y cinco", 75.0),
            ("mil", 1000.0),
            ("MEDIA", 0.5),  # case-insensitive
        ],
    )
    def test_parses(self, raw: str, expected: float):
        assert parse_spanish_number(raw) == pytest.approx(expected)

    @pytest.mark.parametrize("raw", ["cuadra", "", "   ", "al sur", "rotonda"])
    def test_rejects_non_numbers(self, raw: str):
        assert parse_spanish_number(raw) is None

    def test_stops_at_first_unknown_token(self):
        # "dos cuadras" is a quantity followed by a unit, not a two-word number.
        assert parse_spanish_number("dos cuadras") == pytest.approx(2.0)
