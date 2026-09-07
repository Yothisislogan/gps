"""Conflation of OSM, Overture and field-survey POIs into one published row.

Implements docs/PLAN.md section 4.2.  The scoring half is pure: it takes iterables of records
and returns decisions, with no database, no network and no filesystem, so the whole thing is
testable and so the admin UI can re-score a pair on demand to explain itself.  Only
:func:`main` touches files, through :mod:`pipeline.common.io`.

Why this is hard in Nicaragua, and what the code does about it:

* **The same business is written three ways.**  OSM says "Restaurante El Zaguán", Overture
  (via Meta) says "EL ZAGUAN" and the survey sheet says "El Zaguan".  Names are compared on
  :func:`common.text.strip_generic_prefix` / :func:`common.text.name_key` output, which is
  accent-folded, lower-cased and stripped of the category words Nicaraguan listings prepend
  inconsistently.
* **Chains are everywhere and they are close together.**  Two Puma stations on opposite sides
  of Carretera a Masaya are 60 m apart and are two different POIs; "Farmacia Xolotlán" appears
  dozens of times across Managua.  A brand-aware guard (:data:`CHAIN_MAX_MERGE_DISTANCE_M`)
  keeps them apart, and no pair ever auto-merges on name alone across more than
  :data:`NAME_ONLY_MAX_MERGE_DISTANCE_M`.
* **Positions disagree by tens of metres.**  Overture points are often the centroid of a
  Facebook check-in cloud; OSM points are hand-placed by someone standing at the door.  Hence
  75 m blocking, a distance decay rather than a hard cut, and OSM winning the *position* field
  while Overture wins phone/socials/hours.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz
from rapidfuzz.distance import JaroWinkler

from common.geo import haversine_m, local_projection
from common.text import name_key, normalize, strip_generic_prefix
from pipeline.common.io import (
    atomic_write_text,
    read_geojsonseq,
    setup_logging,
    write_geojsonseq,
)
from pipeline.pois.taxonomy import FALLBACK_CATEGORY, group_of, load_taxonomy

__all__ = [
    "AUTO_MERGE_SCORE",
    "BLOCK_RADIUS_M",
    "BUCKET_SIZE_M",
    "CHAIN_MAX_MERGE_DISTANCE_M",
    "FIELD_PRECEDENCE",
    "NAME_ONLY_MAX_MERGE_DISTANCE_M",
    "REVIEW_SCORE",
    "ConflationResult",
    "Decision",
    "MatchPair",
    "MatchScore",
    "MergedPoi",
    "PoiRecord",
    "brand_key",
    "candidate_pairs",
    "chain_keys_for",
    "conflate",
    "decide",
    "main",
    "merge_records",
    "score_pair",
]

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Tuning constants
# --------------------------------------------------------------------------- #

#: Blocking radius.  75 m from docs/PLAN.md section 4.2: wide enough to catch an Overture
#: point placed at the middle of a block when the OSM node is at the door, narrow enough that
#: two adjacent pulperías on the same street do not become candidates.
BLOCK_RADIUS_M = 75.0

#: Grid cell edge for the blocking index, in metres.  Any two points within
#: :data:`BLOCK_RADIUS_M` fall in cells at most one apart when the cell is at least the radius,
#: so a 3x3 neighbourhood scan is exhaustive.  The extra 5 m is slack for the equirectangular
#: scale error of the single national projection frame (<1 % over Nicaragua's latitude span,
#: i.e. well under a metre at 75 m) so a true 75 m pair can never land two cells apart.
BUCKET_SIZE_M = 80.0

#: Score bands from the plan.  Everything at or above :data:`AUTO_MERGE_SCORE` merges without a
#: human; :data:`REVIEW_SCORE` up to that lands in ``poi_match_queue``; below it the two rows
#: stay separate POIs.
AUTO_MERGE_SCORE = 0.85
REVIEW_SCORE = 0.60

#: Component weights.  Name dominates because position is the least trustworthy field in this
#: data and category agreement is nearly free (both sides come from the same taxonomy).
NAME_WEIGHT = 0.60
DISTANCE_WEIGHT = 0.25
CATEGORY_WEIGHT = 0.15

#: Exponential decay length for the distance component: 1.0 at 0 m, 0.61 at 20 m, 0.37 at 40 m,
#: 0.15 at the 75 m blocking edge.  A decay, not a step, because "how far apart" is evidence
#: rather than a rule.
DISTANCE_DECAY_M = 40.0

#: Two records that share a brand and are further apart than this are two branches, not one
#: place: the Puma-either-side-of-the-highway case.  40 m is about the width of a forecourt.
CHAIN_MAX_MERGE_DISTANCE_M = 40.0

#: A brand seen at least this many times in one conflation run is treated as a chain.  Two
#: (one OSM row + one Overture row for a single unique business) must not trip it; three
#: means the name is genuinely repeated in the corpus.
CHAIN_MIN_OCCURRENCES = 3

#: Beyond this distance nothing auto-merges, however identical the names.  Guards the
#: "Farmacia Xolotlán" failure mode where the name is a perfect match and the two rows are two
#: real, different branches.
NAME_ONLY_MAX_MERGE_DISTANCE_M = 50.0

#: Names of chains that are known to repeat across Managua.  Frequency detection catches the
#: rest; this seed list catches the ones that appear only twice inside a small extract.
KNOWN_CHAIN_NAMES: tuple[str, ...] = (
    "Puma",
    "Uno",
    "Petronic",
    "DINSA",
    "Farmacia Xolotlán",
    "Farmacia Bendición de Dios",
    "Pali",
    "Maxi Pali",
    "La Colonia",
    "La Unión",
    "Walmart",
    "Tip-Top",
    "Pollo Estrella",
    "Eskimo",
    "Subway",
    "McDonald's",
    "Pizza Hut",
    "Papa John's",
    "BAC",
    "Lafise",
    "Banpro",
    "Ficohsa",
    "Bancentro",
    "Avanz",
    "Claro",
    "Tigo",
    "Western Union",
)

# UNVERIFIED: the tuning constants above (decay length, weights, the 40 m chain guard) are
# reasoned from how the two sources behave, not measured against a labelled Managua sample.
# On the first real run, hand-label ~200 pairs out of the 0.60-0.85 review band and move the
# thresholds to whatever the precision/recall curve says; the components are all reported on
# MatchScore precisely so that exercise is possible without re-running the conflation.
# UNVERIFIED: KNOWN_CHAIN_NAMES is from general knowledge of Managua brands, not from the
# data.  Confirm the spellings against the first Overture pull (``brand`` and ``brand:wikidata``)
# and against OSM ``brand=*``; a misspelled entry simply does nothing, it never merges wrongly.

#: Blocking keys of the seeded chains.
KNOWN_CHAIN_KEYS: frozenset[str] = frozenset(name_key(name) for name in KNOWN_CHAIN_NAMES)


class Decision(StrEnum):
    """What the conflator concluded about a candidate pair.

    ``REVIEW`` rows become ``poi_match_queue`` entries with ``decision='pending'``; the DB
    column only stores the human's answer (``merge``/``separate``).
    """

    MERGE = "merge"
    REVIEW = "review"
    SEPARATE = "separate"


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #

#: Scalar attribute fields carried through from ``poi``.  Order is the order they are merged in
#: and reported in, nothing more.
SCALAR_FIELDS: tuple[str, ...] = (
    "name",
    "category",
    "subcategory",
    "address_text",
    "city",
    "phone",
    "whatsapp",
    "website",
    "facebook",
    "instagram",
    "opening_hours",
    "price_level",
    "status",
    "brand",
)

#: List-valued fields, unioned across the cluster rather than picked from one source.
LIST_FIELDS: tuple[str, ...] = ("name_alt", "cuisine")

#: Per-field source precedence, from docs/PLAN.md section 4.2 step 5.  Read it as "ask these
#: sources in this order; take the first non-empty answer".
#:
#: * A **field survey** wins everything: somebody stood there and looked.
#: * **Overture** beats OSM for phone, socials, hours, price and open/closed status — that data
#:   comes from the business's own Facebook page and is refreshed monthly.
#: * **OSM** beats Overture for *position*, name, category and the local address string: OSM
#:   points are hand-placed at the door by MapaNica mappers, Overture's are frequently the
#:   centroid of a check-in cloud a block away.
#: * ``user`` is a moderated app submission — trusted just under a survey.
#: * ``wikidata`` is last everywhere; it is good for landmark identity, not for shop details.
FIELD_PRECEDENCE: dict[str, tuple[str, ...]] = {
    "position": ("survey", "user", "osm", "overture", "wikidata"),
    "name": ("survey", "user", "osm", "overture", "wikidata"),
    "name_alt": ("survey", "user", "osm", "overture", "wikidata"),
    "category": ("survey", "user", "osm", "overture", "wikidata"),
    "subcategory": ("survey", "user", "osm", "overture", "wikidata"),
    "cuisine": ("survey", "user", "osm", "overture", "wikidata"),
    "address_text": ("survey", "user", "osm", "overture", "wikidata"),
    "city": ("survey", "user", "osm", "overture", "wikidata"),
    "brand": ("survey", "user", "osm", "overture", "wikidata"),
    "phone": ("survey", "user", "overture", "osm", "wikidata"),
    "whatsapp": ("survey", "user", "overture", "osm", "wikidata"),
    "website": ("survey", "user", "overture", "osm", "wikidata"),
    "facebook": ("survey", "user", "overture", "osm", "wikidata"),
    "instagram": ("survey", "user", "overture", "osm", "wikidata"),
    "opening_hours": ("survey", "user", "overture", "osm", "wikidata"),
    "price_level": ("survey", "user", "overture", "osm", "wikidata"),
    "status": ("survey", "user", "overture", "osm", "wikidata"),
}

#: Fallback for any field not named above.
DEFAULT_PRECEDENCE: tuple[str, ...] = ("survey", "user", "overture", "osm", "wikidata")

#: ``poi.sources`` jsonb keys, per source.  Keeping the Overture GERS id here is what makes the
#: monthly refresh a join instead of a re-conflation (plan section 4.2 step 6).
SOURCE_ID_KEYS: dict[str, str] = {
    "osm": "osm_id",
    "overture": "overture_gers_id",
    "survey": "survey_id",
    "user": "user_id",
    "wikidata": "wikidata_id",
}

#: Extra confidence granted per additional independent source agreeing on a POI.  Agreement is
#: the only evidence the conflator has; a field visit is what actually sets ``verified_at``.
CONFIDENCE_PER_EXTRA_SOURCE = 0.10


@dataclass(frozen=True, slots=True)
class PoiRecord:
    """One source's view of a place, before conflation.

    Mirrors the ``poi`` / ``poi_source`` columns in db/migrations/001_init.sql.  ``status``
    defaults to ``"unverified"``, which the merger treats as "no opinion" rather than as a
    value, so an OSM row does not overwrite Overture's ``closed``.
    """

    source: str
    source_id: str
    name: str
    lat: float
    lon: float
    category: str | None = None
    subcategory: str | None = None
    name_alt: tuple[str, ...] = ()
    cuisine: tuple[str, ...] = ()
    address_text: str | None = None
    city: str | None = None
    phone: str | None = None
    whatsapp: str | None = None
    website: str | None = None
    facebook: str | None = None
    instagram: str | None = None
    opening_hours: str | None = None
    price_level: int | None = None
    status: str = "unverified"
    brand: str | None = None
    gers_id: str | None = None
    license: str | None = None
    confidence: float = 0.0
    popularity: float = 0.0

    @property
    def key(self) -> str:
        """``"source:source_id"`` — the same string ``poi_match_queue.left_key`` stores."""
        return f"{self.source}:{self.source_id}"

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> PoiRecord:
        """Build a record from a loose dict (a DB row, a GeoJSON ``properties`` bag).

        Unknown keys are ignored so the caller can hand over a full ``poi_source.raw`` payload.
        """
        missing = [
            key for key in ("source", "source_id", "name", "lat", "lon") if data.get(key) is None
        ]
        if missing:
            raise ValueError(f"POI record is missing required field(s) {missing}: {dict(data)!r}")
        values: dict[str, Any] = {}
        for name in cls.__dataclass_fields__:
            if name not in data:
                continue
            value = data[name]
            if name in LIST_FIELDS:
                values[name] = tuple(value or ())
            else:
                values[name] = value
        values["lat"] = float(values["lat"])
        values["lon"] = float(values["lon"])
        values["source"] = str(values["source"])
        values["source_id"] = str(values["source_id"])
        values["name"] = str(values["name"] or "")
        values["license"] = data.get("license") or data.get("overture_license")
        return cls(**values)


def brand_key(record: PoiRecord) -> str:
    """Chain-identity key for a record: the explicit brand when tagged, else the name key.

    ``brand=*`` is the reliable signal (OSM tags it, Overture carries ``brand``), but most
    Nicaraguan rows have no brand tag at all, so the accent-folded, generic-stripped name key
    stands in: "Farmacia Xolotlán" and "FARMACIA XOLOTLAN" both key to ``xolotlan``.
    """
    return name_key(record.brand) if record.brand else name_key(record.name)


def chain_keys_for(
    records: Iterable[PoiRecord], *, min_occurrences: int = CHAIN_MIN_OCCURRENCES
) -> frozenset[str]:
    """Brand keys that behave like a chain in this corpus, plus the seeded well-known ones.

    Frequency is measured over the *input* rows, so a single business contributed by two
    sources counts twice; :data:`CHAIN_MIN_OCCURRENCES` is set above that on purpose.
    """
    counts = Counter(brand_key(record) for record in records)
    frequent = {key for key, count in counts.items() if key and count >= min_occurrences}
    return frozenset(frequent | KNOWN_CHAIN_KEYS)


# --------------------------------------------------------------------------- #
# Blocking
# --------------------------------------------------------------------------- #


def candidate_pairs(
    records: Sequence[PoiRecord], *, radius_m: float = BLOCK_RADIUS_M
) -> Iterator[tuple[int, int]]:
    """Yield index pairs ``(i, j)``, ``i < j``, whose points are within ``radius_m``.

    Uses a fixed-size grid over a single equirectangular frame (``common.geo.local_projection``
    centred on the corpus) rather than comparing every pair: the national POI set is several
    hundred thousand points and an O(n^2) scan is ~10^11 comparisons.  Each point is binned
    into one :data:`BUCKET_SIZE_M` cell and compared only against the 3x3 block of cells around
    it, which is exhaustive because the cell edge is larger than the radius.  The final
    distance test is a real haversine, so the grid only ever over-generates.
    """
    count = len(records)
    if count < 2:
        return
    lat0 = sum(record.lat for record in records) / count
    lon0 = sum(record.lon for record in records) / count
    to_xy, _ = local_projection(lat0, lon0)

    buckets: dict[tuple[int, int], list[int]] = {}
    cells: list[tuple[int, int]] = []
    for index, record in enumerate(records):
        x, y = to_xy(record.lat, record.lon)
        cell = (math.floor(x / BUCKET_SIZE_M), math.floor(y / BUCKET_SIZE_M))
        cells.append(cell)
        buckets.setdefault(cell, []).append(index)

    for cell in sorted(buckets):
        cx, cy = cell
        neighbourhood: list[int] = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                neighbourhood.extend(buckets.get((cx + dx, cy + dy), ()))
        neighbourhood.sort()
        for i in buckets[cell]:
            left = records[i]
            for j in neighbourhood:
                # j > i dedupes: the mirrored visit from j's own cell is skipped.
                if j <= i:
                    continue
                right = records[j]
                if haversine_m(left.lat, left.lon, right.lat, right.lon) <= radius_m:
                    yield (i, j)


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class MatchScore:
    """A 0..1 score plus every component that produced it, so a decision can be explained."""

    score: float
    name_score: float
    distance_score: float
    category_score: float
    distance_m: float
    same_source: bool
    same_brand: bool
    chain: bool
    capped_by: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Flat dict for ``poi_match_queue`` and for the admin JSON."""
        return {
            "score": round(self.score, 4),
            "name_score": round(self.name_score, 4),
            "distance_score": round(self.distance_score, 4),
            "category_score": round(self.category_score, 4),
            "distance_m": round(self.distance_m, 2),
            "same_source": self.same_source,
            "same_brand": self.same_brand,
            "chain": self.chain,
            "capped_by": self.capped_by,
        }

    def explain_es(self) -> str:
        """One line of Spanish for the admin review queue."""
        parts = [
            f"nombre {self.name_score:.2f}",
            f"distancia {self.distance_m:.0f} m",
            f"categoría {self.category_score:.2f}",
        ]
        if self.chain:
            parts.append("cadena conocida")
        if self.same_source:
            parts.append("misma fuente")
        reasons = {
            "chain_distance_guard": "sucursales distintas de la misma cadena",
            "name_only_distance": "demasiado lejos para unir sólo por el nombre",
            "same_source": "dos filas de la misma fuente requieren revisión humana",
        }
        if self.capped_by:
            parts.append(reasons.get(self.capped_by, self.capped_by))
        return ", ".join(parts)


def _name_similarity(left: str, right: str) -> float:
    """0..1 similarity over accent-folded, generic-stripped Nicaraguan business names.

    Two measures because they fail differently.  ``token_set_ratio`` handles word order and
    extra words — "El Zaguán Restaurante" vs "Restaurante El Zaguán" — but happily scores 100
    for a subset, so "Farmacia Xolotlán" matches "Farmacia Xolotlán Bello Horizonte".
    Jaro-Winkler over the sorted-token blocking key punishes exactly that extra token.  The
    blend keeps the first's tolerance without its blind spot.
    """
    left_stripped = strip_generic_prefix(left)
    right_stripped = strip_generic_prefix(right)
    if not left_stripped or not right_stripped:
        return 0.0
    token = fuzz.token_set_ratio(left_stripped, right_stripped) / 100.0
    jaro = JaroWinkler.similarity(name_key(left), name_key(right))
    return 0.6 * token + 0.4 * jaro


def _distance_score(distance_m: float) -> float:
    """Exponential decay; see :data:`DISTANCE_DECAY_M`."""
    return math.exp(-max(0.0, distance_m) / DISTANCE_DECAY_M)


def _category_score(left: str | None, right: str | None) -> float:
    """1.0 same category, 0.6 same group, 0.5 when either side has no opinion, else 0.0.

    "No opinion" scores like a weak agreement rather than a disagreement: a great many Overture
    rows arrive with a category nicanav has no mapping for, and refusing to merge those would
    throw away the phone numbers they carry.
    """
    if not left or not right or FALLBACK_CATEGORY in {left, right}:
        return 0.5
    if left == right:
        return 1.0
    try:
        return 0.6 if group_of(left) == group_of(right) else 0.0
    except KeyError:
        # An id outside docs/taxonomy.csv is an upstream bug; treat it as no evidence rather
        # than as disagreement so one bad row cannot split an otherwise good cluster.
        log.warning("unknown category id in conflation: %r / %r", left, right)
        return 0.5


def score_pair(
    left: PoiRecord, right: PoiRecord, *, chain_keys: frozenset[str] = frozenset()
) -> MatchScore:
    """Score one candidate pair in 0..1 with its components.

    Pure: no I/O.  ``chain_keys`` comes from :func:`chain_keys_for` over the whole corpus; pass
    an empty set to score a pair in isolation (the admin UI does this when re-explaining a
    queued decision, where the seeded :data:`KNOWN_CHAIN_KEYS` still apply).
    """
    distance_m = haversine_m(left.lat, left.lon, right.lat, right.lon)
    name_score = _name_similarity(left.name, right.name)
    distance_score = _distance_score(distance_m)
    category_score = _category_score(left.category, right.category)
    raw = (
        NAME_WEIGHT * name_score
        + DISTANCE_WEIGHT * distance_score
        + CATEGORY_WEIGHT * category_score
    )

    left_brand, right_brand = brand_key(left), brand_key(right)
    same_brand = bool(left_brand) and left_brand == right_brand
    is_chain = same_brand and (left_brand in chain_keys or left_brand in KNOWN_CHAIN_KEYS)

    # Caps, in the order they are applied.  Each one only ever lowers the score, and records
    # which guard fired so the admin queue can say why.
    score = raw
    capped_by: str | None = None
    if is_chain and distance_m > CHAIN_MAX_MERGE_DISTANCE_M:
        # Two Puma stations either side of the highway.  Not a maybe — separate.
        score = min(score, REVIEW_SCORE - 0.01)
        capped_by = "chain_distance_guard"
    elif distance_m > NAME_ONLY_MAX_MERGE_DISTANCE_M and score >= AUTO_MERGE_SCORE:
        score = AUTO_MERGE_SCORE - 0.01
        capped_by = "name_only_distance"
    elif left.source == right.source and score >= AUTO_MERGE_SCORE:
        # Within one source, two nearby rows with the same name are more often two real
        # branches (or a mapping error worth a human's eyes) than one place counted twice.
        score = AUTO_MERGE_SCORE - 0.01
        capped_by = "same_source"

    return MatchScore(
        score=max(0.0, min(1.0, score)),
        name_score=name_score,
        distance_score=distance_score,
        category_score=category_score,
        distance_m=distance_m,
        same_source=left.source == right.source,
        same_brand=same_brand,
        chain=is_chain,
        capped_by=capped_by,
    )


def decide(score: MatchScore | float) -> Decision:
    """Map a score onto the three bands from docs/PLAN.md section 4.2 step 4."""
    value = score.score if isinstance(score, MatchScore) else float(score)
    if value >= AUTO_MERGE_SCORE:
        return Decision.MERGE
    if value >= REVIEW_SCORE:
        return Decision.REVIEW
    return Decision.SEPARATE


@dataclass(frozen=True, slots=True)
class MatchPair:
    """A scored candidate pair and what to do with it."""

    left: PoiRecord
    right: PoiRecord
    score: MatchScore
    decision: Decision

    def as_queue_row(self) -> dict[str, Any]:
        """Row shaped for ``poi_match_queue`` (db/migrations/001_init.sql)."""
        return {
            "left_key": self.left.key,
            "right_key": self.right.key,
            "score": round(self.score.score, 4),
            "name_score": round(self.score.name_score, 4),
            "distance_m": round(self.score.distance_m, 2),
            "category_ok": self.score.category_score >= 1.0,
            "decision": "pending",
            "explanation_es": self.score.explain_es(),
        }


# --------------------------------------------------------------------------- #
# Merging
# --------------------------------------------------------------------------- #


def _is_empty(field_name: str, value: Any) -> bool:
    """Whether a field carries no information for precedence purposes."""
    if value is None:
        return True
    if field_name == "status":
        # The schema default; means "nobody said", not "unverified is the answer".
        return str(value) == "unverified"
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, tuple | list):
        return not value
    return False


def _precedence_sorted(records: Sequence[PoiRecord], field_name: str) -> list[PoiRecord]:
    """Records ordered by the precedence table for one field, deterministically.

    Ties inside one source fall back to higher ``confidence`` first and then to ``source_id``,
    so re-running the nightly build over unchanged input produces byte-identical output.
    """
    order = FIELD_PRECEDENCE.get(field_name, DEFAULT_PRECEDENCE)
    rank = {source: index for index, source in enumerate(order)}
    return sorted(
        records,
        key=lambda record: (
            rank.get(record.source, len(order)),
            -record.confidence,
            record.source_id,
        ),
    )


@dataclass(frozen=True, slots=True)
class MergedPoi:
    """One published POI, assembled from a cluster of source records."""

    key: str
    name: str
    lat: float
    lon: float
    category: str
    name_alt: tuple[str, ...] = ()
    cuisine: tuple[str, ...] = ()
    subcategory: str | None = None
    address_text: str | None = None
    city: str | None = None
    phone: str | None = None
    whatsapp: str | None = None
    website: str | None = None
    facebook: str | None = None
    instagram: str | None = None
    opening_hours: str | None = None
    price_level: int | None = None
    status: str = "unverified"
    brand: str | None = None
    confidence: float = 0.0
    popularity: float = 0.0
    sources: dict[str, str] = field(default_factory=dict)
    source_keys: tuple[str, ...] = ()
    source_licenses: dict[str, str] = field(default_factory=dict)
    field_sources: dict[str, str] = field(default_factory=dict)

    @property
    def gers_id(self) -> str | None:
        """Overture GERS id, kept so the monthly refresh joins instead of re-conflating."""
        return self.sources.get("overture_gers_id")

    def as_feature(self) -> dict[str, Any]:
        """GeoJSON Feature for ``data/exports/pois.geojson`` (tippecanoe + Meilisearch)."""
        properties: dict[str, Any] = {
            "key": self.key,
            "name": self.name,
            "name_alt": list(self.name_alt),
            "category": self.category,
            "subcategory": self.subcategory,
            "cuisine": list(self.cuisine),
            "address_text": self.address_text,
            "city": self.city,
            "phone": self.phone,
            "whatsapp": self.whatsapp,
            "website": self.website,
            "facebook": self.facebook,
            "instagram": self.instagram,
            "opening_hours": self.opening_hours,
            "price_level": self.price_level,
            "status": self.status,
            "brand": self.brand,
            "confidence": round(self.confidence, 4),
            "popularity": round(self.popularity, 4),
            "sources": dict(self.sources),
            "source_keys": list(self.source_keys),
            "source_licenses": dict(self.source_licenses),
            "field_sources": dict(self.field_sources),
        }
        return {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [self.lon, self.lat]},
            "properties": properties,
        }


def merge_records(records: Sequence[PoiRecord]) -> MergedPoi:
    """Collapse a cluster of matched records into the published row.

    Field by field, the first non-empty value in :data:`FIELD_PRECEDENCE` order wins, and
    ``field_sources`` records which source that was — the admin POI editor shows it, and a
    reviewer arguing with a phone number needs to know whether it came off a Facebook page or
    off a wall.  Position is taken as a *pair* from a single record: mixing one source's
    latitude with another's longitude would invent a point that nobody surveyed.
    """
    if not records:
        raise ValueError("merge_records needs at least one record")

    field_sources: dict[str, str] = {}
    values: dict[str, Any] = {}

    position = _precedence_sorted(records, "position")[0]
    field_sources["position"] = position.source

    for name in SCALAR_FIELDS:
        for record in _precedence_sorted(records, name):
            value = getattr(record, name)
            if not _is_empty(name, value):
                values[name] = value
                field_sources[name] = record.source
                break

    chosen_name = values.get("name") or records[0].name
    # Alt names are the union of every other spelling any source knows, deduped on the
    # accent-folded form so "El Zaguán" and "El Zaguan" do not both survive.
    seen = {normalize(chosen_name)}
    alt: list[str] = []
    for record in _precedence_sorted(records, "name_alt"):
        for candidate in (record.name, *record.name_alt):
            folded = normalize(candidate or "")
            if folded and folded not in seen:
                seen.add(folded)
                alt.append(candidate)

    cuisine: list[str] = []
    for record in _precedence_sorted(records, "cuisine"):
        for item in record.cuisine:
            if item and item not in cuisine:
                cuisine.append(item)

    sources: dict[str, str] = {}
    for record in records:
        id_key = SOURCE_ID_KEYS.get(record.source, f"{record.source}_id")
        if record.source == "overture":
            sources.setdefault(id_key, record.gers_id or record.source_id)
        else:
            sources.setdefault(id_key, record.source_id)

    distinct_sources = len({record.source for record in records})
    base_confidence = max((record.confidence for record in records), default=0.0)
    confidence = min(1.0, base_confidence + CONFIDENCE_PER_EXTRA_SOURCE * (distinct_sources - 1))

    # The published key is the position winner's key: stable across runs and pointing at the
    # row whose geometry ended up on the map.
    return MergedPoi(
        key=position.key,
        name=chosen_name,
        lat=position.lat,
        lon=position.lon,
        category=values.get("category") or FALLBACK_CATEGORY,
        name_alt=tuple(alt),
        cuisine=tuple(cuisine),
        subcategory=values.get("subcategory"),
        address_text=values.get("address_text"),
        city=values.get("city"),
        phone=values.get("phone"),
        whatsapp=values.get("whatsapp"),
        website=values.get("website"),
        facebook=values.get("facebook"),
        instagram=values.get("instagram"),
        opening_hours=values.get("opening_hours"),
        price_level=values.get("price_level"),
        status=values.get("status", "unverified"),
        brand=values.get("brand"),
        confidence=confidence,
        popularity=max((record.popularity for record in records), default=0.0),
        sources=sources,
        source_keys=tuple(sorted(record.key for record in records)),
        source_licenses={record.key: record.license for record in records if record.license},
        field_sources=field_sources,
    )


# --------------------------------------------------------------------------- #
# The whole job
# --------------------------------------------------------------------------- #


class _UnionFind:
    """Tiny disjoint-set over record indices; clusters are transitive merges."""

    def __init__(self, size: int) -> None:
        self._parent = list(range(size))

    def find(self, item: int) -> int:
        root = item
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[item] != root:  # path compression
            self._parent[item], item = root, self._parent[item]
        return root

    def union(self, left: int, right: int) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            self._parent[max(left_root, right_root)] = min(left_root, right_root)


@dataclass(frozen=True, slots=True)
class ConflationResult:
    """Everything one conflation run produced."""

    merged: tuple[MergedPoi, ...]
    review: tuple[MatchPair, ...]
    pairs: tuple[MatchPair, ...]
    stats: dict[str, int]


def conflate(
    records: Iterable[PoiRecord | Mapping[str, Any]],
    *,
    radius_m: float = BLOCK_RADIUS_M,
    chain_keys: frozenset[str] | None = None,
    decisions: Sequence[Mapping[str, Any]] = (),
) -> ConflationResult:
    """Block, score, decide and merge a corpus of source records.

    Pure: takes records in, returns decisions and merged rows out.  Nothing here reads a
    database — the caller loads ``poi_source`` rows (or GeoJSON-seq files) and writes the
    result back.

    Records are processed in ``source:source_id`` order so a rerun over unchanged input
    produces an identical export, which is what makes the nightly tile diff meaningful.
    """
    parsed = [
        item if isinstance(item, PoiRecord) else PoiRecord.from_mapping(item) for item in records
    ]
    parsed.sort(key=lambda record: record.key)
    if chain_keys is None:
        chain_keys = chain_keys_for(parsed)

    pairs: list[MatchPair] = []
    union = _UnionFind(len(parsed))
    indices = {record.key: i for i, record in enumerate(parsed)}
    overrides = {}
    for row in decisions:
        key = tuple(sorted((row["left_key"], row["right_key"])))
        decision = Decision(row["decision"])
        if decision is Decision.REVIEW:
            raise ValueError("Only final decisions belong in a snapshot")
        if key in overrides and overrides[key] != decision:
            raise ValueError(f"Conflicting decisions for {key}")
        overrides[key] = decision
    forbidden = [
        (indices[a], indices[b])
        for (a, b), d in overrides.items()
        if d is Decision.SEPARATE and a in indices and b in indices
    ]

    def join(i, j, *, explicit=False):
        roots = {union.find(i), union.find(j)}
        if any(union.find(a) in roots and union.find(b) in roots for a, b in forbidden):
            if explicit:
                raise ValueError("Merge decisions conflict with a separate decision")
            return False
        union.union(i, j)
        return True

    # Human merges are evaluated before automatic edges, including distant pairs.
    for (a, b), decision in sorted(overrides.items()):
        if decision is Decision.MERGE and a in indices and b in indices:
            join(indices[a], indices[b], explicit=True)
    candidates = set(candidate_pairs(parsed, radius_m=radius_m))
    candidates.update(
        tuple(sorted((indices[a], indices[b])))
        for a, b in overrides
        if a in indices and b in indices
    )
    for i, j in sorted(candidates):
        score = score_pair(parsed[i], parsed[j], chain_keys=chain_keys)
        decision = overrides.get(tuple(sorted((parsed[i].key, parsed[j].key))), decide(score))
        if decision is Decision.MERGE and not join(i, j):
            decision = Decision.SEPARATE
        pairs.append(MatchPair(left=parsed[i], right=parsed[j], score=score, decision=decision))

    clusters: dict[int, list[PoiRecord]] = {}
    for index, record in enumerate(parsed):
        clusters.setdefault(union.find(index), []).append(record)

    merged = tuple(merge_records(cluster) for _, cluster in sorted(clusters.items()))
    review = tuple(pair for pair in pairs if pair.decision is Decision.REVIEW)
    stats = {
        "input_records": len(parsed),
        "candidate_pairs": len(pairs),
        "auto_merged_pairs": sum(1 for pair in pairs if pair.decision is Decision.MERGE),
        "review_pairs": len(review),
        "separate_pairs": sum(1 for pair in pairs if pair.decision is Decision.SEPARATE),
        "merged_pois": len(merged),
        "clusters_with_multiple_sources": sum(1 for poi in merged if len(poi.source_keys) > 1),
        "chain_keys": len(chain_keys),
    }
    log.info(
        "conflated %(input_records)d records into %(merged_pois)d POIs "
        "(%(auto_merged_pairs)d auto-merges, %(review_pairs)d queued)",
        stats,
    )
    return ConflationResult(merged=merged, review=review, pairs=pairs, stats=stats)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _record_from_feature(feature: Mapping[str, Any]) -> PoiRecord:
    """GeoJSON Feature (Point, ``[lon, lat]``) -> :class:`PoiRecord`."""
    geometry = feature.get("geometry") or {}
    coordinates = geometry.get("coordinates") or []
    if geometry.get("type") != "Point" or len(coordinates) < 2:
        raise ValueError(f"expected a Point feature, got {geometry.get('type')!r}")
    properties = dict(feature.get("properties") or {})
    properties.setdefault("lon", coordinates[0])
    properties.setdefault("lat", coordinates[1])
    return PoiRecord.from_mapping(properties)


def _read_inputs(paths: Sequence[Path]) -> list[PoiRecord]:
    records: list[PoiRecord] = []
    for path in paths:
        count = 0
        for feature in read_geojsonseq(path):
            records.append(_record_from_feature(feature))
            count += 1
        log.info("read %d records from %s", count, path)
    return records


def build_parser() -> argparse.ArgumentParser:
    """Argument parser for ``python -m pipeline.pois.conflate``."""
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.pois.conflate",
        description=(
            "Conflate OSM, Overture and survey POI records into the published POI export. "
            "Reads GeoJSON-seq (one Point Feature per line) and writes GeoJSON-seq plus a "
            "JSON review queue."
        ),
    )
    parser.add_argument(
        "--input",
        dest="inputs",
        action="append",
        required=True,
        type=Path,
        metavar="PATH",
        help="GeoJSON-seq of source records; repeat for each source",
    )
    parser.add_argument(
        "--output", required=True, type=Path, help="GeoJSON-seq of merged POIs to publish"
    )
    parser.add_argument(
        "--queue", type=Path, default=None, help="optional JSON file for the 0.60-0.85 review band"
    )
    parser.add_argument(
        "--radius-m",
        type=float,
        default=BLOCK_RADIUS_M,
        help=f"blocking radius in metres (default {BLOCK_RADIUS_M:g})",
    )
    parser.add_argument("--decisions", type=Path, help="snapshot of durable human decisions")
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point; returns a process exit code (non-zero so cron notices a failure)."""
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level)
    try:
        # Fail before doing any work if the taxonomy is broken: category agreement depends on it.
        load_taxonomy()
        records = _read_inputs(args.inputs)
        from pipeline.pois.review import load_decisions

        decisions = load_decisions(args.decisions) if args.decisions else []
        result = conflate(records, radius_m=args.radius_m, decisions=decisions)
        written = write_geojsonseq(args.output, (poi.as_feature() for poi in result.merged))
        log.info("wrote %d merged POIs to %s", written, args.output)
        if args.queue is not None:
            _write_queue(args.queue, result)
    except Exception:
        log.exception("conflation failed")
        return 1
    return 0


def _write_queue(path: Path, result: ConflationResult) -> None:
    """Write the review band as JSON rows ready for ``poi_match_queue``."""
    payload = {
        "stats": result.stats,
        "rows": [pair.as_queue_row() for pair in result.review],
    }
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))
    log.info("wrote %d review rows to %s", len(result.review), path)


if __name__ == "__main__":  # pragma: no cover - exercised by the CLI, not the unit tests
    sys.exit(main())
