# nicanav — engineering contract

This file is the interface contract between the parts of the system. It is
normative: if code and this document disagree, that is a bug in one of them.
The *why* lives in [PLAN.md](PLAN.md); the *what exactly* lives here.

---

## 1. Repository layout

```
common/         shared, dependency-light: geo, text, polyline, config, models
api/            FastAPI service (routers/, templates/, deps.py, clients/)
pipeline/       batch jobs: fetch, build, conflate, index, QA
  common/       pipeline-only helpers (osm parsing, io)
  pois/         fetch_overture.py fetch_osm_pois.py conflate.py export_geojson.py
  geocode/      relative_address.py kmpost.py gazetteer_build.py
  search/       build_index.py
  qa/           golden_routes.py kpis.py islands.py
  *.sh          orchestration: fetch_osm.sh build_tiles.sh build_valhalla.sh nightly.sh
web/            no-bundler PWA: index.html, js/, style/, sw.js, manifest.webmanifest
infra/          docker-compose.yml, nginx.conf, valhalla/, .env.example, Makefile targets
db/migrations/  numbered .sql, applied in order, idempotent
docs/           PLAN.md, SPEC.md, taxonomy.csv, gazetteer_seed.csv, golden_routes.json
data/           gitignored build artifacts (see §6)
tests/          pytest; unit tests never touch the network or a database
```

**Import direction is one-way:** `common` ← `pipeline`, `common` ← `api`.
`pipeline` never imports `api`. Nothing in `common` may import either.

One documented exception: `api` may import `pipeline.geocode.*`, which is pure
(no I/O, no database, stdlib + `common` only) and is where the address grammar
lives. `api` imports nothing else from `pipeline`.

## 2. Conventions

* Python ≥ 3.11, `from __future__ import annotations`, full type hints.
* Coordinates are always `(lat, lon)` in Python and `[lon, lat]` in GeoJSON.
  Name the variables `lat`/`lon` — never `lng`, never bare `x`/`y` for degrees.
* Distances are metres (`_m` suffix), durations seconds (`_s`), bearings
  degrees clockwise from north (`_deg`).
* No network calls at import time. No `print()` in library code — use
  `logging.getLogger(__name__)`.
* Every pipeline script is runnable as `python -m pipeline.<pkg>.<mod> --help`
  and exits non-zero on failure so cron/CI notices.
* Secrets come from the environment via `common.config.get_settings()`. Never
  read `os.environ` directly outside that module; never commit a real value.
* Spanish is the product language: user-facing strings are Spanish first,
  English behind a toggle. Identifiers and comments are English.
* Tests: `pytest`, no network, no Docker. Anything needing a live service is
  marked `@pytest.mark.docker` or `@pytest.mark.network` and skipped by default.

## 3. Configuration (`NICANAV_*`)

| Env var | Default | Used by |
|---|---|---|
| `NICANAV_VALHALLA_URL` | `http://valhalla:8002` | api |
| `NICANAV_MEILI_URL` | `http://meilisearch:7700` | api, pipeline |
| `NICANAV_MEILI_KEY` | *(empty)* | api, pipeline |
| `NICANAV_MEILI_INDEX` | `nicanav` | api, pipeline |
| `NICANAV_DATABASE_URL` | `postgresql://nicanav:nicanav@postgis:5432/nicanav` | api, pipeline |
| `NICANAV_DATA_DIR` | `<repo>/data` | pipeline |
| `NICANAV_TILES_DIR` | `<repo>/data/tiles` | pipeline, nginx mount |
| `NICANAV_PUBLIC_BASE_URL` | `http://localhost:8080` | api (share links) |
| `NICANAV_TILES_BASE_URL` | `/tiles` | web style URLs |
| `NICANAV_CORS_ORIGINS` | `*` | api |
| `NICANAV_ADMIN_USER` / `NICANAV_ADMIN_PASSWORD` | `admin` / *(empty)* | api `/admin` |
| `NICANAV_RATE_LIMIT_ROUTE` / `_SEARCH` / `_REPORT` | `60/minute`, `300/minute`, `20/minute` | api |
| `NICANAV_MAPILLARY_TOKEN` | *(empty)* | api (POI photos; feature off when empty) |
| `NICANAV_DEFAULT_LANGUAGE` | `es-ES` | api |

## 4. HTTP API

Base path `/api`. All responses `application/json; charset=utf-8`. Models are
the Pydantic classes in `common/models.py` — the browser sees exactly those
field names.

| Method | Path | Query / body | Response |
|---|---|---|---|
| `GET` | `/api/healthz` | — | `{status, version, valhalla, meili, db}` |
| `GET` | `/api/search` | `q` (req), `lat`, `lon`, `limit≤50`, `kind`, `category`, `radius_m` | `SearchResponse` |
| `GET` | `/api/geocode` | `q` (req), `lat`, `lon`, `limit≤5` | `GeocodeResponse` |
| `GET` | `/api/reverse` | `lat`, `lon` (both req) | `GeocodeResponse` whose first candidate carries `relative` |
| `GET` | `/api/poi/{id}` | — | `PoiCard` (404 if unknown) |
| `GET` | `/api/poi/along_route` | `polyline` (encoded, precision 6), `category`, `radius_m≤1000`, `limit≤50` | `{hits: SearchHit[]}` |
| `POST` | `/api/route` | `RouteRequest` | Valhalla response, pass-through, plus `nicanav: {closures_applied, alternates}` |
| `POST` | `/api/report` | `ReportSubmission` | `{id, status}` |
| `POST` | `/api/poi/suggest` | `PoiSuggestion` | `{id, status}` |
| `POST` | `/api/alias` | `AliasSubmission` | `{id, status}` |
| `GET` | `/admin/*` | HTML (Jinja + HTMX), Basic auth | review queues, closures, KPIs |

Rules:

* `/api/search` runs the Meilisearch query **and**, when the query matches the
  relative-address or km-post grammar, the geocoder — both results come back in
  one payload (`hits` + `geocode`) so the client makes one request per keystroke.
* `/api/route` is the only way clients reach Valhalla. It injects active
  closures as `exclude_polygons`, forces `directions_options.language`, clamps
  `alternates`, applies the rate limit and logs O/D at ~1 km precision.
* Errors use `{"error": {"code": "...", "message": "..."}}` with a 4xx/5xx
  status. `code` is a stable snake_case string; `message` is Spanish.
* Every write endpoint is rate-limited and lands in a moderation queue. Nothing
  a user posts is published without review.

## 5. Geocoder contract (`pipeline/geocode/`)

`relative_address.parse(text) -> RelativeAddress | None` is **pure**: no I/O, no
database. It parses; it does not resolve. Resolution is a separate function that
takes a landmark-lookup callable, so the parser is testable without a stack.

Grammar it must cover (see `tests/test_relative_address.py` for the corpus):

```
[de|del|desde] <landmark> [, <n> <unit> <direction>]* [, <modifier>]*
```

* units: `c`, `cuadra(s)`, `v`, `vrs`, `varas`, `m`, `mts`, `metros`, `km`
* quantities: digits, `½`, `1/2`, and Spanish words via `common.text.parse_spanish_number`
* directions: `norte|al lago`, `sur|a la montaña`, `este|arriba`, `oeste|abajo`
  (+ `hacia`, `al`, intercardinals) — resolved by `common.geo.resolve_direction`
* modifiers kept verbatim for display: `frente a`, `contiguo a`, `esquina
  opuesta`, `casa esquinera`, `mano derecha|izquierda`, `portón …`
* `donde fue`, `donde era`, `antiguo`, `ex` mark `former_landmark=True`, which
  makes the resolver prefer `gazetteer.former = true` rows

`kmpost.geocode(text, ...)` handles `Km 12.5 Carretera a Masaya`, `km 9½
carretera norte`, and returns a point plus, when `mano derecha/izquierda` is
present, the side of the carretera.

Confidence is a real number in `[0, 1]`, and the API returns candidates sorted
by it descending. Never fabricate a high confidence: a landmark matched by trigram
similarity 0.6 must not come back at 0.95.

## 6. Data artifacts (`data/`, all gitignored)

```
data/osm/nicaragua-latest.osm.pbf     nightly Geofabrik download
data/osm/mga48.osm.pbf                circle clip, QA/stats only
data/osm/circle48.geojson             generated by pipeline/geocode or scripts
data/tiles/base.pmtiles               Planetiler, whole country, OpenMapTiles schema
data/tiles/pois.pmtiles               tippecanoe, conflated POIs
data/tiles/buildings.pmtiles          optional, Overture buildings in the circle
data/valhalla/                        graph tiles + valhalla.json (bind-mounted)
data/exports/pois.geojson             conflation output feeding tippecanoe + Meili
data/exports/gazetteer.geojson        landmark export
data/overture/places-*.parquet        monthly Overture pull
data/backups/                         pg_dump output
```

Builds are **atomic**: write `X.pmtiles.tmp`, `fsync`, then `os.replace()` onto
`X.pmtiles`. nginx keeps serving the old file until the rename lands. Never
build in place — a half-written PMTiles is served as corrupt tiles, not as an
error.

## 7. Category taxonomy

`docs/taxonomy.csv` maps external tags to these canonical ids. Columns:

```
category_id,group,label_es,label_en,icon,osm_selectors,overture_categories,min_zoom
```

`osm_selectors` is a `;`-separated list of `key=value` (`amenity=restaurant`),
`overture_categories` a `;`-separated list of Overture category strings.
`min_zoom` is the tippecanoe/style zoom where the icon starts appearing.

Canonical `category_id` values (stable — the style, the sprite sheet and the
search synonyms all key off them):

```
comida:     restaurante fritanga comedor buffet cafetin cafe bar discoteca
            heladeria panaderia reposteria pizzeria comida_rapida
compras:    pulperia supermercado mercado tienda ferreteria distribuidora
            centro_comercial libreria
auto:       gasolinera vulcanizacion taller_mecanico lavado_autos repuestos parqueo
dinero:     banco cajero casa_de_cambio remesas
salud:      hospital clinica farmacia dentista veterinaria
turismo:    hotel hostal playa mirador volcan laguna museo iglesia parque
            sitio_turistico
servicios:  policia bomberos correo embajada universidad escuela gimnasio
            salon_belleza lavanderia hotel_paso
transporte: terminal_buses parada_bus aeropuerto puerto taxi
referencia: rotonda semaforo puente monumento estadio cementerio
otro:       otro
```

## 8. Search index (Meilisearch)

Index `nicanav`, primary key `id` (`"<kind>:<source_id>"`, e.g. `poi:9f2c…`).

```jsonc
{
  "id": "poi:9f2c...", "kind": "poi|street|neighbourhood|place|landmark|alias",
  "name": "...", "alt_names": ["..."], "name_norm": "...",   // accent-folded
  "category": "restaurante", "group": "comida", "subcategory": null,
  "address_text": "...", "city": "Managua",
  "lat": 12.1, "lon": -86.2, "_geo": {"lat": 12.1, "lng": -86.2},
  "popularity": 0.0, "verified": true, "former": false
}
```

* `searchableAttributes`: `["name", "alt_names", "name_norm", "address_text", "category", "city"]`
* `filterableAttributes`: `["kind", "category", "group", "city", "verified", "former", "_geo"]`
* `sortableAttributes`: `["popularity", "_geo"]`
* ranking rules end with `popularity:desc`; typo tolerance stays on (accents).
* Synonyms are Nicaraguan: `gas`↔`gasolinera`↔`bomba`, `cajero`↔`atm`,
  `vulca`↔`vulcanización`, `super`↔`supermercado`, `farmacia`↔`botica`.

## 9. Valhalla usage

Defensive rule: **no Valhalla request field is hardcoded in three places.** The
request builder lives in `api/clients/valhalla.py`; everything else calls it.

* Costing `auto`; `costing_options.auto` carries `use_tracks: 0`, an unpaved
  penalty, and `maneuver_penalty` tuned for the Managua grid.
* `directions_options: {"language": <settings.default_language>, "units": "kilometers"}`.
* Shapes are **encoded polyline precision 6** — decode with
  `common.polyline.decode` (default precision is already 6).
* Closures become `exclude_polygons`: a list of rings, each ring a list of
  `[lon, lat]` pairs.
* Golden-route tests (`pipeline/qa/golden_routes.py`) compare geometry overlap
  ≥ 90 % and duration within ±15 % against `docs/golden_routes.json`.

## 10. Web client

* No bundler, no Node build step. ES modules from a CDN, pinned by exact version.
* `web/js/api.js` is the only module that knows API URLs; screens import it.
* The style is `web/style/nicanav.json`, generated/edited by hand, with
  `["coalesce", ["get", "name:es"], ["get", "name"]]` for every label.
* Runtime config comes from `web/config.js` (generated by `infra` at deploy
  time from the env), never hardcoded hostnames.
* Service worker caches the app shell; PMTiles range requests are cached
  opportunistically. Navigation state must survive a page reload.
* Accessibility and the dashboard reality: 44 px minimum touch targets, high
  contrast night mode, no interaction required to keep guidance running.

## 11. Definition of done for a module

1. `ruff check .` and `ruff format --check .` pass.
2. `pytest` passes with no network and no Docker.
3. Every public function has a docstring saying what it does *and* what it
   assumes about Nicaraguan data.
4. New behaviour has a test; a bug fix has a regression test.
5. Anything that could not be verified against a live service is marked
   `# UNVERIFIED:` with what to check on first deploy.
