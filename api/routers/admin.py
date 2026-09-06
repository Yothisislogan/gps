"""The moderation UI.

Nothing a user submits is published until somebody looks at it. That rule is
what keeps a crowd-sourced map usable, and it is enforced by the schema — every
submission table defaults to ``status = 'pending'`` — rather than by convention.
These pages are where a human turns those queues into decisions.

Server-rendered Jinja with HTMX for the row-level actions: no build step, no
SPA, and a moderator working through two hundred rows never waits for a page
load. Basic auth is applied at the nginx edge (see ``infra/nginx.conf``) and
re-checked here, so the pages cannot be reached without it even if the proxy is
misconfigured.

The SQL lives in this module rather than in ``api/clients/db.py`` because it is
admin-only and would otherwise bloat the hot-path client. Queries that turn out
to be useful elsewhere should be promoted there.
"""

from __future__ import annotations

import json
import logging
import secrets
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from api.clients.db import Database
from api.deps import get_db, get_settings_dep
from common.config import Settings
from common.data_status import data_status
from common.geo import NICARAGUA_BBOX

router = APIRouter(prefix="/admin", tags=["admin"])
log = logging.getLogger(__name__)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
# Jinja2Templates enables autoescape for .html by default; assert it, because
# POI names and user notes are attacker-controlled text.
TEMPLATES.env.autoescape = True


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #


def require_admin(request: Request, settings: Settings = Depends(get_settings_dep)) -> str:
    """Verify Basic auth, in constant time, and return the user name.

    nginx already gates ``/admin``; this is the second lock. A deployment that
    forgets the nginx block should still not hand the moderation queue to the
    internet.
    """
    header = request.headers.get("authorization", "")
    expected_user = settings.admin_user
    expected_password = settings.admin_password

    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        allowed = {settings.public_base_url.rstrip("/"), str(request.base_url).rstrip("/")}
        if request.headers.get("sec-fetch-site") == "cross-site" or (
            origin is not None and origin.rstrip("/") not in allowed
        ):
            raise HTTPException(status_code=403, detail="Origen no autorizado")

    if not expected_password:
        # Refuse rather than fall open. An admin UI with no password configured
        # is worse than no admin UI.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="NICANAV_ADMIN_PASSWORD no está configurada",
        )

    if header.lower().startswith("basic "):
        import base64

        try:
            decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
            user, _, password = decoded.partition(":")
        except (ValueError, UnicodeDecodeError):
            user, password = "", ""
        if secrets.compare_digest(user.encode(), expected_user.encode()) and secrets.compare_digest(
            password.encode(), expected_password.encode()
        ):
            return user

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="No autorizado",
        headers={"WWW-Authenticate": 'Basic realm="nicanav"'},
    )


def render(request: Request, template: str, context: dict[str, Any]) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, template, context)


async def _safe(database: Database, sql: str, params: Any = None) -> list[dict[str, Any]]:
    """Run a query, returning [] when the database is unavailable.

    Every page degrades to an empty state rather than a traceback: a moderator
    hitting a Postgres restart should see "no disponible", not a 500.
    """
    try:
        return await database._fetch(sql, params)
    except Exception:
        log.warning("admin query failed", exc_info=True)
        return []


# --------------------------------------------------------------------------- #
# SQL
# --------------------------------------------------------------------------- #

_COUNTS = """
SELECT
  (SELECT count(*) FROM poi)                                             AS pois,
  (SELECT count(*) FROM poi WHERE in_circle)                             AS pois_circle,
  (SELECT count(*) FROM poi WHERE verified_at > now() - interval '12 months') AS pois_verified,
  (SELECT count(*) FROM poi_match_queue WHERE decision = 'pending')      AS match_queue,
  (SELECT count(*) FROM poi_suggestion  WHERE status   = 'pending')      AS suggestions,
  (SELECT count(*) FROM poi_flag        WHERE status   = 'pending')      AS flags,
  (SELECT count(*) FROM report          WHERE status   = 'pending')      AS reports,
  (SELECT count(*) FROM alias           WHERE status   = 'pending')      AS aliases,
  (SELECT count(*) FROM closure         WHERE active)                    AS closures,
  (SELECT count(*) FROM gazetteer)                                       AS landmarks,
  (SELECT count(*) FROM gazetteer WHERE former)                          AS former_landmarks
"""

_LATEST_KPIS = "SELECT taken_at, metrics FROM kpi_snapshot ORDER BY taken_at DESC LIMIT 1"

_MATCH_QUEUE = """
SELECT q.id, q.left_key, q.right_key, q.score, q.name_score, q.distance_m, q.category_ok,
       q.created_at,
       l.name AS left_name, l.category AS left_category, l.source AS left_source,
       ST_Y(l.geom) AS left_lat, ST_X(l.geom) AS left_lon,
       r.name AS right_name, r.category AS right_category, r.source AS right_source,
       ST_Y(r.geom) AS right_lat, ST_X(r.geom) AS right_lon
FROM poi_match_queue q
LEFT JOIN poi_source l ON l.source || ':' || l.source_id = q.left_key
LEFT JOIN poi_source r ON r.source || ':' || r.source_id = q.right_key
WHERE q.decision = 'pending'
ORDER BY q.score DESC
LIMIT %s OFFSET %s
"""

_SUGGESTIONS = """
SELECT id, payload, ST_Y(geom) AS lat, ST_X(geom) AS lon, created_at
FROM poi_suggestion WHERE status = 'pending' ORDER BY created_at DESC LIMIT %s
"""

_REPORTS = """
SELECT id, kind, note, photo_url, votes, ST_Y(geom) AS lat, ST_X(geom) AS lon, created_at
FROM report WHERE status = 'pending' ORDER BY created_at DESC LIMIT %s
"""

_ALIASES = """
SELECT id, text, confidence, hits, created_at, ST_Y(geom) AS lat, ST_X(geom) AS lon
FROM alias WHERE status = 'pending' ORDER BY hits DESC, created_at DESC LIMIT %s
"""

_CLOSURES = """
SELECT id, reason, note, starts_at, ends_at, active, source, created_by,
       ST_AsGeoJSON(geom) AS geom, ST_Y(ST_Centroid(geom)) AS lat, ST_X(ST_Centroid(geom)) AS lon
FROM closure
ORDER BY active DESC, starts_at DESC
LIMIT 100
"""

_POI = """
SELECT p.id::text AS id, p.name, p.name_alt, p.category, p.subcategory, p.cuisine,
       ST_Y(p.geom) AS lat, ST_X(p.geom) AS lon, p.address_text, p.city,
       p.phone, p.whatsapp, p.website, p.facebook, p.instagram, p.opening_hours,
       p.price_level, p.status, p.confidence, p.verified_at, p.verified_by, p.sources
FROM poi p WHERE p.id = %s
"""

_POI_SOURCES = """
SELECT source, source_id, name, category, license, confidence, fetched_at
FROM poi_source WHERE poi_id = %s ORDER BY source
"""


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    database: Database = Depends(get_db),
    user: str = Depends(require_admin),
) -> HTMLResponse:
    """What the map looks like today, and what is waiting for a decision."""
    counts_rows = await _safe(database, _COUNTS)
    kpi_rows = await _safe(database, _LATEST_KPIS)

    metrics = {}
    taken_at = None
    if kpi_rows:
        raw = kpi_rows[0].get("metrics")
        metrics = json.loads(raw) if isinstance(raw, str) else (raw or {})
        taken_at = kpi_rows[0].get("taken_at")

    return render(
        request,
        "dashboard.html",
        {
            "counts": counts_rows[0] if counts_rows else {},
            "metrics": metrics,
            "kpi_taken_at": taken_at,
            "available": getattr(database, "available", False),
            "tiles": _tile_ages(request),
            "data_status": data_status(request.app.state.settings.metadata_dir),
        },
    )


def _tile_ages(request: Request) -> list[dict[str, Any]]:
    """Age and size of the published archives.

    A stale ``base.pmtiles`` is the quietest failure the stack has: everything
    keeps working, with last month's roads.
    """
    import time

    settings: Settings = request.app.state.settings
    out = []
    for name in ("base.pmtiles", "circle.pmtiles", "pois.pmtiles"):
        path = Path(settings.tiles_dir) / name
        if path.exists():
            stat = path.stat()
            out.append(
                {
                    "name": name,
                    "mb": round(stat.st_size / 1e6, 1),
                    "age_hours": round((time.time() - stat.st_mtime) / 3600, 1),
                }
            )
        else:
            out.append({"name": name, "mb": None, "age_hours": None})
    return out


@router.get("/queue", response_class=HTMLResponse)
async def queue(
    request: Request,
    page: int = 0,
    database: Database = Depends(get_db),
    user: str = Depends(require_admin),
) -> HTMLResponse:
    """The conflation review band plus user submissions."""
    limit = 50
    return render(
        request,
        "queue.html",
        {
            "pairs": await _safe(database, _MATCH_QUEUE, (limit, page * limit)),
            "suggestions": await _safe(database, _SUGGESTIONS, (25,)),
            "reports": await _safe(database, _REPORTS, (25,)),
            "page": page,
            "available": getattr(database, "available", False),
        },
    )


@router.get("/aliases", response_class=HTMLResponse)
async def aliases(
    request: Request,
    database: Database = Depends(get_db),
    user: str = Depends(require_admin),
) -> HTMLResponse:
    """Learned address strings awaiting approval.

    Each row is shown with the parser's reading of it, because approving an
    alias whose landmark the parser could not find teaches the system nothing.
    """
    rows = await _safe(database, _ALIASES, (100,))
    from pipeline.geocode import relative_address

    enriched = []
    for row in rows:
        parsed = relative_address.parse(row["text"])
        enriched.append(
            {
                **row,
                "landmark": parsed.landmark_query if parsed else None,
                "offsets": [
                    f"{offset.quantity:g} {offset.unit} {offset.direction_text}"
                    for offset in (parsed.offsets if parsed else [])
                ],
                "former": bool(parsed and parsed.former_landmark),
            }
        )
    return render(
        request,
        "aliases.html",
        {"aliases": enriched, "available": getattr(database, "available", False)},
    )


@router.get("/closures", response_class=HTMLResponse)
async def closures(
    request: Request,
    database: Database = Depends(get_db),
    user: str = Depends(require_admin),
) -> HTMLResponse:
    """Road closures.

    These are injected into *every* routing request as ``exclude_polygons``, so
    this page reroutes the country's traffic. The template says so.
    """
    return render(
        request,
        "closures.html",
        {
            "closures": await _safe(database, _CLOSURES),
            "available": getattr(database, "available", False),
        },
    )


@router.get("/poi/{poi_id}", response_class=HTMLResponse)
async def poi_edit(
    request: Request,
    poi_id: str,
    database: Database = Depends(get_db),
    user: str = Depends(require_admin),
) -> HTMLResponse:
    """Edit one place, and see which source won each field."""
    from pipeline.pois.taxonomy import all_categories

    rows = await _safe(database, _POI, (poi_id,))
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No existe ese lugar")
    return render(
        request,
        "poi_edit.html",
        {
            "poi": rows[0],
            "sources": await _safe(database, _POI_SOURCES, (poi_id,)),
            "categories": [category.category_id for category in all_categories()],
            "available": getattr(database, "available", False),
        },
    )


# --------------------------------------------------------------------------- #
# Actions — every one of them POST, and every one recorded
# --------------------------------------------------------------------------- #


async def _execute(database: Database, sql: str, params: Any) -> bool:
    try:
        await database._execute(sql, params)
        return True
    except Exception:
        log.warning("admin write failed", exc_info=True)
        return False


@router.post("/queue/{pair_id}/decide", response_class=HTMLResponse)
async def decide_pair(
    request: Request,
    pair_id: int,
    decision: str = Form(...),
    database: Database = Depends(get_db),
    user: str = Depends(require_admin),
) -> HTMLResponse:
    """Accept or reject one conflation pair.

    Returns just the row's replacement, which is what HTMX swaps in — a
    moderator working through hundreds of these never waits for a page load.
    """
    if decision not in {"merge", "separate"}:
        raise HTTPException(status_code=400, detail="Decisión inválida")
    ok = await _execute(
        database,
        """
        UPDATE poi_match_queue
        SET decision = %s, decided_by = %s, decided_at = now()
        WHERE id = %s RETURNING id
        """,
        (decision, user, pair_id),
    )
    label = "unir" if decision == "merge" else "separar"
    return HTMLResponse(
        f'<tr class="decided"><td colspan="5">Decisión guardada: {label} por {user}. Pendiente de aplicar al mapa.</td></tr>'
        if ok
        else '<tr class="failed"><td colspan="5">No se pudo guardar</td></tr>'
    )


@router.post("/poi/{poi_id}/verify", response_class=HTMLResponse)
async def verify_poi(
    request: Request,
    poi_id: str,
    database: Database = Depends(get_db),
    user: str = Depends(require_admin),
) -> HTMLResponse:
    """Stamp a place as verified.

    The date is the whole point: a "verificado" badge with no date is a claim,
    and one with a date two years old is an honest warning.
    """
    ok = await _execute(
        database,
        "UPDATE poi SET verified_at = now(), verified_by = %s, status = 'open' WHERE id = %s RETURNING id",
        (user, poi_id),
    )
    return HTMLResponse(
        f'<span class="ok">Verificado por {user}</span>' if ok else '<span class="bad">Error</span>'
    )


@router.post("/poi/{poi_id}", response_class=HTMLResponse)
async def update_poi(
    request: Request,
    poi_id: str,
    name: str = Form(...),
    category: str = Form(...),
    phone: str = Form(""),
    whatsapp: str = Form(""),
    website: str = Form(""),
    facebook: str = Form(""),
    opening_hours: str = Form(""),
    address_text: str = Form(""),
    poi_status: str = Form("unverified"),
    lat: float = Form(...),
    lon: float = Form(...),
    database: Database = Depends(get_db),
    user: str = Depends(require_admin),
):
    """Save an edit, and count it as a verification."""
    from pipeline.pois.taxonomy import load_taxonomy

    if category not in load_taxonomy():
        raise HTTPException(status_code=400, detail="Categoría desconocida")
    if poi_status not in {"open", "closed", "unverified"}:
        raise HTTPException(status_code=400, detail="Estado inválido")
    min_lon, min_lat, max_lon, max_lat = NICARAGUA_BBOX
    if not (min_lat <= lat <= max_lat and min_lon <= lon <= max_lon):
        raise HTTPException(status_code=400, detail="Ese punto está fuera de Nicaragua")

    await _execute(
        database,
        """
        UPDATE poi SET
            name = %s, category = %s,
            phone = NULLIF(%s, ''), whatsapp = NULLIF(%s, ''), website = NULLIF(%s, ''),
            facebook = NULLIF(%s, ''), opening_hours = NULLIF(%s, ''),
            address_text = NULLIF(%s, ''), status = %s,
            geom = ST_SetSRID(ST_MakePoint(%s, %s), 4326),
            verified_at = now(), verified_by = %s
        WHERE id = %s RETURNING id
        """,
        (
            name,
            category,
            phone,
            whatsapp,
            website,
            facebook,
            opening_hours,
            address_text,
            poi_status,
            lon,
            lat,
            user,
            poi_id,
        ),
    )
    return RedirectResponse(f"/admin/poi/{poi_id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/alias/{alias_id}/decide", response_class=HTMLResponse)
async def decide_alias(
    request: Request,
    alias_id: str,
    decision: str = Form(...),
    database: Database = Depends(get_db),
    user: str = Depends(require_admin),
) -> HTMLResponse:
    """Approve or reject a learned address.

    An approved alias outranks the parser for that exact string, so approving a
    wrong one is worse than rejecting a right one.
    """
    if decision not in {"approved", "rejected"}:
        raise HTTPException(status_code=400, detail="Decisión inválida")
    ok = await _execute(
        database,
        "UPDATE alias SET status = %s, created_by = COALESCE(created_by, %s) WHERE id = %s RETURNING id",
        (decision, user, alias_id),
    )
    word = "Aprobada" if decision == "approved" else "Rechazada"
    return HTMLResponse(
        f'<tr class="decided"><td colspan="4">{word}</td></tr>'
        if ok
        else '<tr class="failed"><td colspan="4">No se pudo guardar</td></tr>'
    )


@router.post("/report/{report_id}/decide", response_class=HTMLResponse)
async def decide_report(
    request: Request,
    report_id: str,
    decision: str = Form(...),
    database: Database = Depends(get_db),
    user: str = Depends(require_admin),
) -> HTMLResponse:
    if decision not in {"accepted", "rejected"}:
        raise HTTPException(status_code=400, detail="Decisión inválida")
    ok = await _execute(
        database,
        "UPDATE report SET status = %s WHERE id = %s RETURNING id",
        (decision, report_id),
    )
    return HTMLResponse('<span class="ok">Listo</span>' if ok else '<span class="bad">Error</span>')


@router.post("/closures", response_class=HTMLResponse)
async def create_closure(
    request: Request,
    reason: str = Form(...),
    ring: str = Form(...),
    note: str = Form(""),
    ends_at: str = Form(""),
    database: Database = Depends(get_db),
    user: str = Depends(require_admin),
):
    """Create a closure from a drawn polygon.

    ``ring`` is a GeoJSON ring of ``[lon, lat]`` pairs. Lat-first would exclude
    a patch of ocean instead of the flooded cauce, and nothing would report an
    error, so the order is validated against Nicaragua's bounding box.
    """
    try:
        coordinates = json.loads(ring)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Polígono inválido") from exc
    if not isinstance(coordinates, list) or len(coordinates) < 3:
        raise HTTPException(status_code=400, detail="Se necesitan al menos 3 puntos")

    min_lon, min_lat, max_lon, max_lat = NICARAGUA_BBOX
    for point in coordinates:
        if not (isinstance(point, list) and len(point) >= 2):
            raise HTTPException(status_code=400, detail="Polígono inválido")
        lon, lat = float(point[0]), float(point[1])
        if not (min_lon <= lon <= max_lon and min_lat <= lat <= max_lat):
            raise HTTPException(
                status_code=400,
                detail="El polígono está fuera de Nicaragua (¿coordenadas invertidas?)",
            )

    if coordinates[0] != coordinates[-1]:
        coordinates.append(coordinates[0])

    await _execute(
        database,
        """
        INSERT INTO closure (geom, reason, note, ends_at, source, created_by)
        VALUES (ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326), %s, NULLIF(%s, ''),
                NULLIF(%s, '')::timestamptz, 'admin', %s)
        RETURNING id
        """,
        (
            json.dumps({"type": "Polygon", "coordinates": [coordinates]}),
            reason,
            note,
            ends_at,
            user,
        ),
    )
    return RedirectResponse("/admin/closures", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/closures/{closure_id}/deactivate", response_class=HTMLResponse)
async def deactivate_closure(
    request: Request,
    closure_id: str,
    database: Database = Depends(get_db),
    user: str = Depends(require_admin),
) -> HTMLResponse:
    ok = await _execute(
        database, "UPDATE closure SET active = false WHERE id = %s RETURNING id", (closure_id,)
    )
    return HTMLResponse(
        '<span class="ok">Desactivado</span>' if ok else '<span class="bad">Error</span>'
    )
