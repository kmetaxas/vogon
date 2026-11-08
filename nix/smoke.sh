#!/usr/bin/env bash
# Run inside nix develop; services use a disposable directory, never project data.
set -euo pipefail
project_root="${VOGON_DEV_ROOT:-$PWD}"
export VOGON_DEV_ROOT
VOGON_DEV_ROOT=$(mktemp -d)
pg_pid=""
temporal_pid=""
cleanup() {
  [[ -z "$temporal_pid" ]] || kill "$temporal_pid" 2>/dev/null || true
  [[ -z "$pg_pid" ]] || kill "$pg_pid" 2>/dev/null || true
  [[ -z "$temporal_pid" ]] || wait "$temporal_pid" 2>/dev/null || true
  [[ -z "$pg_pid" ]] || wait "$pg_pid" 2>/dev/null || true
  rm -rf "$VOGON_DEV_ROOT"
}
trap cleanup EXIT
# Reserve distinct ephemeral ports while discovering them (small release/start race).
read -r VOGON_POSTGRES_PORT VOGON_TEMPORAL_PORT VOGON_TEMPORAL_UI_PORT < <(python - <<'PY'
import socket
sockets = [socket.socket() for _ in range(3)]
for sock in sockets:
    sock.bind(("127.0.0.1", 0))
print(*(sock.getsockname()[1] for sock in sockets))
PY
)
export VOGON_POSTGRES_PORT VOGON_TEMPORAL_PORT VOGON_TEMPORAL_UI_PORT
export PGPORT="$VOGON_POSTGRES_PORT"
export TEMPORAL_ADDRESS="127.0.0.1:$VOGON_TEMPORAL_PORT"
if vogon-postgres invalid; then exit 1; fi
if vogon-postgres start; then exit 1; fi
vogon-postgres init
vogon-postgres init
vogon-postgres start > "$VOGON_DEV_ROOT/postgres.log" 2>&1 &
pg_pid=$!
for _ in {1..60}; do
  if vogon-postgres status; then break; fi
  sleep 1
done
vogon-postgres status
vogon-postgres setup
vogon-postgres setup
psql -h 127.0.0.1 -U postgres -d vogon -v ON_ERROR_STOP=1 \
  -c "SELECT '[1,2,3]'::vector; SELECT similarity('vogon', 'vogon'); SHOW listen_addresses;"
if [[ "${VOGON_SMOKE_MIGRATE:-0}" == 1 ]]; then
  DJANGO_DB_HOST=127.0.0.1 DJANGO_DB_PORT="$PGPORT" DJANGO_DB_USER=postgres \
    DJANGO_DB_NAME=vogon python "$project_root/manage.py" migrate --noinput
fi
vogon-temporal > "$VOGON_DEV_ROOT/temporal.log" 2>&1 &
temporal_pid=$!
for _ in {1..90}; do
  if temporal operator cluster health; then break; fi
  sleep 1
done
temporal operator cluster health
test -s "$VOGON_DEV_ROOT/.dev/temporal.sqlite"
echo "Native PostgreSQL/pgvector and persistent Temporal smoke checks passed."
