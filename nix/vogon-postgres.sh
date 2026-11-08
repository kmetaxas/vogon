set -euo pipefail
cd "${VOGON_DEV_ROOT:-$PWD}"
mkdir -p .dev
state="$PWD/.dev/postgres"
export PGPORT="${VOGON_POSTGRES_PORT:-5432}"
[[ "$PGPORT" =~ ^[0-9]+$ ]] || { echo "Invalid VOGON_POSTGRES_PORT" >&2; exit 2; }
case "${1:-start}" in
  init)
    if [[ ! -e "$state" ]]; then
      initdb -D "$state" --username=postgres --auth-local=trust --auth-host=scram-sha-256
      printf 'local all all trust\nhost all all 127.0.0.1/32 trust\nhost all all ::1/128 trust\n' > "$state/pg_hba.conf"
    elif [[ ! -f "$state/PG_VERSION" ]]; then
      echo "Existing state is not an initialized database: $state" >&2
      exit 1
    fi
    echo "Cluster ready. Run vogon-postgres start; in another shell run vogon-postgres setup."
    ;;
  start)
    [[ -f "$state/PG_VERSION" ]] || { echo "Run vogon-postgres init first." >&2; exit 1; }
    exec postgres -D "$state" -h 127.0.0.1 -p "$PGPORT" -k "$PWD/.dev"
    ;;
  setup)
    if [[ $(psql -h 127.0.0.1 -U postgres -d postgres -Atc "SELECT 1 FROM pg_database WHERE datname = 'vogon'") != 1 ]]; then
      createdb -h 127.0.0.1 -U postgres vogon
    fi
    psql -v ON_ERROR_STOP=1 -h 127.0.0.1 -U postgres -d vogon -c 'CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS pg_trgm;'
    ;;
  status) exec pg_isready -h 127.0.0.1 -U postgres -d vogon ;;
  *) echo "Usage: vogon-postgres {init|start|setup|status}" >&2; exit 2 ;;
esac
