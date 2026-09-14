"""Tests for the POI taxonomy loader and the OSM/Overture category mappings.

The shipped ``docs/taxonomy.csv`` is treated as production data here: if it stops matching
docs/SPEC.md section 7 the nightly build publishes a POI layer with blank icons, so these
assertions are deliberately strict.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.pois import taxonomy
from pipeline.pois.taxonomy import (
    CANONICAL_CATEGORY_IDS,
    GROUPS,
    TAXONOMY_PATH,
    all_categories,
    category_for_osm,
    category_for_overture,
    group_of,
    load_taxonomy,
    min_zoom_for,
)

HEADER = "category_id,group,label_es,label_en,icon,osm_selectors,overture_categories,min_zoom"
GOOD_ROW = "restaurante,comida,Restaurante,Restaurant,restaurante,amenity=restaurant,eat_and_drink.restaurant,15"


def write_csv(tmp_path: Path, *rows: str, header: str = HEADER, name: str = "t.csv") -> Path:
    """Write a taxonomy CSV that is *not* the shipped one, so canonical coverage is not checked."""
    path = tmp_path / name
    path.write_text("\n".join(("# test fixture", header, *rows)) + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# The shipped file
# --------------------------------------------------------------------------- #


def test_shipped_taxonomy_matches_spec_section_7() -> None:
    loaded = load_taxonomy()
    assert set(loaded) == set(CANONICAL_CATEGORY_IDS)
    assert len(loaded) == len(CANONICAL_CATEGORY_IDS)


def test_shipped_taxonomy_is_the_documented_path() -> None:
    assert TAXONOMY_PATH.name == "taxonomy.csv"
    assert TAXONOMY_PATH.parent.name == "docs"
    assert TAXONOMY_PATH.is_file()


def test_every_group_is_a_spec_group_and_every_group_is_used() -> None:
    used = {category.group for category in all_categories()}
    assert used <= set(GROUPS)
    assert used == set(GROUPS), "a group heading with no categories means a row was lost"


def test_every_row_has_labels_and_an_icon() -> None:
    for category in all_categories():
        assert category.label_es.strip()
        assert category.label_en.strip()
        assert category.icon.strip()


def test_nicaraguan_labels_are_the_local_words() -> None:
    loaded = load_taxonomy()
    assert loaded["fritanga"].label_es == "Fritanga"
    assert loaded["pulperia"].label_es == "Pulpería"
    assert loaded["vulcanizacion"].label_es == "Vulcanización"
    assert loaded["gasolinera"].label_es == "Gasolinera"
    assert loaded["cajero"].label_es == "Cajero automático"


def test_row_order_is_preserved_and_unique() -> None:
    categories = all_categories()
    assert [category.row_order for category in categories] == list(range(len(categories)))


def test_min_zoom_bands_landmarks_low_everyday_shops_high() -> None:
    # Landmarks and fuel have to be visible while driving; a pulpería at z12 would be noise.
    for landmark in ("gasolinera", "hospital", "centro_comercial", "aeropuerto", "volcan"):
        assert min_zoom_for(landmark) <= 13, landmark
    for everyday in ("pulperia", "salon_belleza", "lavanderia", "libreria", "panaderia"):
        assert min_zoom_for(everyday) >= 15, everyday


def test_group_and_min_zoom_lookups() -> None:
    assert group_of("fritanga") == "comida"
    assert group_of("vulcanizacion") == "auto"
    assert group_of("otro") == "otro"
    assert isinstance(min_zoom_for("volcan"), int)


def test_unknown_category_raises_key_error() -> None:
    with pytest.raises(KeyError):
        group_of("no_existe")
    with pytest.raises(KeyError):
        min_zoom_for("no_existe")


def test_load_taxonomy_is_cached_and_clearable() -> None:
    first = load_taxonomy()
    assert load_taxonomy() == first
    load_taxonomy.cache_clear()
    assert load_taxonomy() == first


# --------------------------------------------------------------------------- #
# Validation failures — a broken taxonomy must stop the nightly build
# --------------------------------------------------------------------------- #


def test_duplicate_id_names_the_row(tmp_path: Path) -> None:
    path = write_csv(tmp_path, GOOD_ROW, GOOD_ROW)
    with pytest.raises(ValueError, match=r"row 3 \(restaurante\).*duplicate id"):
        load_taxonomy(path)


def test_unknown_group_names_the_row(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path,
        "restaurante,comidas,Restaurante,Restaurant,restaurante,amenity=restaurant,,15",
    )
    with pytest.raises(ValueError, match=r"row 2 \(restaurante\).*unknown group 'comidas'"):
        load_taxonomy(path)


@pytest.mark.parametrize(
    ("row", "message"),
    [
        ("bar,comida,,Bar,bar,amenity=bar,,15", "label_es is empty"),
        ("bar,comida,Bar,,bar,amenity=bar,,15", "label_en is empty"),
        ("bar,comida,Bar,Bar,,amenity=bar,,15", "icon"),
        ("bar,comida,Bar,Bar,Bar_Icon,amenity=bar,,15", "kebab-case"),
        ("bar,comida,Bar,Bar,bar,amenity=bar,,quince", "not an integer"),
        ("bar,comida,Bar,Bar,bar,amenity=bar,,99", "outside"),
        ("bar,comida,Bar,Bar,bar,amenity=bar,,-1", "outside"),
        ("bar,comida,Bar,Bar,bar,amenity,,15", "not key=value"),
        ("bar,comida,Bar,Bar,bar,amenity=bar;amenity=bar,,15", "repeated"),
        ("bar,comida,Bar,Bar,bar,amenity=bar,Eat And Drink,15", "dotted snake_case"),
        ("Bar,comida,Bar,Bar,bar,amenity=bar,,15", "category_id must match"),
        (",comida,Bar,Bar,bar,amenity=bar,,15", "category_id must match"),
    ],
)
def test_bad_row_raises_value_error_naming_the_row(tmp_path: Path, row: str, message: str) -> None:
    path = write_csv(tmp_path, row, name=f"{abs(hash(row))}.csv")
    with pytest.raises(ValueError, match=r"row 2"):
        load_taxonomy(path)
    with pytest.raises(ValueError, match=message):
        load_taxonomy(path)


def test_wrong_header_is_rejected(tmp_path: Path) -> None:
    path = write_csv(tmp_path, GOOD_ROW, header="id,group,label,icon")
    with pytest.raises(ValueError, match="header"):
        load_taxonomy(path)


def test_empty_file_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "empty.csv"
    path.write_text("# only a comment\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no rows"):
        load_taxonomy(path)


def test_missing_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cannot read taxonomy"):
        load_taxonomy(tmp_path / "nope.csv")


def test_canonical_coverage_is_only_enforced_on_the_shipped_file(tmp_path: Path) -> None:
    # A two-row experiment file loads fine; the shipped one must be complete.
    path = write_csv(tmp_path, GOOD_ROW)
    assert set(load_taxonomy(path)) == {"restaurante"}
    with pytest.raises(ValueError, match=r"does not match docs/SPEC\.md section 7"):
        taxonomy._build(path, require_canonical=True)


def test_comment_lines_are_ignored(tmp_path: Path) -> None:
    path = tmp_path / "commented.csv"
    path.write_text(
        "\n".join(
            (
                "# header comment",
                "#",
                HEADER,
                GOOD_ROW,
                "   # indented comment",
                "",
                "bar,comida,Bar,Bar,bar,amenity=bar,,15",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    assert set(load_taxonomy(path)) == {"restaurante", "bar"}


# --------------------------------------------------------------------------- #
# OSM mapping
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        ({"amenity": "restaurant"}, "restaurante"),
        ({"amenity": "fuel"}, "gasolinera"),
        ({"shop": "convenience"}, "pulperia"),
        ({"shop": "tyres"}, "vulcanizacion"),
        ({"amenity": "bank"}, "banco"),
        ({"amenity": "atm"}, "cajero"),
        ({"amenity": "pharmacy"}, "farmacia"),
        ({"amenity": "marketplace"}, "mercado"),
        ({"tourism": "hotel"}, "hotel"),
        ({"natural": "volcano"}, "volcan"),
        ({"junction": "roundabout"}, "rotonda"),
        ({"highway": "bus_stop"}, "parada_bus"),
        ({"aeroway": "aerodrome"}, "aeropuerto"),
        ({"amenity": "place_of_worship", "religion": "christian"}, "iglesia"),
    ],
)
def test_single_tag_mapping(tags: dict[str, str], expected: str) -> None:
    assert category_for_osm(tags) == expected


@pytest.mark.parametrize(
    "tags",
    [
        {},
        {"highway": "milestone", "distance": "12"},  # km posts feed the km-post geocoder
        {"highway": "residential", "name": "Pista Juan Pablo II"},
        {"building": "yes", "addr:city": "Managua"},
        {"barrier": "gate"},
        {"amenity": "restaurante"},  # Spanish value: not an OSM tag
    ],
)
def test_non_poi_tags_map_to_nothing(tags: dict[str, str]) -> None:
    assert category_for_osm(tags) is None


def test_fuel_station_with_a_shop_is_a_gasolinera() -> None:
    # Every Puma/Uno forecourt in Managua has a mini-market tagged on the same node.
    assert (
        category_for_osm({"amenity": "fuel", "shop": "convenience", "brand": "Puma"})
        == "gasolinera"
    )


def test_hotel_with_a_restaurant_is_a_hotel() -> None:
    assert category_for_osm({"tourism": "hotel", "amenity": "restaurant"}) == "hotel"


def test_market_tagged_as_an_attraction_stays_a_market() -> None:
    # Mercado Oriental is an attraction, but people navigate to it as a market.
    assert category_for_osm({"amenity": "marketplace", "tourism": "attraction"}) == "mercado"


def test_cuisine_refines_the_shared_food_amenities() -> None:
    assert category_for_osm({"amenity": "fast_food"}) == "comida_rapida"
    assert category_for_osm({"amenity": "fast_food", "cuisine": "nicaraguan"}) == "fritanga"
    assert category_for_osm({"amenity": "restaurant", "cuisine": "pizza"}) == "pizzeria"
    assert category_for_osm({"amenity": "restaurant", "cuisine": "buffet"}) == "buffet"


def test_shared_selector_falls_to_row_order_and_the_unique_one_wins() -> None:
    # shop=car_repair is claimed by taller_mecanico and vulcanizacion; shop=tyres only by
    # vulcanizacion, so a tyre shop that also does mechanics is still a vulcanización.
    assert category_for_osm({"shop": "car_repair"}) == "taller_mecanico"
    assert category_for_osm({"shop": "tyres", "craft": "car_repair"}) == "vulcanizacion"


def test_love_hotel_is_a_hotel_de_paso_not_a_hotel() -> None:
    assert category_for_osm({"tourism": "love_hotel"}) == "hotel_paso"


def test_lifecycle_prefixed_tags_are_not_pois() -> None:
    assert category_for_osm({"disused:amenity": "restaurant", "name": "El Zaguán"}) is None
    assert category_for_osm({"was:shop": "convenience"}) is None
    assert category_for_osm({"demolished:amenity": "cinema"}) is None


def test_lifecycle_prefix_does_not_hide_a_live_tag() -> None:
    assert category_for_osm({"disused:amenity": "cinema", "shop": "convenience"}) == "pulperia"


def test_matching_is_independent_of_tag_order() -> None:
    tags = {
        "amenity": "fuel",
        "shop": "convenience",
        "tourism": "attraction",
        "building": "yes",
        "name": "Puma La Subasta",
    }
    forward = category_for_osm(tags)
    backward = category_for_osm(dict(reversed(list(tags.items()))))
    assert forward == backward == "gasolinera"


def test_non_string_tag_values_are_ignored() -> None:
    assert category_for_osm({"amenity": "bar", "level": 1, 2: "x"}) == "bar"  # type: ignore[dict-item]


def test_tag_values_are_whitespace_tolerant() -> None:
    assert category_for_osm({" amenity ": " restaurant "}) == "restaurante"


# --------------------------------------------------------------------------- #
# Overture mapping
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("category", "expected"),
    [
        # Verified against the 2026-08-19.0 release: these are the tokens the
        # data actually carries. Overture's schema forbids dots in a category,
        # so the dotted paths this list used to hold matched nothing.
        ("restaurant", "restaurante"),
        ("bakery", "panaderia"),
        ("tire_dealer_and_repair", "vulcanizacion"),
        ("atms", "cajero"),
        ("pharmacy", "farmacia"),
        ("pizza_restaurant", "pizzeria"),
        ("grocery_store", "supermercado"),
    ],
)
def test_overture_exact_mapping(category: str, expected: str) -> None:
    assert category_for_overture(category) == expected


def test_overture_dotted_input_still_falls_back_to_its_leaf() -> None:
    # The CSV now holds bare leaf tokens, because that is what the data carries.
    # The dotted handling is kept for defence: if a caller ever passes a
    # hierarchy path (the docs render one with dots), the leaf must still win
    # rather than the whole branch dropping on the floor.
    assert category_for_overture("eat_and_drink.restaurant") == "restaurante"
    assert category_for_overture("food_and_drink.casual_eatery.pizza_restaurant") == "pizzeria"


def test_overture_is_case_and_whitespace_insensitive() -> None:
    assert category_for_overture("  EAT_AND_DRINK.Restaurant  ") == "restaurante"


@pytest.mark.parametrize("value", [None, "", "   ", "no_such_root", "no_such_root.child"])
def test_overture_unknown_maps_to_nothing(value: str | None) -> None:
    assert category_for_overture(value) is None


def test_overture_strings_are_unique_across_rows() -> None:
    seen: dict[str, str] = {}
    for category in all_categories():
        for overture in category.overture_categories:
            assert overture not in seen, (
                f"{overture} claimed by {seen.get(overture)} and {category.category_id}"
            )
            seen[overture] = category.category_id
