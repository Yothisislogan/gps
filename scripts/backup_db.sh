#!/usr/bin/env sh
# Dump PostGIS to data/backups/, keeping a fortnight of daily dumps.
#
# What is worth backing up here is precisely what cannot be rebuilt: the
# gazetteer's curated and "donde fue" landmarks, the learned address aliases,
# moderation decisions and field surveys. Everything else — tiles, the routing
# graph, the Overture and OSM imports — is reproducible from source data.

set -eu

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA_DIR="${NICANAV_DATA_DIR:-${REPO_ROOT}/data}"
BACKUP_DIR="${DATA_DIR}/backups"
KEEP_DAYS="${NICANAV_BACKUP_KEEP_DAYS:-14}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="${BACKUP_DIR}/nicanav-${STAMP}.sql.gz"

mkdir -p "$BACKUP_DIR"

echo "dumping to ${OUT}"
docker compose -f "${REPO_ROOT}/infra/docker-compose.yml" --env-file "${REPO_ROOT}/infra/.env" \
  exec -T postgis pg_dump -U "${NICANAV_PG_USER:-nicanav}" -d "${NICANAV_PG_DB:-nicanav}" \
  | gzip -9 > "${OUT}.tmp"

# A dump that failed half way through still leaves a plausible-looking gzip, so
# check it decompresses and carries the tables that matter before publishing it.
gzip -t "${OUT}.tmp" || { echo "backup is not a valid gzip; discarding" >&2; rm -f "${OUT}.tmp"; exit 1; }
if ! zcat "${OUT}.tmp" | grep -q "CREATE TABLE public.gazetteer"; then
  echo "backup does not contain the gazetteer table; discarding" >&2
  rm -f "${OUT}.tmp"
  exit 1
fi
mv -f "${OUT}.tmp" "$OUT"
echo "backup ok: $(du -h "$OUT" | cut -f1)"

find "$BACKUP_DIR" -name 'nicanav-*.sql.gz' -mtime "+${KEEP_DAYS}" -print -delete

# Offsite copy, when configured. Losing the box and the backups together is the
# only failure mode here that cannot be recovered from.
if [ -n "${NICANAV_BACKUP_RCLONE_TARGET:-}" ] && command -v rclone >/dev/null 2>&1; then
  echo "copying to ${NICANAV_BACKUP_RCLONE_TARGET}"
  rclone copy "$OUT" "$NICANAV_BACKUP_RCLONE_TARGET"
fi
