"""Async Meilisearch client.

Only the handful of operations the API needs (search and health), written
against Meilisearch's REST API directly rather than the sync SDK, because the
API is async and the SDK is not.  The pipeline's indexer uses the SDK.

Documents follow docs/SPEC.md section 8.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

__all__ = ["MeiliClient", "MeiliError"]

log = logging.getLogger(__name__)


class MeiliError(RuntimeError):
    """Meilisearch refused or failed a request."""


class MeiliClient:
    """Thin async wrapper around one Meilisearch index."""

    def __init__(
        self,
        base_url: str = "http://meilisearch:7700",
        *,
        api_key: str = "",
        index: str = "nicanav",
        timeout: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.index = index
        self._api_key = api_key
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def search(
        self,
        query: str,
        *,
        limit: int = 20,
        offset: int = 0,
        filters: list[str] | None = None,
        lat: float | None = None,
        lon: float | None = None,
        radius_m: float | None = None,
        sort_by_distance: bool = True,
    ) -> dict[str, Any]:
        """Run a search, optionally biased to the user's position.

        Geo behaviour follows Meilisearch's geosearch: ``_geoRadius`` filters and
        ``_geoPoint`` sorts.  Position bias is what makes "farmacia" useful — the
        nearest pharmacy, not an alphabetical list of every pharmacy in the
        country — so when a position is supplied the results are sorted by
        distance and only then by the stored popularity.
        """
        body: dict[str, Any] = {"q": query, "limit": limit, "offset": offset}
        all_filters = list(filters or [])
        if lat is not None and lon is not None and radius_m:
            all_filters.append(f"_geoRadius({lat}, {lon}, {int(radius_m)})")
        if all_filters:
            body["filter"] = all_filters
        if lat is not None and lon is not None and sort_by_distance:
            body["sort"] = [f"_geoPoint({lat}, {lon}):asc"]

        try:
            response = await self._http().post(
                f"{self.base_url}/indexes/{self.index}/search", json=body, headers=self._headers
            )
        except httpx.HTTPError as exc:
            raise MeiliError(f"meilisearch unreachable: {exc}") from exc
        if response.status_code >= 400:
            raise MeiliError(f"meilisearch returned {response.status_code}: {response.text[:300]}")
        return response.json()

    async def health(self) -> bool:
        """True when the index is reachable; never raises."""
        try:
            response = await self._http().get(f"{self.base_url}/health", timeout=2.0)
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def stats(self) -> dict[str, Any]:
        response = await self._http().get(
            f"{self.base_url}/indexes/{self.index}/stats", headers=self._headers
        )
        if response.status_code >= 400:
            raise MeiliError(f"meilisearch stats returned {response.status_code}")
        return response.json()
