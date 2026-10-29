#!/usr/bin/env bash
# Restore drill: take the latest successful backup artifact, decrypt it locally, restore
# it into a fresh database in the local Docker Postgres (docker-compose.yml), and compare
# every table's row count with the counts taken when the backup was made.
#
# Usage: ops/restore-drill.sh [--key PATH] [--run RUN_ID]
#   --key  age private key (default: ~/.config/ddm/backup-age-key.txt)
#   --run  a specific backup workflow run (default: the latest successful one)
#
# Touches only the local container's database "ddm_restore". Decrypted data only ever
# passes through a pipe; nothing in plaintext is written to disk.
set -euo pipefail

KEY="${HOME}/.config/ddm/backup-age-key.txt"
RUN_ID=""
RESTORE_DB="ddm_restore"
RESTORE_URL="postgresql://ddm:ddm-local-only@127.0.0.1:54329/${RESTORE_DB}"

while [ $# -gt 0 ]; do
  case "$1" in
    --key) KEY="$2"; shift 2 ;;
    --run) RUN_ID="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

cd "$(dirname "$0")/.."
for tool in gh age docker uv; do
  command -v "$tool" >/dev/null || { echo "missing tool: $tool" >&2; exit 2; }
done
test -r "$KEY" || { echo "age private key not readable: $KEY" >&2; exit 2; }
running=$(docker compose ps --status running --services)
case $'\n'"$running"$'\n' in
  *$'\n'db$'\n'*) ;;
  *) echo "local database is not running: docker compose up -d db" >&2; exit 2 ;;
esac

if [ -z "$RUN_ID" ]; then
  RUN_ID=$(gh run list --workflow backup.yml --status success --limit 1 \
             --json databaseId --jq '.[0].databaseId')
fi
case "$RUN_ID" in
  ''|*[!0-9]*) echo "no successful backup run found" >&2; exit 2 ;;
esac

# A fresh download directory per run id, under the git-ignored data/ directory.
DIR="data/restore/${RUN_ID}"
rm -rf -- "$DIR" && mkdir -p -- "$DIR"
started=$(date -u +%s)
echo "drill started $(date -u +%Y-%m-%dT%H:%M:%SZ) backup run ${RUN_ID}"
gh run download "$RUN_ID" --name db-backup --dir "$DIR"
DUMP=$(ls "$DIR"/*.dump.age)
echo "artifact $(basename "$DUMP") $(du -h "$DUMP" | cut -f1)"

docker compose exec -T db psql -q -U ddm -d postgres \
  -c "DROP DATABASE IF EXISTS ${RESTORE_DB}" -c "CREATE DATABASE ${RESTORE_DB}"
age -d -i "$KEY" "$DUMP" \
  | docker compose exec -T db pg_restore -U ddm -d "$RESTORE_DB" --no-owner --no-acl --exit-on-error
echo "restored into local database ${RESTORE_DB} in $(( $(date -u +%s) - started )) s"

# api_usage is left out of backup counts (it gains a row with every API call).
DATABASE_URL="$RESTORE_URL" uv run ddm check-restore --expected "$DIR/counts.json" \
  --exclude api_usage
echo "drill finished $(date -u +%Y-%m-%dT%H:%M:%SZ) in $(( $(date -u +%s) - started )) s"
