#!/usr/bin/env bash
set -euo pipefail
export COPYFILE_DISABLE=1

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="${DIST_DIR:-$ROOT_DIR/dist}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

mkdir -p "$DIST_DIR"
rm -f "$DIST_DIR"/UNKNOWN-*.whl "$DIST_DIR"/memgit-*.whl

TMP_VENV="$(mktemp -d "${TMPDIR:-/tmp}/memgit_build_venv_XXXXXX")"
trap 'rm -rf "$TMP_VENV"' EXIT

echo "[MemGit] Preparing clean build environment ..."
"$PYTHON_BIN" -m venv "$TMP_VENV"
"$TMP_VENV/bin/python" -m pip install --quiet --upgrade pip setuptools wheel

echo "[MemGit] Building Python wheel into $DIST_DIR ..."
if "$TMP_VENV/bin/python" -m build --version >/dev/null 2>&1; then
  "$TMP_VENV/bin/python" -m build --outdir "$DIST_DIR" "$ROOT_DIR"
else
  "$TMP_VENV/bin/python" -m pip wheel --no-build-isolation --no-deps --wheel-dir "$DIST_DIR" "$ROOT_DIR"
fi

WHEEL_FILE="$(ls -t "$DIST_DIR"/memgit-*.whl | head -n 1)"
echo "[MemGit] Built wheel: $WHEEL_FILE"

# Smoke-test wheel installation in a separate clean runtime venv
TEST_VENV="$TMP_VENV/runtime_test"
"$PYTHON_BIN" -m venv "$TEST_VENV"
"$TEST_VENV/bin/pip" install --quiet --no-deps "$WHEEL_FILE"
"$TEST_VENV/bin/memgit" --version
"$TEST_VENV/bin/memgit" doctor --json >/dev/null
echo "[MemGit] Wheel smoke test PASSED."
