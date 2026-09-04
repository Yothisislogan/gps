#!/usr/bin/env bash
# The nightly build, end to end.
#
# Design rules, in priority order:
#   1. Never publish a broken artifact. Every step writes .tmp and renames.
#   2. A failure leaves yesterday's data serving. Stale beats wrong.
#   3. Say what happened. This runs unattended; the log is the only witness.
#
#   ./pipeline/nightly.sh [--skip-tiles] [--skip-valhalla] [--skip-pois] [--force]

SCRIPT_NAME=nightly
. "$(cd "$(dirname "$0")" && pwd)/lib.sh"

SKIP_TILES=0 SKIP_VALHALLA=0 SKIP_POIS=0 FORCE=""
for arg in "$@"; do
  case "$arg" in
    --skip-tiles)    SKIP_TILES=1 ;;
    --skip-valhalla) SKIP_VALHALLA=1 ;;
    --skip-pois)     SKIP_POIS=1 ;;
    --force)         FORCE="--force" ;;
    *) die "unknown option: $arg" ;;
  esac
done

take_lock
ensure_dirs

STARTED="$(date -u +%s)"
FAILED_STEPS=()

# Run a step, timed. A failed step is recorded and the build continues, because
# a broken POI export must not stop the road data from being published.
step() {
  local name="$1"; shift
  local began; began="$(date -u +%s)"
  log "=== ${name} ==="
  if "$@"; then
    log "=== ${name}: ok in $(( $(date -u +%s) - began ))s ==="
  else
    log "=== ${name}: FAILED after $(( $(date -u +%s) - began ))s ==="
    FAILED_STEPS+=("$name")
  fi
}

py() { (cd "$REPO_ROOT" && python3 "$@"); }

step "fetch osm"          "${REPO_ROOT}/pipeline/fetch_osm.sh" $FORCE

if [ "$SKIP_TILES" -eq 0 ]; then
  step "build base tiles" "${REPO_ROOT}/pipeline/build_tiles.sh" --base-only
  # The offline archive users download for the curation circle.
  step "build circle tiles" "${REPO_ROOT}/pipeline/build_tiles.sh" --circle-only
fi

if [ "$SKIP_VALHALLA" -eq 0 ]; then
  step "build routing graph" "${REPO_ROOT}/pipeline/build_valhalla.sh" $FORCE
fi

if [ "$SKIP_POIS" -eq 0 ]; then
  # ingest -> conflate -> load -> export -> tiles -> index.
  #
  # The database sits in the middle on purpose: it is the system of record once
  # conflation has run, so moderation decisions and field verifications survive
  # the next nightly build instead of being overwritten by it. Tiles and the
  # search index are both derived from the database, never from the raw merge.
  #
  # Each step reads what the previous one published, so a failure part-way
  # leaves the last good artifact in place and the map keeps showing yesterday's
  # POIs rather than none.
  step "ingest osm pois"  py -m pipeline.pois.fetch_osm_pois

  # Overture publishes monthly; pipeline/monthly_overture.sh refreshes it. The
  # nightly run reuses whatever was last pulled, if anything.
  OVERTURE_SRC="${EXPORT_DIR}/src_overture.geojsonseq"
  CONFLATE_ARGS=(--input "${EXPORT_DIR}/src_osm.geojsonseq")
  [ -s "$OVERTURE_SRC" ] && CONFLATE_ARGS+=(--input "$OVERTURE_SRC")
  [ -s "${EXPORT_DIR}/src_survey.geojsonseq" ] && CONFLATE_ARGS+=(--input "${EXPORT_DIR}/src_survey.geojsonseq")

  step "conflate pois"    py -m pipeline.pois.conflate                             "${CONFLATE_ARGS[@]}"                             --output "${EXPORT_DIR}/pois_merged.geojsonseq"                             --queue "${EXPORT_DIR}/review_queue.json"
  step "load pois"        py -m pipeline.pois.load_pois
  step "build gazetteer"  py -m pipeline.geocode.gazetteer_build
  step "export pois"      py -m pipeline.pois.export_geojson
  step "build poi tiles"  "${REPO_ROOT}/pipeline/build_tiles.sh" --pois-only
  step "reindex search"   py -m pipeline.search.build_index
fi

# QA runs last and does not gate publication: the artifacts are already live, so
# what these produce is a verdict on them, not a gate. A red golden-route run is
# the signal to roll back, which the runbook documents.
step "golden routes"      py -m pipeline.qa.golden_routes --json
step "kpis"               py -m pipeline.qa.kpis

ELAPSED=$(( $(date -u +%s) - STARTED ))
if [ ${#FAILED_STEPS[@]} -eq 0 ]; then
  log "nightly build finished in ${ELAPSED}s — all steps ok"
  exit 0
fi

log "nightly build finished in ${ELAPSED}s with ${#FAILED_STEPS[@]} failed step(s): ${FAILED_STEPS[*]}"
log "the previously published artifacts are still being served"
exit 1
