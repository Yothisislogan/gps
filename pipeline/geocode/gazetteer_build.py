"""Assemble the landmark gazetteer that the relative-address geocoder walks from.

Nicaraguan addresses have no street numbers: every one of them starts at a
landmark ("de la Rotonda El Güegüense, 2c al sur").  This job builds the table
those landmarks come from, out of three sources with different authority:

1. **OSM** (exported to GeoJSON-seq by ``osmium export``) — thousands of named
   rotondas, barrios, mercados, iglesias, colegios, gasolineras and malls, with
   hand-placed node positions that are usually better than anything we could
   type by hand.
2. **``docs/gazetteer_seed.csv``** — the curated list OSM cannot give us: the
   names people actually say, and the *ghost* landmarks ("donde fue el Cine
   Cabrera") that vanished in 1972 and are still the only way half of Managua
   gives directions.
3. **Approved user-submitted aliases** — what the app learns from the
   "¿qué punto de referencia usás?" prompt, after moderation.

The precedence rules follow from where each source is strong:

* the **seed wins on name, kind, ``former`` and ``era``** — it exists precisely
  because OSM's naming is not the spoken naming;
* **OSM wins on position** — its nodes were placed on imagery by someone who was
  looking at the thing, while the seed coordinates were typed from documentary
  knowledge and are only good to a few hundred metres;
* **unless the seed row is ``former``**, in which case OSM has no counterpart at
  all and any "match" is a coincidence, so the seed keeps its own point;
* user aliases never outrank either: they fold in as extra ``name_alt`` when
  they land on a known landmark, and only become rows of their own when they do
  not.

Outputs ``data/exports/gazetteer.geojson`` (atomically, via
:func:`pipeline.common.io.write_geojson`) and, with ``--upsert``, the PostGIS
``gazetteer`` table.  The psycopg import is guarded so the export path — and the
whole test-suite — runs with no database anywhere near it.

Run it with::

    python3 -m pipeline.geocode.gazetteer_build \\
        --osm data/osm/gazetteer_features.geojsonl \\
        --seed docs/gazetteer_seed.csv \\
        --out data/exports/gazetteer.geojson
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from common.geo import (
    NICARAGUA_BBOX,
    distance_from_mga_m,
    haversine_m,
    interpolate_along,
    line_length_m,
    local_projection,
)
from common.text import collapse_ws, name_key, normalize
from pipeline.common.io import read_geojsonseq, setup_logging, write_geojson

try:  # pragma: no cover - exercised only on a machine with a database
    import psycopg
except ImportError:  # pragma: no cover - the export path must work without one
    psycopg = None  # type: ignore[assignment]

__all__ = [
    "DEFAULT_MERGE_RADIUS_M",
    "DEFAULT_POPULARITY",
    "DEFAULT_SEED_PATH",
    "GAZETTEER_KINDS",
    "KIND_GROUPS",
    "GazetteerStats",
    "Landmark",
    "build_gazetteer",
    "default_popularity",
    "export_geojson",
    "geometry_centroid",
    "kind_from_tags",
    "landmark_from_osm_feature",
    "load_aliases",
    "load_osm",
    "load_seed",
    "main",
    "merge_landmarks",
    "summarize",
    "upsert_postgis",
]

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SEED_PATH = REPO_ROOT / "docs" / "gazetteer_seed.csv"

#: Two landmarks with the same name key merge when they are closer than this.
#: 500 m is deliberately generous: the seed coordinates are only good to a few
#: hundred metres, so a tighter radius would leave a seed row and its OSM twin
#: side by side and the geocoder would offer both as separate candidates.
DEFAULT_MERGE_RADIUS_M = 500.0

# --------------------------------------------------------------------------- #
# Kind vocabulary
# --------------------------------------------------------------------------- #

#: Canonical ``gazetteer.kind`` values.  ``db/migrations/001_init.sql`` documents
#: the core set in a comment (there is no CHECK constraint); this extends it with
#: the classes ``tests/conftest.py`` already uses (``centro_comercial``,
#: ``parque``) plus the few the OSM mapping below needs to stay honest.  Anything
#: outside this set is a typo, and :func:`load_seed` refuses it.
GAZETTEER_KINDS: frozenset[str] = frozenset(
    {
        # vial
        "rotonda",
        "semaforo",
        "puente",
        # comercio
        "mercado",
        "centro_comercial",
        "empresa",
        "gasolinera",
        "hotel",
        "banco",
        # civico
        "iglesia",
        "colegio",
        "universidad",
        "hospital",
        "edificio",
        "monumento",
        "plaza",
        "parque",
        "estadio",
        "terminal",
        "aeropuerto",
        "cementerio",
        # areas habitadas
        "barrio",
        "reparto",
        "colonia",
        "residencial",
        # natural
        "laguna",
        "volcan",
        "mirador",
        "playa",
        # comodines
        "referencia",
        "otro",
    }
)

#: Coarse groups used as a sanity guard when two records match only through an
#: *alternative* name.  "El Güegüense" is a rotonda, a theatre character and half
#: a dozen restaurants; without this guard an alias match would silently glue a
#: fritanga onto a roundabout.  ``referencia`` and ``otro`` are wildcards because
#: that is exactly what they mean.
KIND_GROUPS: dict[str, str] = {
    "rotonda": "vial",
    "semaforo": "vial",
    "puente": "vial",
    "mercado": "comercio",
    "centro_comercial": "comercio",
    "empresa": "comercio",
    "gasolinera": "comercio",
    "hotel": "comercio",
    "banco": "comercio",
    "iglesia": "civico",
    "colegio": "civico",
    "universidad": "civico",
    "hospital": "civico",
    "edificio": "civico",
    "monumento": "civico",
    "plaza": "civico",
    "parque": "civico",
    "estadio": "civico",
    "terminal": "civico",
    "aeropuerto": "civico",
    "cementerio": "civico",
    "barrio": "area",
    "reparto": "area",
    "colonia": "area",
    "residencial": "area",
    "laguna": "natural",
    "volcan": "natural",
    "mirador": "natural",
    "playa": "natural",
    "referencia": "comodin",
    "otro": "comodin",
}

#: Default ``popularity`` per kind: how often a landmark of this class turns up
#: as the anchor of a spoken address.  Rotondas and mercados are how Managua
#: describes itself; a colegio is usually only a reference to its own neighbours,
#: and OSM has hundreds of them, so they sit at the bottom.
DEFAULT_POPULARITY: dict[str, float] = {
    "rotonda": 0.85,
    "mercado": 0.8,
    "aeropuerto": 0.8,
    "centro_comercial": 0.75,
    "universidad": 0.65,
    "hospital": 0.65,
    "monumento": 0.6,
    "estadio": 0.6,
    "terminal": 0.55,
    "iglesia": 0.55,
    "semaforo": 0.55,
    "puente": 0.5,
    "barrio": 0.5,
    "reparto": 0.5,
    "colonia": 0.5,
    "residencial": 0.5,
    "referencia": 0.45,
    "parque": 0.45,
    "plaza": 0.45,
    "gasolinera": 0.4,
    "mirador": 0.4,
    "laguna": 0.4,
    "volcan": 0.4,
    "playa": 0.35,
    "hotel": 0.35,
    "empresa": 0.3,
    "edificio": 0.3,
    "banco": 0.3,
    "cementerio": 0.3,
    "colegio": 0.25,
    "otro": 0.2,
}


def default_popularity(kind: str, name: str = "") -> float:
    """Popularity for a landmark whose source did not state one.

    Nicaraguan speech is the yardstick, not fame: a rotonda nobody has heard of
    still gets used in addresses because it is on the way, whereas a famous
    museum almost never anchors one.
    """
    base = DEFAULT_POPULARITY.get(kind, 0.2)
    folded = normalize(name)
    if folded.startswith("rotonda"):
        base = max(base, DEFAULT_POPULARITY["rotonda"])
    elif folded.startswith("mercado"):
        base = max(base, DEFAULT_POPULARITY["mercado"])
    elif kind == "colegio" and folded.startswith(("escuela", "instituto")):
        # A state school named "Escuela Rubén Darío" exists in every barrio;
        # matching one of them by name is a coin toss, so keep it out of the way.
        base = min(base, 0.15)
    return round(base, 3)


# --------------------------------------------------------------------------- #
# The record
# --------------------------------------------------------------------------- #

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(*parts: str | None) -> str:
    """Stable, accent-folded id fragment: ``("Rotonda El Güegüense", "Managua")``
    -> ``"rotonda-el-gueguense-managua"``."""
    joined = " ".join(part for part in parts if part)
    return _SLUG_RE.sub("-", normalize(joined)).strip("-") or "sin-nombre"


@dataclass
class Landmark:
    """One gazetteer row, before or after merging.

    ``popularity`` is ``None`` when the source did not state one; it is filled in
    from :func:`default_popularity` at export time so that a merge can still tell
    "the seed deliberately said 0.4" apart from "nobody said anything".
    """

    name: str
    kind: str
    lat: float
    lon: float
    name_alt: list[str] = field(default_factory=list)
    city: str | None = None
    former: bool = False
    era: str | None = None
    popularity: float | None = None
    source: str = "osm"
    source_id: str | None = None
    note: str | None = None
    osm_id: str | None = None

    @property
    def primary_key(self) -> str:
        """Blocking key of the display name (see :func:`common.text.name_key`)."""
        return name_key(self.name)

    @property
    def match_keys(self) -> set[str]:
        """Blocking keys of the name *and* every alternative name."""
        keys = {self.primary_key}
        keys.update(name_key(alt) for alt in self.name_alt if alt.strip())
        return {key for key in keys if key}

    @property
    def id(self) -> str:
        """``"<source>:<source_id>"`` — what the search index keys landmarks on."""
        return f"{self.source}:{self.source_id or slugify(self.name, self.city)}"

    def resolved_popularity(self) -> float:
        """The stated popularity, or the default for the kind."""
        if self.popularity is None:
            return default_popularity(self.kind, self.name)
        return round(max(0.0, min(1.0, self.popularity)), 3)

    def to_feature(self) -> dict[str, Any]:
        """GeoJSON Feature with ``[lon, lat]`` coordinates, as the format demands."""
        properties: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "name_alt": list(self.name_alt),
            "kind": self.kind,
            "city": self.city,
            "former": self.former,
            "era": self.era,
            "popularity": self.resolved_popularity(),
            "source": self.source,
            "source_id": self.source_id,
        }
        if self.note:
            properties["note"] = self.note
        if self.osm_id:
            properties["osm_id"] = self.osm_id
        return {
            "type": "Feature",
            "id": self.id,
            "geometry": {
                "type": "Point",
                # 6 decimals is ~0.1 m: far finer than anything in this table is
                # known to, but it keeps round-trips through the file lossless.
                "coordinates": [round(self.lon, 6), round(self.lat, 6)],
            },
            "properties": properties,
        }


@dataclass
class GazetteerStats:
    """Counters for the build summary; every drop is accounted for."""

    seed_rows: int = 0
    osm_features: int = 0
    osm_used: int = 0
    osm_skipped: Counter[str] = field(default_factory=Counter)
    alias_rows: int = 0
    merged_duplicates: int = 0

    @property
    def osm_skipped_total(self) -> int:
        return sum(self.osm_skipped.values())


# --------------------------------------------------------------------------- #
# Geometry: everything becomes one point
# --------------------------------------------------------------------------- #


def _ring_centroid(ring: Sequence[Sequence[float]]) -> tuple[float, float] | None:
    """Area centroid of a ``[[lon, lat], ...]`` ring, as (lat, lon).

    Projected to a local metric frame first: the shoelace formula on raw degrees
    is biased because a degree of longitude is only 0.98 of a degree of latitude
    at Nicaraguan latitudes.  Degenerate rings (a roundabout mapped as a single
    doubled node) fall back to the mean of the vertices.
    """
    points = [(float(p[0]), float(p[1])) for p in ring if p is not None and len(p) >= 2]
    if not points:
        return None
    if len(points) < 3:
        lon = sum(p[0] for p in points) / len(points)
        lat = sum(p[1] for p in points) / len(points)
        return (lat, lon)

    to_xy, to_lonlat = local_projection(points[0][1], points[0][0])
    xy = [to_xy(lat, lon) for lon, lat in points]
    if xy[0] == xy[-1]:
        xy = xy[:-1]
    if len(xy) < 3:
        x = sum(p[0] for p in xy) / len(xy)
        y = sum(p[1] for p in xy) / len(xy)
        return to_lonlat(x, y)

    twice_area = 0.0
    cx = 0.0
    cy = 0.0
    for i in range(len(xy)):
        x0, y0 = xy[i]
        x1, y1 = xy[(i + 1) % len(xy)]
        cross = x0 * y1 - x1 * y0
        twice_area += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    if abs(twice_area) < 1e-9:  # collinear vertices: no area to take a centroid of
        x = sum(p[0] for p in xy) / len(xy)
        y = sum(p[1] for p in xy) / len(xy)
        return to_lonlat(x, y)
    area = twice_area / 2.0
    return to_lonlat(cx / (6.0 * area), cy / (6.0 * area))


def _line_centroid(coords: Sequence[Sequence[float]]) -> tuple[float, float] | None:
    """Representative point of a ``[[lon, lat], ...]`` line, as (lat, lon).

    A closed way is a roundabout or a building outline, so its area centroid is
    the point people mean.  An open way (a named calle, a bridge) has no inside:
    the midpoint along its length is the honest answer.
    """
    points = [p for p in coords if p is not None and len(p) >= 2]
    if not points:
        return None
    if len(points) == 1:
        return (float(points[0][1]), float(points[0][0]))
    closed = points[0][0] == points[-1][0] and points[0][1] == points[-1][1]
    if closed:
        return _ring_centroid(points)
    return interpolate_along(points, line_length_m(points) / 2.0)


def geometry_centroid(geometry: Mapping[str, Any] | None) -> tuple[float, float] | None:
    """Reduce any GeoJSON geometry to a representative ``(lat, lon)``.

    OSM gives landmarks as nodes, closed ways (rotondas, mercados, manzanas) and
    relations; the gazetteer stores a single point, because an address is walked
    *from* a point.  Returns ``None`` for an empty or unsupported geometry rather
    than raising: a bad geometry in a 200 000-feature export must not kill a
    nightly build.
    """
    if not geometry:
        return None
    kind = geometry.get("type")
    coords = geometry.get("coordinates")
    if kind == "Point":
        if not coords or len(coords) < 2:
            return None
        return (float(coords[1]), float(coords[0]))
    if kind == "MultiPoint":
        points = [p for p in (coords or []) if p and len(p) >= 2]
        if not points:
            return None
        return (
            sum(float(p[1]) for p in points) / len(points),
            sum(float(p[0]) for p in points) / len(points),
        )
    if kind == "LineString":
        return _line_centroid(coords or [])
    if kind == "MultiLineString":
        lines = [line for line in (coords or []) if line]
        if not lines:
            return None
        return _line_centroid(max(lines, key=lambda line: line_length_m(line)))
    if kind == "Polygon":
        rings = coords or []
        return _ring_centroid(rings[0]) if rings else None
    if kind == "MultiPolygon":
        polygons = [poly[0] for poly in (coords or []) if poly]
        if not polygons:
            return None
        # Largest outer ring wins: a multipolygon barrio is one big area plus
        # slivers, and the slivers must not drag the point off the barrio.
        return _ring_centroid(max(polygons, key=_ring_area_m2))
    if kind == "GeometryCollection":
        for member in geometry.get("geometries") or []:
            point = geometry_centroid(member)
            if point is not None:
                return point
        return None
    return None


def _ring_area_m2(ring: Sequence[Sequence[float]]) -> float:
    """Unsigned planar area of a ``[[lon, lat], ...]`` ring, in square metres."""
    points = [p for p in ring if p is not None and len(p) >= 2]
    if len(points) < 3:
        return 0.0
    to_xy, _ = local_projection(points[0][1], points[0][0])
    xy = [to_xy(float(p[1]), float(p[0])) for p in points]
    twice = 0.0
    for i in range(len(xy)):
        x0, y0 = xy[i]
        x1, y1 = xy[(i + 1) % len(xy)]
        twice += x0 * y1 - x1 * y0
    return abs(twice) / 2.0


# --------------------------------------------------------------------------- #
# OSM -> gazetteer
# --------------------------------------------------------------------------- #

#: ``place=*`` values worth keeping.  Barrios, repartos, colonias and
#: residenciales *are* addresses in Nicaragua ("Reparto San Juan, casa 24"), so
#: they belong in the same table as the rotondas.
_PLACE_VALUES: frozenset[str] = frozenset({"neighbourhood", "suburb", "quarter", "borough"})

#: The first word of a Nicaraguan settlement name is its class, and it is more
#: reliable than any OSM tag: "Reparto Los Robles", "Colonia Centroamérica",
#: "Residencial Las Colinas", "Barrio Riguero".
_PLACE_NAME_KIND: dict[str, str] = {
    "reparto": "reparto",
    "rpto": "reparto",
    "colonia": "colonia",
    "col": "colonia",
    "residencial": "residencial",
    "urbanizacion": "residencial",
    "barrio": "barrio",
    "bo": "barrio",
    "anexo": "barrio",
    "asentamiento": "barrio",
    "villa": "barrio",
}

_AMENITY_KIND: dict[str, str] = {
    "marketplace": "mercado",
    "place_of_worship": "iglesia",
    "school": "colegio",
    "kindergarten": "colegio",
    "college": "universidad",
    "university": "universidad",
    "hospital": "hospital",
    "clinic": "hospital",
    "fuel": "gasolinera",
    "bus_station": "terminal",
    "ferry_terminal": "terminal",
    "bank": "banco",
    "cinema": "edificio",
    "theatre": "edificio",
    "townhall": "edificio",
    "police": "edificio",
    "fire_station": "edificio",
    "courthouse": "edificio",
    "grave_yard": "cementerio",
}

_TOURISM_KIND: dict[str, str] = {
    "hotel": "hotel",
    "motel": "hotel",
    "hostel": "hotel",
    "guest_house": "hotel",
    "apartment": "hotel",
    "resort": "hotel",
    "museum": "edificio",
    "gallery": "edificio",
    "viewpoint": "mirador",
    "artwork": "monumento",
    "attraction": "referencia",
    "theme_park": "referencia",
    "zoo": "referencia",
    "picnic_site": "otro",
    "information": "otro",
    "camp_site": "otro",
}

_LEISURE_KIND: dict[str, str] = {
    "park": "parque",
    "stadium": "estadio",
    "sports_centre": "estadio",
    "nature_reserve": "referencia",
}

_HISTORIC_KIND: dict[str, str] = {
    "monument": "monumento",
    "memorial": "monumento",
    "castle": "monumento",
    "fort": "monumento",
    "ruins": "monumento",
    "archaeological_site": "monumento",
}


def kind_from_tags(tags: Mapping[str, Any], name: str = "") -> str | None:
    """Map OSM tags to a ``gazetteer.kind``, or ``None`` to skip the feature.

    The order below is the priority order, and it is not arbitrary:

    ``junction=roundabout`` → ``rotonda``
        First, because a roundabout way also carries ``highway=primary`` and the
        highway rule would otherwise swallow it.  In Managua the rotonda *is* the
        address anchor, so it outranks everything else on the same object.
    ``highway=*`` whose name contains "rotonda" → ``rotonda``
        Catches the ones mapped as a plain junction node or an unclosed way.
    ``highway=traffic_signals`` → ``semaforo``
        "De los semáforos de Villa Fontana" is a real, common address.
    ``place=neighbourhood|suburb|quarter|borough`` → barrio/reparto/colonia/…
        Refined by the first word of the name (see ``_PLACE_NAME_KIND``).
    ``amenity`` / ``shop=mall`` / ``tourism`` / ``leisure`` / ``historic`` /
    ``aeroway`` / ``natural`` / ``man_made=bridge`` / ``office`` → per table.

    Anything else returns ``None``: a gazetteer of every OSM object would drown
    the real landmarks in bus stops and driveways.
    """
    folded_name = normalize(name or str(tags.get("name") or ""))

    if str(tags.get("junction") or "").lower() == "roundabout":
        return "rotonda"
    if tags.get("highway"):
        highway = str(tags["highway"]).lower()
        if "rotonda" in folded_name or "redoma" in folded_name:
            return "rotonda"
        if highway == "traffic_signals":
            return "semaforo"
        if highway == "milestone":
            return None  # km-posts have their own table (db kmpost)
        return None

    place = str(tags.get("place") or "").lower()
    if place in _PLACE_VALUES:
        first = folded_name.split()[0] if folded_name else ""
        return _PLACE_NAME_KIND.get(first, "barrio")

    amenity = str(tags.get("amenity") or "").lower()
    if amenity in _AMENITY_KIND:
        return _AMENITY_KIND[amenity]

    if str(tags.get("shop") or "").lower() == "mall":
        return "centro_comercial"

    tourism = str(tags.get("tourism") or "").lower()
    if tourism:
        return _TOURISM_KIND.get(tourism, "referencia")

    leisure = str(tags.get("leisure") or "").lower()
    if leisure in _LEISURE_KIND:
        return _LEISURE_KIND[leisure]

    historic = str(tags.get("historic") or "").lower()
    if historic in _HISTORIC_KIND:
        return _HISTORIC_KIND[historic]

    aeroway = str(tags.get("aeroway") or "").lower()
    if aeroway in {"aerodrome", "terminal"}:
        return "aeropuerto"

    natural = str(tags.get("natural") or "").lower()
    if natural == "volcano":
        return "volcan"
    if natural == "beach":
        return "playa"
    if natural == "water" and ("laguna" in folded_name or "lago" in folded_name):
        return "laguna"

    if str(tags.get("man_made") or "").lower() == "bridge":
        return "puente"
    if str(tags.get("landuse") or "").lower() == "cemetery":
        return "cementerio"
    if tags.get("office") or str(tags.get("building") or "").lower() in {"industrial", "office"}:
        return "empresa"
    return None


def _osm_name(tags: Mapping[str, Any]) -> str | None:
    """Preferred display name: Spanish first, then the plain name, then official."""
    for key in ("name:es", "name", "official_name", "alt_name", "short_name"):
        value = tags.get(key)
        if value and str(value).strip():
            return collapse_ws(str(value))
    return None


def _osm_alt_names(tags: Mapping[str, Any], chosen: str) -> list[str]:
    """Every other spelling OSM carries, minus the one already used as the name."""
    alts: list[str] = []
    for key in ("name", "name:es", "alt_name", "short_name", "official_name", "old_name"):
        raw = tags.get(key)
        if not raw:
            continue
        for part in str(raw).split(";"):
            candidate = collapse_ws(part)
            if candidate and normalize(candidate) != normalize(chosen):
                alts.append(candidate)
    seen: set[str] = set()
    unique: list[str] = []
    for alt in alts:
        key = normalize(alt)
        if key not in seen:
            seen.add(key)
            unique.append(alt)
    return unique


def _osm_id(feature: Mapping[str, Any], tags: Mapping[str, Any]) -> str | None:
    """Best-effort OSM identifier, normalised to ``"<type>/<id>"`` when possible.

    # UNVERIFIED: which of these keys ``osmium export`` actually emits depends on
    # its ``--add-unique-id`` flag (``type_id`` gives ``@id`` like ``"w12345"``,
    # ``counter`` gives a meaningless integer) and on the osmium version.  On the
    # first real export, check what lands in the file and drop the branches that
    # never fire.
    """
    for key in ("@id", "id", "osm_id", "@osm_id"):
        raw = tags.get(key) if key in tags else feature.get(key)
        if raw in (None, ""):
            continue
        value = str(raw)
        # NB: never ``feature["type"]`` — in GeoJSON that is the string "Feature".
        osm_type = str(tags.get("@type") or tags.get("osm_type") or "")
        if osm_type and "/" not in value and not value[:1].isalpha():
            return f"{osm_type}/{value}"
        return value
    return None


def _skip_reason(feature: Mapping[str, Any]) -> str | None:
    """``None`` when the OSM feature is usable, otherwise why it was dropped."""
    tags = feature.get("properties") or {}
    name = _osm_name(tags)
    if kind_from_tags(tags, name or "") is None:
        return "sin_categoria"
    if not name:
        # A nameless rotonda cannot anchor an address, but it is worth counting:
        # a high count here is a mapping gap the field programme should close.
        return "sin_nombre"
    if geometry_centroid(feature.get("geometry")) is None:
        return "sin_geometria"
    return None


def landmark_from_osm_feature(feature: Mapping[str, Any]) -> Landmark | None:
    """Convert one exported OSM feature into a :class:`Landmark`, or ``None``."""
    if _skip_reason(feature) is not None:
        return None
    tags = feature.get("properties") or {}
    name = _osm_name(tags) or ""
    kind = kind_from_tags(tags, name)
    assert kind is not None  # guaranteed by _skip_reason
    point = geometry_centroid(feature.get("geometry"))
    assert point is not None
    lat, lon = point
    osm_id = _osm_id(feature, tags)
    city = tags.get("addr:city") or tags.get("is_in:city") or None
    return Landmark(
        name=name,
        kind=kind,
        lat=lat,
        lon=lon,
        name_alt=_osm_alt_names(tags, name),
        city=collapse_ws(str(city)) if city else None,
        former=False,  # OSM does not map what no longer exists; that is the seed's job
        era=None,
        popularity=None,
        source="osm",
        source_id=osm_id,
        osm_id=osm_id,
    )


def load_osm(
    paths: Iterable[str | Path], *, stats: GazetteerStats | None = None
) -> Iterator[Landmark]:
    """Stream landmarks out of one or more ``osmium export`` GeoJSON-seq files.

    Uses :func:`pipeline.common.io.read_geojsonseq`, which tolerates the
    RS-prefixed RFC 8142 flavour osmium emits as well as plain ``.geojsonl``.
    """
    counters = stats if stats is not None else GazetteerStats()
    for path in paths:
        log.info("reading OSM features from %s", path)
        for feature in read_geojsonseq(path):
            counters.osm_features += 1
            reason = _skip_reason(feature)
            if reason is not None:
                counters.osm_skipped[reason] += 1
                continue
            landmark = landmark_from_osm_feature(feature)
            if landmark is None:  # pragma: no cover - _skip_reason already agreed
                counters.osm_skipped["sin_categoria"] += 1
                continue
            counters.osm_used += 1
            yield landmark


# --------------------------------------------------------------------------- #
# Seed CSV
# --------------------------------------------------------------------------- #

_TRUE_WORDS = frozenset({"1", "true", "t", "yes", "y", "si", "sí", "verdadero"})
_FALSE_WORDS = frozenset({"", "0", "false", "f", "no", "n", "falso"})

SEED_COLUMNS: tuple[str, ...] = (
    "name",
    "name_alt",
    "kind",
    "lat",
    "lon",
    "city",
    "former",
    "era",
    "popularity",
    "note",
)


def _as_bool(value: str | None, *, row: int, column: str) -> bool:
    folded = normalize(str(value or ""))
    if folded in _TRUE_WORDS:
        return True
    if folded in _FALSE_WORDS:
        return False
    raise ValueError(f"row {row}: {column}={value!r} is not a boolean")


def load_seed(path: str | Path = DEFAULT_SEED_PATH) -> list[Landmark]:
    """Read ``docs/gazetteer_seed.csv``.

    Lines starting with ``#`` are the file's own documentation and are skipped.
    Validation is strict and fails the build rather than warning: a landmark with
    a typo'd kind or a coordinate outside Nicaragua would silently poison every
    address anchored on it, and this file is small enough to fix by hand.
    """
    seed_path = Path(path)
    landmarks: list[Landmark] = []
    with open(seed_path, encoding="utf-8") as handle:
        rows = (line for line in handle if not line.lstrip().startswith("#"))
        reader = csv.DictReader(rows)
        missing = [column for column in SEED_COLUMNS if column not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{seed_path}: missing columns {missing}")
        for number, row in enumerate(reader, start=2):
            name = collapse_ws(row.get("name") or "")
            if not name:
                continue  # a blank separator line, not an error
            kind = (row.get("kind") or "").strip()
            if kind not in GAZETTEER_KINDS:
                raise ValueError(f"row {number} ({name}): unknown kind {kind!r}")
            try:
                lat = float(row["lat"])
                lon = float(row["lon"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"row {number} ({name}): bad coordinate") from exc
            min_lon, min_lat, max_lon, max_lat = NICARAGUA_BBOX
            if not (min_lat <= lat <= max_lat and min_lon <= lon <= max_lon):
                raise ValueError(f"row {number} ({name}): {lat},{lon} is outside Nicaragua")
            popularity_raw = (row.get("popularity") or "").strip()
            popularity: float | None = None
            if popularity_raw:
                popularity = float(popularity_raw)
                if not 0.0 <= popularity <= 1.0:
                    raise ValueError(f"row {number} ({name}): popularity {popularity} not in 0..1")
            former = _as_bool(row.get("former"), row=number, column="former")
            note = collapse_ws(row.get("note") or "") or None
            if former and not note:
                # The whole point of a ghost landmark is that a reviewer can tell
                # what it was; an unexplained one cannot be verified in the field.
                raise ValueError(f"row {number} ({name}): a former landmark needs a note")
            city = collapse_ws(row.get("city") or "") or None
            landmarks.append(
                Landmark(
                    name=name,
                    kind=kind,
                    lat=lat,
                    lon=lon,
                    name_alt=_split_alts(row.get("name_alt")),
                    city=city,
                    former=former,
                    era=collapse_ws(row.get("era") or "") or None,
                    popularity=popularity,
                    source="seed",
                    source_id=slugify(name, city),
                    note=note,
                )
            )
    log.info("seed: %d landmarks from %s", len(landmarks), seed_path)
    return landmarks


def _split_alts(raw: str | None) -> list[str]:
    """``"Rotonda Plaza España;El Güegüense"`` -> two names, blanks dropped."""
    if not raw:
        return []
    return [collapse_ws(part) for part in str(raw).split(";") if collapse_ws(part)]


# --------------------------------------------------------------------------- #
# Approved user aliases
# --------------------------------------------------------------------------- #

_GEOJSONSEQ_SUFFIXES = frozenset({".geojsonl", ".geojsonseq", ".ndjson", ".jsonl"})


def load_aliases(path: str | Path) -> list[Landmark]:
    """Read approved user-submitted aliases from CSV or GeoJSON-seq.

    These come out of the ``alias`` table with ``status = 'approved'`` — a human
    has already looked at each one.  They still carry the lowest authority here:
    an alias that lands on a known landmark becomes another ``name_alt``, and one
    that does not becomes its own low-popularity row for the next reviewer to
    promote.
    """
    alias_path = Path(path)
    records: list[Mapping[str, Any]] = []
    if alias_path.suffix.lower() in _GEOJSONSEQ_SUFFIXES:
        for feature in read_geojsonseq(alias_path):
            properties = dict(feature.get("properties") or {})
            point = geometry_centroid(feature.get("geometry"))
            if point is None:
                continue
            properties["lat"], properties["lon"] = point
            records.append(properties)
    else:
        with open(alias_path, encoding="utf-8") as handle:
            rows = (line for line in handle if not line.lstrip().startswith("#"))
            records.extend(dict(row) for row in csv.DictReader(rows))

    landmarks: list[Landmark] = []
    for record in records:
        name = collapse_ws(
            str(record.get("name") or record.get("text") or record.get("alias") or "")
        )
        if not name:
            continue
        try:
            lat = float(record["lat"])
            lon = float(record["lon"])
        except (KeyError, TypeError, ValueError):
            log.warning("alias %r has no usable coordinate; skipped", name)
            continue
        kind = str(record.get("kind") or "referencia").strip() or "referencia"
        if kind not in GAZETTEER_KINDS:
            kind = "referencia"
        city = collapse_ws(str(record.get("city") or "")) or None
        landmarks.append(
            Landmark(
                name=name,
                kind=kind,
                lat=lat,
                lon=lon,
                name_alt=_split_alts(record.get("name_alt")),
                city=city,
                former=_as_bool(str(record.get("former") or ""), row=0, column="former"),
                popularity=0.3,
                source="user",
                source_id=slugify(name, city),
                note=collapse_ws(str(record.get("note") or "")) or None,
            )
        )
    log.info("aliases: %d approved rows from %s", len(landmarks), alias_path)
    return landmarks


# --------------------------------------------------------------------------- #
# Merge
# --------------------------------------------------------------------------- #

#: Who decides the *name*, ``kind``, ``former``/``era`` and ``city`` of a merged
#: row.  The seed exists because OSM's naming is not the spoken naming.
_NAME_RANK: dict[str, int] = {"seed": 3, "osm": 2, "user": 1}

#: Who decides the *position*.  OSM nodes were placed by someone looking at
#: imagery of the thing; the seed coordinates were typed from documentary
#: knowledge.  A ``former`` seed row is the exception: OSM cannot have a building
#: that stopped existing in 1972, so any OSM "match" is a coincidence and the
#: seed keeps its own point.
_POSITION_RANK: dict[str, int] = {"osm": 2, "seed": 1, "user": 0}


def _position_rank(entry: Landmark) -> int:
    if entry.source == "seed" and entry.former:
        return 3
    return _POSITION_RANK.get(entry.source, 0)


def _merge_alt_names(winner: Landmark, loser: Landmark) -> list[str]:
    """Union of both records' alternative names, plus the loser's display name."""
    merged: list[str] = []
    seen = {normalize(winner.name)}
    for candidate in [*winner.name_alt, loser.name, *loser.name_alt]:
        key = normalize(candidate)
        if key and key not in seen:
            seen.add(key)
            merged.append(collapse_ws(candidate))
    return merged


def merge_landmarks(first: Landmark, second: Landmark) -> Landmark:
    """Combine two records for the same landmark under the precedence rules.

    Ties go to ``first``, which is the record already in the table, so the build
    is order-stable: re-running it on the same inputs gives the same rows.
    """
    if _NAME_RANK.get(second.source, 0) > _NAME_RANK.get(first.source, 0):
        winner, loser = second, first
    else:
        winner, loser = first, second
    position = first if _position_rank(first) >= _position_rank(second) else second

    popularity = winner.popularity
    if popularity is None:
        popularity = loser.popularity

    return Landmark(
        name=winner.name,
        kind=winner.kind,
        lat=position.lat,
        lon=position.lon,
        name_alt=_merge_alt_names(winner, loser),
        city=winner.city or loser.city,
        former=winner.former or loser.former,
        era=winner.era or loser.era,
        popularity=popularity,
        source=winner.source,
        source_id=winner.source_id,
        note=winner.note or loser.note,
        osm_id=winner.osm_id or loser.osm_id,
    )


def _kinds_compatible(left: str, right: str) -> bool:
    """True when two kinds could plausibly describe the same object."""
    left_group = KIND_GROUPS.get(left, "comodin")
    right_group = KIND_GROUPS.get(right, "comodin")
    return left_group == right_group or "comodin" in (left_group, right_group)


def _can_merge(existing: Landmark, candidate: Landmark, radius_m: float, *, strong: bool) -> bool:
    """Decide whether ``candidate`` is another record of ``existing``.

    ``strong`` means the two *display* names share a blocking key, which is
    strong enough on its own inside the radius.  A match found only through an
    alternative name also has to pass the kind-group guard, because nicknames
    collide: "El Güegüense" is a rotonda, a play and several restaurants.
    """
    if haversine_m(existing.lat, existing.lon, candidate.lat, candidate.lon) > radius_m:
        return False
    if existing.city and candidate.city and normalize(existing.city) != normalize(candidate.city):
        # Every Nicaraguan town has a "Parque Central" and an "Iglesia San Juan".
        return False
    # A match found only through a nickname must also be plausible by kind.
    return strong or _kinds_compatible(existing.kind, candidate.kind)


def build_gazetteer(
    *,
    seed: Iterable[Landmark] = (),
    osm: Iterable[Landmark] = (),
    aliases: Iterable[Landmark] = (),
    merge_radius_m: float = DEFAULT_MERGE_RADIUS_M,
    stats: GazetteerStats | None = None,
) -> tuple[list[Landmark], GazetteerStats]:
    """Fold the three sources into one de-duplicated landmark list.

    De-duplication blocks on :func:`common.text.name_key` of the name and of
    every alternative name, then confirms with
    :func:`common.geo.haversine_m`; the result is order-independent because the
    precedence rules live in :func:`merge_landmarks`, not in the iteration order.
    """
    counters = stats if stats is not None else GazetteerStats()
    entries: list[Landmark] = []
    index: dict[str, set[int]] = defaultdict(set)

    def register(position: int) -> None:
        for key in entries[position].match_keys:
            index[key].add(position)

    def absorb(candidate: Landmark) -> None:
        best: tuple[float, int] | None = None
        seen: set[int] = set()
        for key in candidate.match_keys:
            for position in index.get(key, ()):
                if position in seen:
                    continue
                seen.add(position)
                existing = entries[position]
                strong = existing.primary_key == candidate.primary_key
                if not _can_merge(existing, candidate, merge_radius_m, strong=strong):
                    continue
                distance = haversine_m(existing.lat, existing.lon, candidate.lat, candidate.lon)
                if best is None or distance < best[0]:
                    best = (distance, position)
        if best is None:
            entries.append(candidate)
            register(len(entries) - 1)
            return
        position = best[1]
        entries[position] = merge_landmarks(entries[position], candidate)
        register(position)
        counters.merged_duplicates += 1

    seed_list = list(seed)
    counters.seed_rows += len(seed_list)
    for landmark in seed_list:
        absorb(landmark)
    for landmark in osm:  # a generator: never materialise a country-sized export
        absorb(landmark)
    alias_list = list(aliases)
    counters.alias_rows += len(alias_list)
    for landmark in alias_list:
        absorb(landmark)

    # Most useful landmarks first: the search index and every eyeball reading the
    # export benefit, and the order is deterministic for diffing two builds.
    entries.sort(key=lambda entry: (-entry.resolved_popularity(), normalize(entry.name)))
    return entries, counters


def summarize(entries: Sequence[Landmark], stats: GazetteerStats) -> str:
    """Human-readable build report, logged at INFO by :func:`main`."""
    by_kind = Counter(entry.kind for entry in entries)
    by_source = Counter(entry.source for entry in entries)
    former = sum(1 for entry in entries if entry.former)
    in_circle = sum(1 for entry in entries if distance_from_mga_m(entry.lat, entry.lon) <= 48_300)
    lines = [
        f"gazetteer: {len(entries)} landmarks "
        f"({former} former, {in_circle} inside the 48.3 km circle)",
        f"  inputs: seed={stats.seed_rows} osm_features={stats.osm_features} "
        f"osm_used={stats.osm_used} aliases={stats.alias_rows}",
        f"  merged as duplicates: {stats.merged_duplicates}",
    ]
    if stats.osm_skipped:
        skipped = " ".join(
            f"{reason}={count}" for reason, count in sorted(stats.osm_skipped.items())
        )
        lines.append(f"  osm skipped ({stats.osm_skipped_total}): {skipped}")
    lines.append("  by source: " + " ".join(f"{k}={v}" for k, v in sorted(by_source.items())))
    lines.append("  by kind:")
    for kind, count in sorted(by_kind.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"    {kind:<18} {count}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Outputs
# --------------------------------------------------------------------------- #


def export_geojson(entries: Sequence[Landmark], path: str | Path) -> Path:
    """Write the gazetteer as a FeatureCollection, atomically."""
    return write_geojson(
        path,
        (entry.to_feature() for entry in entries),
        name="nicanav gazetteer",
    )


_UPSERT_SQL = """
INSERT INTO gazetteer (name, name_alt, kind, geom, city, former, era, popularity, source, source_id)
VALUES (
    %(name)s, %(name_alt)s, %(kind)s,
    ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326),
    %(city)s, %(former)s, %(era)s, %(popularity)s, %(source)s, %(source_id)s
)
ON CONFLICT (source, source_id) WHERE source_id IS NOT NULL DO UPDATE SET
    name       = EXCLUDED.name,
    name_alt   = EXCLUDED.name_alt,
    kind       = EXCLUDED.kind,
    geom       = EXCLUDED.geom,
    city       = EXCLUDED.city,
    former     = EXCLUDED.former,
    era        = EXCLUDED.era,
    popularity = EXCLUDED.popularity
"""


def upsert_postgis(
    entries: Sequence[Landmark],
    dsn: str,
    *,
    replace: bool = False,
) -> int:
    """Upsert landmarks into the PostGIS ``gazetteer`` table; returns the count.

    Keyed on ``(source, source_id)``, which ``db/migrations/001_init.sql`` makes
    unique wherever ``source_id`` is not null, so re-running a nightly build
    updates rows in place instead of duplicating the whole table.

    ``replace=True`` first deletes the rows this job owns (``source`` in
    ``seed``/``osm``/``user``), which is how a landmark that disappeared from OSM
    stops being offered as an address anchor.  It runs inside the same
    transaction, so a failed build leaves yesterday's gazetteer serving.

    # UNVERIFIED: no PostGIS instance was reachable while this was written.  On
    # first deploy check that ``ON CONFLICT (source, source_id) WHERE source_id
    # IS NOT NULL`` matches the partial index ``gazetteer_source_uniq``
    # (PostgreSQL requires the inference predicate to imply the index one) and
    # that psycopg adapts the Python list in ``name_alt`` to ``text[]``.
    """
    if psycopg is None:  # pragma: no cover - depends on the environment
        raise RuntimeError("psycopg is not installed; re-run without --upsert")
    rows = [
        {
            "name": entry.name,
            "name_alt": list(entry.name_alt),
            "kind": entry.kind,
            "lat": entry.lat,
            "lon": entry.lon,
            "city": entry.city,
            "former": entry.former,
            "era": entry.era,
            "popularity": entry.resolved_popularity(),
            "source": entry.source,
            "source_id": entry.source_id or slugify(entry.name, entry.city),
        }
        for entry in entries
    ]
    with psycopg.connect(dsn) as connection:  # pragma: no cover - needs a database
        with connection.cursor() as cursor:
            if replace:
                cursor.execute("DELETE FROM gazetteer WHERE source IN ('seed', 'osm', 'user')")
                log.info("replace: deleted %d previously published rows", cursor.rowcount)
            cursor.executemany(_UPSERT_SQL, rows)
        connection.commit()
    log.info("upserted %d landmarks into %s", len(rows), "gazetteer")
    return len(rows)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m pipeline.geocode.gazetteer_build",
        description=(
            "Build the Nicaraguan landmark gazetteer from OSM, the curated seed CSV "
            "and approved user aliases."
        ),
    )
    parser.add_argument(
        "--osm",
        action="append",
        default=None,
        metavar="PATH",
        help=(
            "GeoJSON-seq export of the OSM features to consider (repeatable). "
            "Produce it with: osmium export --geometry-types=point,polygon,linestring "
            "-f geojsonseq -o data/osm/gazetteer_features.geojsonl data/osm/mga48.osm.pbf"
        ),
    )
    parser.add_argument(
        "--seed",
        default=str(DEFAULT_SEED_PATH),
        metavar="PATH",
        help="curated seed CSV (default: docs/gazetteer_seed.csv; pass '' to skip)",
    )
    parser.add_argument(
        "--aliases",
        default=None,
        metavar="PATH",
        help="approved user aliases, CSV or GeoJSON-seq (optional)",
    )
    parser.add_argument(
        "--out",
        default=None,
        metavar="PATH",
        help="GeoJSON export path (default: <NICANAV_DATA_DIR>/exports/gazetteer.geojson)",
    )
    parser.add_argument(
        "--merge-radius-m",
        type=float,
        default=DEFAULT_MERGE_RADIUS_M,
        help=f"de-duplication radius in metres (default: {DEFAULT_MERGE_RADIUS_M:.0f})",
    )
    parser.add_argument(
        "--upsert",
        action="store_true",
        help="also upsert into the PostGIS gazetteer table",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="PostgreSQL DSN for --upsert (default: NICANAV_DATABASE_URL)",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="with --upsert, delete previously published seed/osm/user rows first",
    )
    parser.add_argument("--log-level", default="INFO", help="logging level (default: INFO)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point.  Returns 0 on success and non-zero on any failure."""
    args = _build_parser().parse_args(argv)
    setup_logging(args.log_level)

    seed_landmarks: list[Landmark] = []
    if args.seed:
        seed_path = Path(args.seed)
        if not seed_path.exists():
            log.error("seed CSV not found: %s", seed_path)
            return 2
        seed_landmarks = load_seed(seed_path)

    osm_paths = [Path(path) for path in (args.osm or [])]
    for path in osm_paths:
        if not path.exists():
            log.error("OSM export not found: %s", path)
            return 2

    alias_landmarks: list[Landmark] = []
    if args.aliases:
        alias_path = Path(args.aliases)
        if not alias_path.exists():
            log.error("alias file not found: %s", alias_path)
            return 2
        alias_landmarks = load_aliases(alias_path)

    if not seed_landmarks and not osm_paths and not alias_landmarks:
        log.error("nothing to build: pass --seed, --osm or --aliases")
        return 2

    stats = GazetteerStats()
    entries, stats = build_gazetteer(
        seed=seed_landmarks,
        osm=load_osm(osm_paths, stats=stats),
        aliases=alias_landmarks,
        merge_radius_m=args.merge_radius_m,
        stats=stats,
    )

    out_path = Path(args.out) if args.out else _default_out_path()
    export_geojson(entries, out_path)
    log.info("%s", summarize(entries, stats))

    if args.upsert:
        from common.config import get_settings  # imported late: no config at import time

        dsn = args.database_url or get_settings().database_url
        upsert_postgis(entries, dsn, replace=args.replace)
    return 0


def _default_out_path() -> Path:
    """``<NICANAV_DATA_DIR>/exports/gazetteer.geojson``, per docs/SPEC.md §6."""
    from common.config import get_settings

    return get_settings().data_dir / "exports" / "gazetteer.geojson"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
