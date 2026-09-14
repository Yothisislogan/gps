-- Existing installations also need the PostgreSQL 17 index-maintenance fix.
-- Results are unchanged, so existing indexes do not need rebuilding.
BEGIN;

CREATE OR REPLACE FUNCTION public.nicanav_normalize(text)
  RETURNS text
  LANGUAGE sql
  IMMUTABLE
  PARALLEL SAFE
  STRICT
AS $$ SELECT pg_catalog.lower(public.nicanav_unaccent($1)) $$;

COMMIT;
