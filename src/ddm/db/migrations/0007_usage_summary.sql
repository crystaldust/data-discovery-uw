-- API and MCP calls per day and surface, for the cost report. No key ids: the read-only
-- role may see how much the service is used, not by whom. It still cannot read api_usage.

CREATE VIEW api_usage_daily AS
SELECT (date_trunc('day', window_start AT TIME ZONE 'UTC') AT TIME ZONE 'UTC') AS day,
       surface,
       sum(calls)::bigint AS calls
FROM api_usage
GROUP BY 1, 2;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ddm_reader') THEN
    GRANT SELECT ON api_usage_daily TO ddm_reader;
  END IF;
END
$$;
