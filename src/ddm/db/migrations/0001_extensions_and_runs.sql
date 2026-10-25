-- Extensions, text normalisation, source registry and run bookkeeping.

CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS fuzzystrmatch;
CREATE EXTENSION IF NOT EXISTS unaccent;

-- Fixed dictionary wrapped as IMMUTABLE so it can back a generated column and an index.
-- Changing this function later requires rebuilding entities.norm_name and its index.
CREATE FUNCTION norm_text(t text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT AS
$$ SELECT lower(btrim(regexp_replace(public.unaccent('public.unaccent'::regdictionary, t), '\s+', ' ', 'g'))) $$;

CREATE TABLE sources (
  source_id   smallint PRIMARY KEY,
  name        text NOT NULL UNIQUE,
  license     text NOT NULL,
  access      text NOT NULL
);
INSERT INTO sources (source_id, name, license, access) VALUES
  (1, 'podcastindex_dump', 'MIT',
   'public database export (https://public.podcastindex.org/podcastindex_feeds.db.tgz)');

-- One row per input. The same input (same fingerprint) is registered once, which makes
-- re-imports idempotent. Deletions may only be inferred from complete snapshots.
CREATE TABLE import_runs (
  run_id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source_id            smallint NOT NULL REFERENCES sources,
  run_kind             text NOT NULL
                       CHECK (run_kind IN ('historical_backfill', 'refresh', 'discovery')),
  input_fingerprint    text NOT NULL,
  is_complete_snapshot boolean NOT NULL,
  params               jsonb NOT NULL DEFAULT '{}'::jsonb,
  started_at           timestamptz NOT NULL DEFAULT now(),
  finished_at          timestamptz,
  status               text NOT NULL DEFAULT 'running'
                       CHECK (status IN ('running', 'ok', 'partial', 'failed')),
  UNIQUE (source_id, input_fingerprint),
  CHECK ((status = 'running') = (finished_at IS NULL))
);

-- One row per run; NULL means "not measured by this kind of run", never zero by default.
CREATE TABLE run_metrics (
  run_id              bigint PRIMARY KEY REFERENCES import_runs,
  rows_in             integer,
  rows_rejected       integer,
  rows_new            integer,
  rows_changed        integer,
  rows_unchanged      integer,
  entities_created    integer,
  identifiers_added   integer,
  strong_id_merges    integer,
  candidates_created  integer,
  ambiguous_identifiers integer,
  http_requests       integer,
  bytes_fetched       bigint,
  duration_ms         integer,
  db_size_bytes       bigint
);

-- Input rows that could not be normalised. Kept with their raw values, never silently dropped.
CREATE TABLE import_rejects (
  run_id      bigint NOT NULL REFERENCES import_runs,
  row_number  integer NOT NULL,
  source_key  text,
  reason      text NOT NULL,
  raw         jsonb NOT NULL,
  PRIMARY KEY (run_id, row_number)
);
