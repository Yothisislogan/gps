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

# Our Nicaraguan speed table, referenced from valhalla.json. The container is
# configured with use_default_speeds_config=False so its entrypoint does not
# download the upstream table over this file on every start.
cp -f "${REPO_ROOT}/infra/valhalla/default_speeds.json" "${VALHALLA_DIR}/default_speeds.json"

# mjolnir.default_speeds_config is not one of the nine keys the entrypoint
# rewrites on every start, so a value set here survives the config merge.
CONFIG="${VALHALLA_DIR}/valhalla.json"
if [ -f "$CONFIG" ]; then
  if command -v jq >/dev/null 2>&1; then
    jq '.mjolnir.default_speeds_config = "/custom_files/default_speeds.json"' "$CONFIG" > "${CONFIG}.tmp" \
      && publish "${CONFIG}.tmp" "$CONFIG"
  else
    log "WARNING: jq not installed; cannot point valhalla.json at default_speeds.json"
  fi
else
  log "no valhalla.json yet; the container will generate one on first start"
  log "re-run this script afterwards so the speed table is wired in"
fi

if [ "$FORCE" = "--force" ]; then
  log "forcing a full graph rebuild"
  compose run --rm -e force_rebuild=True valhalla build_tiles || die "forced rebuild failed"
fi

log "restarting valhalla to pick up the new extract"
compose up -d valhalla

# The build runs inside the container on start; a Nicaragua-sized graph takes a
# few minutes. /status answers only once tiles are being served, so it doubles
# as the readiness signal.
wait_for_http "http://localhost:8002/status" "${VALHALLA_BUILD_TIMEOUT:-1800}"

STATUS="$(curl -fsS http://localhost:8002/status)"
log "valhalla: ${STATUS}"

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
