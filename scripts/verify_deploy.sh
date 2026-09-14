#!/usr/bin/env bash
# Post-deploy verification.
#
# Every check here exists because the corresponding failure is SILENT: the stack
# comes up, the health endpoints are green, and the product is subtly broken.
# Run it after every deploy and after every Valhalla or nginx config change.
#
#   ./scripts/verify_deploy.sh [base-url]

set -euo pipefail

# Must match the compose file's published port (NICANAV_HTTP_PORT, default
# 8400). A verify run against the wrong port fails every check and looks like
# a broken deploy.
BASE="${1:-http://127.0.0.1:${NICANAV_HTTP_PORT:-8400}}"
VALHALLA="${NICANAV_VALHALLA_URL:-http://localhost:8002}"
FAILURES=0

pass() { printf '  \033[32mok\033[0m   %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; FAILURES=$((FAILURES + 1)); }
note() { printf '  \033[33m--\033[0m   %s\n' "$1"; }

echo "== tiles: byte-range serving =="
# The PMTiles failure mode: with gzip on, nginx answers 200 with the whole
# archive, chunked, no Content-Length, weak ETag — and pmtiles.js reports what
# looks like a client bug. These three headers are the difference.
HEADERS="$(curl -sS -D - -o /dev/null -r 0-1023 "${BASE}/tiles/base.pmtiles" 2>/dev/null || true)"
if printf '%s' "$HEADERS" | grep -qi '^HTTP/[0-9.]* 206'; then
  pass "206 Partial Content"
else
  fail "expected 206 for a Range request (gzip is probably on for .pmtiles)"
fi
printf '%s' "$HEADERS" | grep -qi '^content-range:' && pass "Content-Range present" \
  || fail "no Content-Range header"
if printf '%s' "$HEADERS" | grep -qi '^etag: *"'; then
  pass "strong ETag"
elif printf '%s' "$HEADERS" | grep -qi '^etag: *W/'; then
  fail "ETag is weak (W/...) — pmtiles.js discards it and cannot detect a rebuilt archive"
else
  fail "no ETag"
fi
printf '%s' "$HEADERS" | grep -qi '^access-control-expose-headers:.*etag' \
  && pass "ETag exposed to the browser" \
  || fail "Access-Control-Expose-Headers must include ETag or the browser cannot read it"
printf '%s' "$HEADERS" | grep -qi '^content-encoding: *gzip' \
  && fail "tiles are being gzipped — this breaks range requests" \
  || pass "not gzipped"

echo "== routing: Spanish narration =="
# An unsupported language tag falls back to en-US silently. A stack that
# navigates in English for Nicaraguan drivers is a failure nobody's health check
# would catch.
ROUTE="$(curl -sS -X POST "${VALHALLA}/route" -H 'Content-Type: application/json' -d '{
  "locations":[{"lat":12.1415,"lon":-86.1682},{"lat":12.1150,"lon":-86.2504}],
  "costing":"auto","language":"es-ES","units":"kilometers"}' 2>/dev/null || true)"
if [ -z "$ROUTE" ]; then
  fail "no response from ${VALHALLA}/route"
elif printf '%s' "$ROUTE" | grep -qiE '"instruction": *"(Drive|Turn|Head|Continue|Keep)'; then
  fail "instructions came back in English — es-ES is not in the server's locale set"
elif printf '%s' "$ROUTE" | grep -qiE '"instruction": *"(Conduzca|Gire|Siga|Continúe|Mantén|Tome)'; then
  pass "instructions are in Spanish"
else
  note "could not classify the narration language; check by hand"
fi

echo "== routing: the Nicaraguan speed table is loaded =="
# A malformed default_speeds.json is ignored with only a log warning, so the
# only visible symptom is that tuning did nothing.
if printf '%s' "$ROUTE" | grep -q '"summary"'; then
  pass "router returns a trip"
else
  fail "router returned no trip for MGA -> Managua"
fi
note "confirm no 'unable to parse' warning: docker compose logs valhalla | grep -i speed"

echo "== routing: the graph is current =="
STATUS="$(curl -sS "${VALHALLA}/status" 2>/dev/null || true)"
MODIFIED="$(printf '%s' "$STATUS" | sed -n 's/.*"tileset_last_modified":[[:space:]]*\([0-9]*\).*/\1/p')"
if [ -n "$MODIFIED" ]; then
  AGE_DAYS=$(( ( $(date -u +%s) - MODIFIED ) / 86400 ))
  [ "$AGE_DAYS" -le 2 ] && pass "tileset is ${AGE_DAYS}d old" \
    || fail "tileset is ${AGE_DAYS}d old — the nightly rebuild is not landing"
else
  fail "no tileset_last_modified in /status"
fi

echo "== api =="
HEALTH="$(curl -sS "${BASE}/api/healthz" 2>/dev/null || true)"
printf '%s' "$HEALTH" | grep -q '"status": *"ok"' && pass "api healthy" || fail "api unhealthy"
for dep in valhalla meili db; do
  printf '%s' "$HEALTH" | grep -q "\"${dep}\": *{\"ok\": *true" && pass "${dep} reachable" \
    || note "${dep} reports not ok (the app degrades, but check it)"
done

echo "== search =="
SEARCH="$(curl -sS "${BASE}/api/search?q=gasolinera&lat=12.1415&lon=-86.1682" 2>/dev/null || true)"
printf '%s' "$SEARCH" | grep -q '"hits"' && pass "search responds" || fail "search failed"

echo "== geocoding: the Nicaraguan address grammar =="
GEO="$(curl -sS --get "${BASE}/api/geocode" --data-urlencode 'q=De la Rotonda El Güegüense, 2c al sur, 1c abajo' 2>/dev/null || true)"
printf '%s' "$GEO" | grep -q '"relative"' \
  && pass "relative addresses parse" \
  || fail "relative address did not parse — the gazetteer is probably empty"

echo
if [ "$FAILURES" -eq 0 ]; then
  echo "all checks passed"
  exit 0
fi
echo "${FAILURES} check(s) failed"
exit 1
