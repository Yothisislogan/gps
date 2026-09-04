"""Canonical POI taxonomy: the one place OSM tags and Overture categories become nicanav ids.

``docs/taxonomy.csv`` is the data; this module is the contract around it.  Everything that
labels a point — the nightly conflation, the tippecanoe export, the Meilisearch indexer, the
sprite sheet and the map style — reads its category ids from here, so a typo in the CSV has to
fail the nightly build rather than silently publish a layer with no icons.

Two mappings matter:

* :func:`category_for_osm` turns a bag of OSM tags into exactly one category id.  Real OSM
  nodes carry several classifying tags at once (a Puma station is ``amenity=fuel`` *and*
  ``shop=convenience``; a hotel with a dining room is ``tourism=hotel`` *and*
  ``amenity=restaurant``), so the choice is made by a documented, total precedence order.  The
  same node must always land in the same category or the tile diff between two nightly builds
  becomes noise.
* :func:`category_for_overture` turns an Overture Places dotted category into the same ids,
  walking up the dotted hierarchy so an unseen leaf still lands in the right group.

Nicaraguan reality drives several of the mappings; see the comment header of the CSV.
"""

from __future__ import annotations

import csv
import functools
import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "CANONICAL_CATEGORY_IDS",
    "FALLBACK_CATEGORY",
    "GROUPS",
    "TAXONOMY_PATH",
    "Category",
    "all_categories",
    "category_for_osm",
    "category_for_overture",
    "group_of",
    "load_taxonomy",
    "min_zoom_for",
]

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
TAXONOMY_PATH = REPO_ROOT / "docs" / "taxonomy.csv"

# UNVERIFIED: the ``overture_categories`` column of docs/taxonomy.csv was written from the
# published *shape* of the Overture Places taxonomy; the authoritative category list ships
# with each Overture release and is unreachable from this build environment.  On the first
# ingest, cross-check every string in that column against the release CSV and fail the job on
# any that does not exist — a silently unmatched category sends real POIs to `otro`.
# UNVERIFIED: a handful of OSM selectors are plausible but unconfirmed against live taginfo
# counts for Nicaragua: ``water=lagoon`` (lagunas may be plain ``water=lake``),
# ``cuisine=nicaraguan`` (fritangas may be untagged for cuisine), ``shop=money_transfer``
# and ``landuse=port``.  Check the extract with ``osmium tags-filter`` on the first nightly
# run and add whatever the local mappers actually use.

#: Group headings, in the order docs/SPEC.md section 7 lists them.  A row with any other
#: group is a typo, not a new group: adding one means changing SPEC, the style and the sprite
#: sheet together.
GROUPS: tuple[str, ...] = (
    "comida",
    "compras",
    "auto",
    "dinero",
    "salud",
    "turismo",
    "servicios",
    "transporte",
    "referencia",
    "otro",
)

#: The category everything unclassifiable becomes.  Never matched from a tag; assigned by the
#: ingest jobs so ``poi.category`` is always a valid id.
FALLBACK_CATEGORY = "otro"

#: Every canonical id from docs/SPEC.md section 7, in SPEC's order.  The shipped CSV is
#: checked against this set on load: a missing id would drop a whole class of POIs off the
#: map, an extra one would render as a blank icon.
CANONICAL_CATEGORY_IDS: frozenset[str] = frozenset(
    (
        # comida
        "restaurante",
        "fritanga",
        "comedor",
        "buffet",
        "cafetin",
        "cafe",
        "bar",
        "discoteca",
        "heladeria",
        "panaderia",
        "reposteria",
        "pizzeria",
        "comida_rapida",
        # compras
        "pulperia",
        "supermercado",
        "mercado",
        "tienda",
        "ferreteria",
        "distribuidora",
        "centro_comercial",
        "libreria",
        # auto
        "gasolinera",
        "vulcanizacion",
        "taller_mecanico",
        "lavado_autos",
        "repuestos",
        "parqueo",
        # dinero
        "banco",
        "cajero",
        "casa_de_cambio",
        "remesas",
        # salud
        "hospital",
        "clinica",
        "farmacia",
        "dentista",
        "veterinaria",
        # turismo
        "hotel",
        "hostal",
        "playa",
        "mirador",
        "volcan",
        "laguna",
        "museo",
        "iglesia",
        "parque",
        "sitio_turistico",
        # servicios
        "policia",
        "bomberos",
        "correo",
        "embajada",
        "universidad",
        "escuela",
        "gimnasio",
        "salon_belleza",
        "lavanderia",
        "hotel_paso",
        # transporte
        "terminal_buses",
        "parada_bus",
        "aeropuerto",
        "puerto",
        "taxi",
        # referencia
        "rotonda",
        "semaforo",
        "puente",
        "monumento",
        "estadio",
        "cementerio",
        # otro
        "otro",
    )
)

_COLUMNS: tuple[str, ...] = (
    "category_id",
    "group",
    "label_es",
    "label_en",
    "icon",
    "osm_selectors",
    "overture_categories",
    "min_zoom",
)

_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_ICON_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SELECTOR_RE = re.compile(r"^[a-z][a-z0-9_:]*=[A-Za-z0-9_:./+-]+$")
_OVERTURE_RE = re.compile(r"^[a-z0-9_]+(?:\.[a-z0-9_]+)*$")

#: Vector-tile zooms.  z22 is MapLibre's overzoom ceiling; a min_zoom above the tile max would
#: hide the icon forever.
_MIN_ZOOM_RANGE = (0, 22)

# --------------------------------------------------------------------------- #
# OSM matching precedence
# --------------------------------------------------------------------------- #

#: Tag keys ordered by how much *identity* they carry, most first.  Two deliberate choices:
#:
#: * ``cuisine`` is first because it is the only tag that separates the Nicaraguan sub-types
#:   from the generic amenity they share: ``amenity=fast_food`` + ``cuisine=nicaraguan`` is a
#:   fritanga, ``amenity=restaurant`` + ``cuisine=pizza`` a pizzería.
#: * ``tourism`` sits above ``amenity`` because "hotel with a restaurant" is common in
#:   Nicaragua and the hotel is what people navigate to.  The reverse mistake — a restaurant
#:   swallowed by ``tourism=attraction`` — is prevented by :data:`_WEAK_SELECTORS`.
_KEY_PRIORITY: tuple[str, ...] = (
    "cuisine",
    "aeroway",
    "natural",
    "water",
    "tourism",
    "historic",
    "healthcare",
    "amenity",
    "shop",
    "craft",
    "office",
    "leisure",
    "man_made",
    "junction",
    "public_transport",
    "highway",
    "landuse",
    "building",
)
_KEY_RANK: dict[str, int] = {key: rank for rank, key in enumerate(_KEY_PRIORITY)}

#: Selectors that describe a *quality* rather than an identity, and therefore lose to every
#: other match.  Mercado Oriental is tagged ``amenity=marketplace`` + ``tourism=attraction``:
#: it is a market that happens to be an attraction, and it belongs in `mercado`.
_WEAK_SELECTORS: frozenset[tuple[str, str]] = frozenset(
    {
        ("tourism", "attraction"),
        ("building", "church"),
        ("building", "stadium"),
        ("landuse", "port"),
        ("landuse", "cemetery"),
        ("public_transport", "station"),
        ("public_transport", "platform"),
    }
)

#: Lifecycle prefixes.  ``disused:amenity=restaurant`` is a dead restaurant: it is a landmark
#: ("donde fue el…"), not a place you can eat at, and the gazetteer builder wants it instead.
_LIFECYCLE_PREFIXES: tuple[str, ...] = (
    "disused:",
    "was:",
    "abandoned:",
    "demolished:",
    "removed:",
    "razed:",
    "proposed:",
    "planned:",
    "construction:",
)


@dataclass(frozen=True, slots=True)
class Category:
    """One row of ``docs/taxonomy.csv``.

    ``row_order`` is the zero-based position of the row in the file and is load-bearing: it is
    the final tie-break when two categories claim the same OSM selector, which several
    Nicaraguan categories legitimately do (fritanga and comida_rapida both live under
    ``amenity=fast_food``).
    """

    category_id: str
    group: str
    label_es: str
    label_en: str
    icon: str
    osm_selectors: tuple[tuple[str, str], ...]
    overture_categories: tuple[str, ...]
    min_zoom: int
    row_order: int


@dataclass(frozen=True, slots=True)
class _Taxonomy:
    """Loaded taxonomy plus the lookup tables derived from it."""

    categories: tuple[Category, ...]
    by_id: Mapping[str, Category]
    #: ``(key, value)`` -> categories claiming it, in row order.
    by_selector: Mapping[tuple[str, str], tuple[Category, ...]]
    #: Overture dotted string -> category id (first claiming row wins).
    by_overture: Mapping[str, str]


def _fail(row_number: int, category_id: str, problem: str) -> ValueError:
    """Build the ValueError that stops the nightly build, naming the offending row."""
    return ValueError(
        f"{TAXONOMY_PATH.name} row {row_number} ({category_id or '<no id>'}): {problem}"
    )


def _parse_selectors(raw: str, *, row_number: int, category_id: str) -> tuple[tuple[str, str], ...]:
    selectors: list[tuple[str, str]] = []
    for chunk in raw.split(";"):
        selector = chunk.strip()
        if not selector:
            continue
        if not _SELECTOR_RE.match(selector):
            raise _fail(row_number, category_id, f"osm selector {selector!r} is not key=value")
        key, _, value = selector.partition("=")
        pair = (key, value)
        if pair in selectors:
            raise _fail(row_number, category_id, f"osm selector {selector!r} repeated")
        selectors.append(pair)
    return tuple(selectors)


def _parse_overture(raw: str, *, row_number: int, category_id: str) -> tuple[str, ...]:
    values: list[str] = []
    for chunk in raw.split(";"):
        value = chunk.strip().lower()
        if not value:
            continue
        if not _OVERTURE_RE.match(value):
            raise _fail(
                row_number,
                category_id,
                f"overture category {value!r} is not a dotted snake_case string",
            )
        if value not in values:
            values.append(value)
    return tuple(values)


def _read_rows(path: Path) -> list[dict[str, str]]:
    """Read the CSV, dropping ``#`` comment lines and blank lines."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:  # missing or unreadable taxonomy is fatal, not a warning
        raise ValueError(f"cannot read taxonomy {path}: {exc}") from exc
    lines = [
        line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")
    ]
    if not lines:
        raise ValueError(f"taxonomy {path} has no rows")
    reader = csv.DictReader(lines)
    header = tuple(reader.fieldnames or ())
    if header != _COLUMNS:
        raise ValueError(
            f"taxonomy {path} header is {header!r}; docs/SPEC.md section 7 requires {_COLUMNS!r}"
        )
    return list(reader)


def _build(path: Path, *, require_canonical: bool) -> _Taxonomy:
    categories: list[Category] = []
    seen: dict[str, int] = {}

    for index, row in enumerate(_read_rows(path)):
        # Row 1 is the header, so the first data row is line 2 as a human counts them.
        row_number = index + 2
        category_id = (row.get("category_id") or "").strip()
        if not _ID_RE.match(category_id):
            raise _fail(row_number, category_id, "category_id must match [a-z][a-z0-9_]*")
        if category_id in seen:
            raise _fail(
                row_number, category_id, f"duplicate id (first seen on row {seen[category_id]})"
            )
        seen[category_id] = row_number

        group = (row.get("group") or "").strip()
        if group not in GROUPS:
            raise _fail(
                row_number, category_id, f"unknown group {group!r}; expected one of {GROUPS}"
            )

        label_es = (row.get("label_es") or "").strip()
        label_en = (row.get("label_en") or "").strip()
        if not label_es:
            raise _fail(row_number, category_id, "label_es is empty")
        if not label_en:
            raise _fail(row_number, category_id, "label_en is empty")

        icon = (row.get("icon") or "").strip()
        if not _ICON_RE.match(icon):
            raise _fail(row_number, category_id, f"icon {icon!r} must be kebab-case sprite name")

        raw_zoom = (row.get("min_zoom") or "").strip()
        try:
            min_zoom = int(raw_zoom)
        except ValueError:
            raise _fail(
                row_number, category_id, f"min_zoom {raw_zoom!r} is not an integer"
            ) from None
        if not _MIN_ZOOM_RANGE[0] <= min_zoom <= _MIN_ZOOM_RANGE[1]:
            raise _fail(row_number, category_id, f"min_zoom {min_zoom} outside {_MIN_ZOOM_RANGE}")

        categories.append(
            Category(
                category_id=category_id,
                group=group,
                label_es=label_es,
                label_en=label_en,
                icon=icon,
                osm_selectors=_parse_selectors(
                    row.get("osm_selectors") or "", row_number=row_number, category_id=category_id
                ),
                overture_categories=_parse_overture(
                    row.get("overture_categories") or "",
                    row_number=row_number,
                    category_id=category_id,
                ),
                min_zoom=min_zoom,
                row_order=index,
            )
        )

    if require_canonical:
        _check_canonical(categories, path)

    by_selector: dict[tuple[str, str], list[Category]] = {}
    by_overture: dict[str, str] = {}
    for category in categories:
        for selector in category.osm_selectors:
            by_selector.setdefault(selector, []).append(category)
        for overture in category.overture_categories:
            # First row claiming a string wins, which is why row order is documented as
            # load-bearing in the CSV header.
            by_overture.setdefault(overture, category.category_id)

    return _Taxonomy(
        categories=tuple(categories),
        by_id={category.category_id: category for category in categories},
        by_selector={key: tuple(value) for key, value in by_selector.items()},
        by_overture=by_overture,
    )


def _check_canonical(categories: Iterable[Category], path: Path) -> None:
    ids = {category.category_id for category in categories}
    missing = sorted(CANONICAL_CATEGORY_IDS - ids)
    extra = sorted(ids - CANONICAL_CATEGORY_IDS)
    if missing or extra:
        raise ValueError(
            f"taxonomy {path} does not match docs/SPEC.md section 7: "
            f"missing={missing or '[]'} unexpected={extra or '[]'}"
        )


@functools.lru_cache(maxsize=8)
def _load(path: Path, require_canonical: bool) -> _Taxonomy:
    taxonomy = _build(path, require_canonical=require_canonical)
    log.debug("loaded %d categories from %s", len(taxonomy.categories), path)
    return taxonomy


def load_taxonomy(path: str | Path | None = None) -> dict[str, Category]:
    """Return ``{category_id: Category}``, parsed and validated, cached per path.

    The shipped ``docs/taxonomy.csv`` is additionally checked against
    :data:`CANONICAL_CATEGORY_IDS`; any other path (test fixtures, a tool experimenting with a
    variant file) is only checked structurally.  Raises :class:`ValueError` naming the offending
    row for a duplicate id, an unknown group, an empty label, a bad icon, a malformed selector
    or an out-of-range ``min_zoom`` — a broken taxonomy must stop the nightly build, not
    publish a layer with missing icons.

    Call ``load_taxonomy.cache_clear()`` after editing the CSV in a long-lived process.
    """
    resolved = Path(path) if path is not None else TAXONOMY_PATH
    return dict(_load(resolved, resolved == TAXONOMY_PATH).by_id)


load_taxonomy.cache_clear = _load.cache_clear  # type: ignore[attr-defined]


def all_categories(path: str | Path | None = None) -> tuple[Category, ...]:
    """Every category in CSV row order (which is the icon-drawing order for the style)."""
    resolved = Path(path) if path is not None else TAXONOMY_PATH
    return _load(resolved, resolved == TAXONOMY_PATH).categories


def group_of(category_id: str, path: str | Path | None = None) -> str:
    """Group heading for a category id.

    Raises :class:`KeyError` for an unknown id: callers normalise to
    :data:`FALLBACK_CATEGORY` *before* asking, so an unknown id here is a bug upstream.
    """
    resolved = Path(path) if path is not None else TAXONOMY_PATH
    return _load(resolved, resolved == TAXONOMY_PATH).by_id[category_id].group


def min_zoom_for(category_id: str, path: str | Path | None = None) -> int:
    """Zoom at which this category's icon first appears; raises ``KeyError`` if unknown."""
    resolved = Path(path) if path is not None else TAXONOMY_PATH
    return _load(resolved, resolved == TAXONOMY_PATH).by_id[category_id].min_zoom


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #


def _selector_rank(
    selector: tuple[str, str], category: Category, shared: bool
) -> tuple[int, int, int, int]:
    """Sort key for one matched selector; lower is better.

    Four levels, in order:

    1. **Weak selectors last.** ``tourism=attraction`` is a quality, not an identity.
    2. **Key priority** (:data:`_KEY_PRIORITY`), so ``cuisine`` refines and ``tourism`` beats
       ``amenity`` for hotels.  An unlisted key sorts after every listed one.
    3. **Unique before shared.** ``shop=tyres`` is claimed only by vulcanización and beats
       ``shop=car_repair``, which taller_mecanico also claims.
    4. **CSV row order**, which makes the result total: the same tag bag always yields the same
       category, across processes and across nightly builds.
    """
    key, _value = selector
    return (
        1 if selector in _WEAK_SELECTORS else 0,
        _KEY_RANK.get(key, len(_KEY_PRIORITY)),
        1 if shared else 0,
        category.row_order,
    )


def category_for_osm(tags: Mapping[str, str], path: str | Path | None = None) -> str | None:
    """Map an OSM tag bag to one category id, or ``None`` if nothing in it is a POI.

    Assumes raw OSM tags as ``osmium export`` emits them (keys and values unmodified).  Tags
    carrying a lifecycle prefix (``disused:amenity=…``) are ignored: a closed business is a
    former landmark for the relative-address gazetteer, not a place to route someone to.

    The choice among several matching tags is total and documented in :func:`_selector_rank`,
    so re-running the nightly build over unchanged data produces an unchanged tile set.
    """
    resolved = Path(path) if path is not None else TAXONOMY_PATH
    taxonomy = _load(resolved, resolved == TAXONOMY_PATH)

    best: tuple[tuple[int, int, int, int], str] | None = None
    for raw_key, raw_value in tags.items():
        if not isinstance(raw_key, str) or not isinstance(raw_value, str):
            continue
        key = raw_key.strip().lower()
        if key.startswith(_LIFECYCLE_PREFIXES):
            continue
        value = raw_value.strip()
        claimants = taxonomy.by_selector.get((key, value))
        if not claimants:
            continue
        shared = len(claimants) > 1
        for category in claimants:
            rank = _selector_rank((key, value), category, shared)
            if best is None or rank < best[0]:
                best = (rank, category.category_id)
    return best[1] if best else None


def category_for_overture(category: str | None, path: str | Path | None = None) -> str | None:
    """Map an Overture Places category string to a category id, or ``None``.

    Overture categories are dotted and hierarchical
    (``eat_and_drink.restaurant.pizza_restaurant``).  An unknown leaf falls back to its parent,
    so a new Overture release adding ``…pizza_restaurant.neapolitan`` still lands in
    ``pizzeria`` instead of dropping to `otro` — the monthly refresh must not silently lose a
    whole leaf of the tree.
    """
    if not category:
        return None
    resolved = Path(path) if path is not None else TAXONOMY_PATH
    taxonomy = _load(resolved, resolved == TAXONOMY_PATH)

    parts = category.strip().lower().split(".")
    while parts:
        hit = taxonomy.by_overture.get(".".join(parts))
        if hit is not None:
            return hit
        parts.pop()
    return None
