#!/usr/bin/env sh
# Set up a development environment, idempotently.
#
#   ./scripts/bootstrap_dev.sh
#
# Installs the Python side, reports which external tools the *pipeline* needs
# and which are missing, then runs the tests. A missing external tool is not a
# failure: the whole test suite runs offline without osmium, Java, tippecanoe or
# Docker — those are only needed to build real data.

set -eu

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

VENV="${NICANAV_VENV:-.venv}"

echo "==> python environment"
if [ ! -d "$VENV" ]; then
  python3 -m venv "$VENV"
  echo "    created $VENV"
else
  echo "    reusing $VENV"
fi

# shellcheck disable=SC1090
. "$VENV/bin/activate"
python3 -m pip install --quiet --upgrade pip
python3 -m pip install --quiet -r requirements/dev.txt
echo "    $(python3 --version), dependencies installed"

echo
echo "==> external tools (needed to build data, not to run the tests)"
missing=""
check() {
  name="$1"
  hint="$2"
  if command -v "$name" >/dev/null 2>&1; then
    version="$("$name" --version 2>&1 | head -n 1 || echo '?')"
    printf '    %-12s %s\n' "$name" "$version"
  else
    printf '    %-12s MISSING — %s\n' "$name" "$hint"
    missing="$missing $name"
  fi
}

check osmium     "apt install osmium-tool"
check java       "apt install openjdk-21-jre-headless  (Planetiler needs Java 21+)"
check tippecanoe "build from github.com/felt/tippecanoe"
check docker     "docs.docker.com/engine/install"
check node       "only for the navigation-geometry tests"

if [ -n "$missing" ]; then
  echo
  echo "    Missing:${missing}"
  echo "    The test suite still runs; you just cannot build tiles or a routing graph."
fi

echo
echo "==> the curation circle"
python3 scripts/circle48.py --output data/osm/circle48.geojson >/dev/null

echo
echo "==> tests"
python3 -m pytest -q -m "not network and not docker"

if command -v node >/dev/null 2>&1; then
  echo
  echo "==> navigation geometry"
  node --test "tests/js/*.test.mjs" 2>&1 | tail -n 5
fi

echo
echo "Ready. 'make help' lists the rest."
