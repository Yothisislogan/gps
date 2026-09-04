# nicanav runbook

Operating the stack on one box. Written for the person who is on call, which for
a while is the person who wrote it.

The governing rule everywhere below: **stale beats wrong.** A day-old map is a
minor annoyance; a half-built one sends drivers into a wall. Every job publishes
atomically and every failure path leaves yesterday's data serving.

---

## 1. First deploy

```bash
cp infra/.env.example infra/.env
$EDITOR infra/.env              # generate secrets: openssl rand -base64 32
make up                         # nginx, valhalla, meilisearch, postgis, api
make nightly                    # ~30-60 min the first time: Planetiler stages ~1 GB of sources
make verify                     # the checks that catch silent failures
```

Then work the **first-deploy checklist** in §7. It exists because this codebase
was written without network access to any of the external services, so a set of
assumptions has never met reality. They are marked in the source:

```bash
grep -rn "UNVERIFIED" --include="*.py" --include="*.js" --include="*.sh" .
```

Do not skip this. Every marker is a place where the code will run happily and
produce something subtly wrong.

## 2. What runs, and when

| Job | When | Produces |
|---|---|---|
| `pipeline/nightly.sh` | 02:15 daily | Fresh OSM → tiles, routing graph, POIs, gazetteer, search index, then QA |
| `pipeline/qa/golden_routes.py` | Also at midday | A verdict on the graph that has been serving all morning |
| `pipeline/monthly_overture.sh` | 03:30 on the 5th | Overture refresh, joined on the GERS ids already stored |
| `scripts/backup_db.sh` | 01:45 daily | `pg_dump` before the night's work touches anything |
| Closure/report expiry | Hourly | Stops the router avoiding a cauce that drained weeks ago |

Install with `crontab infra/crontab`. Times are local; Nicaragua is UTC-6 with
no daylight saving, so shift by six hours on a UTC-configured server.

## 3. The nightly build, step by step

1. **fetch_osm.sh** — downloads the Geofabrik extract (conditional on
   `Last-Modified`, so an unchanged day costs nothing), verifies the published
   md5, clips the curation circle for QA, and exports POIs and roads as
   GeoJSON-seq with `osmium`.
2. **build_tiles.sh --base-only** — Planetiler → `base.pmtiles` (whole country).
3. **build_tiles.sh --circle-only** — `circle.pmtiles`, the archive users
   download for offline use. Housenumbers are excluded: they are the biggest z14
   contributor and near-useless in a country without street numbers.
4. **build_valhalla.sh** — stages a **date-stamped** copy of the PBF and
   restarts Valhalla. The date stamp is load-bearing; see §6.
5. **POI chain** — `fetch_osm_pois` → `conflate` → `load_pois` (PostGIS) →
   `gazetteer_build` → `export_geojson` → `build_tiles --pois-only` →
   `build_index`.
6. **QA** — golden routes and KPIs. These do not gate publication: the artifacts
   are already live, so what QA produces is a verdict, and the response to a red
   verdict is §8.

Artifacts live under `data/` (see `docs/SPEC.md` §6) and are all disposable —
they rebuild from source data. What is *not* disposable is PostGIS: the curated
gazetteer, the learned address aliases, moderation decisions and field surveys
exist nowhere else. That is what the backup is for.

## 4. Disk

Budget roughly:

| | |
|---|---|
| OSM extract | ~60-100 MB |
| Planetiler static sources (once) | ~1 GB |
| `base.pmtiles` | a few hundred MB |
| `circle.pmtiles` | far smaller; check before telling users the download size |
| Valhalla graph + tar | under 1 GB for Nicaragua |
| PostGIS | small; POIs are text |

A build briefly needs double the space for one artifact, because it writes
`X.tmp` beside the live `X`. `make clean-tmp` removes leftovers from a killed
run.

## 5. Rolling back a bad build

Everything published is a single file replaced by `os.replace`, so rollback is a
file copy — but there is no automatic previous copy. Before a risky change:

```bash
cp data/tiles/base.pmtiles data/tiles/base.pmtiles.bak
cp -r data/valhalla data/valhalla.bak
```

To restore: copy back and restart the affected service. For routing,
`docker compose restart valhalla` is enough; tiles need nothing (nginx serves
whatever file is there, and pmtiles.js notices the ETag change).

Restoring the database:

```bash
gunzip -c data/backups/nicanav-<stamp>.sql.gz | \
  docker compose -f infra/docker-compose.yml exec -T postgis psql -U nicanav -d nicanav
```

## 6. When routing looks wrong

**First: is the graph even current?**

```bash
curl -s localhost:8002/status | jq
```

`tileset_last_modified` is a Unix timestamp. If it is older than the last
nightly run, the rebuild is not landing, and there are two specific reasons it
might not be:

1. **The rebuild check hashes the PBF's *path string*, not its contents.**
   Overwriting the extract under the same filename looks identical to yesterday.
   `build_valhalla.sh` stages a date-stamped filename precisely to defeat this.
   If someone "simplifies" that away, rebuilds stop silently.
2. **`use_tiles_ignore_pbf` defaults to `True`**, which short-circuits the
   new-PBF check before it is reached. The compose file sets it to `False`.

`build_valhalla.sh` fails the run when `tileset_last_modified` did not move, so
a silent stall should show up as a red cron mail rather than as month-old roads.

**Instructions came back in English.** An unsupported language tag falls back to
`en-US` with no warning. `build_valhalla.sh` runs a canary route and fails on
English narration; `make verify` re-checks it.

**Speeds are not what the table says.** A malformed `default_speeds.json` is
ignored with only a log warning, so tuning silently does nothing:

```bash
make check-speeds
docker compose -f infra/docker-compose.yml logs valhalla | grep -i speed
```

Speeds apply at *tile build* time. Changing them means rebuilding — or running
`valhalla_assign_speeds`, which rewrites speeds on existing tiles.

**A route is wrong but the graph is current.** That is a data problem, not an
infrastructure one: check the road in OSM. It is usually a missing one-way, an
unmapped retorno or a rotonda with the wrong flow. Fix it upstream, and add a
golden route so it cannot come back.

## 7. First-deploy checklist

Work through these once, on the real box, with real services:

- [ ] `make verify` passes end to end.
- [ ] **Tiles**: a `Range` request returns **206**, with `Content-Range`, a
      **strong** ETag (not `W/`), `Access-Control-Expose-Headers: ETag`, and no
      `Content-Encoding: gzip`. If gzip is on for `.pmtiles`, nginx drops the
      Range header entirely and returns the whole archive — and pmtiles.js
      reports what looks like a client bug.
- [ ] **Speeds**: `docker compose logs valhalla` shows no "unable to parse"
      warning, and changing a value in `infra/valhalla/default_speeds.json`
      visibly moves a golden route's duration. The optional keys in that file
      (`link_exiting`, `roundabout`, `driveway`, …) are written from the
      published schema shape and have never been loaded by a real build.
- [ ] **Overture**: every string in `docs/taxonomy.csv`'s
      `overture_categories` column exists in the release's own category list.
      An unmatched category silently sends real POIs to `otro`. Also mirror the
      release's category CSV locally — only the two most recent releases stay on
      S3.
- [ ] **OSM selectors**: check the plausible-but-unconfirmed ones flagged in
      `pipeline/pois/taxonomy.py` (`water=lagoon`, `cuisine=nicaraguan`,
      `shop=money_transfer`, `landuse=port`) against the real extract with
      `osmium tags-filter`.
- [ ] **Conflation thresholds**: hand-label ~200 pairs from the first review
      band and move the constants to what the precision/recall curve says. They
      are currently reasoned, not measured.
- [ ] **The gazetteer's coordinates**, especially the "donde fue" rows: they are
      the fuel for every relative address, and a wrong landmark poisons every
      address built on it.
- [ ] **Km 0** for the carreteras. The datum is not authoritatively documented;
      photograph real km posts and calibrate.
- [ ] **The cuadra**. 84 m (100 varas) is a prior, not a measurement. Measure the
      median block from the OSM street graph per city and update
      `common/geo.CUADRA_M`.
- [ ] **"al lago" per town.** Managua and Granada are established; Masaya, León,
      Rivas and Tipitapa are marked `UNVERIFIED` in `common/geo.py`. Confirm
      with locals before trusting an address in those towns.
- [ ] **The PWA in a real browser on a real phone**: it has never been opened.
      Check that the map paints, the sprite sheet resolves, a search returns, a
      card opens, and navigation speaks Spanish.
- [ ] **Image digests**: `make verify-images`, then pin by digest rather than
      tag.

## 8. When QA goes red

**Golden routes.** `--baseline`/`--compare` shows what moved:

```bash
docker compose --profile tools run --rm pipeline \
  python3 -m pipeline.qa.golden_routes --json /data/qa/today.json --baseline /data/qa/yesterday.json
```

A handful of routes moving together after an OSM update is usually one real
change. Everything moving at once is usually a costing or speed-table change.
One route failing on `must_pass_near` is usually a data problem at that spot.

**KPIs.** `pipeline/qa/kpis.py` is meant to be unflattering. The share of
primary/secondary/tertiary carrying `oneway`, `surface` and `maxspeed` is a
to-do list for the field programme, not a dashboard.

**Islands.** `pipeline/qa/islands.py` lists named roads the router cannot reach
from MGA. Each is either genuinely unreachable or — far more often — a road
drawn a metre short of the one it meets. The output includes an OSM link per
road.

## 9. When search looks wrong

```bash
curl -s localhost:7700/health
curl -s -H "Authorization: Bearer $NICANAV_MEILI_KEY" localhost:7700/indexes/nicanav/stats
```

The indexer builds into `nicanav_build` and swaps atomically, and refuses to
publish an index below a document floor — so a truncated export leaves the old
index serving rather than emptying search. If the document count is far off,
look at `data/exports/pois_index.jsonl` before touching Meilisearch.

An empty search with a healthy index usually means the *export* is empty, which
usually means PostGIS is empty, which usually means `load_pois` failed. Follow
that chain rather than reindexing hopefully.

## 10. Monthly Overture refresh

Only the two most recent releases stay on S3, so a pinned release 404s within
about two months. `pipeline/pois/fetch_overture.py` discovers the newest release
and adapts its query to the columns that release actually has — the `categories`
column was removed in the September 2026 release in favour of `basic_category`
and `taxonomy`.

Re-run by hand with `./pipeline/monthly_overture.sh [release]`.

## 11. Privacy

Search and route logs store positions rounded to ~1 km, and the client
identifier is a salted hash of the forwarded address. That is enough to find
coverage gaps and build speed profiles, and not enough to follow anybody home.
Keep it that way: this is a small country, and a map that tracks people is worse
than no map.
