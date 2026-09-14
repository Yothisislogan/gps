# Recover an interrupted database installation

The September 14 server output establishes that the locale correction worked,
but migration 001 then failed at the POI search index with
`function nicanav_unaccent(text) does not exist`. PostgreSQL restarted and became
healthy while skipping initialization because PGDATA was already initialized.
The application schema still needs installation. These observations came from
terminal output supplied by the operator; no remote shell was used here.

PostgreSQL 17 restricts function search paths during index maintenance. Our
normalization function called its helper without a schema prefix. Migration 001
now qualifies that call; migration 005 repairs the function on databases that
already completed installation. The function's results are unchanged.
See the [PostgreSQL 17 compatibility notes](https://www.postgresql.org/docs/17/release-17.html).

CI runs complete schema installation and correction tests on both
`postgis/postgis:16-3.4` and the deployment image, `postgis/postgis:17-3.5`.
Regression tests exercise normalization with a restricted search path,
populated index rebuilds, repair of the old function without losing rows, and
API database readiness when the server accepts connections but tables are missing.

## Operator recovery

1. Leave the now-running PostGIS container and its data volume in place. The
   failed routing container remains stopped until a valid extract is staged.
2. Preserve `/opt/nicanav/infra/.env`, the existing Compose configuration and
   the untracked `/opt/nicanav/web/config.js` in a private backup directory.
3. Check for tracked local changes. Fetch `codex/nicanav-reliability` and check
   out the exact commit whose PostgreSQL 17 CI passed. Do not use `main`, which
   still contains only the repository skeleton. Keep the original branch and
   local runtime configuration available for recovery.
4. Before SQL changes, take a custom-format `pg_dump` of the database using the
   existing container's `POSTGRES_USER` and `POSTGRES_DB`. Verify that
   `pg_restore --list` can read the saved archive. Keep this local recovery
   snapshot separate from the later offsite-backup rollout gate.
5. Apply **every** numbered file in `db/migrations/`, in order, using `psql -X
   -v ON_ERROR_STOP=1`. Stop at the first error. Migration 005 alone cannot
   recover the empty schema left by a rolled-back migration 001. Do not delete
   PGDATA or reset the database volume to trigger initialization again.
6. Verify that `public.poi` and `public.release_revision` can be queried, and
   that `public.nicanav_normalize('Café Ñandú')` returns `cafe nandu` with
   `search_path` set to `pg_catalog, pg_temp`.

This repairs the schema only. Routing data, POI imports, search indexing, the
frontend build and HTTPS staging verification remain necessary. Keep the
temporary PostGIS recovery override until its resource limits have been carried
into the staging configuration. The current API's database health probe checks
the required schema as well as connectivity; Docker's `pg_isready` health check
continues to describe the PostgreSQL server itself.
