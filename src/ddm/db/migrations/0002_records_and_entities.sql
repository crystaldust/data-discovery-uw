-- Entities, strong identifiers, per-source current state, append-only observations
-- and the review queue for matches we will not merge automatically.

-- Unknown attributes stay NULL; entity ids are neutral uuid5 values computed in Python.
CREATE TABLE entities (
  entity_id        uuid PRIMARY KEY,
  entity_type      text NOT NULL CHECK (entity_type IN ('podcast', 'journalist')),
  display_name     text,
  norm_name        text GENERATED ALWAYS AS (norm_text(display_name)) STORED,
  first_seen_kind  text NOT NULL CHECK (first_seen_kind IN ('historical', 'observed')),
  first_seen_run   bigint NOT NULL REFERENCES import_runs,
  created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX entities_norm_name_trgm ON entities USING gin (norm_name gin_trgm_ops);

-- Automatic merges happen only on an exact hit in this table.
CREATE TABLE entity_identifiers (
  id_kind     text NOT NULL CHECK (id_kind IN
                ('podcast_guid', 'itunes_id', 'podcastindex_id', 'feed_url', 'wikidata_qid')),
  id_value    text NOT NULL CHECK (id_value <> ''),
  entity_id   uuid NOT NULL REFERENCES entities,
  source_id   smallint NOT NULL REFERENCES sources,
  added_run   bigint NOT NULL REFERENCES import_runs,
  PRIMARY KEY (id_kind, id_value)
);
CREATE INDEX entity_identifiers_entity ON entity_identifiers (entity_id);

-- Identifier values seen on too many records to mean one entity (e.g. a hosting platform's
-- default podcast:guid shared by hundreds of unrelated feeds). Never used for merging.
CREATE TABLE ambiguous_identifiers (
  id_kind       text NOT NULL CHECK (id_kind IN
                  ('podcast_guid', 'itunes_id', 'podcastindex_id', 'feed_url', 'wikidata_qid')),
  id_value      text NOT NULL CHECK (id_value <> ''),
  record_count  integer NOT NULL,
  source_id     smallint NOT NULL REFERENCES sources,
  first_run     bigint NOT NULL REFERENCES import_runs,
  PRIMARY KEY (id_kind, id_value)
);

-- Current state of each source record. provenance is written on insert only.
CREATE TABLE source_records (
  source_id          smallint NOT NULL REFERENCES sources,
  source_key         text NOT NULL CHECK (source_key <> ''),
  entity_id          uuid REFERENCES entities,
  provenance         text NOT NULL CHECK (provenance IN ('historical', 'observed')),
  display_name       text,
  attrs              jsonb NOT NULL,
  content_hash       bytea NOT NULL CHECK (octet_length(content_hash) = 32),
  source_updated_at  timestamptz,
  first_seen_run     bigint NOT NULL REFERENCES import_runs,
  last_seen_run      bigint NOT NULL REFERENCES import_runs,
  last_changed_run   bigint NOT NULL REFERENCES import_runs,
  deleted_at         timestamptz,
  deleted_run        bigint REFERENCES import_runs,
  PRIMARY KEY (source_id, source_key),
  CHECK ((deleted_at IS NULL) = (deleted_run IS NULL))
);
CREATE INDEX source_records_entity ON source_records (entity_id);

-- One row per new, changed or deleted record per run; unchanged records are not written.
CREATE TABLE observations (
  run_id        bigint NOT NULL REFERENCES import_runs,
  source_id     smallint NOT NULL REFERENCES sources,
  source_key    text NOT NULL,
  change_kind   text NOT NULL CHECK (change_kind IN ('new', 'changed', 'deleted')),
  provenance    text NOT NULL CHECK (provenance IN ('historical', 'observed')),
  payload       jsonb,
  content_hash  bytea,
  observed_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (run_id, source_id, source_key),
  -- A new or changed record carries what was seen; a deletion carries nothing.
  CHECK ((change_kind = 'deleted') = (payload IS NULL))
);
CREATE INDEX observations_record ON observations (source_id, source_key);

-- Jobs connect as the table owner, so REVOKE would not stop them. These triggers stop
-- UPDATE, DELETE and TRUNCATE from application code; an owner could still drop them
-- deliberately, which a separate insert-only job role would prevent (not needed for the demo).
CREATE FUNCTION reject_observation_rewrite() RETURNS trigger
LANGUAGE plpgsql AS
$$ BEGIN RAISE EXCEPTION 'observations are append-only'; END $$;

CREATE TRIGGER observations_append_only
  BEFORE UPDATE OR DELETE ON observations
  FOR EACH ROW EXECUTE FUNCTION reject_observation_rewrite();

CREATE TRIGGER observations_no_truncate
  BEFORE TRUNCATE ON observations
  FOR EACH STATEMENT EXECUTE FUNCTION reject_observation_rewrite();

CREATE TABLE candidate_matches (
  candidate_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source_id       smallint NOT NULL REFERENCES sources,
  source_key      text NOT NULL,
  entity_id       uuid NOT NULL REFERENCES entities,
  method          text NOT NULL,
  score           real NOT NULL CHECK (score BETWEEN 0 AND 1),
  features        jsonb NOT NULL,
  status          text NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'accepted', 'rejected')),
  created_run     bigint NOT NULL REFERENCES import_runs,
  UNIQUE (source_id, source_key, entity_id, method)
);
