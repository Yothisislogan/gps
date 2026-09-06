#!/usr/bin/env bash
# The nightly build, end to end.
#
# Stops on the first failure. Earlier successful steps may already be live;
# coordinated publication across files, database and services is still pending.
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

ensure_dirs
take_lock
SCOPE=full
if [ "$SKIP_TILES" -eq 1 ] || [ "$SKIP_VALHALLA" -eq 1 ] || [ "$SKIP_POIS" -eq 1 ]; then
  SCOPE=partial
fi
start_run "$SCOPE"

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
  # Stop before any dependent step can consume stale intermediate output.
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

record_status succeeded --stage "complete"
log "nightly build finished ($SCOPE)"
