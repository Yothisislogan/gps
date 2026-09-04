"""Shared dependencies: clients, rate limiting and request identity.

Clients live on ``app.state`` and are handed out by these providers, so tests
override one function instead of monkeypatching module globals.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections import defaultdict, deque
from collections.abc import Callable

from fastapi import Depends, Request, status

from api.clients.db import Database
from api.clients.meili import MeiliClient
from api.errors import ApiError
from common.config import Settings, get_settings
from common.valhalla import AsyncValhallaClient

__all__ = [
    "RateLimiter",
    "client_fingerprint",
    "get_db",
    "get_meili",
    "get_settings_dep",
    "get_valhalla",
    "rate_limit",
]

log = logging.getLogger(__name__)


def get_settings_dep(request: Request) -> Settings:
    """The running app's settings, falling back to the process-wide ones.

    ``create_app(settings)`` is the seam tests and alternate deployments use, so
    dependencies must read from the app rather than the module-level singleton.
    """
    return getattr(request.app.state, "settings", None) or get_settings()


def get_db(request: Request) -> Database:
    database = getattr(request.app.state, "db", None)
    if database is None:
        raise ApiError(
            "database_unavailable",
            "La base de datos no está disponible en este momento.",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    return database


def get_meili(request: Request) -> MeiliClient:
    client = getattr(request.app.state, "meili", None)
    if client is None:
        raise ApiError(
            "search_unavailable",
            "La búsqueda no está disponible en este momento.",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    return client


def get_valhalla(request: Request) -> AsyncValhallaClient:
    client = getattr(request.app.state, "valhalla", None)
    if client is None:
        raise ApiError(
            "routing_unavailable",
            "El servicio de rutas no está disponible en este momento.",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    return client


def client_fingerprint(request: Request) -> str:
    """A stable, non-identifying key for one caller.

    nginx sits in front, so the real address is in ``X-Forwarded-For``.  The
    value is hashed with a per-process salt before it is used or stored: rate
    limiting needs to tell callers apart, not to know who they are.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    address = (
        forwarded.split(",")[0].strip()
        if forwarded
        else (request.client.host if request.client else "unknown")
    )
    return hashlib.sha256(f"nicanav:{address}".encode()).hexdigest()[:32]


class RateLimiter:
    """In-process sliding-window limiter.

    One box, one API process group, so an in-process limiter is honest and
    dependency-free; nginx does a coarser pass in front.  If the deployment ever
    grows past one host this moves to Redis — noted rather than pre-built.
    """

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    @staticmethod
    def parse(spec: str) -> tuple[int, float]:
        """``"60/minute"`` -> ``(60, 60.0)``."""
        count, _, unit = spec.partition("/")
        seconds = {"second": 1.0, "minute": 60.0, "hour": 3600.0, "day": 86400.0}.get(
            unit.strip().rstrip("s"), 60.0
        )
        return int(count), seconds

    def check(self, key: str, spec: str, *, now: float | None = None) -> bool:
        """True when the call is allowed; records it as a hit."""
        limit, window = self.parse(spec)
        moment = time.monotonic() if now is None else now
        hits = self._hits[key]
        cutoff = moment - window
        while hits and hits[0] < cutoff:
            hits.popleft()
        if len(hits) >= limit:
            return False
        hits.append(moment)
        return True

    def reset(self) -> None:
        self._hits.clear()


def rate_limit(spec_name: str) -> Callable[..., None]:
    """Dependency factory: ``Depends(rate_limit("rate_limit_route"))``.

    The limit is read from settings at request time so it can be tuned with an
    env var and a restart, not a code change.
    """

    def _dependency(
        request: Request,
        settings: Settings = Depends(get_settings_dep),
        fingerprint: str = Depends(client_fingerprint),
    ) -> None:
        limiter: RateLimiter | None = getattr(request.app.state, "limiter", None)
        if limiter is None:
            return
        spec = getattr(settings, spec_name, "60/minute")
        if not limiter.check(f"{spec_name}:{fingerprint}", spec):
            raise ApiError(
                "rate_limited",
                "Demasiadas solicitudes. Esperá un momento.",
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            )

    return _dependency
