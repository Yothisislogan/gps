# Mobile release and deployment plan

This is the implementation and launch checklist for the mobile-focused NicaNav
release. Code is on PR #1 (`codex/nicanav-reliability`). It incorporates the
newer development-branch deployment/simulator changes. A passing CI run is a
candidate for staging; no production server, domain, or real-phone results are
implied by this document.

## What changed

| Work | Implemented behavior | Remaining release evidence |
|---|---|---|
| Startup | Search/settings bind before the renderer loads; map retry has an owner so old callbacks cannot replace it; correct full-height map CSS | Cold-start timing on an ordinary Android |
| Mobile journeys | Welcome, Home/Work, recent places, search bottom sheet with numbered map pins, simpler place cards, entrance confirmation, manual origin after GPS denial, clearer route alternatives | Local address and entrance usability sessions |
| Driving | Browsing controls disappear during guidance; position requests happen when needed; updates check every tab for an active trip | Real GPS, audio, wake lock, heat/battery and interruption tests |
| Installation | Real PNG/maskable/Apple icons, safe-area/keyboard sizing, 48px controls and text zoom | Android and iOS installation from the real HTTPS domain |
| Downloads | Streaming with backpressure, OPFS where available, storage estimates, progress/cancel, validation before pointer replacement; downloads survive new releases | Large archive on low-storage phones and iOS storage eviction |
| Asset delivery | Content-addressed JS/CSS/style/vendor URLs, gzip/Brotli sidecars, gzip serving, cache-first installed shell; 900 KiB compressed shell budget excluding map data | First map bytes and render timing on Nicaragua mobile networks |
| Data publication | Opt-in isolated candidate stack with cloned DB, source refresh, route/coverage/readiness gates and one gateway switch | Full candidate build/promotion/rollback rehearsal on staging |
| Edits during refresh | Revision counter and API write locks; promotion refuses if accepted edits arrived after snapshot; old generations remain read-only | Staging exercise with actual moderator submissions during a build |
| Operations | Explicit credentials/origins, working host environment wrapper, loopback service ports, readiness endpoint, protected backups, privacy-conscious edge logs, managed job templates | Restore drill, offsite backup destination, external alert receiver |
| Regression coverage | Python/JS tests plus Chromium and WebKit journeys, real PostGIS transactions/revision checks and real nginx routing tests | Field routes and physical-device sign-off |

The browser fixtures draw a small synthetic road and return deterministic places
and routes. They test the interface, not the correctness of live Nicaragua data.
Playwright's service-worker tooling supports Chromium; its WebKit offline reload
case is explicitly skipped. An actual iPhone airplane-mode restart is still a
launch requirement. See [Playwright's supported surface](https://playwright.dev/docs/service-workers).

## Deployment choice

Ship the installable HTTPS PWA first. Keep the web app, tile files and API on one
origin. A Linux VM runs nginx, FastAPI, PostGIS, Meilisearch and Valhalla with
persistent disks; the host nginx terminates TLS and forwards to loopback-only
container ports. Start serving tests with roughly 4 vCPU / 8 GB, then size from
measured peaks. Running an isolated candidate alongside production needs more
headroom: plan a 16 GB staging/build host or separate the builder before public
scale. These are starting allocations, not measured capacity claims.

Choose the hosting region by measuring latency from Nicaragua. Begin with a
nearby US region and compare first-map, search and route timings from the actual
mobile networks used for the pilot. Keep the deployment architecture simple
until there is measured traffic to justify separating services.

Cloudflare can provide DNS and later cache immutable assets. Start with the
origin path working. Any CDN must preserve byte ranges, strong ETags and PMTiles
bodies. Cache `/releases/<hash>/...`; bypass `/api`, `/admin`, `/config.js`,
`/sw.js` and all generation API paths. Run the range checks through the public
hostname before enabling tile caching. The server already compresses text assets;
PMTiles archives are served without HTTP content encoding.

## First staging installation

Use a dedicated staging hostname and a disposable deployment until these steps
have passed. Keep the source checkout at `/opt/nicanav`; use an explicit release
commit that passed CI. Install Docker/Compose, Python 3.11+, Node 22+, OpenSSL,
nginx and the pipeline prerequisites in [RUNBOOK.md](RUNBOOK.md) (including Java,
osmium, jq and tippecanoe). Keep a Python virtualenv at `/opt/nicanav/.venv`.

1. Copy `infra/.env.example` to `infra/.env`, make it mode 600, generate unique
   secrets, and set the exact HTTPS origin in `NICANAV_PUBLIC_BASE_URL` and
   `NICANAV_CORS_ORIGINS`. Use absolute data paths in deployment configurations.
2. Activate the virtualenv, install `requirements/dev.txt`, and run `make up`.
   `make prepare` builds `dist/web`, renders runtime config and nginx auth.
   Compose serves **`dist/web`**, so copying the editable `web` folder alone is
   insufficient. Existing databases need `make migrate` before the new API.
   Migration `004_release_revision.sql` follows the correction migration `003`.
3. With the host still private, run `make nightly`, then
   `python scripts/run_host.py bash pipeline/monthly_overture.sh`. Confirm real
   source metadata, nonempty search, route narration and `make verify`.
   The host wrapper reads the same env file as Compose and translates its paths
   and service ports for host-side tools.
4. Configure the HTTPS host using `infra/nginx-site.conf.example`, obtain a valid
   certificate, and verify the public endpoint. Check `/api/readyz`, not only
   `/api/healthz`: readiness returns 503 if any required dependency is unavailable.
5. Test backup and restore on a separate disposable database. Preserve both the
   dump and the exact source/release metadata. Configure an offsite rclone target
   and perform a restore from that offsite copy before relying on it.

The initial import writes to its own private staging stack. For subsequent live
refreshes, adopt the managed release flow below. The legacy individual scripts
still replace files/services sequentially and must not run against a managed
active generation.

## Coordinated refreshes: opt in after the staging drill

The release manager builds a separate Compose project with its own PostGIS and
search volumes, copied map data and detached code worktree. The active stack
continues serving. A consistent `pg_dump` snapshot carries the revision counter;
publication pauses API mutations briefly, drains in-flight writes, and checks
that the active revision still matches the snapshot. If it does not, the
candidate is rejected and writes resume on the current version.

The gates reject empty datasets, a POI drop exceeding 10%, missing/failed route
checks, a route distance/time change over 25% from the previous run, failed
service readiness, broken ranges and failed public generation checks. Initial
route estimates are not field truth; record driven benchmarks before launch.
Changes rejected by a gate need investigation, not a routine bypass.

Before adoption, stop and disable the legacy jobs and wait for any running jobs
to finish. Also remove the equivalent legacy cron entries:

```sh
sudo systemctl disable --now nicanav-nightly.timer nicanav-overture.timer nicanav-expire.timer nicanav-golden.timer nicanav-backup.timer
```

Replace the HTTPS host's location blocks with the managed template
`infra/nginx-managed-site.conf.example`. It includes
`/etc/nginx/nicanav-current.conf`, which `init` generates. Keep the currently
loaded nginx configuration serving until `init` tests and reloads the new file.
Run adoption in a planned maintenance window; it recreates API/nginx containers.
Use the **existing Compose project name** so the current database volume is used:

```sh
sudo /opt/nicanav/.venv/bin/python /opt/nicanav/scripts/releases.py init \
  --env-file /opt/nicanav/infra/.env \
  --public-url https://mapa.example.ni \
  --gateway /etc/nginx/nicanav-current.conf \
  --project nicanav
```

The registry is written only after both normal and versioned HTTPS readiness
checks pass. Handled initialization failures restore the previous web entry files,
gateway and original Compose configuration. A machine crash still requires
operator recovery: inspect the protected `adoption.json`, registry, gateway and
`writes-paused` markers before resuming writes. Never remove the marker from a
retired generation or guess which database accepted the latest correction.

Then rehearse each operation, reviewing the candidate before promotion:

```sh
sudo /opt/nicanav/.venv/bin/python /opt/nicanav/scripts/releases.py build
sudo /opt/nicanav/.venv/bin/python /opt/nicanav/scripts/releases.py promote \
  --candidate /var/lib/nicanav-releases/<candidate-id>/candidate.json
sudo /opt/nicanav/.venv/bin/python /opt/nicanav/scripts/releases.py status
sudo /opt/nicanav/.venv/bin/python /opt/nicanav/scripts/releases.py rollback
```

Rollback refuses to discard edits accepted since promotion. If new edits exist,
prepare a forward fix from the latest active database. Do not force a snapshot
rollback. Generation-specific API and tile URLs keep an existing trip on its
original snapshot; the previous two generations remain available. Before a new
build, an eligible older generation can be stopped after at least 24 hours. Its
files and database volumes are retained for recovery; monitor disk space and
archive/remove retired copies only after verifying backups. Insufficient disk
space stops a build before publication.

After a successful drill, install the `nicanav-managed@.service` and managed timer
templates plus the failure handler. Enable the four **managed** timers. Do not
re-enable the legacy timers. `refresh` checks OSM and Overture within the candidate;
`backup`, `expire`, and `check` resolve the current generation through the registry.
Seven-day search/route log retention is applied by `expire`. Job failures are
recorded in journald and `/var/lib/nicanav/last-failure`. Attach an external monitor
and alert destination before calling this unattended production operation.

## Phone and performance release gates

Use a midrange Android with limited RAM and a real iPhone. Repeat on slow mobile
service, with an ordinary cold browser cache and with an installed PWA:

- Find a place, distinguish results on the map, confirm its entrance, select an
  alternate route, start guidance and reach the correct door.
- Deny GPS and use a manual start point; lose/recover signal; change destination;
  interrupt a route request; rotate the phone and open/close the keyboard.
- Download the real offline archive; cancel midway; run out of storage; reopen
  the installed app in airplane mode; reconnect and verify the POI layers return.
- Keep one trip active while another tab sees an update. Verify no forced reload,
  lost destination, stale reroute or duplicate speech. Lock/unlock the screen and
  test audio interruptions. The PWA pilot is foreground navigation; continuous
  background guidance is not yet a validated capability.
- Rehearse candidate rejection after a new correction, failed QA, failed HTTPS
  smoke test, successful switch and rollback without edits. Restore a backup.

Track median and slow-case results rather than quoting one fast desktop run.
Initial product targets: search controls usable within 1.5 seconds, useful map
within 3 seconds on the chosen mobile test profile, and search/route responses
within 1 second once services are warm. The 900 KiB shell budget is enforced in
builds; full offline-map size is a separate visible download. Record actual first
view tile bytes, memory, long tasks and 30-minute navigation battery use. These
are acceptance targets, not performance measurements already achieved.

A public pilot also needs checked entrances/landmarks and driven route fixtures
for the launch area. Aging-record re-verification and broader data coverage remain
next product work. No dataset freshness claim should come only from a rebuilt
file timestamp.
