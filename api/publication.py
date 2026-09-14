"""Briefly pause mutations while a data generation is promoted.

The file lock spans API workers. Reads, including POST /api/route, keep serving.
The release manager marks the old generation read-only before switching nginx.
"""

from __future__ import annotations

import asyncio
import fcntl
import time
from pathlib import Path

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


def is_mutation(method: str, path: str) -> bool:
    return method in {"POST", "PUT", "PATCH", "DELETE"} and (
        path.startswith("/admin") or path in {"/api/report", "/api/alias", "/api/poi/suggest"}
    )


class PublicationGuard(BaseHTTPMiddleware):
    def __init__(self, app, *, directory: Path | None, release_id: str):
        super().__init__(app)
        self.directory = directory
        self.release_id = release_id

    async def dispatch(self, request, call_next):
        lock = None
        try:
            if self.directory and is_mutation(request.method, request.url.path):
                try:
                    lock = (self.directory / "writes.lock").open("rb")
                    deadline = time.monotonic() + 3
                    while True:
                        try:
                            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            if time.monotonic() >= deadline:
                                return self.paused()
                            await asyncio.sleep(0.05)
                    if (self.directory / "writes-paused").exists():
                        return self.paused()
                except OSError:
                    return self.paused()
            response = await call_next(request)
            if self.release_id:
                response.headers["X-NicaNav-Release"] = self.release_id
            return response
        finally:
            if lock:
                lock.close()

    @staticmethod
    def paused():
        return JSONResponse(
            {
                "error": {
                    "code": "publication_pending",
                    "message": "Estamos actualizando el mapa. Tu cambio no se envió; reintentá en la versión actual.",
                }
            },
            status_code=503,
            headers={"Retry-After": "30", "Cache-Control": "no-store"},
        )
