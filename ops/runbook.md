# Runbook

How this demo runs, how to tell it is healthy, and what to do when it is not.

## Moving parts

```
GitHub Actions (scheduled jobs, CI, backups)          Vercel (syd1)
  refresh-podcasts  daily  18:00 UTC  ─┐                web pages, /api/v1, /mcp
  discover-newfeeds daily  18:30 UTC  ─┤  owner role    read-only role, pooled
  sync-journalists  weekly Sun 19:00  ─┼──────────▶  Neon Postgres 18  ◀──────────┘
  backup            weekly Sun 20:00  ─┘  (direct)      (ap-southeast-2)
  refresh-search    by hand (also the last step of each job above)
  ci                every push and pull request
```

- Jobs connect as the database owner through the direct (non-pooled) URL: temp tables and
  advisory locks do not survive PgBouncer's transaction pooling.
- The web function connects as `ddm_reader` through Neon's pooler. That role can read the
  query views, call two key functions, and nothing else. Its sessions default to read-only
  transactions and a 5 s statement timeout (set on the role by migration 0006).
- The function runs in Vercel's `syd1` region, next to the database. In `iad1`, the
  cross-Pacific round trips alone put search p95 above 2 s.

## Scheduled jobs

| Workflow | When (UTC) | What it does | Defaults when scheduled |
|---|---|---|---|
| `refresh-podcasts` | daily 18:00 | Re-fetches baseline RSS feeds, least recently checked first | 300 feeds |
| `discover-newfeeds` | daily 18:30 | Classifies feeds Podcast Index added since the last run | 500 feeds |
| `sync-journalists` | Sunday 19:00 | Wikidata snapshot, then outlet RSS bylines | 50 author feeds |
| `backup` | Sunday 20:00 | Encrypted `pg_dump` plus row counts, kept 14 days | — |
| `refresh-search` | by hand | Migrations, then rebuilds the search view | — |
| `ci` | push, PR | ruff and the full test suite on Postgres 18, coverage ≥ 80% | — |

Every job also runs by hand from the Actions tab (**Run workflow**), with inputs.

**Exit code 3 means "partial".** A source refused us (401/403/429) or was down. What was
fetched is committed and the backoff is remembered, so the job never retries sooner.
The step is shown as failed on purpose, so that someone notices.

## Alerts

- GitHub emails the person who last changed a workflow's `schedule` when a scheduled run
  fails. Check that it is on: GitHub → Settings → Notifications → Actions → "Send
  notifications for failed workflows only".
- GitHub turns schedules off in a public repository after 60 days without a commit.
  The Actions tab then shows a banner; re-enable the workflow there.
- Each job prints a summary at the end (counts, HTTP requests, duration, database size);
  the same numbers go to `run_metrics` and to the **Metrics → Cost** section.

## Deploying

- Web: `npx vercel deploy --prod` from a checkout of `main`. Merging does not deploy.
- Roll back the web app: `npx vercel rollback` (or promote an earlier deployment in the
  Vercel dashboard). Migrations only ever add; there is no down-migration.
- Database migrations: run the `refresh-search` workflow (or `uv run ddm migrate` with
  the owner URL). Re-running applies nothing new. **Migrate before deploying** code that
  reads a new view: the web app does not create schema.
- Vercel environment variables (Production): `DATABASE_URL_READONLY`, `SESSION_SECRET`
  (32+ random characters), `DEMO_LOGIN_USER`, `DEMO_LOGIN_PASSWORD_HASH`
  (from `uv run ddm hash-password`).
- GitHub: secrets `DATABASE_URL`, `PODCASTINDEX_API_KEY`, `PODCASTINDEX_API_SECRET`;
  variables `DDM_USER_AGENT` and `BACKUP_AGE_RECIPIENT`.

## API and MCP keys

```bash
# owner connection; the key goes only into the new file (mode 0600), never to the terminal
uv run ddm create-api-key --label <who> --rate-limit 60 --out data/<who>-api-key.txt
uv run ddm revoke-api-key <key_id>
```

Keys are stored as SHA-256 only. The limit is per key per minute, shared by the API and
MCP. MCP clients connect to `https://uw-data-discovery.vercel.app/mcp` with
`Authorization: Bearer <key>`.

## Backups

- The `backup` workflow pipes `pg_dump --format=custom` into `age`, encrypting to the
  public key in the `BACKUP_AGE_RECIPIENT` variable. Only ciphertext touches the runner's
  disk. The private key is kept off GitHub: artifacts of a public repository can be
  downloaded by any signed-in GitHub user.
- Row counts of every table are taken before and after the dump. If they differ,
  something wrote during the backup: the run fails and is simply run again.
- The artifact `db-backup` holds the encrypted dump and `counts.json` (row counts only)
  and is kept 14 days.
- Neon itself also keeps 6 hours of point-in-time history on the Free plan.

Setting up a key pair (once, on the machine that will restore):

```bash
age-keygen -o ~/.config/ddm/backup-age-key.txt     # private key: keep it safe and off GitHub
age-keygen -y ~/.config/ddm/backup-age-key.txt     # prints the public key (age1...)
gh variable set BACKUP_AGE_RECIPIENT               # paste the public key
```

## Restore drill

```bash
docker compose up -d db
ops/restore-drill.sh            # latest successful backup; --run <id> for another one
```

The script downloads the artifact and decrypts it into a pipe. It restores into the
local container's `ddm_restore` database (`--no-owner --no-acl`) and compares every
table's row count with `counts.json`. A mismatch exits non-zero and names the table.

The drill checks the data. It does not restore roles: the dump is taken without owners
or grants, so it restores into any Postgres 18.

Last drill, 2026-10-29: production backup from `backup` run 40853427951, restored on a
laptop into the local Postgres 18 container:

```
$ ops/restore-drill.sh
drill started 2026-10-29T09:57:31Z backup run 40853427951
artifact ddm-20261029T0955Z.dump.age  81M
restored into local database ddm_restore in 111 s
relations=19 mismatches=0
drill finished 2026-10-29T09:59:24Z in 113 s
```

All 19 tables and views (everything but `api_usage`) have the same row counts as when
the backup was taken. Most of the time is the artifact download.

### Restoring for real

1. Create an empty Postgres 18 database (a new Neon project or branch) and restore:
   `age -d -i <key> <file>.dump.age | pg_restore -d "<owner url>" --no-owner --no-acl --exit-on-error`.
2. Recreate the read-only role and give it a new password, then re-apply its grants and
   session defaults. Migrations 0003, 0006 and 0007 contain the exact `REVOKE`, `GRANT`
   and `ALTER ROLE ... SET` statements; run their final `DO $$ ... $$` blocks as the owner.
   Also `GRANT USAGE ON SCHEMA public` and `SELECT` on the other tables to `ddm_reader`.
3. Point `DATABASE_URL` (GitHub secret) and `DATABASE_URL_READONLY` (Vercel) at the new
   database, run the `refresh-search` workflow, then `npx vercel deploy --prod`.
4. Issue new API keys if the old ones should not carry over (`api_keys` is restored too).

## Costs

The **Metrics → Cost** section shows the last 30 days:
- per-source runs, HTTP requests, data fetched and job minutes;
- API and MCP calls per day;
- for each resource, use against its free tier and what the same use would cost on the
  cheapest paid tier.

Current spend is $0: everything fits the free tiers.

| Resource | Free tier | Paid reference (as of 2026-10-29) |
|---|---|---|
| Neon | 1 GB storage, 100 CU-hours per project, 6 h history | Launch: $0.35 / GB-month, $0.106 / CU-hour |
| GitHub Actions | free on public repositories | private: $0.006 / minute (Linux 2-core) |
| Vercel | Hobby: free, **non-commercial use only** | a client deployment needs Pro |

Storage is the limit to watch: the daily jobs add to it, and the Cost section shows the
current share of the 1 GB Free cap. It reports the logical database size; Neon bills
history and compute separately, which are only visible in the Neon console. The
Actions minutes are the jobs' own run time, a lower bound.

## Recommended, not configured

- A Vercel Firewall rate-limit rule per IP on `/login`, `/api/` and `/mcp` (Hobby allows
  3 custom rules). The app limits failed logins per instance and API/MCP calls per key;
  an edge rule also limits unauthenticated traffic.

## Known limits

- The read-only role can still `SELECT` raw `source_records.attrs` and
  `observations.payload`, which contain contact emails. The app never returns them; a
  production setup would expose those tables only through views.
- Rate limits count per clock minute, so a burst across a minute boundary can reach twice
  the limit.
- New entities appear in search after the view is refreshed (the last step of each job).
