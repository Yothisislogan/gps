#!/usr/bin/env bash
# Check daily for a monthly Overture release. Full conflation is still used.
#   ./pipeline/monthly_overture.sh [release]

SCRIPT_NAME=monthly_overture
. "$(cd "$(dirname "$0")" && pwd)/lib.sh"

RELEASE="${1:-}"
ensure_dirs
take_lock
require_free_space 2048
py() { (cd "$REPO_ROOT" && python3 "$@"); }
ALREADY_APPLIED="$(py -m pipeline.status "$METADATA_DIR" "$SCRIPT_NAME" check)"
start_run
CURRENT_STAGE="fetch overture"
record_status running --stage "$CURRENT_STAGE"
FETCH_ARGS=(--skip-unchanged --metadata "$METADATA_DIR/overture.json")
[ -n "$RELEASE" ] && FETCH_ARGS+=(--release "$RELEASE")
FETCH_CODE=0
py -m pipeline.pois.fetch_overture "${FETCH_ARGS[@]}" || FETCH_CODE=$?
if [ "$FETCH_CODE" -eq 3 ]; then
  if [ "$ALREADY_APPLIED" = yes ]; then
    record_status unchanged --stage "release unchanged"
    log "no new Overture release; previous refresh completed"
    exit 0
  fi
  log "extract unchanged, but previous publication incomplete; retrying downstream steps"
elif [ "$FETCH_CODE" -ne 0 ]; then
  exit "$FETCH_CODE"
fi

CONFLATE_ARGS=(--input "${EXPORT_DIR}/src_osm.geojsonseq" --input "${EXPORT_DIR}/src_overture.geojsonseq")
[ -s "${EXPORT_DIR}/src_survey.geojsonseq" ] && CONFLATE_ARGS+=(--input "${EXPORT_DIR}/src_survey.geojsonseq")
step "conflate pois" py -m pipeline.pois.conflate "${CONFLATE_ARGS[@]}" \
  --output "${EXPORT_DIR}/pois_merged.geojsonseq" --queue "${EXPORT_DIR}/review_queue.json"
step "load pois" py -m pipeline.pois.load_pois
step "export pois" py -m pipeline.pois.export_geojson
step "build poi tiles" "${REPO_ROOT}/pipeline/build_tiles.sh" --pois-only
step "reindex search" py -m pipeline.search.build_index
record_status succeeded --stage "complete"
log "done"
