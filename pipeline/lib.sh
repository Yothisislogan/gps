#!/usr/bin/env bash
# Shared helpers for the pipeline shell scripts.
#
# Sourced, never executed:  . "$(dirname "$0")/lib.sh"

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="${NICANAV_DATA_DIR:-${REPO_ROOT}/data}"
TILES_DIR="${NICANAV_TILES_DIR:-${DATA_DIR}/tiles}"
OSM_DIR="${DATA_DIR}/osm"
EXPORT_DIR="${DATA_DIR}/exports"
METADATA_DIR="${NICANAV_METADATA_DIR:-${DATA_DIR}/metadata}"
VALHALLA_DIR="${NICANAV_VALHALLA_DIR:-${DATA_DIR}/valhalla}"
COMPOSE_FILE="${REPO_ROOT}/infra/docker-compose.yml"

GEOFABRIK_URL="${NICANAV_GEOFABRIK_URL:-https://download.geofabrik.de/central-america/nicaragua-latest.osm.pbf}"
PLANETILER_JAR="${PLANETILER_JAR:-${DATA_DIR}/tools/planetiler.jar}"
PLANETILER_VERSION="${PLANETILER_VERSION:-0.10.2}"

log()  { printf '%s [%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "${SCRIPT_NAME:-pipeline}" "$*" >&2; }
die()  { log "ERROR: $*"; exit 1; }

require() {
  # require <binary> [install hint]
  command -v "$1" >/dev/null 2>&1 || die "missing required tool '$1'${2:+ — $2}"
}

ensure_dirs() {
  mkdir -p "$OSM_DIR" "$TILES_DIR" "$EXPORT_DIR" "$VALHALLA_DIR" "${DATA_DIR}/tools" "${DATA_DIR}/backups" "$METADATA_DIR"
}

# publish <tmp> <final>
#
# The whole pipeline's safety rule in three lines: nginx keeps serving the old
# file until a complete one is renamed over it, and the rename is atomic only
# because the temp file lives in the same directory (same filesystem).
publish() {
  local tmp="$1" final="$2"
  [ -s "$tmp" ] || die "refusing to publish empty or missing $tmp"
  mv -f "$tmp" "$final"
  log "published $final ($(du -h "$final" | cut -f1))"
}

# Take the pipeline lock, or exit quietly.  Two nightly builds at once would
# fight over the same temp filenames and could publish a mixture of both.
take_lock() {
  local lock="${DATA_DIR}/.pipeline.lock"
  exec 9>"$lock"
  if ! flock -n 9; then
    log "another pipeline run holds the lock; exiting"
    exit 0
  fi
}

# Free space in MB on the data filesystem.
free_mb() { df -Pm "$DATA_DIR" | awk 'NR==2 {print $4}'; }

require_free_space() {
  local needed="$1" have
  have="$(free_mb)"
  [ "$have" -ge "$needed" ] || die "only ${have} MB free on ${DATA_DIR}; need ${needed} MB"
}

# Wait for an HTTP endpoint to answer 200.
wait_for_http() {
  local url="$1" timeout="${2:-180}" waited=0
  until curl -fsS -o /dev/null "$url" 2>/dev/null; do
    waited=$((waited + 5))
    [ "$waited" -lt "$timeout" ] || die "timed out after ${timeout}s waiting for ${url}"
    sleep 5
  done
  log "ready: ${url}"
}

compose() { docker compose -f "$COMPOSE_FILE" --env-file "${REPO_ROOT}/infra/.env" "$@"; }

# The lock is held by callers for the complete refresh. EXIT records failures,
# including signals; a killed process leaves "running" rather than fake success.
record_status() {
  (cd "$REPO_ROOT" && python3 -m pipeline.status "$METADATA_DIR" "$SCRIPT_NAME" "$@")
}

start_run() {
  CURRENT_STAGE="starting"
  record_status start --scope "${1:-full}"
  trap 'run_exit $?' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
}

run_exit() {
  local code="$1"
  if [ "$code" -ne 0 ]; then
    record_status failed --stage "$CURRENT_STAGE" || true
    log "refresh failed at: $CURRENT_STAGE; earlier steps may already be published"
  fi
}

step() {
  CURRENT_STAGE="$1"; shift
  record_status running --stage "$CURRENT_STAGE"
  log "=== $CURRENT_STAGE ==="
  # Do not invoke this function in an if/|| context: Bash would disable -e
  # for shell functions used as step commands.
  "$@"
  log "=== $CURRENT_STAGE: ok ==="
}
