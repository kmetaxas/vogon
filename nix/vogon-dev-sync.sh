set -euo pipefail
: "${VOGON_DEV_ROOT:=$PWD}"
: "${UV_PYTHON:?Run this command inside nix develop}"
cd "$VOGON_DEV_ROOT"
if [[ ! -f pyproject.toml || ! -f uv.lock ]]; then
  echo "Run nix develop from the Vogon repository root." >&2
  exit 1
fi
if [[ -e .venv || -L .venv ]]; then
  if [[ ! -f .venv/.vogon-nix-python ]] || [[ $(< .venv/.vogon-nix-python) != "$UV_PYTHON" ]] ||
     [[ ! -x .venv/bin/python ]] ||
     [[ $(.venv/bin/python -c 'import sys; print(sys.base_prefix)') != "$("$UV_PYTHON" -c 'import sys; print(sys.base_prefix)')" ]]; then
    echo "Foreign or stale .venv detected. Move it aside explicitly, then retry nix develop." >&2
    exit 1
  fi
else
  uv venv --python "$UV_PYTHON" .venv
  printf '%s\n' "$UV_PYTHON" > .venv/.vogon-nix-python
fi
uv sync --locked --extra dev --no-install-project
uv sync --locked --extra dev
