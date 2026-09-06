#!/usr/bin/env bash
# Download the Nicaragua OSM extract and clip the curation circle.
#
# The whole country is what tiles and routing are built from — a route to Leon
# or Ometepe has to work. The circle clip exists only so the QA job can compute
# coverage statistics for the area that gets field-verified.
#
#   ./pipeline/fetch_osm.sh [--force]

SCRIPT_NAME=fetch_osm
. "$(cd "$(dirname "$0")" && pwd)/lib.sh"

FORCE="${1:-}"

require curl
require osmium "apt install osmium-tool"
ensure_dirs
require_free_space 2048

PBF="${OSM_DIR}/nicaragua-latest.osm.pbf"
TMP="${PBF}.tmp"
CIRCLE="${OSM_DIR}/circle48.geojson"
CLIP="${OSM_DIR}/mga48.osm.pbf"

# Geofabrik publishes a .md5 beside every extract; a truncated download that
# still parses would poison every downstream artifact silently.
log "downloading ${GEOFABRIK_URL}"
CURL_OPTS=(--fail --location --show-error --silent --retry 4 --retry-delay 5 --retry-connrefused)
if [ -f "$PBF" ] && [ "$FORCE" != "--force" ]; then
  # Only fetch when the server has something newer.
  CURL_OPTS+=(--time-cond "$PBF")
fi
curl "${CURL_OPTS[@]}" -o "$TMP" "$GEOFABRIK_URL"

if [ ! -s "$TMP" ]; then
  log "no newer extract available; keeping $(basename "$PBF")"
  rm -f "$TMP"
else
  log "verifying checksum"
  if curl --fail --location --silent --show-error -o "${TMP}.md5" "${GEOFABRIK_URL}.md5"; then
    expected="$(awk '{print $1}' "${TMP}.md5")"
    actual="$(md5sum "$TMP" | awk '{print $1}')"
    [ "$expected" = "$actual" ] || die "checksum mismatch: expected ${expected}, got ${actual}"
    log "checksum ok"
  else
    # A missing .md5 is not fatal — osmium's own parse below still catches a
    # truncated file — but it is worth saying out loud.
    log "WARNING: could not fetch ${GEOFABRIK_URL}.md5; skipping checksum"
  fi
  rm -f "${TMP}.md5"

  osmium fileinfo "$TMP" >/dev/null || die "downloaded file is not a valid PBF"
  publish "$TMP" "$PBF"
fi

[ -f "$PBF" ] || die "no extract on disk and none downloaded"

# The circle polygon is generated rather than committed so the radius stays a
# single constant in common/geo.py.
if [ ! -f "$CIRCLE" ]; then
  log "generating the curation circle"
  (cd "$REPO_ROOT" && python3 scripts/circle48.py --output "$CIRCLE")
fi

log "clipping the 48.3 km circle for QA"
osmium extract --overwrite --polygon "$CIRCLE" "$PBF" -o "${CLIP}.tmp"
publish "${CLIP}.tmp" "$CLIP"

log "extract statistics"  # long-form flags only: -e is --expressions on tags-filter and --extended here
osmium fileinfo --extended "$CLIP" | sed -n '1,40p' >&2

# POIs for the ingest job: nodes and ways carrying the tags the taxonomy maps.
# Doing the filtering with osmium keeps a full OSM parser out of Python.
POI_PBF="${OSM_DIR}/pois.osm.pbf"
log "filtering POI tags"
osmium tags-filter --overwrite "$PBF" \
  nwr/amenity nwr/shop nwr/tourism nwr/leisure nwr/office nwr/healthcare \
  nwr/craft nwr/aeroway=aerodrome nwr/public_transport=station nwr/place \
  nwr/highway=milestone nwr/junction=roundabout \
  -o "${POI_PBF}.tmp"
publish "${POI_PBF}.tmp" "$POI_PBF"

log "exporting POIs as GeoJSON-seq"
# Ways and relations become their centroid, which is what a POI marker wants.
osmium export "$POI_PBF" \
  --overwrite \
  --geometry-types=point,polygon \
  --output-format=geojsonseq \
  --add-unique-id=type_id \
  -o "${EXPORT_DIR}/osm_pois.geojsonseq.tmp"
publish "${EXPORT_DIR}/osm_pois.geojsonseq.tmp" "${EXPORT_DIR}/osm_pois.geojsonseq"

log "exporting the circle's road network for KPIs"
osmium tags-filter --overwrite "$CLIP" w/highway -o "${OSM_DIR}/roads.osm.pbf.tmp"
publish "${OSM_DIR}/roads.osm.pbf.tmp" "${OSM_DIR}/roads.osm.pbf"
osmium export "${OSM_DIR}/roads.osm.pbf" --overwrite --geometry-types=linestring \
  --output-format=geojsonseq -o "${EXPORT_DIR}/roads.geojsonseq.tmp"
publish "${EXPORT_DIR}/roads.geojsonseq.tmp" "${EXPORT_DIR}/roads.geojsonseq"

SOURCE_TIMESTAMP="$(osmium fileinfo -g header.option.osmosis_replication_timestamp "$PBF")"
(cd "$REPO_ROOT" && python3 -m pipeline.status "$METADATA_DIR" osm source --source-timestamp "$SOURCE_TIMESTAMP")
log "done"
