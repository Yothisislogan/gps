# nicanav

An open-source map, search and turn-by-turn navigation stack for **Nicaragua** —
self-hosted, no API keys, no Google data. Country-wide coverage, curated in
detail within 48.3 km (30 mi) of Managua's airport: the capital, Masaya,
Granada, Carazo and the Pueblos Blancos, which is where nearly all driving
actually happens.

The point is not to clone Google Maps. It is to be **better than Google in
Nicaragua specifically**, at the four things Google does badly here:

| Google's gap | What this does |
|---|---|
| Cannot understand a Nicaraguan address | A parser for `De la Rotonda El Güegüense, 2c al sur, 1c abajo` and for `Km 12.5 Carretera a Masaya` — and the inverse, generating that sentence from a pin, for the WhatsApp share sheet |
| Thin, stale business listings | Overture (which carries Meta's places data, and Nicaraguan businesses live on Facebook) conflated with OSM and field surveys, with a visible "verificado" date |
| Wrong one-ways, missing retornos, bad neighbourhood routing | Valhalla tuned for local conditions, plus 34 golden routes checked nightly against how a local would actually drive |
| No indication of road surface | Unpaved roads drawn differently, and costing that keeps them usable without preferring them |

Nothing here is finished. See [what works today](#what-works-today).

The [prioritized improvement list](docs/IMPROVEMENTS.md) records the code review,
implemented reliability fixes, regression coverage and remaining launch work.

---

## Quickstart

```bash
git clone <this repo> && cd nicanav
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements/dev.txt
make test          # 930 tests, no network, no database, no Docker
```

To run the stack on a server:

```bash
cp infra/.env.example infra/.env && $EDITOR infra/.env   # fill in the secrets
make up                                                  # compose: nginx, valhalla, meili, postgis, api
make nightly                                             # fetch OSM, build tiles + graph + POIs + index
make verify                                              # the post-deploy checks that catch silent failures
```

`make help` lists everything.

## How it fits together

```
  Geofabrik OSM extract ─┬─► Planetiler ──► base.pmtiles ──┐
                         │                 circle.pmtiles  │   nginx (static)
                         └─► Valhalla graph                ├──► the PWA
  Overture Places ───────┬─► conflation ──► PostGIS ───────┤
  OSM POIs ──────────────┘        │                        │
  field surveys ──────────────────┘        ├─► pois.pmtiles┘
                                           └─► Meilisearch ──┐
                                                             ├──► FastAPI ──► clients
                                            Valhalla ────────┘
```

Everything the browser fetches heavily — tiles, style, fonts, sprites, the app
shell — is a static file. Only search, geocoding, routing and reports touch a
process. That is what keeps this runnable on one small box for a few dollars a
month.

## What works today

| Area | State |
|---|---|
| Nicaraguan relative-address parser and resolver | **Built and tested.** Abbreviations, Spanish numerals, fractions on either side of the unit, multi-hop chains, "donde fue" ghost landmarks, per-city lake orientation |
| Reverse relative addressing (pin → "de la rotonda, 2c al sur") | **Built and tested**, round-trips through the forward parser |
| Km-post geocoder with sign calibration | **Built and tested** |
| POI ingest, conflation, taxonomy, load, export | **Built and tested** against synthetic data |
| Search index (Meilisearch, atomic swap, Nicaraguan synonyms) | **Built and tested** |
| Routing proxy, geocoding cascade, POI cards, submissions | **Built and tested** with fakes |
| PWA: map, search, place cards, directions, navigation, offline tiles | **Built**; route selection, session races, request deadlines and worker caching have executable regression tests. Full rendering and offline restart are **not yet verified**. |
| Map style, sprite sheet | **Built and generated** from the taxonomy |
| QA: golden routes, KPIs, disconnection check | **Built and tested** |
| Compose stack, nginx, nightly pipeline, backups | **Written**; config and admin credential preparation are wired into `make up`. *Not yet run against real services.* |
| Admin moderation UI | **Built and tested** with fakes: review queue, alias approval, closures, POI editor |
| Field data programme (§8 of the plan) | **Not started** — this is the moat and it is driving time, not code |
| Native Android / iOS | **Not started** (plan §6.2) |

Because this was built without network access to any of the external services,
**nothing has been verified against a live Valhalla, Meilisearch, PostGIS,
Overture pull or browser.** Assumptions that could not be checked are marked
`UNVERIFIED:` in the source; `docs/RUNBOOK.md` has the first-deploy checklist
that walks them, and `make verify` automates most of it.

## Layout

| Path | What is in it |
|---|---|
| `common/` | Shared foundation: geodesy and Nicaraguan constants, text normalisation, polyline codec, settings, the DTOs that are the wire format, the Valhalla client |
| `pipeline/` | Batch jobs: fetch, build, conflate, geocode, index, QA — plus the shell scripts that orchestrate them nightly |
| `api/` | FastAPI service: search, geocoding, POI cards, the routing proxy, submissions |
| `web/` | The PWA: no bundler, no build step, plain ES modules |
| `db/migrations/` | PostGIS schema, applied in order |
| `infra/` | Compose stack, nginx, Dockerfiles, Valhalla config, cron |
| `scripts/` | One-shot tools: the curation circle, style and sprite generation, backups, deploy verification |
| `docs/` | The plan, the interface contract, and the curated data: taxonomy, gazetteer, carreteras, golden routes |
| `tests/` | 930 Python tests plus 54 `node --test` cases for navigation, route selection, requests and worker behavior |

## Data and licences

This project is Apache-2.0. **The data is not**, and the distinction matters:

- **OpenStreetMap** data is ODbL. "© OpenStreetMap contributors" must appear in
  the UI — it does, in the app shell and in the map style.
- **Overture Places** carries per-source licences (CDLA-Permissive-2.0 for
  Meta/Microsoft, Apache-2.0 for Foursquare, CC0 for AllThePlaces). Each source
  row keeps its own licence in `poi_source.license`, deliberately, so the
  published dataset can be attributed rather than laundered into one blob.
- **Mapillary/Panoramax** imagery is CC BY-SA and is credited on the card.
- **Google data is never ingested, cached or traced from.** Not Places, not
  imagery, not in the app and not in an OSM editor. This is a hard rule.

See [docs/LICENSES.md](docs/LICENSES.md).

## Contributing

Code: [CONTRIBUTING.md](CONTRIBUTING.md).

Data is more valuable than code here. The routing bugs in Managua are wrong
one-ways, unmapped retornos and rotondas with the wrong flow, and they are fixed
in OpenStreetMap with StreetComplete, Every Door or JOSM — upstream, where
everyone gets them, not in a private table here.

## Further reading

- [docs/UX-BRIEF.md](docs/UX-BRIEF.md) — UX priorities, implementation phases and acceptance criteria
- [docs/PLAN.md](docs/PLAN.md) — the whole plan and the reasoning behind every choice
- [docs/SPEC.md](docs/SPEC.md) — the normative interface contract
- [docs/RUNBOOK.md](docs/RUNBOOK.md) — operating it, and the first-deploy checklist
- [docs/LICENSES.md](docs/LICENSES.md) — attribution and the rules

## Licence

Apache-2.0 — see [LICENSE](LICENSE).
