"""Reverse relative geocoding: a pin goes in, a Nicaraguan address comes out.

    >>> describe(12.1334, -86.2807, gazetteer)
    'De la Rotonda El Güegüense, 2c al sur'

This is the inverse of :mod:`pipeline.geocode.relative_address`.  That module
turns speech into a point; this one turns a point back into the sentence a
Nicaraguan would actually say, for the share sheet, the POI card and the
WhatsApp message that follows it.  Google can give you a plus code or a street
name nobody uses; it cannot give you "de la Rotonda El Güegüense, 2c al sur,
1c abajo", which is the only form a taxi driver will accept.

Three problems, in order of difficulty:

**Which landmark.**  Not the nearest one — the *best known* one.  Nicaraguans
anchor on things the listener can already picture, so a rotonda 900 m away beats
an anonymous colegio at 200 m, and the colegio wins again once you are standing
next to it and can see the sign.  :func:`choose_landmark` scores four things
that all matter and trade off against each other: how famous the landmark is,
how far it is, whether you are practically on top of it, and whether the offset
it produces comes out in whole blocks.

**Which offset.**  The delta is projected onto north/south and east/west (the
colonial grid runs square, so those two axes *are* the streets) and each
component is quantised into the register speech uses: cuadras when it lands near
a block boundary, varas on the 25-vara steps, and metres otherwise.  At most two
hops, north/south first — that ordering is a convention as strong as the words
themselves.

**Which words.**  ``al lago`` / ``al sur`` / ``arriba`` / ``abajo`` is the
colloquial register (Managua's cardinals are topographic: the lake is north, the
sun rises ``arriba``), ``al norte`` / ``al sur`` / ``al este`` / ``al oeste`` the
formal one that goes on printed signage.  Every word is checked against
:func:`common.geo.resolve_direction` *for the landmark's own town* before it is
emitted, because half the vocabulary is geographic rather than compass: Lake
Cocibolca is east of Granada, so "al lago" there means something ninety degrees
away from what it means in Managua.  Where the local reading disagrees with the
direction we mean, the cardinal word is used instead — an address that is
slightly less colloquial is a far better outcome than one that is perpendicular.

That last point is the contract: ``parse(describe(p))`` resolved through
:func:`~pipeline.geocode.relative_address.resolve` must land back on ``p``, give
or take the block quantisation.  :func:`round_trip_error_m` measures exactly
that and the test-suite asserts it over a grid.

The module holds no database.  Landmarks are injected as an iterable of
:class:`~pipeline.geocode.relative_address.LandmarkMatch` — in production, the
rows of a PostGIS ``ORDER BY geom <-> point LIMIT n`` query — which keeps it
importable, testable and fast.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Literal

from common.geo import (
    VARA_M,
    cuadra_length_m,
    haversine_m,
    initial_bearing_deg,
    local_projection,
    resolve_direction,
)
from common.models import RelativeAddress, RelativeOffset
from common.text import collapse_ws, normalize, strip_accents
from pipeline.geocode.gazetteer_build import default_popularity
from pipeline.geocode.relative_address import LandmarkLookup, LandmarkMatch, parse, resolve

__all__ = [
    "DEFAULT_MAX_LANDMARK_DISTANCE_M",
    "LandmarkChoice",
    "Style",
    "choose_landmark",
    "describe",
    "landmark_lookup",
    "reverse",
    "round_trip_error_m",
]

log = logging.getLogger(__name__)

Style = Literal["colloquial", "formal"]

# --------------------------------------------------------------------------- #
# Tunables.  Every one of these is a claim about how Nicaraguans speak, so each
# carries the reasoning that sets it; they are not arbitrary knobs.
# --------------------------------------------------------------------------- #

#: How far a landmark may be and still anchor an address.  Fourteen blocks is
#: already a long address; past that the listener stops counting and asks for a
#: different reference, so we would rather return nothing than a 2 km hike.
DEFAULT_MAX_LANDMARK_DISTANCE_M = 1_200.0

#: Below this, a hop is not worth saying.  The smallest unit anyone quotes is
#: 25 varas (~21 m), so half of that is the floor; dropping the component costs
#: at most 12 m, well inside the error the block quantisation already carries.
DEAD_ZONE_M = 12.0

#: How far off a block boundary a distance may be and still be called a cuadra,
#: as a fraction of the block.  A cuadra is a *count of corners*, not a measure:
#: real blocks stretch and shrink, and the listener walks to the second corner
#: whatever a tape would say.  Twelve per cent (~10 m on the 84 m colonial
#: block) is wide enough to absorb that and narrow enough that two adjacent
#: readings never both qualify.  It is a fraction and not a fixed number of
#: metres on purpose: :data:`common.geo.DEFAULT_CUADRA_M` is a prior meant to be
#: recalibrated from the street graph, and a metre window would silently change
#: meaning underneath it.
CUADRA_TOLERANCE_FRACTION = 0.12

#: Varas are quoted on 25-vara steps and essentially never off them: "75 vrs" is
#: idiomatic, "63 vrs" is a survey. Half a step (~10 m) would swallow distances
#: that belong in metres, so the window is tighter than the cuadra's.
VARA_TOLERANCE_M = 6.0
VARA_STEPS: tuple[int, ...] = (25, 50, 75, 100)

#: Spoken metre distances are round: "30 metros", "150 metros". Snapping to 5 m
#: (10 m past a hundred) costs at most 2.5 m and buys a sentence that sounds
#: like a person wrote it.
METRE_STEP_NEAR_M = 5.0
METRE_STEP_FAR_M = 10.0

#: Landmark scoring weights; they sum to 1 so a score reads as a quality in
#: [0, 1].  Prominence leads because the address has to be *findable by someone
#: else* — that is the whole point of anchoring on a landmark — but nearness is
#: a separate term from proximity on purpose: within sight of the door, fame
#: stops mattering because the reader can simply see the thing.
W_PROMINENCE = 0.42
W_PROXIMITY = 0.19
W_IDIOM = 0.15
W_NEARNESS = 0.24

#: Range over which the "you can see it from here" bonus falls off — about one
#: long block.
NEAR_M = 120.0

#: An address whose hops total less than this is comfortable; past the far end
#: nobody follows it on foot and the idiom score has decayed to nothing.
EASY_WALK_M = 500.0
FAR_WALK_M = 2_500.0

#: How much a "donde fue" landmark is discounted.  A ghost landmark is perfect
#: for a Managua native — half the city still navigates by the buildings the
#: 1972 earthquake took down — and useless to the tourist or the delivery driver
#: reading a share sheet, who cannot see a building that is not there.  A
#: three-fold discount means a former landmark only wins when it is the only
#: thing in range, which is exactly when it is genuinely the right answer.
FORMER_PENALTY = 0.35

#: Per-unit "does this sound like an address" weight, used by the idiom term.
_UNIT_IDIOM = {"cuadra": 1.0, "vara": 0.85, "metro": 0.6}

#: What a component dropped by ``max_hops`` costs: an unspoken 300 m is a bad
#: address, and the scorer should prefer a landmark that does not need one.
_TRUNCATED_IDIOM = 0.3

#: Direction words per register.  The colloquial set is Managua's topographic
#: slang; note that south stays "al sur" even colloquially — "a la montaña" is
#: understood but far rarer in speech than the other three.
_DIRECTION_WORDS: dict[str, dict[str, str]] = {
    "colloquial": {"north": "al lago", "south": "al sur", "east": "arriba", "west": "abajo"},
    "formal": {"north": "al norte", "south": "al sur", "east": "al este", "west": "al oeste"},
}

#: What each axis means in compass degrees.  Every word we emit is checked
#: against this, because the geographic half of the vocabulary is local.
_AXIS_BEARINGS: dict[str, float] = {"north": 0.0, "east": 90.0, "south": 180.0, "west": 270.0}

#: Landmark heads that are feminine without ending in -a, for the leading
#: article ("De la Terminal", not "Del Terminal").
_FEMININE_HEADS = frozenset({"terminal", "catedral", "sucursal", "cruz", "torre", "sede"})

#: Words ending like this are feminine in Spanish: universidad, estación, plaza.
_FEMININE_SUFFIXES = ("a", "cion", "sion", "dad", "tad", "tud", "umbre", "eza", "triz")

#: Masculine despite ending in -a.
_MASCULINE_HEADS = frozenset({"dia", "mapa", "problema", "clima", "tranvia", "planeta"})

#: Names that already start with their own article take a contraction instead.
_LEADING_ARTICLE_FORMS = {
    "el": ("Del", "el"),
    "la": ("De la", "la"),
    "los": ("De los", "los"),
    "las": ("De las", "las"),
}


# --------------------------------------------------------------------------- #
# Quantisation
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Hop:
    """One axis component after quantisation, before it becomes an offset."""

    quantity: float
    unit: str
    distance_m: float
    error_m: float
    idiom: float


def _round_metres(distance_m: float) -> float:
    """Snap to the metre values people actually say."""
    step = METRE_STEP_NEAR_M if distance_m < 100.0 else METRE_STEP_FAR_M
    return max(step, round(distance_m / step) * step)


def _quantise(distance_m: float, cuadra_m: float) -> _Hop | None:
    """Express one axis component in cuadras, varas or metres.

    Returns ``None`` when the component is too short to be worth a hop.

    Speech splits the two units by range, and it has to: the colonial cuadra
    *is* 100 varas, so on an 84 m block "100 vrs" and "1c" name the same
    distance and would otherwise compete for it.  Under a block people quote
    varas ("75 vrs al sur"); from a block up they count corners ("2c al sur").
    Giving each range its own ladder keeps the two readings disjoint whatever
    the calibrated block length turns out to be, and the cuadra takes the
    boundary because block counting is the primary register.
    """
    if distance_m < DEAD_ZONE_M:
        return None

    candidates: list[_Hop] = []

    # Half blocks are said as readily as whole ones ("cuadra y media"), quarter
    # blocks are not said at all, so the grid is 0.5c — but only from one block
    # up, since below that the vara ladder is the same ladder.
    blocks = round(distance_m / cuadra_m * 2.0) / 2.0
    if blocks >= 1.0:
        error = abs(blocks * cuadra_m - distance_m)
        if error <= CUADRA_TOLERANCE_FRACTION * cuadra_m:
            candidates.append(
                _Hop(blocks, "cuadra", blocks * cuadra_m, error, _UNIT_IDIOM["cuadra"])
            )

    for step in VARA_STEPS:
        metres = step * VARA_M
        if metres >= cuadra_m:
            continue  # that is a cuadra, and gets said as one
        error = abs(metres - distance_m)
        if error <= VARA_TOLERANCE_M:
            candidates.append(_Hop(float(step), "vara", metres, error, _UNIT_IDIOM["vara"]))

    if candidates:
        candidates.sort(key=lambda hop: (0 if hop.unit == "cuadra" else 1, round(hop.error_m, 3)))
        return candidates[0]

    metres = _round_metres(distance_m)
    return _Hop(metres, "metro", metres, abs(metres - distance_m), _UNIT_IDIOM["metro"])


def _walkability(total_m: float) -> float:
    """1.0 for an address you can walk without counting, decaying to 0."""
    if total_m <= EASY_WALK_M:
        return 1.0
    return max(0.0, 1.0 - (total_m - EASY_WALK_M) / (FAR_WALK_M - EASY_WALK_M))


# --------------------------------------------------------------------------- #
# Decomposition
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class LandmarkChoice:
    """Why one landmark was picked and what address it produces.

    Exposed so ``/api/reverse`` and the admin views can show the working: a pin
    the user thinks is wrong is usually a landmark choice they disagree with,
    not a broken offset.
    """

    match: LandmarkMatch
    distance_m: float
    bearing_deg: float
    score: float
    offsets: tuple[RelativeOffset, ...]
    #: Straight-line distance between the pin and where the rendered address
    #: actually resolves to — the quantisation cost, in metres.
    error_m: float
    cuadra_m: float


def _direction(axis: str, style: Style, city: str | None) -> tuple[str, float]:
    """Word and bearing for one of the four axis directions.

    The bearing is looked up *from the word* through
    :func:`common.geo.resolve_direction` rather than hard-coded, so the offset
    we build and the offset a re-parse of our own output builds cannot drift
    apart.

    That lookup is also the guard on the geographic register.  "Al lago" is
    north in Managua and *east* in Granada, so emitting it for the north/south
    axis outside the lake-is-north towns would write an address that rotates
    ninety degrees the moment someone resolves it.  When the word does not point
    where we mean in this city, we fall back to the cardinal one: "al norte" is
    plain Spanish, it is what Granada's own street signs use, and it cannot be
    misread by a reader — or a parser — that does not know which town it is in.
    """
    intended = _AXIS_BEARINGS[axis]
    word = _DIRECTION_WORDS[style][axis]
    if resolve_direction(word, city) == intended:
        return word, intended
    fallback = _DIRECTION_WORDS["formal"][axis]
    bearing = resolve_direction(fallback, city)
    if bearing != intended:  # pragma: no cover - the cardinals are city-independent
        raise ValueError(f"direction word {fallback!r} does not bear {intended}° in {city!r}")
    log.debug("geographic word %r is not %.0f° in %r; using %r", word, intended, city, fallback)
    return fallback, intended


def _decompose(
    match: LandmarkMatch,
    lat: float,
    lon: float,
    *,
    cuadra_m: float,
    style: Style,
    max_hops: int,
    city: str | None,
) -> tuple[list[RelativeOffset], float, float]:
    """Split the landmark→pin delta into hops.

    Returns ``(offsets, idiom_score, error_m)``.  ``error_m`` is the true cost of
    the quantisation: the distance between the pin and where these offsets land.
    """
    to_xy, _ = local_projection(match.lat, match.lon)
    # (east metres, north metres).  In Nicaragua every latitude is positive and
    # every longitude negative, so "east" means *less* negative — the sign trap
    # that eats every first implementation of this function.
    east_m, north_m = to_xy(lat, lon)

    axes: tuple[tuple[str, float], ...] = (
        ("north" if north_m >= 0 else "south", abs(north_m)),
        ("east" if east_m >= 0 else "west", abs(east_m)),
    )

    scanned = [(axis, raw_m, _quantise(raw_m, cuadra_m)) for axis, raw_m in axes]
    spoken = [item for item in scanned if item[2] is not None]

    # North/south is said first; when only one hop fits, the longer leg is the
    # one that carries the meaning, so that is the one kept.
    if len(spoken) > max_hops:
        spoken.sort(key=lambda item: item[1], reverse=True)
        spoken = spoken[:max_hops]
        spoken.sort(key=lambda item: 0 if item[0] in {"north", "south"} else 1)

    kept = {axis for axis, _, _ in spoken}
    offsets: list[RelativeOffset] = []
    idioms: list[float] = []
    residual = {"north": 0.0, "east": 0.0}

    for axis, raw_m, hop in scanned:
        signed_axis = "north" if axis in {"north", "south"} else "east"
        sign = 1.0 if axis in {"north", "east"} else -1.0
        if hop is None or axis not in kept:
            # Unsaid: the whole component stays in the residual.
            residual[signed_axis] = sign * raw_m
            idioms.append(1.0 if hop is None else _TRUNCATED_IDIOM)
            continue
        word, bearing = _direction(axis, style, city)
        offsets.append(
            RelativeOffset(
                quantity=hop.quantity,
                unit=hop.unit,  # type: ignore[arg-type]
                direction_text=word,
                bearing_deg=bearing,
                distance_m=hop.distance_m,
            )
        )
        idioms.append(hop.idiom)
        residual[signed_axis] = sign * (raw_m - hop.distance_m)

    error_m = (residual["north"] ** 2 + residual["east"] ** 2) ** 0.5
    total_m = sum(offset.distance_m for offset in offsets)
    idiom = (sum(idioms) / len(idioms)) * _walkability(total_m)
    return offsets, idiom, error_m


# --------------------------------------------------------------------------- #
# Landmark choice
# --------------------------------------------------------------------------- #


def _prominence(match: LandmarkMatch) -> float:
    """How likely a listener is to already know this landmark, in [0, 1].

    ``popularity`` is authoritative when the gazetteer stated one; a row that
    never got a value falls back to the per-kind default the gazetteer builder
    uses, so a rotonda is not treated as an anonymous point just because nobody
    scored it yet.
    """
    if match.popularity > 0.0:
        return max(0.0, min(1.0, match.popularity))
    return default_popularity(match.kind or "otro", match.name)


def _score(
    match: LandmarkMatch, distance_m: float, idiom: float, *, max_distance_m: float
) -> float:
    """Rank one candidate landmark.  See the weight constants for the reasoning."""
    proximity = max(0.0, 1.0 - (distance_m / max_distance_m) ** 2)
    nearness = max(0.0, 1.0 - distance_m / NEAR_M)
    score = (
        W_PROMINENCE * _prominence(match)
        + W_PROXIMITY * proximity
        + W_IDIOM * idiom
        + W_NEARNESS * nearness
    )
    if match.former:
        score *= FORMER_PENALTY
    # A caller that is unsure of the row (a fuzzy spatial join, say) can say so.
    score *= max(0.0, min(1.0, match.score))
    return round(score, 6)


def choose_landmark(
    lat: float,
    lon: float,
    landmarks: Iterable[LandmarkMatch],
    *,
    city: str | None = None,
    max_landmark_distance_m: float = DEFAULT_MAX_LANDMARK_DISTANCE_M,
    style: Style = "colloquial",
    max_hops: int = 2,
) -> LandmarkChoice | None:
    """Pick the landmark a Nicaraguan would anchor this pin on.

    Returns ``None`` when nothing is within ``max_landmark_distance_m`` — an
    address invented from a landmark 5 km away is worse than no address, because
    the reader will trust it.
    """
    if style not in _DIRECTION_WORDS:
        raise ValueError(f"unknown style {style!r}; expected 'colloquial' or 'formal'")
    if max_hops not in (1, 2):
        raise ValueError("max_hops must be 1 or 2: the idiom carries two hops at most")

    best: LandmarkChoice | None = None
    for match in landmarks:
        distance_m = haversine_m(lat, lon, match.lat, match.lon)
        if distance_m > max_landmark_distance_m:
            continue
        # Both the block length and the meaning of "al lago" are local, and the
        # landmark is the only thing here that knows which town this is.
        effective_city = city or match.city
        cuadra_m = cuadra_length_m(effective_city)
        offsets, idiom, error_m = _decompose(
            match,
            lat,
            lon,
            cuadra_m=cuadra_m,
            style=style,
            max_hops=max_hops,
            city=effective_city,
        )
        score = _score(match, distance_m, idiom, max_distance_m=max_landmark_distance_m)
        if best is None or score > best.score:
            best = LandmarkChoice(
                match=match,
                distance_m=distance_m,
                bearing_deg=initial_bearing_deg(match.lat, match.lon, lat, lon),
                score=score,
                offsets=tuple(offsets),
                error_m=error_m,
                cuadra_m=cuadra_m,
            )

    if best is None:
        log.debug("no landmark within %.0f m of %.5f,%.5f", max_landmark_distance_m, lat, lon)
    return best


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def _is_feminine(name: str) -> bool:
    """Guess the gender of a landmark's head noun, for the leading article."""
    head = strip_accents(collapse_ws(name)).lower().split(" ")[0] if name.strip() else ""
    if head in _FEMININE_HEADS:
        return True
    if head in _MASCULINE_HEADS:
        return False
    return head.endswith(_FEMININE_SUFFIXES)


def _landmark_phrase(match: LandmarkMatch, *, with_article: bool) -> str:
    """The landmark as it opens the sentence.

    "De la Rotonda El Güegüense", "Del Mercado Oriental", "Donde fue el Cine
    Cabrera".  The forms are chosen so that
    :func:`~pipeline.geocode.relative_address.parse` strips them back off and
    recovers the gazetteer name — that is what makes the output re-parseable.
    """
    name = collapse_ws(match.name)
    if not name:
        return name
    first = strip_accents(name).lower().split(" ")[0]
    leading = _LEADING_ARTICLE_FORMS.get(first)

    if match.former:
        # "donde fue" is what flags a ghost landmark to the parser, so it is not
        # decoration: without it the resolver would look for a building that has
        # been rubble since 1972 in the live rows.
        if leading is not None:
            return f"Donde fue {name}"
        article = "la" if _is_feminine(name) else "el"
        return f"Donde fue {article} {name}"

    if not with_article:
        return name
    if leading is not None:
        # "El Güegüense" -> "Del Güegüense": Spanish contracts, and so does
        # everyone writing the address on a business card.
        contraction, _ = leading
        rest = name.split(" ", 1)[1] if " " in name else name
        return f"{contraction} {rest}" if " " in name else f"{contraction} {name}"
    return f"{'De la' if _is_feminine(name) else 'Del'} {name}"


def reverse(
    lat: float,
    lon: float,
    landmarks: Iterable[LandmarkMatch],
    *,
    city: str | None = None,
    max_landmark_distance_m: float = DEFAULT_MAX_LANDMARK_DISTANCE_M,
    style: Style = "colloquial",
    max_hops: int = 2,
    with_article: bool = True,
    modifiers: Sequence[str] = (),
) -> RelativeAddress | None:
    """Describe a pin as a Nicaraguan landmark-relative address.

    ``landmarks`` is whatever the caller has nearby — in production the rows of a
    ``gazetteer`` KNN query, in tests a list.  ``city`` overrides the cuadra
    length; left ``None``, the chosen landmark's own city decides it.

    ``modifiers`` are free-text hints ("portón negro", "frente al parque") that
    are appended verbatim and never turned into geometry, exactly as the parser
    treats them coming the other way.

    Returns ``None`` when no landmark is close enough to anchor on.
    """
    choice = choose_landmark(
        lat,
        lon,
        landmarks,
        city=city,
        max_landmark_distance_m=max_landmark_distance_m,
        style=style,
        max_hops=max_hops,
    )
    if choice is None:
        return None
    return _address_from_choice(choice, with_article=with_article, modifiers=modifiers)


def _address_from_choice(
    choice: LandmarkChoice, *, with_article: bool, modifiers: Sequence[str] = ()
) -> RelativeAddress:
    """Assemble the model.  ``raw`` is set to the rendered string so that
    ``RelativeAddress.raw`` and ``RelativeAddress.render()`` agree, which they do
    not automatically for an address that was generated rather than parsed."""
    address = RelativeAddress(
        raw="",
        landmark_text=_landmark_phrase(choice.match, with_article=with_article),
        # We know exactly which row this came from, so the gazetteer query is
        # the landmark's own name rather than something scraped out of text.
        landmark_query=choice.match.name,
        offsets=list(choice.offsets),
        modifiers=[collapse_ws(m) for m in modifiers if collapse_ws(m)],
        former_landmark=choice.match.former,
        total_distance_m=sum(offset.distance_m for offset in choice.offsets),
    )
    return address.model_copy(update={"raw": address.render()})


def describe(
    lat: float,
    lon: float,
    landmarks: Iterable[LandmarkMatch],
    *,
    city: str | None = None,
    max_landmark_distance_m: float = DEFAULT_MAX_LANDMARK_DISTANCE_M,
    style: Style = "colloquial",
    max_hops: int = 2,
    with_article: bool = True,
    modifiers: Sequence[str] = (),
) -> str | None:
    """The rendered address string, ready for the share sheet, or ``None``."""
    address = reverse(
        lat,
        lon,
        landmarks,
        city=city,
        max_landmark_distance_m=max_landmark_distance_m,
        style=style,
        max_hops=max_hops,
        with_article=with_article,
        modifiers=modifiers,
    )
    return address.render() if address is not None else None


# --------------------------------------------------------------------------- #
# Round trip
# --------------------------------------------------------------------------- #


def landmark_lookup(landmarks: Iterable[LandmarkMatch]) -> LandmarkLookup:
    """An in-memory stand-in for the gazetteer query, for round-tripping.

    It mirrors what PostGIS does for the forward geocoder — accent-folded name
    match, ``former`` as a preference rather than a hard filter — so a round-trip
    check exercises the same resolution path the API will run, not a shortcut
    that quietly hands back the landmark we started from.
    """
    items = list(landmarks)

    def _lookup(query: str, former_only: bool) -> list[LandmarkMatch]:
        key = normalize(query)
        if not key:
            return []
        hits: list[LandmarkMatch] = []
        for item in items:
            name = normalize(item.name)
            if name == key:
                score = 1.0
            elif key in name or name in key:
                # The parser strips leading articles, so "Del Güegüense" comes
                # back as "Güegüense"; a trigram index would match it too.
                score = 0.85
            else:
                continue
            hits.append(replace(item, score=score))
        if former_only:
            former = [hit for hit in hits if hit.former]
            if former:
                return former
        return hits

    return _lookup


def round_trip_error_m(
    lat: float,
    lon: float,
    landmarks: Iterable[LandmarkMatch],
    *,
    city: str | None = None,
    max_landmark_distance_m: float = DEFAULT_MAX_LANDMARK_DISTANCE_M,
    style: Style = "colloquial",
    max_hops: int = 2,
    with_article: bool = True,
) -> float | None:
    """Describe a point, parse the description back, and measure the drift.

    The number this returns is the honest accuracy of the whole feature: it is
    what a user gets when they paste the generated string into the search box.
    ``None`` means no address could be generated, or the generated one did not
    resolve — both are failures the caller should treat as such.
    """
    items = list(landmarks)
    choice = choose_landmark(
        lat,
        lon,
        items,
        city=city,
        max_landmark_distance_m=max_landmark_distance_m,
        style=style,
        max_hops=max_hops,
    )
    if choice is None:
        return None
    address = _address_from_choice(choice, with_article=with_article)

    effective_city = city or choice.match.city
    reparsed = parse(address.render(), city=effective_city)
    if reparsed is None:
        return None
    candidates = resolve(reparsed, landmark_lookup(items), city=effective_city, limit=1)
    if not candidates:
        return None
    return haversine_m(lat, lon, candidates[0].lat, candidates[0].lon)
