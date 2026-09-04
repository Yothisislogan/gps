# Nicaragua Open Navigation — Build Plan

Working name: **nicanav** (rename at will).
Goal: an open-source, self-hosted, Google-Maps-class driving map + turn-by-turn navigation for Nicaragua, curated to street-and-business-level detail within **30 mi (48 km) of MGA** (Augusto C. Sandino Intl — 12.1415, −86.1682).

Written for a static-first, Python, Docker-on-a-Hetzner-box stack, with no Node toolchain and secrets outside the repo.

---

## 0. Decisions at a glance

| Layer | Choice | Why |
|---|---|---|
| Road / base data | OpenStreetMap — Geofabrik Nicaragua extract (~60 MB), refreshed nightly | Best free road graph for NI. MapaNica (the NI OSM community) mapped Managua hard, incl. all 42 bus routes. Every fix you make goes upstream and flows back into your stack in <24 h. |
| Vector tiles | Planetiler → **PMTiles** (OpenMapTiles schema) served as a static file by nginx | Static-first. Whole country builds in minutes. No tile server process to run. |
| Renderer | MapLibre GL JS (web) → MapLibre Native (Android later) | FOSS, no API keys, PMTiles + offline caching. |
| Routing | **Valhalla** (Docker) | Spanish turn-by-turn text, roundabout exits, map-matching, isochrones, `exclude_polygons` for closures, and OSRM-format output that nav SDKs consume. |
| Nav engine | **Ferrostar** (Rust core; web components now, Kotlin/Swift later) — or a ~300-line custom loop | FOSS, designed for Valhalla; handles snapping, off-route detection, reroute, voice + banner timing. |
| POIs / restaurants | **Overture Places** (Meta + Foursquare + Microsoft + AllThePlaces) ⊕ OSM POIs ⊕ field surveys → conflated in PostGIS → published nightly as a static `pois.pmtiles` layer | Nicaraguan businesses live on Facebook; Overture carries Meta's places data. Foursquare's open places are bundled in Overture too. |
| Search / geocoding | **Meilisearch** index (POIs, streets, barrios, landmarks) + a custom **Nicaraguan relative-address parser** ("de la Rotonda X, 2c al sur, 1c arriba") + a **km-post geocoder** ("Km 12.5 Carretera a Masaya") | Off-the-shelf geocoders can't parse NI addresses. This is the actual gap Google has. |
| Backend | **FastAPI** (Python) | `/search`, `/geocode`, `/poi/{id}`, `/route` (Valhalla proxy that injects closures), `/report`, `/admin` |
| Hosting | Existing Hetzner box, Docker Compose; PMTiles on nginx (or Cloudflare R2 free tier) | ≈ $0–10/mo incremental |
| Client v1 | **PWA** (Android Chrome: installable, Wake Lock, Web Speech TTS, service-worker offline tiles) | Ships without a store, no Node, drivable in the car by ~week 8 |
| Client v2 | Kotlin + MapLibre Native + Ferrostar Android (Gradle only, no Node); iOS via Ferrostar Swift later | Background GPS, on-device routing, store distribution |

Core principle: **build the whole country, curate the circle.** Nicaragua is small enough that tiles and the routing graph should always cover the entire country (routes to León, San Juan del Sur, Ometepe still work). The 30-mile radius is where field-verification effort is spent, not a hard boundary.

---

## 1. Scope: what the 48 km circle actually contains

Straight-line distances from MGA:

| Place | Distance | Notes |
|---|---|---|
| Managua (all districts), Ciudad Sandino, Tipitapa | 0–15 km | Metro core; densest POI + one-way/turn-restriction work |
| Masaya, Nindirí, Masaya Volcano NP | ~20 km | Market town + biggest tourist draw near the capital |
| Ticuantepe, La Concepción, El Crucero | 15–25 km | Carretera a Masaya / Carretera Sur corridors |
| Catarina, San Juan de Oriente, Niquinohomo, Masatepe, Laguna de Apoyo | 25–30 km | Pueblos Blancos loop; lots of restaurants/miradores |
| Granada + Lake Nicaragua shore | ~33 km | #1 tourist city; colonial grid, many one-ways |
| Diriamba, Jinotepe, San Marcos (Carazo) | ~32 km | Regional towns |
| Nagarote, Mateare, Xiloá | 25–45 km | West corridor toward León |
| Nandaime | ~45 km | Southern edge |
| León | ~83 km | **Outside** — covered by data, not by curation |
| Montelimar / Pochomil beaches | ~55–60 km | Just outside; worth a field pass anyway (tourists drive it) |

So the circle covers the Pacific core: capital, Masaya, Granada, Carazo — i.e., where nearly all visitors and most drivers actually go.

---

## 2. Why this can beat Google here (and where it can't)

| Google's gap | What nicanav does |
|---|---|
| Thin, stale business listings; many businesses only exist on Facebook | Overture Places (Meta-sourced) + Foursquare open places + OSM + on-the-ground verification with a visible "verified <date>" badge |
| Cannot understand real addresses ("del Colonial 2c al lago, 1c arriba", "Km 9½ Carretera a Masaya") | Purpose-built relative-address geocoder and km-post geocoder, with a confirm-the-pin UI that learns aliases |
| Bad routing on neighborhood streets: wrong one-ways, missing turn restrictions, unmapped retornos | Field-verify one-ways/turn restrictions on every primary/secondary in Managua + Granada; Mapillary/Panoramax dashcam passes; golden-route regression tests |
| No Street View in Nicaragua | Mapillary/Panoramax street-level imagery captured in the field, shown on POI cards |
| Generic ETAs | Nicaragua-tuned speed defaults per road class/surface; later, speed profiles from probe traces |

Where Google will still win for a while: live traffic, global coverage, and polish. Don't chase those in v1.

---

## 3. Architecture

```
                 ┌────────────────── nightly pipeline (Python + cron) ──────────────────┐
                 │  Geofabrik NI .pbf ──► osmium (clip for QA) ──► Planetiler ──► base.pmtiles │
                 │                     └──► Valhalla graph build ──► valhalla_tiles/          │
                 │  Overture Places (monthly) + OSM POIs + field CSVs ──► PostGIS conflate    │
                 │       └──► tippecanoe ──► pois.pmtiles   └──► Meilisearch reindex          │
                 └────────────────────────────────────────────────────────────────────────┘
                                                  │ atomic swap
   ┌──────────── Hetzner box (Docker Compose) ────┴───────────────────────────────┐
   │  nginx (static): web app, base.pmtiles, pois.pmtiles, sprites, fonts, DEM     │
   │  valhalla:8002  (route / locate / trace_attributes / isochrone)               │
   │  meilisearch:7700                                                             │
   │  postgis:5432   (poi, poi_source, closure, report, alias, gazetteer)          │
   │  api (FastAPI): /search /geocode /reverse /poi /route /report /admin          │
   └──────────────────────────────────────────────────────────────────────────────┘
                                    ▲ HTTPS
   ┌──────────────── clients ───────┴──────────────────────────────────────────────┐
   │  v1: PWA — MapLibre GL JS + pmtiles.js + Ferrostar web (or custom nav loop)   │
   │  v2: Android (Kotlin, MapLibre Native, Ferrostar) → iOS (Swift, Ferrostar)    │
   └──────────────────────────────────────────────────────────────────────────────┘
   Data improvement loop: field drives → StreetComplete / Every Door / JOSM / Rapid → OSM → back in the nightly build
```

Everything the browser fetches heavily (tiles, style, fonts, POI layer) is a static file. Only search, geocoding, routing, and reports are dynamic.

---

## 4. Data layer

### 4.1 Roads and base map (OSM)

- Source: `https://download.geofabrik.de/central-america/nicaragua-latest.osm.pbf` (~60 MB; daily updates, ODbL).
- Clip a QA extract of the circle with `osmium extract -p circle48.geojson` (used for stats and validation — the tiles and router use the whole country).
- Tiles: Planetiler (Java jar, no Node) with the OpenMapTiles schema, `--languages=es,en`, output `.pmtiles`. Vector tiles stop at z14 and MapLibre overzooms to z22, so building-level detail is already in there.
- Buildings: OSM building coverage in Managua is patchy. Add an Overture Buildings extract (includes Google Open Buildings + Microsoft footprints) as a separate `buildings.pmtiles` layer for the circle. It makes the map look like Google's at high zoom.
- Terrain (polish, later): Copernicus DEM 30 m → terrain-RGB raster PMTiles → MapLibre hillshade. Nicaragua's volcanoes deserve it.
- Satellite (optional, license-dependent): EOX Sentinel-2 cloudless is CC BY-NC-SA (non-commercial only); Esri World Imagery has its own ToS. Don't use Google imagery. Skip in v1.

### 4.2 Points of interest and restaurants

Sources (all free, redistributable):

| Source | What it gives you | License | Refresh |
|---|---|---|---|
| Overture Places | Meta-sourced business listings (huge for NI), Foursquare open places, Microsoft, AllThePlaces (brand scrapers). Fields: names, categories, confidence, websites, socials (Facebook/Instagram links), phones, addresses, brand, operating status; stable GERS IDs | CDLA-Permissive-2.0 / Apache-2.0 / CC0 by source | Monthly release |
| OSM POIs | amenity/shop/tourism/leisure nodes + `opening_hours`, `cuisine`, `phone`, `website`; excellent for landmarks, rotondas, markets, gas stations, ATMs | ODbL | Nightly |
| Field surveys | Every Door / StreetComplete on foot; dashcam passes; owner-submitted data via the app | Yours (push to OSM where appropriate) | Continuous |
| Wikidata / Wikimedia Commons | Landmarks, volcanoes, lagoons, churches — descriptions + photos | CC0 / CC BY-SA | Occasional |
| Mapillary / Panoramax | Street-level photos near each POI | CC BY-SA | Continuous |

Pull Overture by bounding box (DuckDB or the `overturemaps` CLI), e.g. bbox `-86.62,11.70,-85.72,12.58` for the circle plus margin, `--type=place`; pull Nicaragua-wide at lower priority.

PostGIS schema (core tables): see [`db/migrations/001_init.sql`](../db/migrations/001_init.sql) for the implemented version.

```
poi(id uuid, name text, name_alt text[], category text, subcategory text, cuisine text[],
    geom geometry(Point,4326), address_text text, phone text, website text,
    facebook text, instagram text, whatsapp text, opening_hours text,  -- OSM opening_hours syntax
    price_level smallint, status text,            -- open | closed | unverified
    confidence real, verified_at timestamptz, verified_by text,
    sources jsonb,                                 -- {osm_id, overture_gers_id, fsq_id, survey_id}
    created_at, updated_at)
poi_source(poi_id, source, source_id, raw jsonb)   -- provenance, keeps licenses separable
poi_photo(id, poi_id, url, credit, license, taken_at)
poi_flag(id, poi_id, kind, note, created_at)       -- "cerrado", "se movió", "horario mal"
closure(id, geom polygon, reason, starts, ends, active bool, source)   -- feeds exclude_polygons
report(id, geom, kind, note, photo, created_at)    -- pothole, flooding, one-way wrong
alias(id, text, geom, confidence, created_by)      -- learned address strings
gazetteer(id, name, name_alt text[], kind, geom, former bool, era text)  -- landmarks incl. "donde fue"
```

Conflation (Python: GeoPandas/DuckDB + `rapidfuzz`, or pure SQL with `pg_trgm` + `unaccent`):
1. Normalize names: lowercase, strip accents, drop generic prefixes ("restaurante", "bar y restaurante", "comedor", "fritanga", "cafetín", "pulpería").
2. Block candidates within 75 m (H3 res-9 cells or `ST_DWithin`).
3. Score = name similarity (Jaro-Winkler) + distance decay + category agreement.
4. Auto-merge ≥ 0.85, queue 0.6–0.85 for review in the admin UI, keep the rest separate.
5. Field surveys always outrank Overture, Overture outranks OSM for phone/socials/hours, OSM outranks Overture for position (OSM points are hand-placed).
6. Keep Overture's GERS ID on each row so the monthly refresh is a join, not a re-conflation.

Taxonomy — map Overture categories and OSM tags to ~40 app categories with icons. Nicaragua-specific ones to add on top of the usual: fritanga, comedor, buffet, cafetín, pulpería, gasolinera (Uno/Puma), banco/cajero (BAC, Lafise, Banpro, Ficohsa), casa de cambio, farmacia, ferretería, taller mecánico, **vulcanización** (tire repair — drivers need this), lavado de autos, hotel/hostal, playa, mirador, volcán, laguna, mercado (Huembes, Oriental, Israel Lewites, Mayoreo), rotonda (as a landmark class), universidad, hospital/clínica, embajada, terminal de buses, distribuidora.

Publishing: nightly export → GeoJSON → tippecanoe → `pois.pmtiles` with zoom-dependent density (`-zg --drop-densest-as-needed`, but pin top categories to appear from z13). Icons by category as a MapLibre sprite sheet. "Abierto ahora" computed client-side from `opening_hours` (opening_hours.js via CDN) — no server call.

Restaurant card contents: name, category/cuisine, open-now + hours, phone (tap-to-call), WhatsApp deep link (`wa.me/505…`), Facebook/Instagram links, street photo (Mapillary), "verificado" badge with date, relative-address string, directions button, flag-a-problem button. Skip reviews in v1 — link to the Facebook page instead; add lightweight thumbs/notes later with moderation.

### 4.3 Addresses — the Nicaraguan geocoder (biggest differentiator)

Nicaraguan addresses are landmark-relative. Real example from MapaNica's own event notice: *"Busto José Martí, 30 metros hacia el este (arriba)"*.

**Relative-address parser** (`pipeline/geocode/relative_address.py`):

- Grammar: `<landmark>[, <n> <unit> <direction>]* [, modifiers]`
- Units: `c`, `cuadra(s)` (≈100 m in Managua/Granada grids; make it a per-city constant), `vrs`/`varas` (1 vara ≈ 0.836 m), `m`/`mts`/`metros`, `km`. Numbers include words and fractions: "media", "1/2", "una", "dos", "cuadra y media".
- Directions (Managua slang): norte / **al lago**; sur / **a la montaña**; este / **arriba**; oeste / **abajo**; plus "hacia", "al".
- Modifiers: "frente a", "contiguo a", "esquina opuesta", "casa esquinera", "mano derecha/izquierda", "portón negro" (ignore, but keep in the display string), and **"donde fue"** / "antiguo" (former landmark — hits the `former=true` gazetteer).
- Algorithm: resolve landmark via Meilisearch with a boost on `gazetteer` docs → apply the offset vectors in order → snap to the road network with Valhalla `/locate` → return 1–3 candidates with confidence.
- UI: drop the pin, ask "¿Es aquí?", let the user drag to correct, save the corrected string→point as an `alias` so the system learns. Over time this becomes the best address database in the country.

**Km-post geocoder**: "Km 12.5 Carretera a Masaya". Resolve the carretera name to its OSM relation/ways (Carretera a Masaya, Carretera Norte, Carretera Sur, Carretera Nueva a León, Carretera Vieja a León, Carretera a Tipitapa, Carretera Masaya–Granada…), measure along the geometry from Nicaragua's Km 0 in old downtown Managua, and **calibrate against real km-post signs** photographed in the field (map them to OSM as `highway=milestone` + `distance=*`). Return the point plus which side of the road when "mano derecha/izquierda" is present.

**Gazetteer**: the landmark list is the fuel for both. Sources: OSM named features (rotondas, semáforos with names, colegios, iglesias, gasolineras, mercados, embassies, "ex-" landmarks), place=neighbourhood/suburb (barrios, colonias, residenciales, repartos — MapaNica mapped a lot of these), plus a curated CSV of ghost landmarks still used in speech ("donde fue el Cine Cabrera", "de la antigua Pepsi", "del Arbolito", "donde fue el Banco Popular"). Crowdsource this list in the app: "¿Qué punto de referencia usás?"

**Reverse relative geocoding** (nobody has this): given a pin, generate "De la Rotonda El Güegüense, 2c al sur, 1c abajo" using the nearest well-known gazetteer landmark and block counting along the grid. That is how Nicaraguans share locations, so it belongs on the share sheet next to the coordinates.

### 4.4 Search index (Meilisearch)

One index, documents shaped like:
```
{id, kind: poi|street|neighbourhood|place|landmark|alias, name, alt_names[], category,
 address_text, city, lat, lon, popularity, verified}
```
- Searchable: name, alt_names, address_text, category synonyms ("gas", "gasolinera", "combustible").
- Ranking: typo tolerance (handles missing accents), `_geoRadius`/`_geoPoint` sort so "farmacia" returns nearby first, boost `verified` and `popularity`.
- Nicaragua-wide, the index is <300k docs — trivial on the Hetzner box.
- Static-first alternative if you'd rather not run a service: SQLite FTS5 file shipped to the client and queried with sql.js from a CDN. Fine for POIs; keep Meilisearch for the relative-address flow.

### 4.5 Update cadence

| Data | Cadence | Mechanism |
|---|---|---|
| OSM roads/base tiles/Valhalla graph | Nightly | cron: download → build → verify (route count sanity + golden routes) → atomic swap |
| Overture Places | Monthly | fetch release by bbox → join on GERS → re-run conflation only for new/changed IDs |
| Field CSVs / app submissions | Immediate to DB, nightly to tiles/index | admin approve → export |
| Closures/reports | Immediate | DB → included in every `/route` request as `exclude_polygons` |

---

## 5. Services on the Hetzner box

`infra/docker-compose.yml` services:

- **nginx** — serves `/web` (static app), `/tiles/*.pmtiles` (must allow HTTP range requests; gzip off for pmtiles, CORS on), sprites, fonts (self-hosted OpenMapTiles fonts), and reverse-proxies `/api` and `/valhalla`.
- **valhalla** — the community `docker-valhalla` image (originally gis-ops; check the current README for the image path). Mount `custom_files/` with the Nicaragua PBF; set `build_admins=True`, `build_time_zones=True` (correct Spanish instructions + local time), `build_elevation` optional, `serve_tiles=True`. RAM for Nicaragua: well under 1 GB.
- **meilisearch** — single binary; master key in `.env` (never in repo).
- **postgis** — `postgis/postgis` image; nightly `pg_dump` to Hetzner Object Storage.
- **api** — FastAPI + uvicorn. Endpoints:
  - `GET /search?q=&lat=&lon=` → Meilisearch passthrough + relative-address parse if the query matches the grammar
  - `GET /geocode?q=` → candidates with confidence
  - `GET /reverse?lat=&lon=` → nearest street + relative-address string
  - `GET /poi/{id}` → full card (DB) + Mapillary image lookup (cached)
  - `POST /route` → adds active closures as `exclude_polygons`, sets `directions_options.language` (`es-ES` or `en-US`), `format: "osrm"` when the nav client asks, rate-limits, logs O/D at low precision for later speed-profile work
  - `POST /report`, `POST /poi/suggest`, `POST /alias` → moderation queue
  - `/admin` → simple HTMX/Jinja pages: review queue, closures on a map, POI edit, stats. Basic auth behind nginx.

Sizing: a CX-class box handles all of this. Valhalla + Meilisearch + PostGIS + API for a Nicaragua-sized dataset is < 2 GB RAM total.

---

## 6. Client

### 6.1 v1 — PWA (Android-first)

Stack: plain HTML/JS modules loaded from a CDN (MapLibre GL JS, pmtiles.js, opening_hours.js, Turf for geometry, Ferrostar web components) — no bundler. Static files under `web/`.

| Google Maps feature | Implementation |
|---|---|
| Map with POIs and labels | MapLibre style: OSM Bright-derived, Spanish labels (`coalesce(name:es, name)`), POI layer from `pois.pmtiles` with category icons, buildings at z15+ |
| Search bar with suggestions | Meilisearch as-you-type (debounced 150 ms), recent searches in localStorage, category chips (Comida, Gasolina, Cajero, Farmacia, Hotel, Vulcanización…) |
| Place card | Bottom sheet: hours/open-now, call, WhatsApp, Facebook, photo, directions, share |
| Directions | Valhalla route; alternates (`alternates: 2`); summary (km, min); turn list; drag to change |
| Turn-by-turn navigation | Ferrostar web `<ferrostar-map>` (or custom loop, below); banner with next maneuver + distance, lane hints where OSM has `turn:lanes`, ETA, speed, speed limit from Valhalla edge attributes, night mode |
| Voice guidance | Web Speech `speechSynthesis`, Spanish voice (`es-US`/`es-MX` on Android Google TTS; fallback `es-ES`), English toggle |
| Rerouting | Off-route detection → new `/route` from current position with `heading` |
| Search along route | PostGIS `ST_DWithin(poi.geom, route_line, 300 m)` via `/search?route_id=` |
| Offline maps | Service worker pre-caches app shell + style + fonts, and range-fetches the circle's tiles from `base.pmtiles`/`pois.pmtiles` (~100–200 MB for z0–14 over the circle) into Cache Storage. Routing stays online in v1. |
| Share location | Deep link `https://<host>/@12.1415,-86.1682,16z` + the generated relative address; WhatsApp share button (Nicaragua runs on WhatsApp) |
| Report a problem | One-tap: "vía cerrada", "bache", "sentido incorrecto", "negocio cerrado" → `/report` with position and optional photo |

Custom nav loop (if you skip Ferrostar on web; ~300 lines):
1. `navigator.geolocation.watchPosition({enableHighAccuracy:true})` + `navigator.wakeLock.request('screen')`.
2. Snap to the route polyline (`turf.nearestPointOnLine`); off-route if > 40 m for 3 consecutive fixes at > 5 km/h.
3. Instruction timing from Valhalla's OSRM-format `voiceInstructions`/`bannerInstructions` (`distanceAlongGeometry` triggers).
4. Reroute: call `/route` with current position + heading; swap polyline; announce "Recalculando".
5. Arrival: within 30 m of destination and speed < 5 km/h.

PWA limits to accept: screen must stay on (true on a dash mount anyway); iOS Safari PWAs have weaker GPS/audio behavior; no background routing. When those bite, go to 6.2.

### 6.2 v2 — Native Android (then iOS)

- Kotlin + MapLibre Native + Ferrostar Android (Gradle only; build in Crostini or on a free GitHub Actions runner — no Node anywhere). Ferrostar has a Rust core with Kotlin, Swift, React Native and web bindings.
- Benefits: background GPS, proper audio focus (ducks music for voice prompts), Android Auto later, on-device routing (bundle Valhalla for Nicaragua — the graph is tiny) for true offline nav.
- iOS: Ferrostar Swift package, same backend. Needs a Mac (or a macOS CI runner) + $99/yr developer account. Do it after Android proves the product.
- Distribution: sideload APK first; Play Store is a one-time $25.

---

## 7. Routing tuned for Nicaragua (Valhalla)

- **Default speeds**: Valhalla accepts a `default_speeds` config with per-country overrides by road class and urban/rural density. Set realistic Nicaraguan values: Managua urban primaries ~30–40 km/h effective, Carretera a Masaya / Norte 60–80, tertiary 40, unclassified paved 35, unpaved 20–25, tracks 15. Iterate against the golden routes.
- **Surface awareness**: honor `surface=*`. Expose a user toggle "Evitar caminos de tierra" → `exclude_unpaved: true`; default off (many places are only reachable by dirt roads) but with a strong unpaved penalty so a dirt shortcut never beats a paved route.
- **Tracks**: `use_tracks: 0` for auto costing (OSM `highway=track` in NI is often a farm path).
- **Roundabouts**: Managua's rotondas (Centroamérica, Rubén Darío, Metrocentro, Jean Paul Genie, El Güegüense, Bello Horizonte, Cristo Rey, Santo Domingo, La Virgen, Universitaria…) must be tagged `junction=roundabout` with correct flow so Valhalla says "toma la segunda salida". Field-verify every one in the circle.
- **Retornos**: dual carriageways (Carretera a Masaya, Pista Juan Pablo II, Carretera Norte) need the U-turn bays mapped as short connecting ways with correct one-way, or the router will send people miles past. High priority in the field program.
- **Turn restrictions**: `restriction=*` relations at every major intersection; verify from dashcam imagery.
- **Speed bumps**: `traffic_calming=*` tags everywhere they exist ("reductores"); mild time penalty, and show them on the map — locals will love it.
- **Rainy-season closures** (May–Nov cauces flood): user-reported closures → `closure` table → `exclude_polygons` on every request while active; auto-expire.
- **Costing knobs per request**: `use_highways` (irrelevant in NI, leave default), `use_tolls` (no tolls), `maneuver_penalty` slightly higher than default to prefer fewer turns in the city grid, `alternates: 2`.
- **Airport specifics**: verify the MGA terminal loop, arrivals/departures levels, parking entrances, and the Carretera Norte access ramps — first impression for every arriving user.
- **Instruction language**: `directions_options.language: "es-ES"`; Valhalla's Spanish phrases are decent; override a few for NI usage in the client ("rotonda" not "glorieta", "retorno").
- **Map matching**: use `trace_attributes` on uploaded GPS traces (opt-in) to catch missing roads and wrong one-ways automatically; later, to derive time-of-day speeds and feed Valhalla's predicted-traffic tiles.

---

## 8. Field data program (this is the moat)

Kit (~$150–650): dedicated Android phone ($100–150 used), windshield mount, car charger; optional 360° cam (Insta360 X4 / GoPro Max, ~$400–500) for Panoramax/Mapillary-quality coverage.

Apps / tools:
- **Mapillary** app for dashcam capture (auto-capture while driving; imagery CC BY-SA; you may derive OSM edits from it), or **Panoramax** (fully open alternative).
- **StreetComplete** (Android): answers tag "quests" — surface, one-way, maxspeed, lanes, opening hours, sidewalks — perfect for passengers.
- **Every Door**: fast POI surveys on foot (names, hours, phones) — Granada centro, Masaya market district, Managua's main restaurant zones (Zona Rosa/Carretera a Masaya corridor, Bolonia, Los Robles, Altamira, Villa Fontana, Las Colinas).
- **Vespucci** (Android) / **JOSM** (Java, runs in Crostini) with the Mapillary plugin for turn restrictions, retornos, roundabouts.
- **Rapid** editor (browser): check whether Meta's AI-detected roads and Microsoft/Google building footprints are available for Nicaragua; validate-and-accept beats tracing.
- QA: **Osmose**, **OSM Inspector** (routing view: unconnected ways, islands), JOSM validator, plus a custom script: isochrones from MGA → any named road not reached in 3 h is probably disconnected.

Drive plan (priority order):
1. MGA ↔ city: Carretera Norte, Pista Juan Pablo II, Pista Suburbana — every retorno, ramp, one-way.
2. Carretera a Masaya end to end (Managua → Masaya → Granada), including every rotonda and retorno.
3. Managua district grids: Bolonia, Altamira, Los Robles, Las Colinas, Villa Fontana, Linda Vista, Bello Horizonte, Ciudad Jardín, Centroamérica, Rubenia, Villa Venezuela; then Ciudad Sandino and Tipitapa.
4. Granada centro (one-way grid), Masaya centro, Pueblos Blancos loop, Laguna de Apoyo access roads, Masaya Volcano park road.
5. Carretera Sur to El Crucero, Ticuantepe/La Concha, Carazo towns, Nagarote/Mateare.
6. Bonus: Pochomil/Masachapa coast road.

What to verify on each pass: one-way direction, turn restrictions, roundabout flow, surface, speed bumps, gas stations/ATMs/vulcanizaciones, km posts (photograph them), and that the router's suggested route matches how a local would actually drive it.

**Golden routes**: 50 origin/destination pairs with a local driver's preferred route recorded as GPX. Nightly test compares Valhalla's route geometry overlap (target ≥ 90%) and time within ±15%. This is the regression suite for both costing changes and OSM edits.

KPIs (computed nightly from the circle extract; shown on `/admin`):
- km of roads with `surface` tag / total km
- % of primary+secondary+tertiary with `oneway` verified (use a `check_date` tag or a local table)
- rotondas with correct `junction=roundabout` (count)
- POIs by category; % verified in last 12 months
- search success rate (query → tap on a result) from logs
- reroutes per navigated trip

---

## 9. Licensing and attribution (not legal advice — but the known rules)

- **OSM (ODbL)**: attribute "© OpenStreetMap contributors" on the map; share-alike applies to *derivative databases*. Keep POI sources in separate tables with provenance and treat the app as a *collective* database (Overture layer beside OSM layer) rather than blending everything into one derived dataset. Overture's own docs note that joining CDLA data to OSM may put the result under ODbL if it's a derivative database. Simplest ethical answer: publish the conflated POI dataset openly anyway (ODbL), and push road fixes straight to OSM.
- **Overture**: CDLA-Permissive-2.0 (Meta/Microsoft), Apache-2.0 (Foursquare), CC0 (AllThePlaces) — keep the license notices with the data.
- **Mapillary / Panoramax imagery**: CC BY-SA; attribution on the photo; deriving OSM edits from it is permitted.
- **Never** ingest Google Places or Google imagery — Google's ToS forbids showing their data on non-Google maps and caching it.
- Tracing sources allowed in OSM editors: Bing, Esri World Imagery (Clarity), Maxar (when enabled), Mapillary. Google imagery is not allowed.
- Driver safety: hands-free disclaimer on first nav start.

---

## 10. Costs

| Item | One-time | Monthly |
|---|---|---|
| Hetzner box (already running; this stack adds < 2 GB RAM) | — | $0–8 |
| PMTiles hosting (nginx on the box, or Cloudflare R2 free tier) | — | $0–2 |
| Domain (existing) | — | $0 |
| Overture / OSM / Mapillary / Foursquare open data | $0 | $0 |
| Dedicated Android phone + mount | $150 | — |
| Optional 360° camera | $400–500 | — |
| Play Store developer account | $25 | — |
| Apple developer (v2 iOS only) | — | $99/yr |
| Hetzner Object Storage for backups + photos | — | $1–5 |

Realistic run-rate: **≈ $5–15/month**. The expensive part is driving time.

---

## 11. Phased roadmap with acceptance criteria

**Phase 0 — Proof (one weekend)**
Local Docker: clip circle, Planetiler → PMTiles, Valhalla up, MapLibre page showing the map + a route MGA → Granada with Spanish instructions.
Accept: 10 known routes look right, or exactly why not is written down.

**Phase 1 — Deployed map + routing (weeks 1–3)**
Compose stack on Hetzner; nightly pipeline with atomic swap; static web app: map, Meilisearch search (streets, barrios, OSM POIs), route + turn list, share links, `/route` proxy.
Accept: `mapa.<yourdomain>` loads in < 2 s on a mid-range Android on Claro 4G; nightly build survives a week unattended.

**Phase 2 — POIs and restaurants (weeks 3–6)**
Overture + OSM ingest, conflation, taxonomy, icons, `pois.pmtiles`, place cards (hours/open-now, call, WhatsApp, Facebook, photo), admin review queue, "sugerir un lugar", monthly Overture refresh job.
Accept: ≥ 3,000 POIs in the circle; every restaurant on Carretera a Masaya between Metrocentro and Km 14 is present with a phone or social link; open-now correct for 20 spot checks.

**Phase 3 — Turn-by-turn navigation (weeks 6–10)**
Ferrostar web (or custom loop), voice es/en, rerouting, ETA/speed/speed-limit, night mode, wake lock, offline tile cache for the circle, PWA install flow, report-a-problem.
Accept: 20 real drives in the circle without a missed or wrong maneuver on primary/secondary roads; reroute within 5 s of leaving the route.

**Phase 4 — Nicaraguan addressing (weeks 10–14)**
Relative-address parser, km-post geocoder, gazetteer (incl. "donde fue" list), confirm-the-pin UI with alias learning, reverse relative geocoding on the share sheet.
Accept: 80% of 100 real addresses collected from friends/businesses resolve within 150 m on first try; the rest resolve after one drag-correct.

**Phase 5 — Ongoing data program + native**
Drive plan §8 with Mapillary + StreetComplete + JOSM; golden routes CI; KPI dashboard; user closures → `exclude_polygons`; Kotlin/Ferrostar Android app when PWA limits bite; on-device Valhalla for offline routing; iOS; transit layer from MapaNica's GTFS; probe-derived speed profiles → time-of-day ETAs.

---

## 12. Repo layout

See [SPEC.md](SPEC.md) §1 for the implemented layout and the interface contract.

---

## 13. Risks and mitigations

| Risk | Mitigation |
|---|---|
| OSM thin outside Managua core (Carazo towns, rural roads) | Field program + Rapid AI roads; publish KPIs so gaps are visible |
| POI churn — restaurants close constantly | "verificado" dates, user flags, monthly Overture refresh via GERS join, Facebook link as ground truth |
| Address parsing ambiguity (three "Rotonda…" matches, ghost landmarks) | Candidates + confirm UI + alias learning; grow the gazetteer from users |
| ODbL share-alike confusion | Separate tables/layers with provenance; attribute everything; default to releasing the POI dataset openly |
| PWA GPS/audio limits, iOS Safari | Android-first; move to Kotlin/Ferrostar when it bites |
| Phone heat/battery on the dash in NI sun | Nav mode with dark, low-refresh UI; vent mount; keep-screen-on only during nav |
| Abuse of public endpoints | nginx rate limits, API keys for the app build, moderation queue for all submissions |
| Solo-maintainer burnout | Nightly builds are unattended; golden routes catch regressions; MapaNica community for road edits |

---

## 14. Other ideas worth folding in

- **Zero-app MVP for existing users**: export the curated POI layer as GPX/KML (and an OsmAnd `.obf`) so people already using OsmAnd/CoMaps/Organic Maps get the restaurants immediately. Also a good marketing hook.
- **WhatsApp-native workflows**: paste a WhatsApp location pin → get a relative address and directions; "Enviar ubicación" from any place card.
- **Business claim flow**: owners verify their listing via WhatsApp OTP and edit hours/menu/photos. This is how restaurants keep their own data fresh.
- **Tourism distribution**: Granada/Managua hotels, tour operators, car-rental desks at MGA hand out the install link; a "Llegando a MGA" onboarding card (SIM, ATMs, taxis, the airport loop) is a natural first screen.
- **Transit layer**: MapaNica already produced GTFS from OSM for Managua's buses; a bus-route overlay and basic transit directions (Valhalla multimodal) are cheap wins.
- **Waze-lite reports**: accidents, floods, police checkpoints ("retén") — moderated, time-limited, and fed into `exclude_polygons`/ETAs.
- **Open data give-back**: publish the POI dataset and address gazetteer openly. It attracts contributors, keeps the project honest on licensing, and nobody else in Nicaragua has it.
- **Predicted traffic from probes**: once there are a few hundred trips/day, derive time-of-day speeds per edge and load them into Valhalla's predicted-traffic tiles. This is the path to Google-like ETAs without a traffic feed.
