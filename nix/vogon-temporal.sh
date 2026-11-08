set -euo pipefail
cd "${VOGON_DEV_ROOT:-$PWD}"
mkdir -p .dev
exec temporal server start-dev --ip 127.0.0.1 --port "${VOGON_TEMPORAL_PORT:-7233}" --ui-ip 127.0.0.1 --ui-port "${VOGON_TEMPORAL_UI_PORT:-8233}" --db-filename "$PWD/.dev/temporal.sqlite"
