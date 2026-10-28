-- Journalists, a narrow slice: Wikidata people and outlet RSS bylines, author-page
-- identifiers, and the count of records a complete snapshot no longer contains.

INSERT INTO sources (source_id, name, license, access) VALUES
  (4, 'wikidata', 'CC0 1.0',
   'Wikidata Query Service SPARQL (https://query.wikidata.org/sparql): honest User-Agent, one query at a time'),
  (5, 'outlet_rss', 'publisher feeds; byline metadata only (author, headline, link, date), no article text',
   'HTTP GET of public outlet RSS/Atom feeds: honest User-Agent, robots.txt, conditional requests');

-- An author page on an outlet's own site (from a Wikidata external id, or a byline that
-- links to it) names one person: a strong identifier, like a feed URL for a podcast.
ALTER TABLE entity_identifiers DROP CONSTRAINT entity_identifiers_id_kind_check;
ALTER TABLE entity_identifiers ADD CONSTRAINT entity_identifiers_id_kind_check CHECK (id_kind IN
  ('podcast_guid', 'itunes_id', 'podcastindex_id', 'feed_url', 'wikidata_qid', 'outlet_author_url'));
ALTER TABLE ambiguous_identifiers DROP CONSTRAINT ambiguous_identifiers_id_kind_check;
ALTER TABLE ambiguous_identifiers ADD CONSTRAINT ambiguous_identifiers_id_kind_check CHECK (id_kind IN
  ('podcast_guid', 'itunes_id', 'podcastindex_id', 'feed_url', 'wikidata_qid', 'outlet_author_url'));

-- Records soft-deleted by a complete snapshot run. NULL: not measured by this kind of run.
ALTER TABLE run_metrics ADD COLUMN rows_deleted integer;
