#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="${SYNAPSE_VERSION:-$(cat "$ROOT/contract_tests/SYNAPSE_VERSION")}"
CACHE="${ZUNO_CONTRACT_CACHE:-$ROOT/.contract}"
SRC="$CACHE/synapse-$VERSION"
VENV="$CACHE/venv-$VERSION"

if [ ! -d "$SRC/tests" ]; then
  rm -rf "$SRC"
  git clone --quiet --depth 1 --branch "v$VERSION" --filter=blob:none --sparse \
    https://github.com/element-hq/synapse "$SRC"
  git -C "$SRC" sparse-checkout set tests
fi

if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install --quiet "matrix-synapse[test,redis]==$VERSION" psycopg2-binary
fi

INSTALLED="$("$VENV/bin/python" -c 'import os, zuno_push; print(os.path.dirname(zuno_push.__file__))' 2>/dev/null || true)"
if [ "$INSTALLED" != "$ROOT/src" ]; then
  "$VENV/bin/pip" install --quiet --no-deps -e "$ROOT"
fi

ln -sfn "$ROOT/contract_tests" "$SRC/tests/zuno_push_contract"
cd "$SRC"
if [ "$#" -eq 0 ]; then
  set -- tests.zuno_push_contract
fi
if [ -z "${ZUNO_CONTRACT_COVERAGE:-}" ]; then
  exec "$VENV/bin/python" -m twisted.trial "$@"
fi

"$VENV/bin/python" -c 'import coverage' 2>/dev/null || "$VENV/bin/pip" install --quiet 'coverage[toml]>=7'
export COVERAGE_RCFILE="$ROOT/pyproject.toml" COVERAGE_FILE="$ROOT/.coverage.contract"
"$VENV/bin/python" -m coverage run -m twisted.trial "$@"
cd "$ROOT"
exec "$VENV/bin/python" -m coverage xml -o coverage-contract.xml
