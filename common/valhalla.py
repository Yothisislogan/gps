"""Valhalla client and request builder.

Both the API (which proxies routing for the app) and the pipeline (which runs
the nightly golden-route regression) talk to Valhalla, and ``pipeline`` may not
import ``api``, so the client lives here.

The request *builder* is a pure function, separate from the HTTP call, for two
reasons: it is the single place where Valhalla's field names appear (rename once,
not in five call sites), and it can be tested exhaustively without a running
routing engine.

Field names follow Valhalla's documented turn-by-turn API.  Anything not
confirmed against a running instance is marked ``# UNVERIFIED`` so the first
deploy has a checklist rather than a mystery.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

import httpx

from common.models import Coordinate
from common.polyline import decode

__all__ = [
    "NICARAGUA_AUTO_COSTING",
    "AsyncValhallaClient",
    "RouteSummary",
    "ValhallaClient",
    "ValhallaError",
    "build_route_payload",
    "summarize_route",
]

log = logging.getLogger(__name__)


class ValhallaError(RuntimeError):
    """Valhalla refused or failed a request.

    ``status`` is the HTTP status when there was one; ``payload`` is Valhalla's
    own error body, which carries an ``error_code`` worth logging (171 =
    "no suitable edges near location", the one users actually hit).
    """

    def __init__(self, message: str, *, status: int | None = None, payload: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.payload = payload


#: Costing defaults tuned for Nicaragua.  Rationale per key:
#:
#: * ``use_tracks``: ``highway=track`` in Nicaragua is usually a farm path, not a
#:   shortcut; sending a rental car down one is the fastest way to lose a user.
#: * ``use_living_streets``: low but non-zero — Managua's residenciales are
#:   legitimately drivable.
#: * ``service_penalty``: parking aisles should not be through-routes.
#: * ``maneuver_penalty``: slightly above Valhalla's default so the router prefers
#:   fewer turns through the city grid, which is how locals actually drive.
#: * ``use_ferry``: Ometepe and the Río San Juan are real, so ferries stay
#:   enabled but are not preferred.
#: * ``country_crossing_penalty``: high — an "efficient" hop into Honduras or
#:   Costa Rica is never what a Managua driver wants.
NICARAGUA_AUTO_COSTING: dict[str, Any] = {
    "use_tracks": 0.0,
    "use_living_streets": 0.2,
    "service_penalty": 30.0,
    "maneuver_penalty": 10.0,
    "use_ferry": 0.5,
    "use_highways": 1.0,
    "use_tolls": 1.0,  # there are no road tolls in Nicaragua; left permissive
    "country_crossing_penalty": 2000.0,
    "shortest": False,
}

#: Valhalla has no "penalise unpaved" costing knob — the auto option is
#: ``exclude_unpaved`` and there is nothing in between.  Discouraging dirt roads
#: without banning them therefore happens at *tile build* time: Valhalla derives
#: edge speeds from the OSM ``surface`` tag, so an unpaved tertiary carries a low
#: speed and a dirt shortcut loses to a paved detour on time while staying
#: routable.  Many Nicaraguan places are only reachable on dirt, so that is the
#: behaviour we want by default; ``avoid_unpaved`` below is the user-facing
#: "Evitar caminos de tierra" toggle.


def build_route_payload(
    locations: Sequence[Coordinate | tuple[float, float] | dict[str, float]],
    *,
    costing: str = "auto",
    language: str = "es-ES",
    units: str = "kilometers",
    alternates: int = 2,
    avoid_unpaved: bool = False,
    exclude_polygons: Sequence[Sequence[Sequence[float]]] | None = None,
    heading: float | None = None,
    date_time: dict[str, Any] | None = None,
    output_format: str | None = None,
    costing_overrides: dict[str, Any] | None = None,
    banner_instructions: bool = True,
    voice_instructions: bool = True,
) -> dict[str, Any]:
    """Build a Valhalla ``/route`` request body.

    Pure: no I/O, no settings lookup.  Callers pass what they want; the defaults
    are the Nicaraguan ones.

    ``exclude_polygons`` is Valhalla's closure mechanism — a list of rings, each
    ring a list of ``[lon, lat]`` pairs (GeoJSON order, *not* the ``lat/lon``
    order used by ``locations``).  Getting that backwards silently excludes a
    patch of ocean instead of the flooded cauce, so it is asserted in tests.

    ``heading`` applies to the first location only: it is what stops a reroute
    from sending the driver back the way they came.
    """
    payload_locations: list[dict[str, Any]] = []
    for index, item in enumerate(locations):
        if isinstance(item, Coordinate):
            entry: dict[str, Any] = {"lat": item.lat, "lon": item.lon}
        elif isinstance(item, dict):
            entry = {"lat": float(item["lat"]), "lon": float(item["lon"])}
        else:
            entry = {"lat": float(item[0]), "lon": float(item[1])}
        # break_through would force a U-turn at vias; plain "break" is right for
        # the app's waypoints.
        entry["type"] = "break"
        if index == 0 and heading is not None:
            entry["heading"] = float(heading) % 360.0
            entry["heading_tolerance"] = 45
        payload_locations.append(entry)

    costing_options = {**NICARAGUA_AUTO_COSTING}
    if avoid_unpaved:
        costing_options["exclude_unpaved"] = True
    if costing_overrides:
        costing_options.update(costing_overrides)

    # `language` and `units` go at the TOP level.  Valhalla still reads a legacy
    # `directions_options` object, but it lifts only units/narrative/format/
    # language out of it and silently drops everything else — so a request that
    # nests banner_instructions or shape_format in there gets no instructions and
    # no error.  Keeping every directions option in one place, at the top level,
    # is what avoids that trap.
    payload: dict[str, Any] = {
        "locations": payload_locations,
        "costing": costing,
        "costing_options": {costing: costing_options},
        "language": language,
        "units": units,
    }
    if alternates:
        payload["alternates"] = int(alternates)
    if exclude_polygons:
        payload["exclude_polygons"] = [list(ring) for ring in exclude_polygons]
    if date_time:
        payload["date_time"] = date_time
    if output_format:
        payload["format"] = output_format
        if output_format == "osrm":
            # Top-level booleans, and only meaningful for format=osrm.  Note for
            # the client: Valhalla writes each maneuver's bannerInstructions and
            # voiceInstructions onto the *previous* step (the Mapbox convention),
            # and the final arrive step carries empty arrays.
            payload["banner_instructions"] = banner_instructions
            payload["voice_instructions"] = voice_instructions
            payload["turn_lanes"] = True  # lane guidance in the sub-banner
    return payload


class RouteSummary:
    """The handful of route facts the app and the QA harness both need."""

    __slots__ = ("distance_km", "duration_s", "maneuvers", "raw", "shape")

    def __init__(
        self,
        *,
        distance_km: float,
        duration_s: float,
        shape: list[tuple[float, float]],
        maneuvers: list[dict[str, Any]],
        raw: dict[str, Any],
    ) -> None:
        self.distance_km = distance_km
        self.duration_s = duration_s
        self.shape = shape
        self.maneuvers = maneuvers
        self.raw = raw

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<RouteSummary {self.distance_km:.1f} km / {self.duration_s / 60:.0f} min>"


def summarize_route(response: dict[str, Any], *, alternate: int | None = None) -> RouteSummary:
    """Pull distance, duration, shape and maneuvers out of a native Valhalla reply.

    ``alternate=None`` summarises the primary trip; ``alternate=0`` the first
    alternative.  Shapes are decoded at precision 6 — Valhalla's default, and the
    thing everyone gets wrong once.
    """
    trip = response.get("trip") if alternate is None else None
    if alternate is not None:
        alternates = response.get("alternates") or []
        if alternate >= len(alternates):
            raise ValhallaError(f"no alternate {alternate} in response")
        trip = alternates[alternate].get("trip")
    if not trip:
        raise ValhallaError("response carries no trip", payload=response)

    summary = trip.get("summary") or {}
    shape: list[tuple[float, float]] = []
    maneuvers: list[dict[str, Any]] = []
    for leg in trip.get("legs") or []:
        if leg.get("shape"):
            shape.extend(decode(leg["shape"]))
        maneuvers.extend(leg.get("maneuvers") or [])

    return RouteSummary(
        distance_km=float(summary.get("length", 0.0)),
        duration_s=float(summary.get("time", 0.0)),
        shape=shape,
        maneuvers=maneuvers,
        raw=response,
    )


def _raise_for_valhalla(response: httpx.Response) -> dict[str, Any]:
    """Turn a Valhalla HTTP response into JSON or a :class:`ValhallaError`."""
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if response.status_code >= 400 or (isinstance(payload, dict) and payload.get("error")):
        message = ""
        if isinstance(payload, dict):
            message = str(payload.get("error") or payload.get("message") or "")
        raise ValhallaError(
            message or f"valhalla returned HTTP {response.status_code}",
            status=response.status_code,
            payload=payload,
        )
    if not isinstance(payload, dict):
        raise ValhallaError("valhalla returned a non-object body", payload=payload)
    return payload


class ValhallaClient:
    """Blocking client, for pipeline jobs and scripts."""

    def __init__(
        self,
        base_url: str = "http://valhalla:8002",
        *,
        timeout: float = 15.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> ValhallaClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._http().post(f"{self.base_url}{path}", json=payload)
        return _raise_for_valhalla(response)

    def route(self, locations: Sequence[Any], **kwargs: Any) -> dict[str, Any]:
        """POST ``/route``; see :func:`build_route_payload` for the options."""
        return self._post("/route", build_route_payload(locations, **kwargs))

    def locate(
        self, locations: Sequence[Any], *, costing: str = "auto", verbose: bool = False
    ) -> Any:
        """POST ``/locate`` — snap points to the graph.

        Used by the geocoder: a Nicaraguan address describes a door, and doors
        are on streets, so a parsed point is snapped before being shown.
        """
        payload = {
            "locations": [
                {"lat": float(loc[0]), "lon": float(loc[1])}
                if not isinstance(loc, dict)
                else {"lat": float(loc["lat"]), "lon": float(loc["lon"])}
                for loc in locations
            ],
            "costing": costing,
            "verbose": verbose,
        }
        response = self._http().post(f"{self.base_url}/locate", json=payload)
        try:
            body = response.json()
        except ValueError as exc:
            raise ValhallaError("locate returned a non-JSON body") from exc
        if response.status_code >= 400:
            raise ValhallaError("locate failed", status=response.status_code, payload=body)
        return body

    def snap(self, lat: float, lon: float, *, costing: str = "auto") -> tuple[float, float] | None:
        """Snap one point to the nearest routable edge, or ``None`` if too far.

        Shaped for :func:`pipeline.geocode.relative_address.resolve`, which takes
        exactly this callable.
        """
        try:
            body = self.locate([(lat, lon)], costing=costing)
        except ValhallaError:
            log.warning("locate failed for %.5f,%.5f", lat, lon, exc_info=True)
            return None
        if not isinstance(body, list) or not body:
            return None
        entry = body[0]
        edges = entry.get("edges") or []
        if edges:
            point = (edges[0].get("correlated_lat"), edges[0].get("correlated_lon"))
            if point[0] is not None and point[1] is not None:
                return (float(point[0]), float(point[1]))
        nodes = entry.get("nodes") or []
        if nodes:
            point = (nodes[0].get("correlated_lat"), nodes[0].get("correlated_lon"))
            if point[0] is not None and point[1] is not None:
                return (float(point[0]), float(point[1]))
        return None

    def trace_attributes(
        self,
        shape: Sequence[tuple[float, float]],
        *,
        costing: str = "auto",
        shape_match: str = "map_snap",
        filters: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """POST ``/trace_attributes`` — map-match a GPS trace onto the graph.

        This is how field drives find wrong one-ways and missing roads, and later
        how probe traces become time-of-day speeds.
        """
        payload: dict[str, Any] = {
            "shape": [{"lat": float(lat), "lon": float(lon)} for lat, lon in shape],
            "costing": costing,
            "shape_match": shape_match,
        }
        if filters:
            payload["filters"] = {"attributes": list(filters), "action": "include"}
        return self._post("/trace_attributes", payload)

    def isochrone(
        self,
        lat: float,
        lon: float,
        *,
        contours_minutes: Sequence[float] = (15, 30, 60),
        costing: str = "auto",
        polygons: bool = True,
    ) -> dict[str, Any]:
        """POST ``/isochrone`` — used by the island/disconnection QA check."""
        return self._post(
            "/isochrone",
            {
                "locations": [{"lat": lat, "lon": lon}],
                "costing": costing,
                "contours": [{"time": float(minutes)} for minutes in contours_minutes],
                "polygons": polygons,
            },
        )

    def status(self, *, verbose: bool = False) -> dict[str, Any]:
        """GET ``/status`` — health check and, verbose, the tile set's bbox."""
        response = self._http().get(
            f"{self.base_url}/status", params={"verbose": "true"} if verbose else None
        )
        return _raise_for_valhalla(response)


class AsyncValhallaClient:
    """Non-blocking client for the FastAPI service."""

    def __init__(
        self,
        base_url: str = "http://valhalla:8002",
        *,
        timeout: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def route(self, locations: Sequence[Any], **kwargs: Any) -> dict[str, Any]:
        payload = build_route_payload(locations, **kwargs)
        response = await self._http().post(f"{self.base_url}/route", json=payload)
        return _raise_for_valhalla(response)

    async def snap(
        self, lat: float, lon: float, *, costing: str = "auto"
    ) -> tuple[float, float] | None:
        """Async twin of :meth:`ValhallaClient.snap`."""
        try:
            response = await self._http().post(
                f"{self.base_url}/locate",
                json={"locations": [{"lat": lat, "lon": lon}], "costing": costing},
            )
            body = response.json()
        except (httpx.HTTPError, ValueError):
            log.warning("locate failed for %.5f,%.5f", lat, lon, exc_info=True)
            return None
        if not isinstance(body, list) or not body:
            return None
        for key in ("edges", "nodes"):
            entries = body[0].get(key) or []
            if entries:
                lat_c = entries[0].get("correlated_lat")
                lon_c = entries[0].get("correlated_lon")
                if lat_c is not None and lon_c is not None:
                    return (float(lat_c), float(lon_c))
        return None

    async def status(self, *, verbose: bool = False) -> dict[str, Any]:
        response = await self._http().get(
            f"{self.base_url}/status", params={"verbose": "true"} if verbose else None
        )
        return _raise_for_valhalla(response)
