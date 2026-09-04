-- nicanav core schema.
--
-- Design rules that the rest of the codebase depends on:
--   * Provenance is never blended.  `poi` is the merged, published view;
--     `poi_source` keeps one row per contributing source with its raw payload,
--     so ODbL (OSM), CDLA (Overture/Meta/Microsoft), Apache-2.0 (Foursquare)
--     and CC0 (AllThePlaces) data stay separable and re-attributable.
--   * Geometry is always geometry(Point,4326); distance filters use
--     ST_DWithin(geom::geography, ..., metres) so radii are real metres.
--   * Nothing here is generated at query time that the nightly pipeline could
--     precompute — the API must stay fast on a small box.

BEGIN;

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS unaccent;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- unaccent() is STABLE, so it cannot be used in an index expression directly.
-- This IMMUTABLE wrapper is the one every fuzzy-name index below uses.
CREATE OR REPLACE FUNCTION nicanav_unaccent(text)
  RETURNS text
  LANGUAGE sql
  IMMUTABLE
  PARALLEL SAFE
  STRICT
AS $$ SELECT public.unaccent('public.unaccent'::regdictionary, $1) $$;

-- Normalised comparison key: accent-folded, lower-cased.  Mirrors
-- common.text.normalize() closely enough for SQL-side blocking.
CREATE OR REPLACE FUNCTION nicanav_normalize(text)
  RETURNS text
  LANGUAGE sql
  IMMUTABLE
  PARALLEL SAFE
  STRICT
AS $$ SELECT lower(nicanav_unaccent($1)) $$;

CREATE OR REPLACE FUNCTION nicanav_touch_updated_at()
  RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END $$;

-- --------------------------------------------------------------------------
-- Points of interest (published, conflated view)
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS poi (
  id            uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  name          text NOT NULL,
  name_alt      text[] NOT NULL DEFAULT '{}',
  category      text NOT NULL,                 -- canonical id from docs/taxonomy.csv
  subcategory   text,
  cuisine       text[] NOT NULL DEFAULT '{}',
  geom          geometry(Point, 4326) NOT NULL,
  address_text  text,                          -- as written locally (relative address)
  city          text,
  phone         text,
  whatsapp      text,
  website       text,
  facebook      text,
  instagram     text,
  opening_hours text,                          -- OSM opening_hours syntax
  price_level   smallint CHECK (price_level BETWEEN 0 AND 4),
  status        text NOT NULL DEFAULT 'unverified'
                CHECK (status IN ('open', 'closed', 'unverified')),
  confidence    real NOT NULL DEFAULT 0.0 CHECK (confidence BETWEEN 0 AND 1),
  popularity    real NOT NULL DEFAULT 0.0,
  verified_at   timestamptz,
  verified_by   text,
  sources       jsonb NOT NULL DEFAULT '{}'::jsonb,  -- {osm_id, overture_gers_id, fsq_id, survey_id}
  in_circle     boolean NOT NULL DEFAULT false,      -- inside the 48.3 km curation circle
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS poi_geom_idx        ON poi USING gist (geom);
CREATE INDEX IF NOT EXISTS poi_geog_idx        ON poi USING gist ((geom::geography));
CREATE INDEX IF NOT EXISTS poi_name_trgm_idx   ON poi USING gin (nicanav_normalize(name) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS poi_category_idx    ON poi (category);
CREATE INDEX IF NOT EXISTS poi_status_idx      ON poi (status) WHERE status <> 'closed';
CREATE INDEX IF NOT EXISTS poi_gers_idx        ON poi ((sources ->> 'overture_gers_id'));
CREATE INDEX IF NOT EXISTS poi_osm_idx         ON poi ((sources ->> 'osm_id'));

DROP TRIGGER IF EXISTS poi_touch ON poi;
CREATE TRIGGER poi_touch BEFORE UPDATE ON poi
  FOR EACH ROW EXECUTE FUNCTION nicanav_touch_updated_at();

-- One row per source contribution.  Never deleted on re-import: the monthly
-- Overture refresh updates in place, keyed on (source, source_id).
CREATE TABLE IF NOT EXISTS poi_source (
  id          bigserial PRIMARY KEY,
  poi_id      uuid REFERENCES poi(id) ON DELETE CASCADE,
  source      text NOT NULL CHECK (source IN ('osm', 'overture', 'survey', 'user', 'wikidata')),
  source_id   text NOT NULL,
  name        text,
  category    text,
  geom        geometry(Point, 4326),
  license     text NOT NULL,               -- 'ODbL-1.0', 'CDLA-Permissive-2.0', 'Apache-2.0', 'CC0-1.0'
  confidence  real,
  raw         jsonb NOT NULL DEFAULT '{}'::jsonb,
  fetched_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (source, source_id)
);

CREATE INDEX IF NOT EXISTS poi_source_poi_idx  ON poi_source (poi_id);
CREATE INDEX IF NOT EXISTS poi_source_geom_idx ON poi_source USING gist (geom);
CREATE INDEX IF NOT EXISTS poi_source_name_trgm_idx
  ON poi_source USING gin (nicanav_normalize(coalesce(name, '')) gin_trgm_ops);

-- Candidate pairs the conflator could not decide (score 0.60-0.85).
CREATE TABLE IF NOT EXISTS poi_match_queue (
  id          bigserial PRIMARY KEY,
  left_key    text NOT NULL,            -- 'source:source_id'
  right_key   text NOT NULL,
  score       real NOT NULL,
  name_score  real,
  distance_m  real,
  category_ok boolean,
  decision    text NOT NULL DEFAULT 'pending'
              CHECK (decision IN ('pending', 'merge', 'separate')),
  decided_by  text,
  decided_at  timestamptz,
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (left_key, right_key)
);

CREATE INDEX IF NOT EXISTS poi_match_queue_pending_idx
  ON poi_match_queue (score DESC) WHERE decision = 'pending';

CREATE TABLE IF NOT EXISTS poi_photo (
  id        bigserial PRIMARY KEY,
  poi_id    uuid NOT NULL REFERENCES poi(id) ON DELETE CASCADE,
  url       text NOT NULL,
  credit    text,
  license   text,
  taken_at  timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS poi_photo_poi_idx ON poi_photo (poi_id);

CREATE TABLE IF NOT EXISTS poi_flag (
  id         bigserial PRIMARY KEY,
  poi_id     uuid NOT NULL REFERENCES poi(id) ON DELETE CASCADE,
  kind       text NOT NULL,            -- 'cerrado', 'se_movio', 'horario_mal', 'duplicado'
  note       text,
  status     text NOT NULL DEFAULT 'pending'
             CHECK (status IN ('pending', 'accepted', 'rejected')),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS poi_flag_pending_idx ON poi_flag (created_at DESC) WHERE status = 'pending';

-- --------------------------------------------------------------------------
-- Gazetteer: the landmark table that powers relative addressing
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS gazetteer (
  id          uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  name        text NOT NULL,
  name_alt    text[] NOT NULL DEFAULT '{}',
  kind        text NOT NULL,     -- rotonda, semaforo, mercado, iglesia, colegio,
                                 -- gasolinera, barrio, reparto, colonia, residencial,
                                 -- puente, monumento, edificio, empresa, otro
  geom        geometry(Point, 4326) NOT NULL,
  city        text,
  former      boolean NOT NULL DEFAULT false,   -- "donde fue el Cine Cabrera"
  era         text,                             -- when it existed, free text
  popularity  real NOT NULL DEFAULT 0.0,        -- how often people navigate by it
  source      text NOT NULL DEFAULT 'osm',
  source_id   text,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS gazetteer_geom_idx      ON gazetteer USING gist (geom);
CREATE INDEX IF NOT EXISTS gazetteer_name_trgm_idx ON gazetteer USING gin (nicanav_normalize(name) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS gazetteer_kind_idx      ON gazetteer (kind);
CREATE INDEX IF NOT EXISTS gazetteer_former_idx    ON gazetteer (former) WHERE former;
CREATE UNIQUE INDEX IF NOT EXISTS gazetteer_source_uniq
  ON gazetteer (source, source_id) WHERE source_id IS NOT NULL;

DROP TRIGGER IF EXISTS gazetteer_touch ON gazetteer;
CREATE TRIGGER gazetteer_touch BEFORE UPDATE ON gazetteer
  FOR EACH ROW EXECUTE FUNCTION nicanav_touch_updated_at();

-- Learned address strings.  Every time a user drags the pin to correct a
-- geocode, the corrected string -> point pair lands here and outranks the
-- parser on the next lookup.  Over time this becomes the address database.
CREATE TABLE IF NOT EXISTS alias (
  id          uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  text        text NOT NULL,
  text_norm   text GENERATED ALWAYS AS (nicanav_normalize(text)) STORED,
  geom        geometry(Point, 4326) NOT NULL,
  poi_id      uuid REFERENCES poi(id) ON DELETE SET NULL,
  confidence  real NOT NULL DEFAULT 0.5 CHECK (confidence BETWEEN 0 AND 1),
  hits        integer NOT NULL DEFAULT 1,
  created_by  text,
  status      text NOT NULL DEFAULT 'pending'
              CHECK (status IN ('pending', 'approved', 'rejected')),
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS alias_norm_trgm_idx ON alias USING gin (text_norm gin_trgm_ops);
CREATE INDEX IF NOT EXISTS alias_geom_idx      ON alias USING gist (geom);
CREATE INDEX IF NOT EXISTS alias_approved_idx  ON alias (text_norm) WHERE status = 'approved';

DROP TRIGGER IF EXISTS alias_touch ON alias;
CREATE TRIGGER alias_touch BEFORE UPDATE ON alias
  FOR EACH ROW EXECUTE FUNCTION nicanav_touch_updated_at();

-- Km-post calibration: photographed milestones tie a carretera's chainage to
-- reality, because OSM geometry length and the ministry's signs disagree.
CREATE TABLE IF NOT EXISTS kmpost (
  id          bigserial PRIMARY KEY,
  highway_key text NOT NULL,          -- 'carretera_a_masaya', 'carretera_norte', ...
  km          real NOT NULL,
  geom        geometry(Point, 4326) NOT NULL,
  source      text NOT NULL DEFAULT 'osm',   -- 'osm' (highway=milestone) | 'survey'
  photo_url   text,
  verified_at timestamptz,
  UNIQUE (highway_key, km, source)
);
CREATE INDEX IF NOT EXISTS kmpost_highway_idx ON kmpost (highway_key, km);
CREATE INDEX IF NOT EXISTS kmpost_geom_idx    ON kmpost USING gist (geom);

-- --------------------------------------------------------------------------
-- Live operational data
-- --------------------------------------------------------------------------

-- Closures are injected into every /route call as exclude_polygons while active.
CREATE TABLE IF NOT EXISTS closure (
  id         uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  geom       geometry(Polygon, 4326) NOT NULL,
  reason     text NOT NULL,
  note       text,
  starts_at  timestamptz NOT NULL DEFAULT now(),
  ends_at    timestamptz,
  active     boolean NOT NULL DEFAULT true,
  source     text NOT NULL DEFAULT 'admin',   -- 'admin' | 'user' | 'import'
  created_by text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS closure_geom_idx ON closure USING gist (geom);
-- The hot path: "give me every closure in force right now".
CREATE INDEX IF NOT EXISTS closure_active_idx ON closure (ends_at) WHERE active;

CREATE TABLE IF NOT EXISTS report (
  id         uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  kind       text NOT NULL,
  geom       geometry(Point, 4326) NOT NULL,
  note       text,
  photo_url  text,
  status     text NOT NULL DEFAULT 'pending'
             CHECK (status IN ('pending', 'accepted', 'rejected', 'expired')),
  votes      integer NOT NULL DEFAULT 1,
  expires_at timestamptz,
  client_id  text,                      -- opaque, salted; never a phone or email
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS report_geom_idx    ON report USING gist (geom);
CREATE INDEX IF NOT EXISTS report_pending_idx ON report (created_at DESC) WHERE status = 'pending';

-- Moderation queue for user-suggested places.
CREATE TABLE IF NOT EXISTS poi_suggestion (
  id         uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  payload    jsonb NOT NULL,
  geom       geometry(Point, 4326) NOT NULL,
  status     text NOT NULL DEFAULT 'pending'
             CHECK (status IN ('pending', 'accepted', 'rejected')),
  poi_id     uuid REFERENCES poi(id) ON DELETE SET NULL,
  client_id  text,
  created_at timestamptz NOT NULL DEFAULT now(),
  decided_at timestamptz
);
CREATE INDEX IF NOT EXISTS poi_suggestion_pending_idx
  ON poi_suggestion (created_at DESC) WHERE status = 'pending';

-- --------------------------------------------------------------------------
-- Observability: nightly KPIs and search quality, both shown on /admin
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS kpi_snapshot (
  id         bigserial PRIMARY KEY,
  taken_at   timestamptz NOT NULL DEFAULT now(),
  scope      text NOT NULL DEFAULT 'circle',   -- 'circle' | 'country'
  metrics    jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS kpi_snapshot_taken_idx ON kpi_snapshot (taken_at DESC);

CREATE TABLE IF NOT EXISTS search_log (
  id         bigserial PRIMARY KEY,
  q          text NOT NULL,
  kind       text,
  hits       integer NOT NULL DEFAULT 0,
  clicked_id text,
  -- Origin is logged at ~1 km precision only: enough for speed profiles and
  -- coverage gaps, not enough to follow anybody home.
  lat_coarse real,
  lon_coarse real,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS search_log_created_idx ON search_log (created_at DESC);

CREATE TABLE IF NOT EXISTS route_log (
  id          bigserial PRIMARY KEY,
  from_lat    real, from_lon real,      -- coarse, as above
  to_lat      real, to_lon   real,
  costing     text,
  length_km   real,
  duration_s  real,
  alternates  smallint,
  reroute     boolean NOT NULL DEFAULT false,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS route_log_created_idx ON route_log (created_at DESC);

COMMIT;
