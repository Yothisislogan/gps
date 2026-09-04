-- Named highway centrelines, for the km-post geocoder.
--
-- "Km 12.5 Carretera a Masaya" is a real Nicaraguan address, and resolving it
-- means measuring along a specific road's geometry.  The routing graph lives in
-- Valhalla and cannot be queried this way, so the pipeline materialises one
-- merged centreline per named carretera from OSM and stores it here.

BEGIN;

CREATE TABLE IF NOT EXISTS highway (
  highway_key   text PRIMARY KEY,               -- 'carretera_a_masaya'
  display_name  text NOT NULL,                  -- 'Carretera a Masaya'
  aliases       text[] NOT NULL DEFAULT '{}',   -- how people actually type it
  osm_ref       text,                           -- 'NIC-4' where known
  geom          geometry(LineString, 4326) NOT NULL,
  length_m      double precision,
  km0_lat       double precision,               -- where chainage starts
  km0_lon       double precision,
  source        text NOT NULL DEFAULT 'osm',
  note          text,
  updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS highway_geom_idx ON highway USING gist (geom);
CREATE INDEX IF NOT EXISTS highway_name_trgm_idx
  ON highway USING gin (nicanav_normalize(display_name) gin_trgm_ops);

DROP TRIGGER IF EXISTS highway_touch ON highway;
CREATE TRIGGER highway_touch BEFORE UPDATE ON highway
  FOR EACH ROW EXECUTE FUNCTION nicanav_touch_updated_at();

COMMIT;
