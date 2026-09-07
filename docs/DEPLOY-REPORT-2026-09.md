# Deploy report — 2026-09

**This session did not run on the Hetzner box.** It ran in a clean sandbox: no
`/opt/nicanav`, no `/var/lib/nicanav*`, no Docker daemon, no `infra/.env`.
Tasks 1–3 and 7 could not be executed. Their repo-side causes were found and
fixed, and the commands that finish them are below. Everything else is a
repository fact verified here.

## 1. postgis

Two independent faults, either enough to hang the healthcheck forever. Both
were in this repository.

**`LANG: es_NI.utf8`.** The postgis image is Debian-based and carries only
Debian's default locales. `initdb` aborts with *"invalid locale settings"*,
leaves `PGDATA` empty, and the container restarts into the same failure while
`pg_isready` never answers. Removed; UTF-8 storage now comes from
`POSTGRES_INITDB_ARGS: "--encoding=UTF8"`.

**A DSN assembled by Compose interpolation.** Compose eats a literal `$` in a
`.env` value, and `/ + @ ? #` corrupt a `postgresql://` URL even when they
survive it. Services now get `NICANAV_PG_HOST/PORT/DB/USER/PASSWORD`
separately and `common/config.py` composes the DSN, percent-encoding user and
password exactly once; an explicit `NICANAV_DATABASE_URL` still wins.
15 tests in `tests/test_config_dsn.py`.

`.env.example` now says `openssl rand -hex 24`. **The `.env` on the box may
hold a base64 secret containing `/`, `+` or `$`** — nothing here reads it, but
rotate to hex before trusting the fix.

Not verified: that postgis comes up.

## 2. The speed table

**No log line to quote — no build ran here.** What was found is why the
reported line was inevitable.

`build_valhalla.sh` patched `mjolnir.default_speeds_config` into
`valhalla.json` *after* starting the container. On a first deploy that file
does not exist, so the entrypoint generated it **and built the graph in the
same run**, from a config without the key — hence *"Disabled default speeds
assignment from config"*. The later patch changed nothing: speeds apply at
tile-build time and a graph does not rebuild for a config edit.

Now: the config is seeded via `valhalla_build_config` **before** the container
starts, so the first build is correct; if that tool or flag is missing it
falls back and forces exactly one rebuild; `default_speeds.json` is validated
as JSON first (Valhalla disables an unparseable config and keeps routing on
compiled-in numbers); after startup the script greps *this run's* log for the
disable/parse warnings and fails on them; `jq` is now a hard requirement.

**Before/after table: not produced.** It needs the box:

```bash
docker compose --profile tools run --rm pipeline \
  python3 -m pipeline.qa.golden_routes --json /data/qa/before.json
./pipeline/build_valhalla.sh --force            # ~35 min
docker compose --profile tools run --rm pipeline \
  python3 -m pipeline.qa.golden_routes --json /data/qa/after.json --baseline /data/qa/before.json
docker compose logs --since 40m valhalla | grep -i 'default speeds'
```

The `--baseline` diff *is* the table. If no duration moves, the config did not
reach the graph whatever the log says.

## 3. Timings, RAM, and the timer

**Not run. No timings, no peak RAM.** **The timer is not enabled**, per the
brief's own condition: no clean manual run has happened.

Units are committed at `infra/systemd/`: `nicanav-nightly`, `-backup`,
`-golden`, `-overture`, `-expire`, plus `nicanav-failure@.service` (`OnFailure`
for all of them; writes `/var/lib/nicanav/last-failure`, logs `daemon.err`).
The nightly unit sets `MemoryAccounting=yes`, `Persistent=true`,
`RandomizedDelaySec=15min`, `Nice=10`, `IOSchedulingClass=idle`. Install steps:
`docs/SERVER.md` §4. To measure first:

```bash
systemd-run --unit=nicanav-manual --working-directory=/opt/nicanav \
  -p MemoryAccounting=yes -p TimeoutStartSec=4h /opt/nicanav/pipeline/nightly.sh
journalctl -fu nicanav-manual            # the script logs per-step timings
systemctl show nicanav-manual -p MemoryPeak    # systemd >= 256
```

`MemoryPeak` on the unit is the only honest figure — the work happens in
containers and a JVM, so the script's own RSS says nothing.

**Bug found on the way.** `nightly.sh` ran `golden_routes --json` with no path:
argparse error, exit 2, before a single route was requested. That step failed
on **every** nightly run, so the build reported failure while everything it
published was fine. Fixed, with the previous run as `--baseline`.

## 4. `UNVERIFIED:` markers

39 substantive markers in 12 files (excluding the enum value in
`common/models.py` and the docs describing the convention).

| | |
|---|---|
| **Settled** | `docs/taxonomy.csv` — the Overture column was invented dotted paths matching nothing. Rebuilt from a real release; §5. |
| **Fixed** | `nightly.sh` bare `--json`; `verify_deploy.sh` defaulting to :8080 while compose publishes :8400 (a verify run would have failed every check and looked like a broken deploy); `nginx.conf` re-declaring MIME types nginx already knows. |
| **New, deliberate** | `build_valhalla.sh` — the `valhalla_build_config --mjolnir-*` flag names are derived from config keys, not run against the image. Falls back safely. |
| **Open, needs the box** | speed-table schema shape (`infra/valhalla/README.md`); `common/valhalla.py` response handling; `gazetteer_build.py` (osmium keys; never run against PostGIS). |
| **Open, needs a phone** | `map.js` long-press on a dash mount; `navmath.js` maneuver types above 27. |
| **Open, needs Nicaragua** | per-city lake/mountain bearings (`common/geo.py`); `reverse.py` weights and the cuadra ceiling; `conflate.py` constants and `KNOWN_CHAIN_NAMES`; `carreteras.csv` km-0 origins and chainages; all 34 golden routes. |

All 34 golden routes now carry `source: agent`, and the harness **cannot fail
the build on an agent-sourced mismatch** — those print `diff`, not `FAIL`.
Only `driven` is ground truth; `--strict` overrides. A test asserts no `driven`
route exists yet, and is meant to fail the day one does.

## 5. Taxonomy and POI counts

Audited against Overture **2026-08-19.0**, read from Parquet over HTTPS range
requests (`python3 -m pipeline.pois.audit_overture`; 6.9 MB of a 10.5 GB
release, 36 s). Repeatable after every release.

| | Nicaragua | 48.3 km bbox around MGA |
|---|---|---|
| Places | 43,030 | 24,444 |
| With a category | 40,654 | 22,976 |
| Mapped by `docs/taxonomy.csv` | 32,671 (**80.4 %**) | 18,104 (**78.8 %**) |
| Falling to `otro` | 7,983 | 4,872 |

Before this work the column held invented dotted paths
(`eat_and_drink.restaurant`); Overture uses bare snake_case leaf tokens, so
**70.6 %** of categorised Nicaraguan places fell to `otro`. The column is now
323 tokens across 68 app categories, 321 of them observed in the real extract.

409 categories remain unmapped; the 153 with count ≥ 5 account for 7,484 of
the 7,983. Largest: `professional_services` 646,
`party_and_event_planning` 481, `structure_and_geography` 336,
`real_estate_service` 319, `central_government_office` 283,
`community_services_non_profits` 255, `river` 219. Mostly B2B services and
Overture's own grouping and terrain names — nothing a driver navigates by.

Top app categories in the bbox: `otro` 4,872 · `tienda` 3,818 · `restaurante`
1,826 · `salon_belleza` 1,468 · `iglesia` 1,351 · `clinica` 1,096 · `escuela`
951 · `bar` 675 · `hotel` 649 · `dentista` 482.

Caveats: this is the **bbox**, not the circle; it is **Overture alone** —
counts after conflation with OSM need the pipeline; and `otro` above excludes
the 1,468 bbox places Overture gives no category at all.

## 6. Push back on this

- **`http2 on;` was wrong.** `nginx-site.conf.example` was verified with
  `nginx -t` (1.24.0), which rejected it: that directive needs nginx ≥ 1.25.1
  and Ubuntu 24.04 ships 1.24.0, so it would have failed validation and left
  the site down on reload. Neither spelling is used now; both are documented.
  Check `nginx -v` before enabling HTTP/2.
- **`X-Forwarded-For` is replaced, not appended, at the host edge.**
  `api/deps.py` reads the first entry for rate limiting, so appending would
  let a client spoof it. If anything else fronts this box, that changes.
- **Ports moved to 8400 everywhere** — `verify_deploy.sh` and
  `NICANAV_PUBLIC_BASE_URL` still defaulted to 8080.
- **Service-worker cache bumped to `v2`**: every client re-downloads the shell
  once.
- **Five systemd timers, not one.** `infra/crontab` covers the same jobs.
  Install one or the other, never both.
- **Every unit runs as root**, because they drive the Docker socket. Fine on a
  single-purpose box; `NoNewPrivileges`/`ProtectHome`/`PrivateTmp` are set,
  which is not the same thing.

**Chosen not to do:** task 7 (screenshots, needs a running stack); the
simulator acceptance criteria that need a browser (prompt ordering, no
double-announce, reroute after exactly three off-route fixes, arrival within
30 m) — the geometry beneath them has 68 unit tests, the end-to-end behaviour
has none; and mapping the 153 unmapped Overture categories, since guessing at
them would put back exactly the kind of unverified content §5 removed.
