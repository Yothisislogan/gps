"""Parser and resolver for Nicaraguan landmark-relative addresses.

Nicaragua does not use street addresses.  A real one looks like:

    De la Rotonda El Güegüense, 2 cuadras al sur, 1 cuadra abajo, casa esquinera
    Del Colonial Los Robles 1c al lago, 75 vrs abajo, portón negro
    Donde fue el Cine Cabrera, media cuadra arriba
    Busto José Martí, 30 metros hacia el este (arriba)

The grammar is: a landmark, then a chain of ``<quantity> <unit> <direction>``
hops, then free-text modifiers that help a human find the door but carry no
geometry.  Directions are Managua's topographic slang — ``al lago`` is north
(Lake Managua), ``a la montaña`` south, ``arriba`` east (sunrise) and ``abajo``
west.

Two responsibilities, deliberately split:

* :func:`parse` is **pure** — no I/O, no database, no network.  It turns a
  string into a :class:`~common.models.RelativeAddress`.
* :func:`resolve` takes that parse plus a *lookup callable* and walks the offset
  vectors from the landmark, producing scored
  :class:`~common.models.GeocodeCandidate` objects.  Injecting the lookup keeps
  the whole module testable without Meilisearch, PostGIS or Valhalla running.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from common.geo import (
    DEFAULT_CUADRA_M,
    VARA_M,
    cuadra_length_m,
    destination_point,
    resolve_direction,
)
from common.models import GeocodeCandidate, GeocodeMethod, RelativeAddress, RelativeOffset
from common.text import collapse_ws, parse_spanish_number, strip_accents

__all__ = [
    "LandmarkMatch",
    "looks_like_relative_address",
    "parse",
    "resolve",
    "unit_to_metres",
]

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #

#: Written unit -> canonical unit.  Nicaraguans abbreviate aggressively and
#: inconsistently: "2c", "2 c.", "2 cds", "2 cuadras" are all two blocks.
UNIT_ALIASES: dict[str, str] = {
    "c": "cuadra",
    "cs": "cuadra",
    "cd": "cuadra",
    "cds": "cuadra",
    "cua": "cuadra",
    "cuad": "cuadra",
    "cuadra": "cuadra",
    "cuadras": "cuadra",
    "v": "vara",
    "vr": "vara",
    "vrs": "vara",
    "var": "vara",
    "vara": "vara",
    "varas": "vara",
    "m": "metro",
    "mt": "metro",
    "mts": "metro",
    "mtr": "metro",
    "mtrs": "metro",
    "metro": "metro",
    "metros": "metro",
    "km": "kilometro",
    "kms": "kilometro",
    "kilometro": "kilometro",
    "kilometros": "kilometro",
}

#: Direction phrases, longest first so "a la montana" wins over "a la".
_DIRECTION_PHRASES: tuple[str, ...] = (
    "hacia el noreste",
    "hacia el noroeste",
    "hacia el sureste",
    "hacia el suroeste",
    "hacia la montana",
    "hacia el oriente",
    "hacia el poniente",
    "hacia el occidente",
    "hacia el norte",
    "hacia el lago",
    "hacia el este",
    "hacia el oeste",
    "hacia el sur",
    "hacia arriba",
    "hacia abajo",
    "a la montana",
    "al occidente",
    "al poniente",
    "al oriente",
    "al noreste",
    "al noroeste",
    "al sureste",
    "al suroeste",
    "al norte",
    "al lago",
    "al este",
    "al oeste",
    "al sur",
    "noreste",
    "noroeste",
    "sureste",
    "suroeste",
    "montana",
    "arriba",
    "abajo",
    "norte",
    "sur",
    "este",
    "oeste",
    "lago",
)

_DIRECTION_ALTERNATION = "|".join(re.escape(phrase) for phrase in _DIRECTION_PHRASES)

#: Quantities: digits with optional decimal, vulgar fractions, or Spanish words.
_QUANTITY_WORDS = (
    "una|uno|un|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez|once|doce|trece|catorce|quince|"
    "dieciseis|diecisiete|dieciocho|diecinueve|veinticinco|veinte|treinta|cuarenta|cincuenta|"
    "sesenta|setenta|ochenta|noventa|cien|ciento|media|medio"
)
_QUANTITY = rf"(?:\d+(?:[.,]\d+)?\s*(?:½|¼|¾|1/2|1/4|3/4)?|½|¼|¾|\d+\s*/\s*\d+|(?:{_QUANTITY_WORDS})(?:\s+y\s+media|\s+y\s+medio)?)"
_UNIT = "|".join(sorted((re.escape(u) for u in UNIT_ALIASES), key=len, reverse=True))

#: "2 cuadras al sur" / "75 vrs abajo" / "media cuadra arriba".
#: The direction must follow the unit directly — no "y" in between.  " y " joins
#: two hops ("1c al sur y 1c abajo"), so allowing it here would let the second
#: hop's direction bind to the first hop's quantity.
#: "cuadra y media" is 1.5 blocks and "media cuadra" is 0.5 — the fraction can
#: sit on either side of the unit, and Nicaraguans use both freely.
_TRAILING_FRACTION = r"(?P<frac>\s+y\s+(?:media|medio|cuarto))?"

_OFFSET_QUD_RE = re.compile(
    rf"\b(?P<qty>{_QUANTITY})\s*\.?\s*(?P<unit>{_UNIT})\b\.?{_TRAILING_FRACTION}\s*(?P<dir>{_DIRECTION_ALTERNATION})?\b",
    re.IGNORECASE,
)
#: "al sur 2 cuadras" — direction first, which older sign-writing prefers.
_OFFSET_DQU_RE = re.compile(
    rf"\b(?P<dir>{_DIRECTION_ALTERNATION})\s+(?P<qty>{_QUANTITY})\s*\.?\s*(?P<unit>{_UNIT})\b\.?{_TRAILING_FRACTION}",
    re.IGNORECASE,
)
#: "al sur cuadra y media" — direction first *and* the quantity implied.
_OFFSET_DUFRAC_RE = re.compile(
    rf"\b(?P<dir>{_DIRECTION_ALTERNATION})\s+(?P<unit>cuadra|cuadras|c)\b\.?(?P<frac>\s+y\s+(?:media|medio|cuarto))",
    re.IGNORECASE,
)
#: "cuadra y media al sur" — the quantity is implied to be one.
_OFFSET_UFRAC_RE = re.compile(
    rf"\b(?P<unit>cuadra|cuadras|c)\b\.?(?P<frac>\s+y\s+(?:media|medio|cuarto))\s*(?P<dir>{_DIRECTION_ALTERNATION})?\b",
    re.IGNORECASE,
)

#: Markers of a landmark that no longer exists.  These are load-bearing: half of
#: Managua navigates by buildings the 1972 earthquake took down.
_FORMER_MARKERS: tuple[str, ...] = (
    "donde fue",
    "donde estuvo",
    "donde era",
    "de donde fue",
    "lo que fue",
    "antiguo",
    "antigua",
    "ex ",
    "ex-",
    "viejo",
    "vieja",
)

#: Free-text modifiers kept verbatim for display and never turned into geometry.
_MODIFIER_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"frente\s+a[l]?\s+.+",
        r"contiguo\s+a[l]?\s+.+",
        r"al\s+lado\s+de[l]?\s+.+",
        r"a\s+la\s+par\s+de[l]?\s+.+",
        r"esquina\s+opuesta.*",
        r"casa\s+esquinera",
        r"esquinera",
        r"mano\s+(?:derecha|izquierda)",
        r"porton\s+\w+",
        r"portón\s+\w+",
        r"casa\s+(?:color\s+)?\w+",
        r"edificio\s+\w+",
        r"segundo\s+piso",
        r"planta\s+alta",
        r"sobre\s+la\s+.+",
        r"detras\s+de[l]?\s+.+",
        r"atras\s+de[l]?\s+.+",
    )
)

#: Leading noise: "de la", "del", "desde el", "a partir de la".
_LEADING_NOISE_RE = re.compile(
    r"^(?:a\s+partir\s+de\s+|desde\s+|de\s+|del\s+|de\s+la\s+|de\s+los\s+|de\s+las\s+)+",
    re.IGNORECASE,
)
_LEADING_ARTICLE_RE = re.compile(r"^(?:el|la|los|las)\s+", re.IGNORECASE)

_SIDE_RE = re.compile(r"mano\s+(derecha|izquierda)", re.IGNORECASE)

#: A leading proximity phrase ("frente al Colegio X") is a modifier wrapped
#: around the landmark, not part of its name — the gazetteer knows "Colegio X".
_LEADING_ANCHOR_RE = re.compile(
    r"^(?P<anchor>frente\s+a[l]?|contiguo\s+a[l]?|a\s+la\s+par\s+de[l]?|al\s+lado\s+de[l]?|"
    r"detras\s+de[l]?|atras\s+de[l]?|detrás\s+de[l]?|atrás\s+de[l]?|"
    r"esquina\s+opuesta\s+a[l]?|diagonal\s+a[l]?)\s+(?P<rest>.+)$",
    re.IGNORECASE,
)


def unit_to_metres(quantity: float, unit: str, *, city: str | None = None) -> float:
    """Convert a quantity in Nicaraguan units to metres.

    A ``cuadra`` is a city block whose length depends on the colonial grid it was
    laid out on, so it is looked up per city (100 m in Managua and Granada).  A
    ``vara`` is 0.836 m — the unit surveyors used and speech never abandoned.
    """
    if unit == "cuadra":
        return quantity * cuadra_length_m(city)
    if unit == "vara":
        return quantity * VARA_M
    if unit == "kilometro":
        return quantity * 1000.0
    if unit == "metro":
        return quantity
    raise ValueError(f"unknown unit {unit!r}")


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def _normalise_for_match(text: str) -> str:
    """Accent-fold and lowercase while keeping the character offsets aligned.

    Offsets matter: the parser matches on the folded string and then slices the
    *original* to keep the landmark's real spelling for display.  Accent folding
    with NFD would change the length, so it is done character by character.
    """
    return "".join(strip_accents(ch).lower() or ch for ch in text)


def _clean_quantity(raw: str) -> float | None:
    value = parse_spanish_number(raw)
    if value is not None:
        return value
    # "1 / 2" with spaces, or "3 1/2" with an odd separator.
    compact = re.sub(r"\s+", "", raw)
    return parse_spanish_number(compact)


def _detect_former(folded: str) -> tuple[bool, str]:
    """Return ``(is_former, marker)`` for a landmark phrase."""
    for marker in _FORMER_MARKERS:
        if re.search(rf"\b{re.escape(marker.strip())}\b", folded):
            return True, marker.strip()
    return False, ""


def _strip_landmark_noise(text: str) -> str:
    """Drop leading prepositions and articles: "De la Rotonda X" -> "Rotonda X"."""
    cleaned = _LEADING_NOISE_RE.sub("", collapse_ws(text)).strip(" ,.;:-")
    cleaned = _LEADING_ARTICLE_RE.sub("", cleaned).strip(" ,.;:-")
    return cleaned


def _extract_modifiers(tail: str) -> list[str]:
    """Pull display-only phrases out of the trailing text, longest match first."""
    modifiers: list[str] = []
    for chunk in re.split(r"[,;]", tail):
        candidate = collapse_ws(chunk).strip(" .")
        if not candidate:
            continue
        folded = _normalise_for_match(candidate)
        if resolve_direction(folded) is not None:
            continue  # a bare trailing direction is not a modifier
        for pattern in _MODIFIER_PATTERNS:
            if pattern.fullmatch(folded) or pattern.match(folded):
                modifiers.append(candidate)
                break
        else:
            if len(candidate) > 2:
                modifiers.append(candidate)
    return modifiers


@dataclass(frozen=True)
class _RawOffset:
    start: int
    end: int
    quantity: float
    unit: str
    direction_text: str
    bearing_deg: float | None


def _scan_offsets(folded: str, original: str) -> list[_RawOffset]:
    """Find every ``quantity unit direction`` hop, in reading order."""
    found: list[_RawOffset] = []
    taken: list[tuple[int, int]] = []

    def overlaps(start: int, end: int) -> bool:
        return any(start < t_end and end > t_start for t_start, t_end in taken)

    # Three passes, in this order, because the two orderings of the same words
    # are ambiguous: in "2 cuadras al sur 1 cuadra abajo" the direction belongs
    # to the quantity *before* it, while in "al sur 2 cuadras" it belongs to the
    # quantity after.  Claiming complete quantity-unit-direction hops first
    # settles every case that has both readings; only then are direction-first
    # hops matched in what is left, and finally dirless quantities are recorded
    # (they are dropped from the geometry, but the log line is worth having).
    passes = (
        (_OFFSET_QUD_RE, True),
        (_OFFSET_DQU_RE, False),
        (_OFFSET_DUFRAC_RE, False),
        (_OFFSET_UFRAC_RE, False),
        (_OFFSET_QUD_RE, False),
    )
    for regex, require_direction in passes:
        for match in regex.finditer(folded):
            if overlaps(match.start(), match.end()):
                continue
            if require_direction and not match.group("dir"):
                continue
            groups = match.groupdict()
            quantity = _clean_quantity(groups["qty"]) if groups.get("qty") else 1.0
            if quantity is None:
                continue
            fraction = groups.get("frac")
            if fraction:
                quantity += {"media": 0.5, "medio": 0.5, "cuarto": 0.25}[
                    fraction.strip().split()[-1]
                ]
            unit = UNIT_ALIASES.get(match.group("unit").lower().rstrip("."))
            if unit is None:
                continue
            direction_raw = match.group("dir") or ""
            bearing = resolve_direction(direction_raw) if direction_raw else None
            # A parenthetical gloss often carries the direction: "hacia el este (arriba)".
            if bearing is None:
                gloss = re.match(r"\s*\(([^)]+)\)", folded[match.end() :])
                if gloss:
                    bearing = resolve_direction(gloss.group(1))
                    if bearing is not None:
                        direction_raw = gloss.group(1)
            taken.append((match.start(), match.end()))
            found.append(
                _RawOffset(
                    start=match.start(),
                    end=match.end(),
                    quantity=quantity,
                    unit=unit,
                    direction_text=collapse_ws(original[match.start() : match.end()])
                    if not direction_raw
                    else collapse_ws(direction_raw),
                    bearing_deg=bearing,
                )
            )
    return sorted(found, key=lambda o: o.start)


def parse(text: str, *, city: str | None = None) -> RelativeAddress | None:
    """Parse a Nicaraguan relative address.  Pure function; no I/O.

    Returns ``None`` when the string carries no landmark at all (an empty or
    purely numeric input).  A string with a landmark but no offsets *does* parse
    — "frente al Colegio Centroamérica" is a perfectly good address — and comes
    back with an empty ``offsets`` list, which the resolver scores lower.

    ``city`` selects the cuadra length; it does not otherwise affect the parse.
    """
    raw = collapse_ws(text or "")
    if not raw:
        return None

    folded = _normalise_for_match(raw)
    offsets_raw = _scan_offsets(folded, raw)

    if offsets_raw:
        landmark_slice = raw[: offsets_raw[0].start]
        tail = raw[offsets_raw[-1].end :]
    else:
        # No hops: the whole string is the landmark plus modifiers.  Split on the
        # first comma so "Iglesia El Calvario, portón negro" keeps both parts.
        head, _, rest = raw.partition(",")
        landmark_slice, tail = head, rest

    landmark_display = collapse_ws(landmark_slice).strip(" ,.;:-")
    if not landmark_display:
        return None

    folded_landmark = _normalise_for_match(landmark_display)
    is_former, marker = _detect_former(folded_landmark)

    landmark_query = _strip_landmark_noise(landmark_display)
    anchor_modifier: str | None = None
    anchored = _LEADING_ANCHOR_RE.match(landmark_query)
    if anchored:
        anchor_modifier = collapse_ws(anchored.group("anchor"))
        landmark_query = _strip_landmark_noise(anchored.group("rest"))
    if is_former and marker:
        # "donde fue el Cine Cabrera" -> query "Cine Cabrera", flag set.
        pattern = re.compile(rf"^\W*{re.escape(marker)}\b\W*", re.IGNORECASE)
        landmark_query = _strip_landmark_noise(
            pattern.sub("", _strip_landmark_noise(landmark_query))
        )
    if not landmark_query:
        landmark_query = landmark_display

    offsets: list[RelativeOffset] = []
    for item in offsets_raw:
        if item.bearing_deg is None:
            # A hop with no direction ("200 metros") cannot be walked; keep it out
            # of the geometry but let the caller see it in the raw string.
            log.debug("offset without direction in %r: %s %s", raw, item.quantity, item.unit)
            continue
        offsets.append(
            RelativeOffset(
                quantity=item.quantity,
                unit=item.unit,  # type: ignore[arg-type]
                direction_text=item.direction_text,
                bearing_deg=item.bearing_deg,
                distance_m=unit_to_metres(item.quantity, item.unit, city=city),
            )
        )

    modifiers = _extract_modifiers(tail)
    if anchor_modifier:
        modifiers.insert(0, anchor_modifier)
    side_match = _SIDE_RE.search(_normalise_for_match(raw))
    side_hint = side_match.group(1).lower() if side_match else None

    return RelativeAddress(
        raw=raw,
        landmark_text=landmark_display,
        landmark_query=landmark_query,
        offsets=offsets,
        modifiers=modifiers,
        former_landmark=is_former,
        side_hint=side_hint,  # type: ignore[arg-type]
        total_distance_m=sum(offset.distance_m for offset in offsets),
    )


def looks_like_relative_address(text: str) -> bool:
    """Cheap gate for the search endpoint: is this worth parsing as an address?

    True when the string contains a direction word *and* something that could be
    a quantity+unit, or an explicit "donde fue"/"frente a" marker.  Keeping this
    conservative matters: every false positive costs a gazetteer lookup on a
    query the user meant as a plain name search.
    """
    if not text:
        return False
    folded = _normalise_for_match(collapse_ws(text))
    has_offset = bool(_OFFSET_QUD_RE.search(folded) or _OFFSET_DQU_RE.search(folded))
    has_direction = any(
        re.search(rf"\b{re.escape(phrase)}\b", folded) for phrase in _DIRECTION_PHRASES
    )
    has_former = any(marker.strip() in folded for marker in _FORMER_MARKERS)
    has_anchor = bool(
        re.search(
            r"\b(frente a|frente al|contiguo a|contiguo al|a la par de|al lado de|esquina opuesta)\b",
            folded,
        )
    )
    return (has_offset and has_direction) or has_former or has_anchor


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class LandmarkMatch:
    """One gazetteer hit, as returned by the lookup callable passed to :func:`resolve`."""

    id: str
    name: str
    lat: float
    lon: float
    score: float = 1.0  # name-match quality in [0, 1]
    city: str | None = None
    former: bool = False
    kind: str | None = None
    popularity: float = 0.0


LandmarkLookup = Callable[[str, bool], Iterable[LandmarkMatch]]
Snapper = Callable[[float, float], tuple[float, float] | None]


def _walk(lat: float, lon: float, offsets: Sequence[RelativeOffset]) -> tuple[float, float]:
    """Apply each hop in order from the landmark."""
    for offset in offsets:
        lat, lon = destination_point(lat, lon, offset.bearing_deg, offset.distance_m)
    return lat, lon


def _confidence(parsed: RelativeAddress, match: LandmarkMatch, *, snapped: bool) -> float:
    """Score a candidate in [0, 1].

    Deliberately pessimistic.  A pin that claims 0.95 and lands two blocks away
    teaches users to distrust every pin; one that says 0.6 and offers a drag
    correction teaches them to help.  Contributions:

    * the landmark's own name-match score dominates (a fuzzy landmark match can
      never produce a confident address);
    * each additional hop compounds the cuadra-length assumption, so long chains
      decay;
    * a walk of more than ~1.5 km is almost certainly a misparse;
    * snapping to a road is mild positive evidence;
    * a "donde fue" address that matched a still-standing landmark is suspicious.
    """
    score = max(0.0, min(1.0, match.score))
    if not parsed.offsets:
        score *= 0.75  # landmark only: the pin is the landmark, not the door
    else:
        score *= 0.97 ** (len(parsed.offsets) - 1)
        if parsed.total_distance_m > 1_500:
            score *= 0.7
        elif parsed.total_distance_m > 800:
            score *= 0.88
    if parsed.former_landmark and not match.former:
        score *= 0.8
    if snapped:
        score = min(1.0, score * 1.05)
    return round(max(0.0, min(1.0, score)), 4)


def resolve(
    parsed: RelativeAddress,
    lookup: LandmarkLookup,
    *,
    city: str | None = None,
    snap: Snapper | None = None,
    limit: int = 3,
) -> list[GeocodeCandidate]:
    """Turn a parse into scored candidates by walking from each landmark match.

    ``lookup(query, former_only)`` returns gazetteer matches; injecting it keeps
    this function free of Meilisearch and PostGIS.  ``snap(lat, lon)`` is an
    optional road-snapper (Valhalla ``/locate`` in production) — a door is on a
    street, so snapping usually improves the pin, but a failed snap is not fatal
    and the unsnapped point is returned instead.

    Candidates come back sorted by confidence, best first, at most ``limit``.
    """
    if parsed is None:  # pragma: no cover - defensive
        return []

    query = parsed.landmark_query or parsed.landmark_text
    matches = list(lookup(query, parsed.former_landmark))
    if not matches:
        return []

    # Re-derive distances if the caller knows the city and the parse did not.
    offsets = parsed.offsets
    if city:
        offsets = [
            offset.model_copy(
                update={"distance_m": unit_to_metres(offset.quantity, offset.unit, city=city)}
            )
            for offset in offsets
        ]

    candidates: list[GeocodeCandidate] = []
    for match in matches:
        lat, lon = _walk(match.lat, match.lon, offsets)
        snapped = False
        if snap is not None:
            try:
                result = snap(lat, lon)
            except Exception:
                log.warning("road snap failed for %.5f,%.5f", lat, lon, exc_info=True)
                result = None
            if result is not None:
                lat, lon = result
                snapped = True

        walked = parsed.model_copy(update={"offsets": offsets})
        candidates.append(
            GeocodeCandidate(
                lat=lat,
                lon=lon,
                label=walked.render(),
                confidence=_confidence(walked, match, snapped=snapped),
                method=GeocodeMethod.RELATIVE,
                snapped_to_road=snapped,
                relative=walked,
                landmark_id=match.id,
                landmark_name=match.name,
                notes=_notes(parsed, match),
            )
        )

    candidates.sort(key=lambda c: c.confidence, reverse=True)
    return candidates[:limit]


def _notes(parsed: RelativeAddress, match: LandmarkMatch) -> list[str]:
    """Short Spanish explanations shown under the pin so users can judge it."""
    notes: list[str] = []
    if parsed.former_landmark and match.former:
        notes.append("Punto de referencia histórico")
    elif parsed.former_landmark:
        notes.append("Se buscó un punto de referencia que ya no existe")
    if not parsed.offsets:
        notes.append("Sin distancias: el punto es el punto de referencia")
    if parsed.total_distance_m > 1_500:
        notes.append("Distancia poco usual para una dirección relativa")
    if any(offset.unit == "cuadra" for offset in parsed.offsets):
        notes.append(f"1 cuadra = {DEFAULT_CUADRA_M:.0f} m")
    if parsed.side_hint:
        notes.append(f"Mano {parsed.side_hint}")
    return notes


def to_dict(parsed: RelativeAddress) -> dict[str, Any]:
    """Convenience for logging and admin views."""
    return parsed.model_dump(mode="json")
