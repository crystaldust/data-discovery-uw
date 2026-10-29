-- The read side: one search row per entity, contact routes with emails masked, and API
-- keys with per-minute metering that the read-only role can use but not read or write.

-- Shows that an address exists and where it points, not the address: first character,
-- then the domain. A local part of 3 characters or fewer is hidden entirely (its first
-- character would give most of it away), as is anything without a usable local part.
-- ddm.query.scrub masks addresses found in free text the same way.
CREATE FUNCTION mask_email(addr text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT AS
$$ SELECT CASE
            WHEN position('@' IN addr) <= 1 THEN '***'
            WHEN length(addr) - length(split_part(addr, '@', -1)) - 1 <= 3
              THEN '***@' || split_part(addr, '@', -1)
            ELSE left(addr, 1) || '***@' || split_part(addr, '@', -1)
          END $$;

-- A timestamp from source attrs, or NULL when the text is not one: a single bad value
-- must not break building or refreshing the search view. Not parallel safe: the
-- exception block runs in a subtransaction, which parallel workers cannot start.
CREATE FUNCTION attr_timestamptz(value text) RETURNS timestamptz
LANGUAGE plpgsql STABLE PARALLEL UNSAFE AS
$$
BEGIN
  RETURN value::timestamptz;
EXCEPTION WHEN invalid_datetime_format OR datetime_field_overflow OR invalid_parameter_value THEN
  RETURN NULL;
END
$$;

-- The only way out for contact routes. Fail closed: every route type except the known
-- URL types is masked, so a new email-like type added later is hidden by default. The
-- raw value column is not in the view at all. Views read their tables with the owner's
-- rights, so the read-only role needs SELECT on this view only, not on contact_routes.
CREATE VIEW contact_routes_public AS
SELECT route_id, entity_id, route_type,
       CASE WHEN route_type IN ('website', 'contact_page', 'outlet_author_page') THEN value
            ELSE mask_email(value) END AS display_value,
       syntax_valid, deliverability_status, affiliation_status, affiliation_basis,
       purpose, purpose_basis, source_id, source_key, provenance,
       first_seen_run, checked_run, checked_at
FROM contact_routes;

-- One row per entity with the fields search filters on, folded from its 1-3 source
-- records. Refreshed by the jobs after each run (REFRESH ... CONCURRENTLY, which needs the
-- unique index below). Only chosen attrs keys are read: no email can end up in here.
-- Podcast fields prefer the RSS record (2), then the API (3), then the dump (1); a
-- journalist's activity is their newest byline across outlets (5). Languages keep only
-- the primary tag: en-US, en_us and en all become en.
CREATE MATERIALIZED VIEW entity_search AS
WITH rec AS (
  SELECT r.entity_id, r.source_id, r.provenance, r.deleted_at IS NULL AS live,
         fr.finished_at AS first_seen_at, lr.finished_at AS last_seen_at,
         CASE r.source_id WHEN 2 THEN 0 WHEN 5 THEN 0 WHEN 3 THEN 1 WHEN 1 THEN 2 ELSE 9 END
           AS prio,
         nullif(lower(split_part(translate(r.attrs->>'language', '_', '-'), '-', 1)), '')
           AS language,
         CASE WHEN jsonb_typeof(r.attrs->'categories') = 'array' THEN r.attrs->'categories' END
           AS categories,
         attr_timestamptz(CASE r.source_id WHEN 2 THEN r.attrs->>'last_episode_at'
                                           WHEN 5 THEN r.attrs->>'latest_byline_at'
                                           WHEN 1 THEN r.attrs->>'newest_item_at'
                                           WHEN 3 THEN r.attrs->>'newest_item_at' END)
           AS activity_at
  FROM source_records r
  JOIN import_runs fr ON fr.run_id = r.first_seen_run
  JOIN import_runs lr ON lr.run_id = r.last_seen_run
  WHERE r.entity_id IS NOT NULL
),
agg AS (
  SELECT entity_id,
         (array_agg(language ORDER BY prio) FILTER (WHERE live AND language IS NOT NULL))[1]
           AS language,
         (array_agg(categories ORDER BY prio) FILTER (WHERE live AND categories IS NOT NULL))[1]
           AS categories,
         (array_agg(activity_at ORDER BY prio, activity_at DESC)
            FILTER (WHERE live AND activity_at IS NOT NULL))[1] AS last_activity_at,
         (array_agg(provenance ORDER BY prio, activity_at DESC)
            FILTER (WHERE live AND activity_at IS NOT NULL))[1] AS activity_basis,
         max(last_seen_at) FILTER (WHERE live AND provenance = 'observed') AS last_checked_at,
         min(first_seen_at) FILTER (WHERE live AND provenance = 'historical') AS imported_at,
         array_agg(DISTINCT source_id ORDER BY source_id) FILTER (WHERE live) AS source_ids,
         bool_and(NOT live) AS all_deleted
  FROM rec
  GROUP BY entity_id
),
outlet AS (
  SELECT entity_id, array_agg(DISTINCT name ORDER BY name) AS outlets
  FROM (
    SELECT r.entity_id, e->>'outlet' AS name
    FROM source_records r,
         jsonb_array_elements(CASE WHEN jsonb_typeof(r.attrs->'employments') = 'array'
                                   THEN r.attrs->'employments' ELSE '[]'::jsonb END) e
    WHERE r.source_id = 4 AND r.deleted_at IS NULL AND r.entity_id IS NOT NULL
    UNION ALL
    SELECT entity_id, attrs->>'outlet'
    FROM source_records
    WHERE source_id = 5 AND deleted_at IS NULL AND entity_id IS NOT NULL
  ) o
  WHERE name IS NOT NULL
  GROUP BY entity_id
),
-- Routes shown by a record that has since been deleted no longer count.
route AS (
  SELECT c.entity_id, count(*)::int AS n
  FROM contact_routes c
  JOIN source_records r ON (r.source_id, r.source_key) = (c.source_id, c.source_key)
  WHERE r.deleted_at IS NULL
  GROUP BY c.entity_id
)
SELECT e.entity_id, e.entity_type, e.display_name, e.norm_name,
       e.first_seen_kind, e.first_seen_run,
       a.language,
       coalesce(ARRAY(SELECT lower(c) FROM jsonb_array_elements_text(a.categories) c),
                '{}'::text[]) AS categories,
       coalesce(o.outlets, '{}'::text[]) AS outlets,
       a.last_activity_at, a.activity_basis, a.last_checked_at, a.imported_at,
       coalesce(rt.n, 0) AS contact_routes,
       coalesce(a.source_ids, '{}'::smallint[]) AS source_ids,
       coalesce(a.all_deleted, false) AS all_deleted,
       now() AS refreshed_at
FROM entities e
LEFT JOIN agg a USING (entity_id)
LEFT JOIN outlet o USING (entity_id)
LEFT JOIN route rt USING (entity_id)
WITH DATA;

CREATE UNIQUE INDEX entity_search_pk ON entity_search (entity_id);
CREATE INDEX entity_search_norm_name ON entity_search USING gist (norm_name gist_trgm_ops);
CREATE INDEX entity_search_activity
  ON entity_search (entity_type, last_activity_at DESC NULLS LAST);
CREATE INDEX entity_search_categories ON entity_search USING gin (categories);
CREATE INDEX entity_search_outlets ON entity_search USING gin (outlets);

-- Keys for the REST API and MCP. Only a SHA-256 of the key is stored; the key itself is
-- written once to a local file when created.
CREATE TABLE api_keys (
  key_id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  label                  text NOT NULL CHECK (label <> ''),
  key_hash               bytea NOT NULL UNIQUE CHECK (octet_length(key_hash) = 32),
  rate_limit_per_minute  integer NOT NULL DEFAULT 60 CHECK (rate_limit_per_minute > 0),
  created_at             timestamptz NOT NULL DEFAULT now(),
  revoked_at             timestamptz
);

-- Calls per key per minute, per surface and route or tool. Refused calls are counted too,
-- so the table shows how much was turned away.
CREATE TABLE api_usage (
  key_id        bigint NOT NULL REFERENCES api_keys,
  window_start  timestamptz NOT NULL,
  surface       text NOT NULL CHECK (surface IN ('api', 'mcp')),
  name          text NOT NULL CHECK (name <> '' AND length(name) <= 64),
  calls         integer NOT NULL CHECK (calls > 0),
  PRIMARY KEY (key_id, window_start, surface, name)
);

-- The read-only role may ask "is this key valid" and "count one call for this key", and
-- nothing else on these tables. SECURITY DEFINER functions run as the owner, so each pins
-- search_path and does one fixed thing.
CREATE FUNCTION api_key_check(p_hash bytea) RETURNS bigint
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS
$$ SELECT key_id FROM api_keys WHERE key_hash = p_hash AND revoked_at IS NULL $$;

-- Locks the key row so concurrent calls for one key are counted one after another.
-- Needs a read-write transaction: the web pool's sessions default to read-only.
CREATE FUNCTION api_meter(p_key bigint, p_surface text, p_name text)
RETURNS TABLE (allowed boolean, retry_after_seconds integer)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS
$$
DECLARE
  v_window timestamptz := date_trunc('minute', now());
  v_limit integer;
  v_total integer;
BEGIN
  SELECT k.rate_limit_per_minute INTO v_limit
  FROM api_keys k WHERE k.key_id = p_key AND k.revoked_at IS NULL
  FOR UPDATE;
  IF v_limit IS NULL THEN
    RETURN QUERY SELECT false, 60;
    RETURN;
  END IF;
  INSERT INTO api_usage AS u (key_id, window_start, surface, name, calls)
  VALUES (p_key, v_window, p_surface, p_name, 1)
  ON CONFLICT (key_id, window_start, surface, name) DO UPDATE SET calls = u.calls + 1;
  SELECT sum(u.calls) INTO v_total
  FROM api_usage u WHERE u.key_id = p_key AND u.window_start = v_window;
  IF v_total <= v_limit THEN
    RETURN QUERY SELECT true, 0;
  ELSE
    RETURN QUERY SELECT false, greatest(1, least(60,
      ceil(extract(epoch FROM v_window + interval '1 minute' - clock_timestamp()))::int));
  END IF;
END
$$;

REVOKE EXECUTE ON FUNCTION api_key_check(bytea) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION api_meter(bigint, text, text) FROM PUBLIC;

-- Default privileges would give the read-only role SELECT on the new tables (as in 0003):
-- take that back, and grant the two views and the two functions instead.
-- Its sessions in this database also default to read-only transactions and a 5 s
-- statement timeout. Set on the role, because Neon's transaction pooler does not pass
-- per-connection startup options through. A role we may not alter only gets a warning:
-- the grants above are the main protection.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ddm_reader') THEN
    REVOKE ALL ON api_keys, api_usage FROM ddm_reader;
    GRANT SELECT ON entity_search, contact_routes_public TO ddm_reader;
    GRANT EXECUTE ON FUNCTION api_key_check(bytea), api_meter(bigint, text, text) TO ddm_reader;
    BEGIN
      EXECUTE format('ALTER ROLE ddm_reader IN DATABASE %I SET default_transaction_read_only = on',
                     current_database());
      EXECUTE format('ALTER ROLE ddm_reader IN DATABASE %I SET statement_timeout = %L',
                     current_database(), '5s');
    EXCEPTION WHEN insufficient_privilege THEN
      RAISE WARNING 'could not set session defaults for ddm_reader; set them by hand';
    END;
  END IF;
END
$$;
