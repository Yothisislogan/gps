#!/usr/bin/env bash
# Monthly Overture Places refresh.
#
# Overture publishes a release roughly monthly. Because every merged POI keeps
# its Overture GERS id, this is a join and not a re-conflation: only new and
# changed ids need scoring, so the monthly run is cheap and — importantly —
# cannot reshuffle POIs that a human already reviewed.
#
#   ./pipeline/monthly_overture.sh [release]

SCRIPT_NAME=monthly_overture
. "$(cd "$(dirname "$0")" && pwd)/lib.sh"

RELEASE="${1:-}"

take_lock
ensure_dirs
require_free_space 2048

log "fetching Overture places"
(cd "$REPO_ROOT" && python3 -m pipeline.pois.fetch_overture ${RELEASE:+--release "$RELEASE"}) \
  || die "overture fetch failed"

log "re-conflating changed records"
(cd "$REPO_ROOT" && python3 -m pipeline.pois.conflate --incremental) || die "conflation failed"

log "exporting and republishing"
(cd "$REPO_ROOT" && python3 -m pipeline.pois.export_geojson) || die "export failed"
"${REPO_ROOT}/pipeline/build_tiles.sh" --pois-only || die "poi tiles failed"
(cd "$REPO_ROOT" && python3 -m pipeline.search.build_index) || die "reindex failed"

log "done"
