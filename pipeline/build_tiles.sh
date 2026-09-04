#!/usr/bin/env bash
# Build the vector tiles: base.pmtiles (Planetiler, OpenMapTiles schema) and,
# when a POI export exists, pois.pmtiles (tippecanoe).
#
# Both are published atomically. nginx serves the previous archive throughout
# the build, and pmtiles.js notices the swap via the ETag.
#
#   ./pipeline/build_tiles.sh [--base-only|--pois-only]

SCRIPT_NAME=build_tiles
. "$(cd "$(dirname "$0")" && pwd)/lib.sh"

WHAT="${1:-all}"

# The offline archive covers only the curation circle. base.pmtiles is the whole
# country and far too large to hold in a phone's Cache Storage; the circle is the
# area people actually drive, and it is the area that gets field-verified.

ensure_dirs
require java "apt install openjdk-21-jre-headless (Planetiler needs Java 21+)"
require_free_space 4096

PBF="${OSM_DIR}/nicaragua-latest.osm.pbf"
BASE="${TILES_DIR}/base.pmtiles"
POIS="${TILES_DIR}/pois.pmtiles"
POI_GEOJSON="${EXPORT_DIR}/pois.geojsonseq"
SOURCES_DIR="${DATA_DIR}/sources"

build_base() {
  [ -f "$PBF" ] || die "no OSM extract at ${PBF}; run pipeline/fetch_osm.sh first"

  if [ ! -f "$PLANETILER_JAR" ]; then
    log "downloading planetiler ${PLANETILER_VERSION}"
    mkdir -p "$(dirname "$PLANETILER_JAR")"
    local base="https://github.com/onthegomap/planetiler/releases/download/v${PLANETILER_VERSION}"
    curl -fsSL -o "${PLANETILER_JAR}.tmp" "${base}/planetiler.jar"
    curl -fsSL -o "${PLANETILER_JAR}.sha256" "${base}/planetiler.jar.sha256"
    ( cd "$(dirname "$PLANETILER_JAR")" && \
      echo "$(cat "$(basename "$PLANETILER_JAR").sha256")  $(basename "${PLANETILER_JAR}.tmp")" | sha256sum -c - ) \
      || die "planetiler checksum mismatch"
    publish "${PLANETILER_JAR}.tmp" "$PLANETILER_JAR"
  fi

  # The OpenMapTiles profile always needs ~1 GB of non-OSM sources (ocean
  # polygons, Natural Earth, lake centrelines). They change rarely, so they are
  # fetched once into data/sources and reused; on a metered link this is the
  # difference between a 2-minute nightly build and a 20-minute one.
  mkdir -p "$SOURCES_DIR"
  if [ ! -f "${SOURCES_DIR}/water-polygons-split-3857.zip" ]; then
    log "pre-staging Planetiler's static sources (~1 GB, once)"
    java -Xmx1g -jar "$PLANETILER_JAR" --only-download --download-dir="$SOURCES_DIR" \
      || die "source download failed"
  fi

  # Memory: Planetiler asks for ~0.5x the input size as heap. Nicaragua's
  # extract is ~60 MB, so 2 GB is generous and leaves the box's RAM for
  # Valhalla, Postgres and Meilisearch.
  #
  # --nodemap-type=sortedtable is documented as ideal for small extracts (12
  # bytes/node, exact) and with --storage=ram it keeps the whole node map in
  # heap: the repo's own config example still claims sortedtable is the default,
  # but it is not — sparsearray is, and it is tuned for planet builds.
  #
  # Note there is no `--download-only`; the flag is `--only-download`, and
  # Planetiler ignores unknown flags silently, so a typo here would produce a
  # full build instead of an error.
  log "building base.pmtiles"
  java -Xmx"${PLANETILER_XMX:-2g}" -jar "$PLANETILER_JAR" \
    --osm-path="$PBF" \
    --output="${BASE}.tmp" \
    --force \
    --languages=es,en \
    --nodemap-type=sortedtable \
    --storage=ram \
    --download-dir="$SOURCES_DIR" \
    --threads="${PLANETILER_THREADS:-$(nproc)}" \
    --building-merge-z13=false \
    || die "planetiler failed"

  # A PMTiles archive starts with the literal magic "PMTiles"; anything else
  # means the build produced something nginx would happily serve as garbage.
  head -c 7 "${BASE}.tmp" | grep -q "PMTiles" || die "output is not a PMTiles archive"
  publish "${BASE}.tmp" "$BASE"
}

build_pois() {
  if [ ! -s "$POI_GEOJSON" ]; then
    log "no POI export at ${POI_GEOJSON}; skipping pois.pmtiles"
    return 0
  fi
  require tippecanoe "build from github.com/felt/tippecanoe"

  # -zg picks the max zoom from feature density. --drop-densest-as-needed keeps
  # tiles under the size limit by thinning the densest cells rather than failing
  # the build — but per-feature minzoom hints in the GeoJSON (written by the
  # export job) keep the categories drivers need visible at low zoom.
  log "building pois.pmtiles"
  tippecanoe \
    --output="${POIS}.tmp" \
    --force \
    --layer=poi \
    --name="nicanav POIs" \
    --attribution="© OpenStreetMap contributors, Overture Maps Foundation" \
    -zg \
    --minimum-zoom=6 \
    --maximum-zoom=14 \
    --drop-densest-as-needed \
    --extend-zooms-if-still-dropping \
    --no-tile-size-limit \
    "$POI_GEOJSON" \
    || die "tippecanoe failed"

  head -c 7 "${POIS}.tmp" | grep -q "PMTiles" || die "tippecanoe output is not a PMTiles archive"
  publish "${POIS}.tmp" "$POIS"
}

build_circle() {
  [ -f "$PBF" ] || die "no OSM extract at ${PBF}; run pipeline/fetch_osm.sh first"
  local circle="${TILES_DIR}/circle.pmtiles"
  local bounds
  # The same 48.3 km circle the QA clip uses, as a bounding box Planetiler
  # understands (west,south,east,north).
  bounds="$(cd "$REPO_ROOT" && python3 -c "
from common.geo import MGA_LAT, MGA_LON, CURATION_RADIUS_M, bbox_around
w, s, e, n = bbox_around(MGA_LAT, MGA_LON, CURATION_RADIUS_M)
print(f'{w:.5f},{s:.5f},{e:.5f},{n:.5f}')")"
  log "building circle.pmtiles for offline use (bounds ${bounds})"

  # Housenumbers are the single biggest z14 contributor and are close to useless
  # in a country that does not use street numbers — dropping them keeps the
  # download something a Claro data plan can absorb.
  java -Xmx"${PLANETILER_XMX:-2g}" -jar "$PLANETILER_JAR"     --osm-path="$PBF"     --output="${circle}.tmp"     --force     --languages=es,en     --bounds="$bounds"     --exclude-layers=housenumber     --nodemap-type=sortedtable     --storage=ram     --download-dir="$SOURCES_DIR"     --building-merge-z13=false     || die "planetiler failed for the circle archive"

  head -c 7 "${circle}.tmp" | grep -q "PMTiles" || die "circle output is not a PMTiles archive"
  publish "${circle}.tmp" "$circle"
  log "offline archive size: $(du -h "$circle" | cut -f1) — shown to users before they download"
}

case "$WHAT" in
  --base-only)   build_base ;;
  --pois-only)   build_pois ;;
  --circle-only) build_circle ;;
  all)           build_base; build_pois ;;
  *)             die "usage: $0 [--base-only|--pois-only|--circle-only]" ;;
esac

log "done"
