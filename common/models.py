"""Shared data-transfer objects.

These are the contract between the pipeline (which produces rows), the API
(which serves them) and the web client (which renders them).  Anything the
browser sees is defined here so a field rename fails a test instead of silently
blanking a card.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "AliasSubmission",
    "Coordinate",
    "GeocodeCandidate",
    "GeocodeMethod",
    "GeocodeResponse",
    "PoiCard",
    "PoiPhoto",
    "PoiStatus",
    "PoiSuggestion",
    "RelativeAddress",
    "RelativeOffset",
    "ReportKind",
    "ReportSubmission",
    "RouteRequest",
    "SearchHit",
    "SearchKind",
    "SearchResponse",
    "SourceName",
]


class Coordinate(BaseModel):
    """A WGS84 point.  ``lat``/``lon``, never ``lng`` — one spelling everywhere."""

    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class SearchKind(StrEnum):
    POI = "poi"
    STREET = "street"
    NEIGHBOURHOOD = "neighbourhood"
    PLACE = "place"
    LANDMARK = "landmark"
    ALIAS = "alias"


class PoiStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    UNVERIFIED = "unverified"


class SourceName(StrEnum):
    """Provenance.  Kept per-row so ODbL and CDLA data stay separable."""

    OSM = "osm"
    OVERTURE = "overture"
    SURVEY = "survey"
    USER = "user"
    WIKIDATA = "wikidata"


class GeocodeMethod(StrEnum):
    """How a coordinate was derived — surfaced in the UI to set expectations."""

    INDEX = "index"  # direct hit in the search index
    RELATIVE = "relative"  # "de la Rotonda X, 2c al sur"
    KMPOST = "kmpost"  # "Km 12.5 Carretera a Masaya"
    INTERSECTION = "intersection"
    COORDINATE = "coordinate"  # user pasted lat,lon or a WhatsApp pin


class ReportKind(StrEnum):
    ROAD_CLOSED = "via_cerrada"
    POTHOLE = "bache"
    FLOODING = "inundacion"
    WRONG_ONEWAY = "sentido_incorrecto"
    BUSINESS_CLOSED = "negocio_cerrado"
    WRONG_POSITION = "ubicacion_incorrecta"
    CHECKPOINT = "reten"
    OTHER = "otro"


# --------------------------------------------------------------------------- #
# Geocoding
# --------------------------------------------------------------------------- #


class RelativeOffset(BaseModel):
    """One hop of a relative address: "2 cuadras al sur"."""

    quantity: float
    unit: Literal["cuadra", "vara", "metro", "kilometro"]
    direction_text: str
    bearing_deg: float
    distance_m: float


class RelativeAddress(BaseModel):
    """The parse of a Nicaraguan landmark-relative address string."""

    raw: str
    landmark_text: str
    offsets: list[RelativeOffset] = []
    modifiers: list[str] = []
    former_landmark: bool = False
    side_hint: Literal["derecha", "izquierda"] | None = None
    total_distance_m: float = 0.0

    def render(self) -> str:
        """Canonical display form, e.g. ``"De la Rotonda El Güegüense, 2c al sur"``."""
        parts = [self.landmark_text]
        for offset in self.offsets:
            if offset.unit == "cuadra":
                qty = f"{offset.quantity:g}c"
            elif offset.unit == "vara":
                qty = f"{offset.quantity:g} vrs"
            elif offset.unit == "kilometro":
                qty = f"{offset.quantity:g} km"
            else:
                qty = f"{offset.quantity:g} m"
            parts.append(f"{qty} {offset.direction_text}")
        parts.extend(self.modifiers)
        return ", ".join(parts)


class GeocodeCandidate(BaseModel):
    """One possible interpretation of a query, with a confidence in [0, 1]."""

    lat: float
    lon: float
    label: str
    confidence: float = Field(ge=0.0, le=1.0)
    method: GeocodeMethod
    snapped_to_road: bool = False
    relative: RelativeAddress | None = None
    landmark_id: str | None = None
    landmark_name: str | None = None
    poi_id: str | None = None
    notes: list[str] = []


class GeocodeResponse(BaseModel):
    query: str
    candidates: list[GeocodeCandidate] = []
    parsed_as: GeocodeMethod | None = None


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #


class SearchHit(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    kind: SearchKind
    name: str
    lat: float
    lon: float
    category: str | None = None
    subcategory: str | None = None
    address_text: str | None = None
    city: str | None = None
    distance_m: float | None = None
    verified: bool = False
    popularity: float = 0.0


class SearchResponse(BaseModel):
    query: str
    hits: list[SearchHit] = []
    geocode: GeocodeResponse | None = None
    took_ms: int = 0


# --------------------------------------------------------------------------- #
# POIs
# --------------------------------------------------------------------------- #


class PoiPhoto(BaseModel):
    url: str
    credit: str | None = None
    license: str | None = None
    taken_at: datetime | None = None


class PoiCard(BaseModel):
    """Everything the bottom sheet renders for one place."""

    id: str
    name: str
    alt_names: list[str] = []
    category: str
    subcategory: str | None = None
    cuisine: list[str] = []
    lat: float
    lon: float
    address_text: str | None = None
    relative_address: str | None = None
    phone: str | None = None
    whatsapp: str | None = None
    website: str | None = None
    facebook: str | None = None
    instagram: str | None = None
    opening_hours: str | None = None  # OSM opening_hours syntax; evaluated client-side
    price_level: int | None = Field(default=None, ge=0, le=4)
    status: PoiStatus = PoiStatus.UNVERIFIED
    confidence: float = 0.0
    verified_at: datetime | None = None
    verified_by: str | None = None
    sources: dict[str, Any] = {}
    photos: list[PoiPhoto] = []


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #


class RouteRequest(BaseModel):
    """Client-facing routing request; the API translates it for Valhalla.

    Deliberately narrower than Valhalla's own schema — the proxy adds closures,
    rate limits and language defaults, so clients never talk to Valhalla direct.
    """

    locations: list[Coordinate] = Field(min_length=2)
    costing: Literal["auto", "bicycle", "pedestrian", "motor_scooter", "truck"] = "auto"
    language: str = "es-ES"
    units: Literal["kilometers", "miles"] = "kilometers"
    alternates: int = Field(default=2, ge=0, le=3)
    avoid_unpaved: bool = False
    heading: float | None = Field(default=None, ge=0, lt=360)
    format: Literal["json", "osrm"] = "json"
    departure_time: datetime | None = None
    exclude_closures: bool = True


# --------------------------------------------------------------------------- #
# User submissions
# --------------------------------------------------------------------------- #


class ReportSubmission(BaseModel):
    kind: ReportKind
    lat: float
    lon: float
    note: str | None = Field(default=None, max_length=500)
    photo_url: str | None = None


class PoiSuggestion(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    category: str
    lat: float
    lon: float
    address_text: str | None = None
    phone: str | None = None
    whatsapp: str | None = None
    facebook: str | None = None
    opening_hours: str | None = None
    note: str | None = Field(default=None, max_length=500)


class AliasSubmission(BaseModel):
    """A corrected address string -> point mapping, learned from the pin UI."""

    text: str = Field(min_length=3, max_length=250)
    lat: float
    lon: float
    poi_id: str | None = None
    source: SourceName = SourceName.USER
