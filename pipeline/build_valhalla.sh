#!/usr/bin/env bash
# Rebuild the Valhalla routing graph from the current OSM extract.
#
# Two traps in the scripted Valhalla image shape this script, and both fail
# silently rather than loudly:
#
#   1. The image decides whether to rebuild by hashing the PBF's *path string*,
#      not its contents. Overwriting nicaragua-latest.osm.pbf in place therefore
#      looks identical to yesterday and no rebuild happens.
#   2. use_tiles_ignore_pbf defaults to True, which skips the new-PBF check
#      before it is even reached.
#
# So: the extract is copied in under a date-stamped name (new path => new hash),
# stale copies are removed, and the compose file sets use_tiles_ignore_pbf=False.
#
#   ./pipeline/build_valhalla.sh [--force]

SCRIPT_NAME=build_valhalla
. "$(cd "$(dirname "$0")" && pwd)/lib.sh"

FORCE="${1:-}"

ensure_dirs
require docker
require curl
require jq "apt-get install -y jq"
require_free_space 2048

PBF="${OSM_DIR}/nicaragua-latest.osm.pbf"
[ -f "$PBF" ] || die "no OSM extract at ${PBF}; run pipeline/fetch_osm.sh first"

STAMP="$(date -u +%Y%m%d)"
TARGET="${VALHALLA_DIR}/nicaragua-${STAMP}.osm.pbf"

if [ -f "$TARGET" ] && [ "$FORCE" != "--force" ]; then
  log "${TARGET} already staged; nothing to do (use --force to rebuild)"
else
  log "staging $(basename "$TARGET")"
  cp -f "$PBF" "${TARGET}.tmp"
  publish "${TARGET}.tmp" "$TARGET"
  # Exactly one PBF must remain, or the image builds tiles from all of them.
  find "$VALHALLA_DIR" -maxdepth 1 -name 'nicaragua-*.osm.pbf' ! -name "$(basename "$TARGET")" -print -delete
fi

# --- speed table -------------------------------------------------------------
#
# Our Nicaraguan speed table, referenced from valhalla.json. The container is
# configured with use_default_speeds_config=False so its entrypoint does not
# download the upstream table over this file on every start.
cp -f "${REPO_ROOT}/infra/valhalla/default_speeds.json" "${VALHALLA_DIR}/default_speeds.json"
jq -e . "${VALHALLA_DIR}/default_speeds.json" >/dev/null \
  || die "infra/valhalla/default_speeds.json is not valid JSON; Valhalla would ignore it silently"

CONFIG="${VALHALLA_DIR}/valhalla.json"
SPEEDS_PATH="/custom_files/default_speeds.json"
NEEDS_REBUILD=0

# First deploy: valhalla.json does not exist yet. Letting the entrypoint create
# it means the entrypoint also builds the graph in that same run — and the
# config it just wrote has no default_speeds_config, so the build logs
# "Disabled default speeds assignment from config" and bakes upstream's speeds
# into 35 minutes of tiles. Patching the file afterwards changes nothing,
# because speeds are applied at build time and the graph will not rebuild for a
# config edit. So the config is seeded *before* the container is ever started.
#
# UNVERIFIED: the --mjolnir-* flag names are derived from the config keys by
# valhalla_build_config's own argparse. If the flag or the tool is missing this
# falls through to the old behaviour plus one forced rebuild, which is slow but
# still correct.
if [ ! -f "$CONFIG" ]; then
  log "no valhalla.json yet; generating one with the speed table already wired in"
  if compose run --rm --no-deps -T --entrypoint valhalla_build_config valhalla \
        --mjolnir-tile-dir /custom_files/valhalla_tiles \
        --mjolnir-tile-extract /custom_files/valhalla_tiles.tar \
        --mjolnir-timezone /custom_files/timezones.sqlite \
        --mjolnir-admin /custom_files/admins.sqlite \
        --mjolnir-default-speeds-config "$SPEEDS_PATH" \
        >"${CONFIG}.tmp" 2>/dev/null \
     && jq -e '.mjolnir.default_speeds_config == $p' --arg p "$SPEEDS_PATH" \
          "${CONFIG}.tmp" >/dev/null 2>&1; then
    publish "${CONFIG}.tmp" "$CONFIG"
  else
    rm -f "${CONFIG}.tmp"
    log "WARNING: could not pre-generate valhalla.json"
    log "the entrypoint will build a first graph without the speed table;"
    log "this script will rewire the config and force exactly one rebuild"
  fi
fi

# mjolnir.default_speeds_config is not one of the keys the entrypoint rewrites
# on every start (update_existing_config only touches the paths it manages), so
# a value set here survives the merge. Rewriting is idempotent: if the file
# already said this, nothing changed and no rebuild is owed.
if [ -f "$CONFIG" ]; then
  jq --arg p "$SPEEDS_PATH" '.mjolnir.default_speeds_config = $p' "$CONFIG" >"${CONFIG}.tmp"
  if cmp -s "$CONFIG" "${CONFIG}.tmp"; then
    rm -f "${CONFIG}.tmp"
    log "valhalla.json already points at ${SPEEDS_PATH}"
  else
    publish "${CONFIG}.tmp" "$CONFIG"
    # The speed table only reaches the graph through a build. An existing
    # tileset was built without it and is now wrong.
    if [ -e "${VALHALLA_DIR}/valhalla_tiles.tar" ] || [ -d "${VALHALLA_DIR}/valhalla_tiles" ]; then
      log "speed config changed and tiles already exist — a rebuild is required"
      NEEDS_REBUILD=1
    fi
  fi
fi

# --- build -------------------------------------------------------------------

if [ "$FORCE" = "--force" ] || [ "$NEEDS_REBUILD" = 1 ]; then
  if [ "$FORCE" = "--force" ]; then
    log "forcing a full graph rebuild (--force)"
  else
    log "forcing a full graph rebuild (speed config changed)"
  fi
  compose run --rm -e force_rebuild=True valhalla build_tiles || die "forced rebuild failed"
fi

log "restarting valhalla to pick up the new extract"
# Bound the log scan below to this run, so a warning from a previous build does
# not fail a start that is fine.
STARTED_AT="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
compose up -d valhalla

# The build runs inside the container on start; a Nicaragua-sized graph takes a
# few minutes. /status answers only once tiles are being served, so it doubles
# as the readiness signal.
wait_for_http "http://localhost:8002/status" "${VALHALLA_BUILD_TIMEOUT:-1800}"

STATUS="$(curl -fsS http://localhost:8002/status)"
log "valhalla: ${STATUS}"

# The speed table's failure mode is a log line, not an error: a config Valhalla
# cannot use is disabled and routing continues on the compiled-in defaults, so
# every tuning change silently does nothing. This is the only place that
# difference is observable.
SPEED_WARN="$(compose logs --since "$STARTED_AT" valhalla 2>/dev/null \
  | grep -iE 'default speeds|default_speeds' || true)"
if printf '%s' "$SPEED_WARN" | grep -qi 'disabl\|unable to parse\|error'; then
  log "$SPEED_WARN"
  die "valhalla rejected default_speeds.json — the graph is using upstream speeds"
fi
if [ -n "$SPEED_WARN" ]; then
  log "speeds: ${SPEED_WARN}"
fi

# tileset_last_modified is a unix timestamp; if it did not move, the rebuild did
# not happen and the router is serving yesterday's roads while claiming health.
MODIFIED="$(printf '%s' "$STATUS" | sed -n 's/.*"tileset_last_modified":[[:space:]]*\([0-9]*\).*/\1/p')"
if [ -n "$MODIFIED" ]; then
  AGE_HOURS=$(( ( $(date -u +%s) - MODIFIED ) / 3600 ))
  log "tileset age: ${AGE_HOURS}h"
  if [ "$AGE_HOURS" -gt 48 ]; then
    die "tileset is ${AGE_HOURS}h old after a rebuild — the new PBF was not picked up"
  fi
fi

# An unsupported language tag falls back to en-US silently, and a router that
# narrates in English for Nicaraguan drivers is a product failure no health check
# would notice. One canary route settles it.
log "canary: Spanish narration"
CANARY="$(curl -fsS -X POST http://localhost:8002/route -H 'Content-Type: application/json' -d '{
  "locations":[{"lat":12.1415,"lon":-86.1682},{"lat":12.1150,"lon":-86.2504}],
  "costing":"auto","language":"es-ES","units":"kilometers"}' || true)"
if [ -z "$CANARY" ]; then
  log "WARNING: canary route returned nothing; check the graph covers Managua"
elif printf '%s' "$CANARY" | grep -qiE '"instruction": *"(Drive|Turn|Head|Continue|Keep)'; then
  die "instructions came back in English — es-ES is not in this build's locale set"
else
  log "canary ok"
fi

log "done"
