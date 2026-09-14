-- Optimistic promotion check: a candidate may never erase an edit accepted
-- while its snapshot was building. Read/search telemetry is deliberately exempt.
CREATE TABLE IF NOT EXISTS release_revision (
  id integer PRIMARY KEY CHECK (id = 1),
  revision bigint NOT NULL DEFAULT 0
);
INSERT INTO release_revision (id) VALUES (1) ON CONFLICT DO NOTHING;
CREATE OR REPLACE FUNCTION nicanav_record_revision() RETURNS trigger AS $$
BEGIN
  UPDATE release_revision SET revision = revision + 1 WHERE id = 1;
  RETURN NULL;
END;
$$ LANGUAGE plpgsql;
DO $$
DECLARE name text;
BEGIN
  FOREACH name IN ARRAY ARRAY['poi','poi_source','poi_match_queue','poi_photo',
    'poi_flag','gazetteer','alias','kmpost','closure','report','poi_suggestion',
    'poi_redirect','highway'] LOOP
    EXECUTE format('DROP TRIGGER IF EXISTS nicanav_revision ON %I', name);
    EXECUTE format('CREATE TRIGGER nicanav_revision AFTER INSERT OR UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION nicanav_record_revision()', name);
  END LOOP;
END;
$$;
