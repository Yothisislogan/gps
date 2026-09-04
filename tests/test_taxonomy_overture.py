"""Overture category matching.

Separate from tests/test_taxonomy.py because it pins one specific, easily
regressed fact: Overture's *data* carries a bare leaf token, not the dotted path
the taxonomy CSV lists. Overture's schema forbids dots in a category
(``^[a-z0-9]+(_[a-z0-9]+)*$``), so a lookup that only understands dotted paths
sends every real POI to ``otro`` — silently, because "otro" is a valid answer.
"""

from __future__ import annotations

import pytest

from pipeline.pois.taxonomy import all_categories, category_for_overture


class TestBareLeafTokens:
    @pytest.mark.parametrize(
        ("overture", "expected"),
        [
            ("restaurant", "restaurante"),
            ("fast_food", "comida_rapida"),
            ("cafe", "cafe"),
            ("bar", "bar"),
            ("pharmacy", "farmacia"),
            ("hotel", "hotel"),
            ("supermarket", "supermercado"),
            ("atm", "cajero"),
            ("bank", "banco"),
            ("hospital", "hospital"),
            ("gas_station", "gasolinera"),
            ("pizza_restaurant", "pizzeria"),
        ],
    )
    def test_bare_token_resolves(self, overture: str, expected: str):
        assert category_for_overture(overture) == expected

    def test_dotted_path_still_resolves(self):
        # The CSV's own form must keep working: it is what the parent fallback
        # walks, and what the tests for the hierarchy rely on.
        assert category_for_overture("eat_and_drink.restaurant") == "restaurante"

    def test_unknown_leaf_falls_back_to_its_parent(self):
        assert category_for_overture("eat_and_drink.restaurant.neapolitan_pizza") in {
            "restaurante",
            "pizzeria",
        }

    @pytest.mark.parametrize("value", [None, "", "   ", "no_such_category_anywhere"])
    def test_unknown_returns_none(self, value: str | None):
        assert category_for_overture(value) is None

    def test_case_and_whitespace_tolerant(self):
        assert category_for_overture("  RESTAURANT  ") == "restaurante"


class TestLeafIndexIntegrity:
    def test_every_declared_overture_leaf_resolves(self):
        # If a CSV row lists a category, looking that category up must return
        # that row — otherwise the mapping is decorative.
        for category in all_categories():
            for overture in category.overture_categories:
                leaf = overture.rsplit(".", 1)[-1]
                assert category_for_overture(leaf) is not None, f"{leaf} resolves to nothing"

    def test_leaf_collisions_prefer_the_more_general_row(self):
        # "cafe" is listed both alone and under a sub-path; the general row wins.
        assert category_for_overture("cafe") == "cafe"
