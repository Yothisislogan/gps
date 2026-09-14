#!/usr/bin/env bash
# Ubuntu staging host: install pipeline tools and build the local web assets.
# This does not start application containers or change deployment credentials.
set -euo pipefail

[ "$EUID" -eq 0 ] || { echo "Run this script as root." >&2; exit 1; }
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
for tool in apt-get docker systemd-run git; do
  command -v "$tool" >/dev/null || { echo "Missing prerequisite: $tool" >&2; exit 1; }
done
docker info >/dev/null

# List services needing a restart; do not let needrestart restart shared apps.
export NEEDRESTART_MODE=l
apt-get update -qq
apt-get install -y --no-install-recommends \
  jq build-essential libsqlite3-dev zlib1g-dev ca-certificates

prefix=/opt/nicanav-tools
install -d -m 755 "$prefix/bin"
if [ ! -x "$prefix/bin/tippecanoe" ] || \
   ! "$prefix/bin/tippecanoe" --version 2>&1 | grep -qx 'tippecanoe v2.78.0'; then
  source_dir="$(mktemp -d "$prefix/tippecanoe-build.XXXXXX")"
  git clone --depth 1 --branch 2.78.0 \
    https://github.com/felt/tippecanoe.git "$source_dir/source"
  test "$(git -C "$source_dir/source" rev-parse HEAD)" = \
    2d548bed0623005ab1ae619595ef4cfe1c745d3e
  echo "Compiling tippecanoe with a two-CPU quota and 3 GiB memory limit."
  if ! systemd-run --wait --pipe --collect \
    -p CPUQuota=200% -p MemoryMax=3G -p Nice=10 \
    /usr/bin/make -C "$source_dir/source" -j2 tippecanoe \
    > "$source_dir/build.log" 2>&1; then
    tail -n 40 "$source_dir/build.log"
    exit 1
  fi
  install -m 755 "$source_dir/source/tippecanoe" "$prefix/bin/tippecanoe"
fi

# Node stays in a disposable container; use the repository's locked packages.
docker run --rm --cpus=2 --memory=2g --memory-swap=2g --pids-limit=256 \
  --mount "type=bind,source=$root,target=/app" --workdir /app \
  node:22-bookworm-slim \
  sh -c 'npm ci --ignore-scripts && npm run build && node scripts/check_offline_assets.mjs'

jq --version
"$prefix/bin/tippecanoe" --version
echo "Tools and web assets are ready. Add /opt/nicanav-tools/bin to the pipeline PATH."
echo "Runtime configuration and HTTPS still need to be prepared before serving the app."
