-- Durable publication evidence and stable links after duplicate reconciliation.
ALTER TABLE poi_match_queue ADD COLUMN IF NOT EXISTS published_at timestamptz;
CREATE TABLE IF NOT EXISTS poi_redirect (
  old_id uuid PRIMARY KEY,
  poi_id uuid NOT NULL REFERENCES poi(id),
  snapshot jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (old_id <> poi_id)
);
CREATE INDEX IF NOT EXISTS poi_redirect_target_idx ON poi_redirect(poi_id);
