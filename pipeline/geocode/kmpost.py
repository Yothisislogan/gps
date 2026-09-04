"""Km-post geocoder: "Km 12.5 Carretera a Masaya" -> a point on the carretera.

Outside Managua's grid, and increasingly inside it, a Nicaraguan address is a kilometre
marker on a named highway:

    Km 12.5 Carretera a Masaya
    km 9½ carretera norte, mano derecha
    Kilómetro 14 Carretera a Masaya, contiguo a la gasolinera
    Carretera Nueva a León Km 22

Resolving one is three separate problems, kept separate here:

1. **Parsing** (:func:`parse`, :func:`match_highway`, :func:`looks_like_kmpost`) — pure
   string work, no I/O.  It turns the string into a :class:`KmPost`: a kilometre, a
   highway key from ``docs/carreteras.csv``, and the side of the road when the writer
   bothered to say ("mano derecha").
2. **Measuring** (:func:`resolve`) — walking that many metres along the highway's
   centreline.  The geometry arrives through an injected ``geometry_lookup`` callable, so
   this module never touches PostGIS and the whole thing is testable with a synthetic line.
3. **Calibrating** — the interesting part, and the reason this is not one line of
   :func:`common.geo.interpolate_along`.

Why calibration is not optional
-------------------------------
The ministry's signs are the authority: a business at "Km 14" is at whatever the sign
says, not at 14 000 m of OSM polyline.  The two disagree, systematically and in both
directions:

* OSM centrelines are digitised from imagery and carry curve error — a hand-traced curve
  is longer than the surveyed centreline, typically by 1–3 %, occasionally much more on a
  mountain road like the subida de El Crucero;
* a merged centreline built from many OSM ways has gaps and stubs where the mapping is
  incomplete, and every missing metre shifts everything downstream;
* the signed chainage itself is historical.  Realignments (the Masaya bypass, the Tipitapa
  four-laning) shortened the road without renumbering the mojones, so signed kilometres
  are not even self-consistent — Km 20 to Km 21 is not always 1 000 m of asphalt.

So the correction is empirical, not a formula: feed in known ``(km, lat, lon)`` mojones —
the ``kmpost`` table, populated from OSM ``highway=milestone`` + ``distance=*`` and from
photographed signs — and interpolate between them.  See :func:`_chainage` for the maths and
the failure modes.

Nothing here claims to be precise: a km post covers a kilometre of road, and the
confidence scores reflect that.  Pessimism is deliberate and matches
``relative_address.py``: a pin that says 0.9 and lands 800 m away teaches users to ignore
every pin.
"""

from __future__ import annotations

import argparse
import csv
import functools
import json
import logging
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from common.geo import (
    NICARAGUA_BBOX,
    destination_point,
    haversine_m,
    initial_bearing_deg,
    interpolate_along,
    line_length_m,
    nearest_point_on_line,
)
from common.models import GeocodeCandidate, GeocodeMethod
from common.text import collapse_ws, normalize, parse_spanish_number, strip_accents

__all__ = [
    "CALIBRATION_MAX_OFFSET_M",
    "CARRETERAS_PATH",
    "KM0_ANCHOR_MAX_M",
    "MAX_KM",
    "SIDE_OFFSET_M",
    "Chainage",
    "Highway",
    "KmPost",
    "all_highways",
    "highway_by_key",
    "load_highways",
    "looks_like_kmpost",
    "main",
    "match_highway",
    "parse",
    "resolve",
]

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
CARRETERAS_PATH = REPO_ROOT / "docs" / "carreteras.csv"

_COLUMNS = (
    "highway_key",
    "display_name",
    "aliases",
    "osm_ref",
    "km0_lat",
    "km0_lon",
    "note",
)

#: How far from the centreline the returned point is pushed for "mano derecha/izquierda".
#: Half a carriageway plus a shoulder on a Nicaraguan primary: enough that the pin is
#: visibly on the correct side at z17 without landing in the next property.
SIDE_OFFSET_M = 12.0

#: Longest chainage anyone quotes as an address in Nicaragua is El Rama at Km ~292; the
#: Caribbean roads add a little more.  Anything past this is a phone number or a year that
#: happened to follow the letters "km", not a km post.
MAX_KM = 500.0

#: A calibration row further than this from the centreline is not a mojón of *this*
#: highway — a mis-keyed row, or a sign on a parallel service road.  Using it would drag
#: the whole interpolation, so it is dropped.
CALIBRATION_MAX_OFFSET_M = 200.0

#: How close a centreline's end has to be to the row's km 0 before the chainage origin is
#: considered confirmed.  Generous, because km0 in docs/carreteras.csv is itself a few
#: hundred metres uncertain and the merged geometry usually starts at a junction near it.
KM0_ANCHOR_MAX_M = 5_000.0

#: Distance either side of the target used to measure the local direction of travel.  Long
#: enough not to be dominated by vertex jitter, short enough to stay on the local tangent.
_BEARING_WINDOW_M = 25.0

_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_REF_RE = re.compile(r"^NIC-\d{1,3}$")

Coord = Sequence[float]  # (lon, lat), GeoJSON order
GeometryLookup = Callable[[str], Sequence[Coord] | None]
#: ``(km, lat, lon)`` — a mojón whose sign says ``km`` standing at that point.
CalibrationPoint = tuple[float, float, float]
Side = Literal["derecha", "izquierda"]


# --------------------------------------------------------------------------- #
# The registry (docs/carreteras.csv)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Highway:
    """One row of ``docs/carreteras.csv``.

    ``km0_lat``/``km0_lon`` are where the *signed* chainage starts, which for the radial
    carreteras is the historic Km 0 of Managua's old centre and not the point where the
    road itself begins.  They are ``None`` for the urban pistas, which carry no signed
    chainage at all; :func:`resolve` scores those lower on purpose.
    """

    highway_key: str
    display_name: str
    aliases: tuple[str, ...]
    osm_ref: str | None
    km0_lat: float | None
    km0_lon: float | None
    note: str
    row_order: int

    @property
    def km0(self) -> tuple[float, float] | None:
        """The chainage origin as ``(lat, lon)``, or ``None`` when unknown."""
        if self.km0_lat is None or self.km0_lon is None:
            return None
        return (self.km0_lat, self.km0_lon)


def _fail(row_number: int, key: str, problem: str) -> ValueError:
    """The ValueError that stops a build, naming the offending row."""
    return ValueError(f"{CARRETERAS_PATH.name} row {row_number} ({key or '<no key>'}): {problem}")


def _read_rows(path: Path) -> list[dict[str, str]]:
    """Read the CSV, dropping ``#`` comment lines and blank lines."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:  # a missing registry breaks every km-post lookup: fail loudly
        raise ValueError(f"cannot read highway registry {path}: {exc}") from exc
    lines = [
        line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")
    ]
    if not lines:
        raise ValueError(f"highway registry {path} has no rows")
    reader = csv.DictReader(lines)
    header = tuple(reader.fieldnames or ())
    if header != _COLUMNS:
        raise ValueError(f"highway registry {path} header is {header!r}; expected {_COLUMNS!r}")
    return list(reader)


def _parse_coordinate(
    row: dict[str, str], row_number: int, key: str
) -> tuple[float | None, float | None]:
    """Read ``km0_lat``/``km0_lon``: both present and in Nicaragua, or both empty."""
    raw_lat = (row.get("km0_lat") or "").strip()
    raw_lon = (row.get("km0_lon") or "").strip()
    if not raw_lat and not raw_lon:
        return (None, None)
    if not raw_lat or not raw_lon:
        raise _fail(row_number, key, "km0_lat and km0_lon must both be set or both be empty")
    try:
        lat, lon = float(raw_lat), float(raw_lon)
    except ValueError as exc:
        raise _fail(row_number, key, f"km0 is not numeric: {exc}") from exc
    min_lon, min_lat, max_lon, max_lat = NICARAGUA_BBOX
    if not (min_lat <= lat <= max_lat and min_lon <= lon <= max_lon):
        raise _fail(row_number, key, f"km0 {lat},{lon} is outside Nicaragua")
    return (lat, lon)


def _parse_aliases(raw: str, row_number: int, key: str) -> tuple[str, ...]:
    """Split and validate the ``;``-separated alias list.

    Aliases are stored pre-normalised so the file itself shows what the matcher sees; a row
    that writes "Ctra. a Masaya" would match nothing and is a data bug, not a shrug.
    """
    aliases: list[str] = []
    for chunk in raw.split(";"):
        alias = chunk.strip()
        if not alias:
            continue
        if normalize(alias) != alias:
            raise _fail(
                row_number,
                key,
                f"alias {alias!r} is not normalised; write it as {normalize(alias)!r}",
            )
        if alias == "carretera":
            raise _fail(row_number, key, "'carretera' on its own identifies no highway")
        if len(alias) < 4:
            raise _fail(row_number, key, f"alias {alias!r} is too short to be safe")
        if alias in aliases:
            raise _fail(row_number, key, f"alias {alias!r} repeated")
        aliases.append(alias)
    if not aliases:
        raise _fail(row_number, key, "no aliases: nothing would ever match this row")
    return tuple(aliases)


@functools.lru_cache(maxsize=4)
def load_highways(path: Path | str | None = None) -> tuple[Highway, ...]:
    """Load and validate ``docs/carreteras.csv``.

    Raises :class:`ValueError` on any malformed row: a typo here silently sends every
    "Km 12 Carretera X" query to the wrong asphalt, which is worse than a failed import.
    Cached per path — the file is read once per process.
    """
    csv_path = Path(path) if path is not None else CARRETERAS_PATH
    highways: list[Highway] = []
    seen_keys: dict[str, int] = {}
    seen_aliases: dict[str, str] = {}

    for index, row in enumerate(_read_rows(csv_path)):
        # Row 1 is the header, so the first data row is line 2 as a human counts them.
        row_number = index + 2
        key = (row.get("highway_key") or "").strip()
        if not _KEY_RE.match(key):
            raise _fail(row_number, key, "highway_key must match [a-z][a-z0-9_]*")
        if key in seen_keys:
            raise _fail(row_number, key, f"duplicate key (first seen on row {seen_keys[key]})")
        seen_keys[key] = row_number

        display_name = (row.get("display_name") or "").strip()
        if not display_name:
            raise _fail(row_number, key, "display_name is empty")

        aliases = _parse_aliases(row.get("aliases") or "", row_number, key)
        for alias in aliases:
            if alias in seen_aliases:
                raise _fail(
                    row_number, key, f"alias {alias!r} already claimed by {seen_aliases[alias]!r}"
                )
            seen_aliases[alias] = key

        ref = (row.get("osm_ref") or "").strip() or None
        if ref is not None and not _REF_RE.match(ref):
            raise _fail(row_number, key, f"osm_ref {ref!r} is not a NIC-<n> ref; leave it empty")

        km0_lat, km0_lon = _parse_coordinate(row, row_number, key)
        highways.append(
            Highway(
                highway_key=key,
                display_name=display_name,
                aliases=aliases,
                osm_ref=ref,
                km0_lat=km0_lat,
                km0_lon=km0_lon,
                note=(row.get("note") or "").strip(),
                row_order=index,
            )
        )

    if not highways:
        raise ValueError(f"highway registry {csv_path} has no rows")
    return tuple(highways)


def all_highways(path: Path | str | None = None) -> tuple[Highway, ...]:
    """Every registry row, in file order."""
    return load_highways(path)


def highway_by_key(key: str, path: Path | str | None = None) -> Highway | None:
    """Registry row for a ``highway_key``, or ``None`` if the key is unknown."""
    for highway in load_highways(path):
        if highway.highway_key == key:
            return highway
    return None


#: Word separator inside an alias: the query may write "ctra. a masaya" or
#: "carretera-a-masaya" and mean the same road.
_ALIAS_GAP = r"[^a-z0-9]+"


@functools.lru_cache(maxsize=4)
def _alias_index(path: Path | str | None = None) -> tuple[tuple[str, str, re.Pattern[str]], ...]:
    """``(alias, highway_key, compiled pattern)`` sorted longest alias first.

    Longest-first is what makes "carretera masaya granada" beat "carretera masaya" on the
    same string; ties fall back to registry order so the result is deterministic.
    """
    entries: list[tuple[str, str, re.Pattern[str]]] = []
    for highway in load_highways(path):
        for alias in highway.aliases:
            pattern = re.compile(
                r"(?<![a-z0-9])"
                + _ALIAS_GAP.join(re.escape(part) for part in alias.split())
                + r"(?![a-z0-9])"
            )
            entries.append((alias, highway.highway_key, pattern))
    entries.sort(key=lambda item: (-len(item[0]), item[0]))
    return tuple(entries)


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def _fold(text: str) -> str:
    """Accent-fold and lowercase *without changing the string length*.

    Offsets are load-bearing here: the km token and the highway alias are matched on the
    folded string and then sliced out of the original so the user's own spelling survives
    into ``highway_text``.  NFD normalisation would change the length, so the fold is done
    character by character — the same trick ``relative_address`` uses.
    """
    return "".join(strip_accents(ch).lower() or ch for ch in text)


def _alnum_only(folded: str) -> str:
    """Replace every non-alphanumeric character with a space, preserving length."""
    return "".join(ch if ch.isalnum() else " " for ch in folded)


_FRACTION = r"(?:½|¼|¾|\d\s*/\s*\d)"
_NUMBER_WORDS = (
    "cero|uno|una|un|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez|once|doce|trece|"
    "catorce|quince|dieciseis|diecisiete|dieciocho|diecinueve|veintiuno|veintidos|"
    "veintitres|veinticuatro|veinticinco|veintiseis|veintisiete|veintiocho|veintinueve|"
    "veinte|treinta|cuarenta|cincuenta|sesenta|setenta|ochenta|noventa|ciento|cien"
)
#: "12", "12.5", "12,5", "9½", "9 1/2", "9 y medio", "doce", "doce y medio".
_QTY = (
    rf"(?:\d+(?:[.,]\d+)?(?:\s*{_FRACTION}|\s+y\s+(?:medio|media))?"
    rf"|{_FRACTION}"
    rf"|(?:{_NUMBER_WORDS})(?:\s+y\s+(?:medio|media))?)"
)
#: The marker word.  A bare "k" is deliberately *not* accepted: "K 5" appears in business
#: names far more often than as a km post, and a false positive here costs a database round
#: trip on every keystroke.
_KM_WORD = r"(?:kilometros|kilometro|kms|km)"
_KMPOST_RE = re.compile(rf"(?<![a-z0-9])(?P<word>{_KM_WORD})\s*[.:\-]?\s*(?P<qty>{_QTY})(?![a-z])")

#: Side of the road.  "mano derecha" is the phrase; "lado derecho" and "a la derecha" turn
#: up in listings copied from Facebook.
_SIDE_RE = re.compile(
    r"(?<![a-z0-9])(?:a\s+)?(?:mano|lado)\s+(?P<side>derecha|derecho|izquierda|izquierdo)(?![a-z0-9])"
    r"|(?<![a-z0-9])a\s+la\s+(?P<side2>derecha|izquierda)(?![a-z0-9])"
)

#: Words that make a string a road reference even when no registry alias matched — the gate
#: fires on "Km 8 carretera a San Marcos" so the API can report "unknown highway" instead of
#: silently treating it as a POI name.
_ROAD_WORD_RE = re.compile(
    r"(?<![a-z0-9])(?:carretera|carreteras|ctra|carr|panamericana|autopista|pista)(?![a-z0-9])"
)

#: Leading connectors to strip off a highway phrase the registry did not recognise.
_LEADING_NOISE_RE = re.compile(
    r"^(?:de\s+la|de\s+el|del|de|en\s+la|en\s+el|en|sobre\s+la|sobre\s+el|sobre|a\s+la|al|a)\s+",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class KmPost:
    """The parse of a km-post address.  Pure data; :func:`resolve` turns it into a point."""

    km: float
    highway_text: str | None
    highway_key: str | None
    side_hint: Side | None
    raw: str

    def render(self) -> str:
        """Canonical display form, e.g. ``"Km 12.5 Carretera a Masaya"``."""
        highway = highway_by_key(self.highway_key) if self.highway_key else None
        name = highway.display_name if highway else (self.highway_text or "")
        label = f"Km {self.km:g}".strip()
        if name:
            label = f"{label} {name}"
        if self.side_hint:
            label = f"{label}, mano {self.side_hint}"
        return label


def _quantity(raw: str) -> float | None:
    """Parse the number after "km", tolerating the spacing people actually type."""
    value = parse_spanish_number(raw)
    if value is not None:
        return value
    # "9 1 / 2" and friends: retry with the whitespace squeezed out.
    return parse_spanish_number(re.sub(r"\s+", "", raw))


def _side_hint(folded: str) -> Side | None:
    """Which side of the carretera the writer said the door is on."""
    match = _SIDE_RE.search(folded)
    if not match:
        return None
    word = match.group("side") or match.group("side2") or ""
    return "derecha" if word.startswith("derech") else "izquierda"


def _strip_side(text: str) -> str:
    """Drop a "mano derecha"-style phrase: it modifies the door, not the highway name.

    The match is made on the folded text and the cut applied to the original, which works
    because :func:`_fold` preserves offsets — the user's own spelling survives.
    """
    match = _SIDE_RE.search(_alnum_only(_fold(text)))
    if not match:
        return text
    return f"{text[: match.start()]} {text[match.end() :]}"


def _find_alias(masked: str, path: Path | str | None = None) -> tuple[str, int, int] | None:
    """First (longest) registry alias occurring in ``masked``, with its offsets."""
    for _alias, key, pattern in _alias_index(path):
        found = pattern.search(masked)
        if found:
            return (key, found.start(), found.end())
    return None


def match_highway(text: str, path: Path | str | None = None) -> str | None:
    """Resolve free text to a ``highway_key``, or ``None``.

    Accent- and case-insensitive, punctuation-tolerant and longest-alias-first, so
    ``"CTRA. A MASAYA"``, ``"carretera masaya"`` and ``"Carretera a Masaya"`` all give
    ``"carretera_a_masaya"`` while ``"carretera"`` on its own gives ``None`` — the bare word
    appears in half the addresses in the country and identifies nothing.
    """
    if not text:
        return None
    found = _find_alias(_alnum_only(_fold(text)), path)
    return found[0] if found else None


def _clean_highway_text(text: str) -> str | None:
    """Tidy the leftover words when no registry alias matched.

    Keeps the user's spelling — the API surfaces it so a missing carretera shows up in the
    logs as a name to add to ``docs/carreteras.csv`` rather than as silence.
    """
    candidate = collapse_ws(re.split(r"[,;]", text)[0]).strip(" .,;:-")
    previous = None
    while candidate and candidate != previous:
        previous = candidate
        candidate = collapse_ws(_LEADING_NOISE_RE.sub("", candidate)).strip(" .,;:-")
    if len(candidate) < 3:
        return None
    return candidate


def parse(text: str, path: Path | str | None = None) -> KmPost | None:
    """Parse a km-post address.  Pure function; no I/O beyond the cached registry read.

    Returns ``None`` when the string carries no usable km token — no "km"/"kilómetro"
    word, an unparseable quantity, or a kilometre outside ``[0, MAX_KM]`` (``"Km 2024"`` is
    a year, not a mojón).  A km token *without* a recognised highway still parses, with
    ``highway_key=None``: the caller reports an unknown carretera rather than guessing, and
    ``highway_text`` carries whatever the user wrote so the gap can be fixed in the registry.
    """
    raw = collapse_ws(text or "")
    if not raw:
        return None

    folded = _fold(raw)
    match = _KMPOST_RE.search(folded)
    if not match:
        return None
    km = _quantity(match.group("qty"))
    if km is None or not (0.0 <= km <= MAX_KM):
        return None

    # Blank the km token so an alias can never be matched out of "kilometro" itself, and so
    # the leftover text on either side is exactly what the user wrote about the highway.
    masked = _alnum_only(folded)
    masked = masked[: match.start()] + " " * (match.end() - match.start()) + masked[match.end() :]

    highway_key: str | None = None
    highway_text: str | None = None
    found = _find_alias(masked, path)
    if found:
        highway_key, start, end = found
        highway_text = collapse_ws(raw[start:end])
    else:
        # No known carretera: keep the words after the km token (the usual order), falling
        # back to the words before it ("Carretera a San Marcos km 8").
        after = _strip_side(raw[match.end() :])
        before = _strip_side(raw[: match.start()])
        highway_text = _clean_highway_text(after) or _clean_highway_text(before)

    return KmPost(
        km=km,
        highway_text=highway_text,
        highway_key=highway_key,
        side_hint=_side_hint(masked),
        raw=raw,
    )


def looks_like_kmpost(text: str, path: Path | str | None = None) -> bool:
    """Cheap gate for ``/api/search``: is this string worth resolving as a km post?

    Conservative on purpose — every false positive costs a geometry fetch on a query the
    user meant as a plain name search.  It demands a km token with a plausible kilometre
    *and* a road reference: either a registry alias ("km 14 masaya") or a road word
    ("km 8 carretera a San Marcos").  So ``"km 12 de mi casa"`` and ``"Restaurante Km 5"``
    do not fire, and neither does ``"2 km al sur de la rotonda"`` — a distance in a relative
    address puts the number *before* the unit, which this grammar never accepts.
    """
    if not text:
        return False
    folded = _fold(collapse_ws(text))
    match = _KMPOST_RE.search(folded)
    if not match:
        return False
    km = _quantity(match.group("qty"))
    if km is None or not (0.0 <= km <= MAX_KM):
        return False
    masked = _alnum_only(folded)
    masked = masked[: match.start()] + " " * (match.end() - match.start()) + masked[match.end() :]
    return bool(_find_alias(masked, path) or _ROAD_WORD_RE.search(masked))


# --------------------------------------------------------------------------- #
# Calibration and resolution
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Chainage:
    """Where a signed kilometre lands on the centreline, and how much to trust it."""

    #: Distance along the (km-0-first) centreline, in metres, already clamped to the line.
    along_m: float
    quality: Literal["bracketed", "extrapolated", "uncalibrated"]
    #: Calibration points that survived filtering.
    used_points: int
    #: Metres of geometry per metre of signed chainage in the neighbourhood of the query.
    scale: float
    #: Bracket width, or distance to the nearest mojón when extrapolating, in km.
    gap_km: float | None
    #: True when the requested km fell off the end of the mapped geometry.
    clamped: bool
    #: Signed kilometres of the mojones used, for the "why" note under the pin.
    anchors_km: tuple[float, ...] = ()


def _as_calibration(items: Sequence[Any] | None) -> list[CalibrationPoint]:
    """Accept ``(km, lat, lon)`` tuples or ``{"km":…, "lat":…, "lon":…}`` mappings."""
    points: list[CalibrationPoint] = []
    for item in items or ():
        try:
            if isinstance(item, Mapping):
                point = (float(item["km"]), float(item["lat"]), float(item["lon"]))
            else:
                km, lat, lon = tuple(item)[:3]
                point = (float(km), float(lat), float(lon))
        except (TypeError, ValueError, KeyError, IndexError):
            log.debug("ignoring malformed calibration entry %r", item)
            continue
        points.append(point)
    return points


def _prefix_lengths(coords: Sequence[Coord]) -> list[float]:
    """Cumulative distance to each vertex, in the metric ``interpolate_along`` walks."""
    totals = [0.0]
    for i in range(len(coords) - 1):
        totals.append(
            totals[-1] + haversine_m(coords[i][1], coords[i][0], coords[i + 1][1], coords[i + 1][0])
        )
    return totals


def _project_calibration(
    coords: Sequence[Coord], calibration: Sequence[CalibrationPoint], prefix: Sequence[float]
) -> list[tuple[float, float]]:
    """Snap mojones onto the centreline: ``[(signed_m, along_m), …]`` sorted by km.

    The along-distance is recomputed from the snapped point in haversine metres rather than
    taken from ``nearest_point_on_line``'s own figure: that one is summed in the local
    equirectangular frame of the *query* point, which drifts ~0.1 % over a 20 km carretera —
    30 m of systematic bias that would otherwise be baked into every calibrated answer,
    because the point is finally placed with :func:`common.geo.interpolate_along`, which
    measures in great-circle metres.  Same line, same ruler.

    Two filters, both defensive against the state of the ``kmpost`` table:

    * a point further than :data:`CALIBRATION_MAX_OFFSET_M` from the line is not a mojón of
      this highway (a mis-keyed row, or a sign photographed on a parallel service road);
    * the remaining points must be monotonic — along-distance rising with signed km.  A
      point that goes backwards means either a bad sign reading or a centreline that
      doubles back on itself, and interpolating through it would invert a whole segment.
    """
    projected: dict[float, tuple[float, float]] = {}  # signed_m -> (along_m, offset_m)
    for km, lat, lon in calibration:
        if not (0.0 <= km <= MAX_KM):
            log.debug("calibration km %.3f out of range", km)
            continue
        snapped, offset_m, _projected_along_m, index = nearest_point_on_line(lat, lon, coords)
        if offset_m > CALIBRATION_MAX_OFFSET_M:
            log.debug("calibration Km %.3f is %.0f m off the centreline; dropped", km, offset_m)
            continue
        along_m = prefix[index] + haversine_m(
            coords[index][1], coords[index][0], snapped[0], snapped[1]
        )
        signed_m = km * 1000.0
        previous = projected.get(signed_m)
        # Duplicate km (an OSM milestone and a photographed sign): keep the closer one.
        if previous is None or offset_m < previous[1]:
            projected[signed_m] = (along_m, offset_m)

    monotonic: list[tuple[float, float]] = []
    for signed_m in sorted(projected):
        along_m = projected[signed_m][0]
        if monotonic and along_m <= monotonic[-1][1]:
            log.debug(
                "calibration Km %.3f is not monotonic along the line; dropped", signed_m / 1000
            )
            continue
        monotonic.append((signed_m, along_m))
    return monotonic


def _segment_scale(lo: tuple[float, float], hi: tuple[float, float]) -> float:
    """Geometry metres per signed metre between two mojones, clamped to the sane range.

    A scale outside [0.5, 2] means the two points cannot both be right — a sign misread as
    Km 5 instead of Km 15 produces exactly that — so the segment is treated as unusable and
    the geometry is trusted 1:1 instead of being stretched by a factor of three.
    """
    d_signed = hi[0] - lo[0]
    if d_signed <= 0:
        return 1.0
    scale = (hi[1] - lo[1]) / d_signed
    if not (0.5 <= scale <= 2.0):
        log.warning("implausible km-post calibration scale %.3f between %s and %s", scale, lo, hi)
        return 1.0
    return scale


def _chainage(
    km: float, coords: Sequence[Coord], calibration: Sequence[CalibrationPoint]
) -> Chainage:
    """Turn a signed kilometre into an along-line distance.

    The model is a piecewise-linear map from *signed* chainage (what the mojones say) to
    *geometric* chainage (metres along the OSM centreline), with the surveyed mojones as
    its knots::

        along(k) = a_i + (k - k_i) · (a_{i+1} - a_i) / (k_{i+1} - k_i)      k_i ≤ k ≤ k_{i+1}

    Outside the mojones' range it extrapolates with the nearest segment's slope, so a
    highway calibrated only between Km 10 and Km 20 still corrects Km 25 by the local
    stretch factor instead of falling back to raw geometry.  With exactly one mojón the
    slope is anchored through the origin (``a_0 / k_0``), which is the best a single point
    supports.  With none, signed metres are used as geometric metres.

    Failure modes, all of which the confidence has to reflect:

    * **no mojones** — a 1–3 % curve-error bias, so ~150–450 m at Km 15, always in the same
      direction (OSM long, sign short).  This is the common case today.
    * **extrapolation** — the local stretch factor is assumed to continue.  It does not
      across a realignment: the Masaya bypass shortened the road without renumbering the
      signs, so a factor fitted before it is wrong after it.
    * **a gap in the merged centreline** — missing ways shorten the line.  Mojones on
      either side of the gap absorb it; a query *inside* the gap cannot be right.
    * **a wrong sign** — the filters in :func:`_project_calibration` catch the gross cases
      (off the line, out of order); a sign that is merely 500 m off is indistinguishable
      from good data and propagates into every query between its neighbours.
    """
    prefix = _prefix_lengths(coords)
    length_m = prefix[-1]
    anchors = _project_calibration(coords, calibration, prefix)
    signed_m = km * 1000.0

    if not anchors:
        raw_along = signed_m
        quality: Literal["bracketed", "extrapolated", "uncalibrated"] = "uncalibrated"
        scale = 1.0
        gap_km: float | None = None
        anchors_km: tuple[float, ...] = ()
    elif len(anchors) == 1:
        k0, a0 = anchors[0]
        scale = a0 / k0 if k0 >= 500.0 else 1.0
        scale = scale if 0.5 <= scale <= 2.0 else 1.0
        raw_along = a0 + (signed_m - k0) * scale
        quality = "extrapolated"
        gap_km = abs(signed_m - k0) / 1000.0
        anchors_km = (k0 / 1000.0,)
    else:
        quality = "extrapolated"
        gap_km = None
        anchors_km = ()
        if signed_m < anchors[0][0]:
            scale = _segment_scale(anchors[0], anchors[1])
            raw_along = anchors[0][1] + (signed_m - anchors[0][0]) * scale
            gap_km = (anchors[0][0] - signed_m) / 1000.0
            anchors_km = (anchors[0][0] / 1000.0,)
        elif signed_m > anchors[-1][0]:
            scale = _segment_scale(anchors[-2], anchors[-1])
            raw_along = anchors[-1][1] + (signed_m - anchors[-1][0]) * scale
            gap_km = (signed_m - anchors[-1][0]) / 1000.0
            anchors_km = (anchors[-1][0] / 1000.0,)
        else:
            # A query landing exactly on the last mojón still counts as bracketed, hence the
            # clamp: it is measured, not extrapolated.
            index = min(
                max(i for i, (k, _a) in enumerate(anchors) if k <= signed_m), len(anchors) - 2
            )
            lo, hi = anchors[index], anchors[index + 1]
            scale = _segment_scale(lo, hi)
            raw_along = lo[1] + (signed_m - lo[0]) * scale
            quality = "bracketed"
            gap_km = (hi[0] - lo[0]) / 1000.0
            anchors_km = (lo[0] / 1000.0, hi[0] / 1000.0)

    along_m = min(max(raw_along, 0.0), length_m)
    # A metre of slop absorbs the difference between summing segment lengths and walking
    # them, so a query at exactly the end of the line is not reported as off the map.
    clamped = abs(along_m - raw_along) > 1.0
    return Chainage(
        along_m=along_m,
        quality=quality,
        used_points=len(anchors),
        scale=scale,
        gap_km=gap_km,
        clamped=clamped,
        anchors_km=anchors_km,
    )


def _orient(coords: Sequence[Coord], highway: Highway | None) -> tuple[list[Coord], bool]:
    """Order the centreline so index 0 is km 0; report whether the origin is confirmed.

    The registry's contract is that stored geometry already starts at km 0, but a merged
    line comes out of PostGIS in whatever direction the ways were assembled, and a reversed
    carretera puts Km 5 where Km 25 belongs.  Comparing both ends against the row's km0 is
    cheap insurance.  When neither end is near km0 — an unknown key, an urban pista with no
    signed origin, or a synthetic line — the geometry is used as given and the caller is
    told the origin is unverified so the confidence can absorb it.
    """
    points = list(coords)
    km0 = highway.km0 if highway else None
    if km0 is None or len(points) < 2:
        return points, False
    start_m = haversine_m(points[0][1], points[0][0], km0[0], km0[1])
    end_m = haversine_m(points[-1][1], points[-1][0], km0[0], km0[1])
    if start_m <= end_m and start_m <= KM0_ANCHOR_MAX_M:
        return points, True
    if end_m < start_m and end_m <= KM0_ANCHOR_MAX_M:
        log.info("centreline for %s runs backwards from km 0; reversed", highway.highway_key)
        return list(reversed(points)), True
    return points, False


def _travel_bearing(coords: Sequence[Coord], along_m: float, length_m: float) -> float | None:
    """Local direction of increasing kilometraje at ``along_m``, in degrees."""
    lo = max(0.0, min(length_m, along_m - _BEARING_WINDOW_M))
    hi = max(0.0, min(length_m, along_m + _BEARING_WINDOW_M))
    if hi - lo < 1.0:
        return None
    lat1, lon1 = interpolate_along(coords, lo)
    lat2, lon2 = interpolate_along(coords, hi)
    if haversine_m(lat1, lon1, lat2, lon2) < 0.5:
        return None
    return initial_bearing_deg(lat1, lon1, lat2, lon2)


def _confidence(chainage: Chainage, *, km0_verified: bool, known_highway: bool) -> float:
    """Score a km-post pin in [0, 1].  Pessimistic, like ``relative_address._confidence``.

    A mojón marks a kilometre of road, and a business "at Km 14" can be anywhere in the
    500 m either side of the sign, so even a perfectly calibrated hit tops out around 0.9.
    From there:

    * **bracketed** by two nearby mojones is the good case; the wider the bracket, the more
      of the interpolation is assumption rather than measurement;
    * **extrapolated** past the last mojón carries the local stretch factor into territory
      that may have been realigned, and decays with how far past it goes;
    * **uncalibrated** is raw OSM geometry — biased long by curve error, so systematically
      short in signed kilometres. It never scores above 0.55;
    * **clamped** means the requested km is off the end of the mapped line. The point
      returned is the end of the carretera, which is not the address, and the score says so.

    The km-0 penalty only applies when the answer is not bracketed: between two mojones the
    origin is irrelevant, because the mojones themselves fix the chainage.
    """
    if chainage.quality == "bracketed":
        score = 0.90 - min(0.20, 0.010 * (chainage.gap_km or 0.0))
    elif chainage.quality == "extrapolated":
        score = 0.72 - min(0.25, 0.020 * (chainage.gap_km or 0.0))
    else:
        score = 0.55
    if not known_highway:
        score *= 0.90
    if chainage.quality != "bracketed" and not km0_verified:
        score *= 0.88
    if abs(chainage.scale - 1.0) > 0.15:
        # The signs and the geometry disagree by more than 15 %: the correction is doing a
        # lot of work, and whatever made the geometry that wrong may not be uniform.
        score *= 0.95
    if chainage.clamped:
        score *= 0.45
    return round(max(0.0, min(1.0, score)), 4)


def _notes(
    parsed: KmPost, highway: Highway | None, chainage: Chainage, *, km0_verified: bool
) -> list[str]:
    """Short Spanish explanations shown under the pin so a user can judge it."""
    notes: list[str] = []
    if chainage.quality == "bracketed":
        lo, hi = chainage.anchors_km[0], chainage.anchors_km[-1]
        notes.append(f"Calibrado entre mojones Km {lo:g} y Km {hi:g}")
    elif chainage.quality == "extrapolated":
        nearest = chainage.anchors_km[0] if chainage.anchors_km else 0.0
        notes.append(f"Extrapolado desde el mojón Km {nearest:g}")
    else:
        notes.append("Sin mojones calibrados: medido sobre la geometría de OSM")
    if chainage.clamped:
        notes.append("El kilómetro pedido queda fuera del tramo cartografiado")
    if not km0_verified:
        notes.append("Origen del kilometraje sin verificar")
    if parsed.side_hint:
        notes.append(
            f"Mano {parsed.side_hint} en sentido de kilometraje ascendente "
            f"({SIDE_OFFSET_M:.0f} m del eje)"
        )
    notes.append("Un mojón cubre ~1 km de vía: confirme el punto exacto")
    if highway is None and parsed.highway_text:
        notes.append(f"Carretera no registrada: {parsed.highway_text}")
    return notes


def resolve(
    parsed: KmPost | None,
    geometry_lookup: GeometryLookup,
    calibration: Sequence[Any] | None = None,
    *,
    side_offset_m: float = SIDE_OFFSET_M,
    highway: Highway | None = None,
    registry_path: Path | str | None = None,
) -> list[GeocodeCandidate]:
    """Turn a parse into scored candidates by measuring along the carretera.

    ``geometry_lookup(highway_key)`` returns the highway's ordered centreline as GeoJSON
    ``[[lon, lat], …]``; injecting it keeps this function free of PostGIS, exactly as
    ``relative_address.resolve`` injects its gazetteer lookup.  ``calibration`` is the
    ``kmpost`` table for that highway as ``(km, lat, lon)`` triples (mappings with those
    keys are accepted too); see :func:`_chainage` for what is done with them.

    Returns at most one candidate — a km post is one point, and offering three would be
    theatre.  Returns nothing at all when the highway is unknown or unmapped: an honest
    empty result lets ``/api/search`` fall through to the plain index query.
    """
    if parsed is None or not parsed.highway_key:
        return []
    coords = geometry_lookup(parsed.highway_key)
    if not coords or len(coords) < 2:
        log.debug("no centreline for highway %s", parsed.highway_key)
        return []

    highway = highway or highway_by_key(parsed.highway_key, registry_path)
    oriented, km0_verified = _orient(coords, highway)
    chainage = _chainage(parsed.km, oriented, _as_calibration(calibration))

    lat, lon = interpolate_along(oriented, chainage.along_m)
    if parsed.side_hint:
        length_m = line_length_m(oriented)
        bearing = _travel_bearing(oriented, chainage.along_m, length_m)
        if bearing is not None:
            # Nicaragua drives on the right, and a km post is quoted in the outbound
            # direction (leaving Managua, i.e. increasing kilometraje), so "mano derecha"
            # is 90° clockwise from the direction of travel.
            offset_deg = (bearing + 90.0) % 360.0
            if parsed.side_hint == "izquierda":
                offset_deg = (bearing - 90.0) % 360.0
            lat, lon = destination_point(lat, lon, offset_deg, side_offset_m)

    label = f"Km {parsed.km:g} {highway.display_name if highway else (parsed.highway_text or '')}"
    return [
        GeocodeCandidate(
            lat=lat,
            lon=lon,
            label=collapse_ws(label),
            confidence=_confidence(
                chainage, km0_verified=km0_verified, known_highway=highway is not None
            ),
            method=GeocodeMethod.KMPOST,
            notes=_notes(parsed, highway, chainage, km0_verified=km0_verified),
        )
    ]


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _load_geometry(path: Path) -> list[list[float]]:
    """Read a centreline from GeoJSON (Feature, FeatureCollection or bare geometry).

    Also accepts a plain ``[[lon, lat], …]`` array, which is what a hand-made fixture
    usually looks like.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        coords = data
    else:
        node: Any = data
        if node.get("type") == "FeatureCollection":
            features = node.get("features") or []
            if not features:
                raise ValueError(f"{path} has no features")
            node = features[0]
        if node.get("type") == "Feature":
            node = node.get("geometry") or {}
        if node.get("type") != "LineString":
            raise ValueError(f"{path} is not a LineString (got {node.get('type')!r})")
        coords = node.get("coordinates") or []
    points = [[float(lon), float(lat)] for lon, lat in coords]
    if len(points) < 2:
        raise ValueError(f"{path} has fewer than two coordinates")
    return points


def _load_calibration(path: Path) -> list[CalibrationPoint]:
    """Read ``[[km, lat, lon], …]`` or ``[{"km":…, "lat":…, "lon":…}, …]``."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, Mapping):
        data = data.get("points") or data.get("kmposts") or []
    return _as_calibration(list(data))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m pipeline.geocode.kmpost",
        description="Parse and resolve a Nicaraguan km-post address (Km 12.5 Carretera a Masaya).",
    )
    parser.add_argument("query", nargs="?", help="the address to parse, quoted")
    parser.add_argument(
        "--geometry",
        default=None,
        help="GeoJSON LineString with the highway centreline; without it only the parse is printed",
    )
    parser.add_argument(
        "--calibration",
        default=None,
        help="JSON list of [km, lat, lon] mojones (the kmpost table for this highway)",
    )
    parser.add_argument(
        "--side-offset-m",
        type=float,
        default=SIDE_OFFSET_M,
        help=f"metres off the centreline for 'mano derecha/izquierda' (default: {SIDE_OFFSET_M:g})",
    )
    parser.add_argument(
        "--list-highways",
        action="store_true",
        help="print the registry from docs/carreteras.csv and exit",
    )
    parser.add_argument("--indent", type=int, default=2, help="JSON indent (default: 2)")
    parser.add_argument("--log-level", default="WARNING", help="logging level (default: WARNING)")
    return parser


def _emit(payload: Any, indent: int) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=indent) + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    """One-off CLI lookup.  Returns 0 on success and non-zero on any failure."""
    args = _build_parser().parse_args(argv)
    from pipeline.common.io import setup_logging

    setup_logging(args.log_level)

    if args.list_highways:
        _emit(
            [
                {
                    "highway_key": highway.highway_key,
                    "display_name": highway.display_name,
                    "aliases": list(highway.aliases),
                    "osm_ref": highway.osm_ref,
                    "km0_lat": highway.km0_lat,
                    "km0_lon": highway.km0_lon,
                    "note": highway.note,
                }
                for highway in all_highways()
            ],
            args.indent,
        )
        return 0

    if not args.query:
        log.error("nothing to do: pass a query or --list-highways")
        return 2

    parsed = parse(args.query)
    if parsed is None:
        log.error("not a km-post address: %r", args.query)
        return 1

    payload: dict[str, Any] = {
        "query": args.query,
        "parsed": {
            "km": parsed.km,
            "highway_key": parsed.highway_key,
            "highway_text": parsed.highway_text,
            "side_hint": parsed.side_hint,
            "label": parsed.render(),
        },
        "candidates": [],
    }

    if args.geometry:
        geometry_path = Path(args.geometry)
        if not geometry_path.exists():
            log.error("geometry file not found: %s", geometry_path)
            return 2
        calibration: list[CalibrationPoint] = []
        if args.calibration:
            calibration_path = Path(args.calibration)
            if not calibration_path.exists():
                log.error("calibration file not found: %s", calibration_path)
                return 2
            calibration = _load_calibration(calibration_path)
        try:
            coords = _load_geometry(geometry_path)
        except ValueError as exc:
            log.error("%s", exc)
            return 2
        candidates = resolve(
            parsed,
            lambda _key: coords,
            calibration,
            side_offset_m=args.side_offset_m,
        )
        payload["candidates"] = [candidate.model_dump(mode="json") for candidate in candidates]

    _emit(payload, args.indent)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
