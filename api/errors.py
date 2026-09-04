"""Error envelope and exception handlers.

Every failure the client can see has the same shape::

    {"error": {"code": "snake_case_stable_string", "message": "texto en español"}}

``code`` is for the client to branch on and never changes; ``message`` is for the
user and is Spanish, because the product is Spanish-first.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from common.valhalla import ValhallaError

__all__ = ["ApiError", "error_body", "install_error_handlers"]

log = logging.getLogger(__name__)


class ApiError(Exception):
    """Raised anywhere in the API to produce a controlled error response."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = status.HTTP_400_BAD_REQUEST,
        detail: Any = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.detail = detail


def error_body(code: str, message: str, detail: Any = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"error": {"code": code, "message": message}}
    if detail is not None:
        payload["error"]["detail"] = detail
    return payload


def install_error_handlers(app: FastAPI) -> None:
    """Attach the handlers that keep every error in the same envelope."""

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code, content=error_body(exc.code, exc.message, exc.detail)
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=error_body(
                "invalid_request",
                "La solicitud no es válida.",
                [
                    {
                        "campo": ".".join(str(part) for part in err.get("loc", [])),
                        "detalle": err.get("msg"),
                    }
                    for err in exc.errors()
                ],
            ),
        )

    @app.exception_handler(ValhallaError)
    async def _valhalla_error(request: Request, exc: ValhallaError) -> JSONResponse:
        # Valhalla's 171 is by far the most common real-world failure: the user
        # dropped a pin on a building interior or a beach with no mapped road.
        code = "no_route"
        message = "No se encontró una ruta entre esos puntos."
        payload = exc.payload if isinstance(exc.payload, dict) else {}
        if payload.get("error_code") in (170, 171, 172):
            code = "no_road_nearby"
            message = "No hay una calle cercana a ese punto. Movelo un poco y probá de nuevo."
        log.warning("valhalla error: %s (%s)", exc, payload.get("error_code"))
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY, content=error_body(code, message)
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=error_body("internal_error", "Ocurrió un error inesperado."),
        )
