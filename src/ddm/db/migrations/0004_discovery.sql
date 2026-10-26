-- Discovery of feeds missing from the historical population: classification per record,
-- the newfeeds cursor, and a trigram index that supports nearest-neighbour ordering.

INSERT INTO sources (source_id, name, license, access) VALUES
  (3, 'podcastindex_api', 'Podcast Index API terms of service',
   'authenticated API (https://api.podcastindex.org), honest User-Agent, 1 request/s');

-- Entities created by a discovery run are neither historical nor refreshed observations.
ALTER TABLE entities DROP CONSTRAINT entities_first_seen_kind_check;
ALTER TABLE entities ADD CONSTRAINT entities_first_seen_kind_check
  CHECK (first_seen_kind IN ('historical', 'observed', 'discovered'));

ALTER TABLE run_metrics
  ADD COLUMN discovered_new integer,
  ADD COLUMN discovered_rename_or_move integer,
  ADD COLUMN discovered_duplicate integer,
  ADD COLUMN discovered_uncertain integer;

-- One row per record first seen by a discovery run. entity_id is the entity the record
-- belongs to after the run; matched_entity_id is the existing entity it was attached to
-- (duplicate, rename_or_move) or suspected to be (uncertain). The best fuzzy neighbour is
-- kept in evidence even below the threshold, for calibration.
CREATE TABLE discoveries (
  run_id             bigint NOT NULL REFERENCES import_runs,
  source_id          smallint NOT NULL REFERENCES sources,
  source_key         text NOT NULL CHECK (source_key <> ''),
  classification     text NOT NULL
                     CHECK (classification IN ('new', 'rename_or_move', 'duplicate', 'uncertain')),
  entity_id          uuid NOT NULL REFERENCES entities,
  matched_entity_id  uuid REFERENCES entities,
  method             text NOT NULL CHECK (method <> ''),
  score              real CHECK (score BETWEEN 0 AND 1),
  evidence           jsonb NOT NULL,
  classifier_version smallint NOT NULL,
  PRIMARY KEY (run_id, source_id, source_key),
  CHECK ((classification = 'new') = (matched_entity_id IS NULL)),
  -- Attached records share the entity they matched; uncertain ones got their own.
  CHECK (classification NOT IN ('duplicate', 'rename_or_move') OR entity_id = matched_entity_id),
  CHECK (classification <> 'uncertain' OR entity_id <> matched_entity_id)
);
CREATE INDEX discoveries_classification ON discoveries (classification, run_id);
-- A record is classified once, by the run that first saw it.
CREATE UNIQUE INDEX discoveries_record ON discoveries (source_id, source_key);

-- Highest Podcast Index feed id a newfeeds run has finished with, per source.
CREATE TABLE discovery_cursors (
  source_id     smallint PRIMARY KEY REFERENCES sources,
  last_feed_id  bigint NOT NULL CHECK (last_feed_id > 0),
  updated_run   bigint NOT NULL REFERENCES import_runs
);

-- When a source refused us (401/403/429), the earliest time to ask it again. Discovery
-- runs do not start before then: never retried sooner, never with another identity.
CREATE TABLE source_backoff (
  source_id    smallint PRIMARY KEY REFERENCES sources,
  retry_after  timestamptz NOT NULL,
  http_status  integer NOT NULL CHECK (http_status IN (401, 403, 429)),
  set_run      bigint NOT NULL REFERENCES import_runs
);

-- Nearest-title lookups order by trigram distance, which GiST supports and GIN does not
-- (about 2.4x faster than the GIN bitmap path at a 0.3 floor on the demo data). GiST also
-- answers the % and LIKE operators, so the GIN index from 0002 is no longer needed.
CREATE INDEX entities_norm_name_gist ON entities USING gist (norm_name gist_trgm_ops);
DROP INDEX entities_norm_name_trgm;
