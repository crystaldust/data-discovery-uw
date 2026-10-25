-- Podcast RSS refresh: the second source, polite-fetching state, a per-feed fetch log,
-- contact routes published by the shows themselves, and fetch outcome counts per run.

INSERT INTO sources (source_id, name, license, access) VALUES
  (2, 'podcast_rss', 'publisher feeds; channel metadata only, no episode content kept',
   'HTTP GET of public RSS feeds: honest User-Agent, robots.txt, conditional requests');

-- NULL means "not measured by this kind of run", as for the other columns.
ALTER TABLE run_metrics
  ADD COLUMN rows_not_modified integer,
  ADD COLUMN rows_failed       integer,
  ADD COLUMN rows_skipped      integer;

-- Mutable on purpose: validators and back-off are not observations, and keeping them out
-- of attrs stops a new ETag from counting as a content change. Keys are Podcast Index
-- feed ids, the same keys as the podcast_rss source records.
CREATE TABLE feed_fetch_state (
  source_key            text PRIMARY KEY CHECK (source_key <> ''),
  fetch_url             text NOT NULL,
  etag                  text,
  last_modified         text,
  last_status           integer CHECK (last_status BETWEEN 100 AND 599),
  last_outcome          text NOT NULL
                        CHECK (last_outcome IN ('fetched', 'not_modified', 'rejected', 'failed', 'skipped')),
  last_attempt_run      bigint NOT NULL REFERENCES import_runs,
  last_attempt_at       timestamptz NOT NULL,
  last_ok_at            timestamptz,
  -- Run of the last fetch whose body parsed: routes stamped by it are the feed's current ones.
  last_fetched_run      bigint REFERENCES import_runs,
  consecutive_failures  integer NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
  retry_after           timestamptz
);
COMMENT ON COLUMN feed_fetch_state.last_ok_at IS
  'Last time the server answered normally (fetched, not_modified or rejected). A rejected '
  'body was not readable, so a "verified on" date must come from fetch_attempts instead.';

-- One row per feed per refresh run. Written once; never updated.
CREATE TABLE fetch_attempts (
  run_id        bigint NOT NULL REFERENCES import_runs,
  source_key    text NOT NULL,
  url           text NOT NULL,
  outcome       text NOT NULL
                CHECK (outcome IN ('fetched', 'not_modified', 'rejected', 'failed', 'skipped')),
  reason        text,
  http_status   integer CHECK (http_status BETWEEN 100 AND 599),
  bytes         integer NOT NULL CHECK (bytes >= 0),
  elapsed_ms    integer NOT NULL CHECK (elapsed_ms >= 0),
  attempted_at  timestamptz NOT NULL,
  PRIMARY KEY (run_id, source_key),
  -- Success needs no explanation; anything else must say why.
  CHECK ((outcome IN ('fetched', 'not_modified')) = (reason IS NULL))
);
-- "When was this feed last verified" reads the latest attempt per feed.
CREATE INDEX fetch_attempts_feed ON fetch_attempts (source_key, attempted_at DESC);

-- One row per route. Syntax, deliverability, affiliation and purpose are separate claims,
-- each with its basis. There is deliberately no "may contact" or "consented" column.
-- Unique without source_key: two feeds of one entity publishing the same address are one
-- route; source_key names the feed that last showed it.
CREATE TABLE contact_routes (
  route_id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  entity_id              uuid NOT NULL REFERENCES entities,
  route_type             text NOT NULL CHECK (route_type IN
                           ('feed_owner_email', 'feed_editor_email', 'website',
                            'contact_page', 'outlet_author_page')),
  value                  text NOT NULL CHECK (value <> ''),
  syntax_valid           boolean NOT NULL,
  deliverability_status  text NOT NULL DEFAULT 'unchecked' CHECK (deliverability_status IN
                           ('unchecked', 'deliverable', 'undeliverable', 'risky', 'unknown')),
  affiliation_status     text NOT NULL
                         CHECK (affiliation_status IN ('confirmed', 'unconfirmed', 'unknown')),
  affiliation_basis      text NOT NULL CHECK (affiliation_basis <> ''),
  purpose                text NOT NULL CHECK (purpose IN ('booking', 'press', 'general', 'unknown')),
  purpose_basis          text NOT NULL CHECK (purpose_basis <> ''),
  source_id              smallint NOT NULL REFERENCES sources,
  source_key             text NOT NULL,
  provenance             text NOT NULL CHECK (provenance IN ('historical', 'observed')),
  first_seen_run         bigint NOT NULL REFERENCES import_runs,
  checked_run            bigint NOT NULL REFERENCES import_runs,
  checked_at             timestamptz NOT NULL,
  UNIQUE (entity_id, route_type, value, source_id),
  CHECK (checked_run >= first_seen_run)
);
CREATE INDEX contact_routes_record ON contact_routes (source_id, source_key);

-- The read-only role gets SELECT on new tables through default privileges. Contact routes
-- hold email addresses, so they are exposed later only through a deliberate view.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ddm_reader') THEN
    REVOKE ALL ON contact_routes FROM ddm_reader;
  END IF;
END
$$;
