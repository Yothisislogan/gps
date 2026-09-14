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
            ("fast_food_restaurant", "comida_rapida"),
            ("cafe", "cafe"),
            ("bar", "bar"),
            ("pharmacy", "farmacia"),
            ("hotel", "hotel"),
            ("grocery_store", "supermercado"),
            ("atms", "cajero"),
            ("bank_credit_union", "banco"),
            ("hospital", "hospital"),
            ("gas_station", "gasolinera"),
            ("pizza_restaurant", "pizzeria"),
        ],
    )
    def test_bare_token_resolves(self, overture: str, expected: str):
        assert category_for_overture(overture) == expected

    def test_the_column_holds_bare_tokens_not_dotted_paths(self):
        # The regression this file exists for: the column used to hold dotted
        # paths that matched nothing, sending 70% of Nicaraguan places to `otro`.
        from pipeline.pois.taxonomy import all_categories

        for category in all_categories():
            for token in category.overture_categories:
                assert "." not in token, (
                    f"{category.category_id} still lists a dotted path: {token}"
                )

    def test_a_dotted_path_resolves_by_its_leaf(self):
        # Belt and braces: the column holds bare tokens now, but a caller that
        # passes a hierarchy path must not silently get `otro`.
        assert category_for_overture("eat_and_drink.restaurant") == "restaurante"
        assert category_for_overture("anything.at.all.pizza_restaurant") == "pizzeria"

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
