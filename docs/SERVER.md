# The box

What runs where, on the Hetzner server that hosts nicanav.

> **Scope note.** This describes the layout the repository is built for and
> the commands that produce it. Where a fact could only come from the machine
> itself — actual disk figures, actual timings, whether a unit is installed —
> it is marked. Nothing here was read off the running server by the session
> that wrote it. `docs/DEPLOY-REPORT-2026-09.md` says exactly what was and was
> not verified.

---

## 1. Layout

| | |
|---|---|
| Deploy tree | `/opt/nicanav` — the git checkout. Disposable: `git pull` and restart. |
| Live data | `$NICANAV_DATA_DIR`, set in `infra/.env`. **Outside the deploy tree** (`/var/lib/nicanav-data`), so a redeploy cannot reach it. |
| Secrets | `/opt/nicanav/infra/.env`, `chmod 600`, gitignored, never printed. |
| Job state | `/var/lib/nicanav/` — last-failure marker, midday QA output. |
| Logs | `journalctl` for the timers, `docker compose logs` for the services. |

Under `$NICANAV_DATA_DIR`:

```
osm/       nicaragua-latest.osm.pbf, circle48.geojson
tiles/     base.pmtiles, pois.pmtiles, circle48.pmtiles   <- served by nginx, read-only
valhalla/  valhalla.json, default_speeds.json, valhalla_tiles.tar, the dated PBF
exports/   src_osm / src_overture / pois_merged .geojsonseq, review_queue.json
qa/        golden-nightly.json, golden-prev.json, golden-midday.json
backups/   nicanav-<stamp>.sql.gz, a fortnight of them
tools/     planetiler.jar
```

The split matters. Everything in `tiles/`, `valhalla/` and `exports/` is
reproducible from OSM and Overture; `backups/` is not, and neither is the
database it comes from. That is the only thing on this box worth panicking
about.

---

## 2. Ports

Every published container port binds to `127.0.0.1`. This is not belt and
braces: **Docker writes its own iptables rules and bypasses UFW entirely**, so
a container published on `0.0.0.0` is on the public internet no matter what
`ufw status` says. The loopback bind is the actual control.

| Port | Service | Reachable from |
|---|---|---|
| `127.0.0.1:8400` | nginx (PWA, tiles, `/api/` proxy) | host only → host nginx |
| `127.0.0.1:8002` | Valhalla | host only |
| `127.0.0.1:7700` | Meilisearch | host only |
| `127.0.0.1:5432` | PostGIS | host only |
| `:80`, `:443` | host nginx | the internet |

The API container is not published at all; it is reachable only over the
compose network, from nginx.

Nothing faces outward until a host nginx site exists.
`infra/nginx-site.conf.example` is that file, unclaimed: it needs a hostname
and a certificate, and installing it is a deliberate act.

```bash
cp infra/nginx-site.conf.example /etc/nginx/sites-available/mapa.<domain>
# replace mapa.example.ni throughout
ln -s /etc/nginx/sites-available/mapa.<domain> /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
certbot --nginx -d mapa.<domain>
```

Do not open UFW ports for the containers. Do not touch other domains'
site files.

---

## 3. Services

`docker compose -f /opt/nicanav/infra/docker-compose.yml --env-file .../infra/.env`,
or `make ps` / `make logs SERVICE=valhalla` from `/opt/nicanav`.

| Service | Image | What it is |
|---|---|---|
| `nginx` | `nginx:1.27-alpine` | PWA, PMTiles by byte range, `/api/` proxy |
| `api` | built from `infra/Dockerfile.api` | FastAPI: search, routing, POIs, admin |
| `valhalla` | `ghcr.io/valhalla/valhalla-scripted:3.8.3` | routing; builds its graph on start |
| `postgis` | `postgis/postgis:17-3.5` | gazetteer, POIs, moderation, closures |
| `meilisearch` | `getmeili/meilisearch:v1.24` | search index |
| `pipeline` | built, `profiles: [tools]` | never running; `run --rm` for jobs |

Two container facts that cause confusing outages:

- **postgis takes no `LANG`.** The image is Debian-based and carries only
  Debian's default locales. Setting `es_NI.utf8` makes `initdb` fail with
  "invalid locale settings", which leaves the data directory empty and the
  container restarting forever behind a healthcheck that will never pass.
  UTF-8 storage comes from `POSTGRES_INITDB_ARGS`.
- **Valhalla rebuilds on a changed PBF *path*, not changed contents**, and
  `use_tiles_ignore_pbf` defaults to skipping even that check. Both are
  handled in `pipeline/build_valhalla.sh` and the compose file; overwriting
  the extract in place by hand will silently do nothing.

---

## 4. Scheduled jobs

Units in `infra/systemd/`. Install:

```bash
cp /opt/nicanav/infra/systemd/*.service /opt/nicanav/infra/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now nicanav-nightly.timer nicanav-backup.timer \
                       nicanav-golden.timer nicanav-overture.timer nicanav-expire.timer
systemctl list-timers 'nicanav-*'
```

| Timer | When (Managua) | Does |
|---|---|---|
| `nicanav-backup` | 01:45 daily | `pg_dump`, before the build touches anything |
| `nicanav-nightly` | 02:15 daily | OSM → tiles → graph → POIs → index → QA |
| `nicanav-golden` | 12:00 daily | golden routes against the graph that has been serving all morning |
| `nicanav-overture` | 03:30 on the 5th | Overture Places refresh and re-conflation |
| `nicanav-expire` | hourly | expire finished closures and stale reports |

`OnCalendar` carries an `America/Managua` suffix, which needs systemd ≥ 252
(24.04 ships 255). Nicaragua is UTC−6 all year, so on older systemd drop the
suffix and add six hours.

Each unit has `OnFailure=nicanav-failure@%n.service`, which writes
`/var/lib/nicanav/last-failure` and logs at `daemon.err`. There is no paging on
this box; that file is the alerting.

`infra/crontab` is the same schedule for a host without systemd. Install one or
the other, never both — two nightly builds racing would fight over the same
temp filenames, and only the pipeline lock would save them.

---

## 5. Redeploy

Code only, no data touched:

```bash
cd /opt/nicanav
git pull
make build            # rebuild the api and pipeline images
make up               # re-render web/config.js, then compose up -d
make verify           # the post-deploy checks (see below)
```

`make verify` runs `scripts/verify_deploy.sh`, which exists for the failures
that leave every health check green and the product broken: PMTiles served
without byte ranges, a weak ETag, a Valhalla graph that silently ignored the
speed table, narration that came back in English.

After a config change to `infra/nginx.conf`: `docker compose restart nginx`,
then `make verify`. After a change to `infra/valhalla/default_speeds.json`:
`make check-speeds`, then `./pipeline/build_valhalla.sh --force`, which is
~35 minutes — the speed table only reaches the graph through a build.

A redeploy must never be able to reach the live data. That is what keeping
`NICANAV_DATA_DIR` outside `/opt/nicanav` buys, and it is the reason not to
"tidy" it back into the tree.

---

## 6. Rolling back

**Code.** `git checkout <previous>` in `/opt/nicanav`, `make build && make up`.

**A bad nightly build.** Every artifact is published by atomic rename, so a
failed run leaves yesterday's file in place and there is nothing to roll back.
A run that *succeeded* and produced something wrong is the case that needs
work, and `docs/RUNBOOK.md` §5 has the per-artifact steps: tiles come back from
the previous `.pmtiles`, the graph from the previous dated PBF plus a forced
rebuild, the search index from a re-run of `build_index`.

**The database.** The only irreplaceable thing here.

```bash
gunzip -c /var/lib/nicanav-data/backups/nicanav-<stamp>.sql.gz \
  | docker compose exec -T postgis psql -v ON_ERROR_STOP=1 -U nicanav -d nicanav
```

Restore into a scratch database first and look at it. A dump that failed
half-way still gunzips.

---

## 7. Logs

```bash
journalctl -u nicanav-nightly.service -n 200          # last build
journalctl -u 'nicanav-*' --since today               # every job
cat /var/lib/nicanav/last-failure                     # what failed last, with context

docker compose logs -f --tail 200 valhalla            # a service
docker compose logs --since 30m valhalla | grep -i speed
```

The two log lines worth knowing by sight:

- `Disabled default speeds assignment from config` — Valhalla could not use
  `default_speeds.json` and built the graph on its own compiled-in numbers.
  Routing still works, which is why this is easy to miss;
  `pipeline/build_valhalla.sh` now fails the run on it.
- pmtiles.js reporting *"Server returned no content-length header"* in a
  browser console — that is not a client bug. Something in front of the
  archive is compressing it and dropping the Range header.

---

## 8. Disk

The build briefly doubles usage: every artifact is written as `.tmp` beside
the live one and renamed over it. `pipeline/lib.sh` refuses to start a step
without headroom, and `make clean-tmp` removes the leftovers of a killed run.

Actual free space, and how close the nightly build comes to filling it, must
be measured on the box — `df -h $NICANAV_DATA_DIR` before and during a run.
