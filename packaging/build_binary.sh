#!/usr/bin/env bash
set -euo pipefail
export COPYFILE_DISABLE=1

# Build a single-file `memgit` executable and release tarball for the current OS/arch.
# Modes:
#   pyinstaller : Compile a zero-Python-dependency native executable via PyInstaller (used in CI Releases)
#   zipapp      : Bundle pure-Python stdlib zipapp executable (fast, works offline with python3 >= 3.9)
#   auto        : Use PyInstaller if installed, otherwise fall back to zipapp

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="${DIST_DIR:-$ROOT_DIR/dist}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
BUILD_MODE="${BUILD_MODE:-auto}"

VERSION="$("$PYTHON_BIN" -c 'import sys; sys.path.insert(0, "'"$ROOT_DIR"'"); import memgit; print(memgit.__version__)')"
OS_RAW="$(uname -s | tr '[:upper:]' '[:lower:]')"
ARCH_RAW="$(uname -m)"
case "$ARCH_RAW" in
  arm64|aarch64) ARCH="arm64" ;;
  x86_64|amd64)  ARCH="x86_64" ;;
  *)             ARCH="$ARCH_RAW" ;;
esac

STAGE_DIR="$DIST_DIR/stage_${OS_RAW}_${ARCH}"
rm -rf "$STAGE_DIR"
mkdir -p "$STAGE_DIR"

if [[ "$BUILD_MODE" == "auto" ]]; then
  if "$PYTHON_BIN" -m PyInstaller --version >/dev/null 2>&1; then
    BUILD_MODE="pyinstaller"
  else
    BUILD_MODE="zipapp"
  fi
fi

echo "[MemGit] Building v${VERSION} (${OS_RAW}/${ARCH}) using mode=${BUILD_MODE} ..."

if [[ "$BUILD_MODE" == "pyinstaller" ]]; then
  ENTRY_PY="$STAGE_DIR/_memgit_entry.py"
  cat > "$ENTRY_PY" <<'PY'
import sys
from memgit.cli import main
if __name__ == "__main__":
    sys.exit(main())
PY
  "$PYTHON_BIN" -m PyInstaller \
    --onefile \
    --name memgit \
    --clean \
    --noconfirm \
    --distpath "$STAGE_DIR/bin" \
    --workpath "$STAGE_DIR/work" \
    --specpath "$STAGE_DIR" \
    --paths "$ROOT_DIR" \
    "$ENTRY_PY"
  BIN_PATH="$STAGE_DIR/bin/memgit"
elif [[ "$BUILD_MODE" == "zipapp" ]]; then
  APP_SRC="$STAGE_DIR/zipapp_src"
  mkdir -p "$APP_SRC" "$STAGE_DIR/bin"
  cp -R "$ROOT_DIR/memgit" "$APP_SRC/memgit"
  find "$APP_SRC" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
  "$PYTHON_BIN" -m zipapp "$APP_SRC" \
    -o "$STAGE_DIR/bin/memgit" \
    -p "/usr/bin/env python3" \
    -m "memgit.cli:main"
  chmod +x "$STAGE_DIR/bin/memgit"
  BIN_PATH="$STAGE_DIR/bin/memgit"
else
  echo "Unknown BUILD_MODE: $BUILD_MODE (expected auto|pyinstaller|zipapp)" >&2
  exit 1
fi

# Smoke-test built executable
"$BIN_PATH" --version
"$BIN_PATH" doctor --json >/dev/null

# Generate shell completions alongside binary
mkdir -p "$STAGE_DIR/completions"
"$BIN_PATH" completion zsh > "$STAGE_DIR/completions/_memgit"
"$BIN_PATH" completion bash > "$STAGE_DIR/completions/memgit.bash"
"$BIN_PATH" completion fish > "$STAGE_DIR/completions/memgit.fish"

# Copy final standalone binary to dist/memgit
cp "$BIN_PATH" "$DIST_DIR/memgit"
chmod +x "$DIST_DIR/memgit"

# Package release tarball
TAR_NAME="memgit-${VERSION}-${OS_RAW}-${ARCH}.tar.gz"
BUNDLE_DIR="$STAGE_DIR/memgit-${VERSION}-${OS_RAW}-${ARCH}"
mkdir -p "$BUNDLE_DIR/completions"
cp "$BIN_PATH" "$BUNDLE_DIR/memgit"
cp "$STAGE_DIR/completions/"* "$BUNDLE_DIR/completions/"
cp "$ROOT_DIR/README.md" "$ROOT_DIR/LICENSE" "$BUNDLE_DIR/"

tar -C "$STAGE_DIR" -czf "$DIST_DIR/$TAR_NAME" "memgit-${VERSION}-${OS_RAW}-${ARCH}"
echo "[MemGit] Created release tarball: $DIST_DIR/$TAR_NAME"

# Compute checksum
if command -v shasum >/dev/null 2>&1; then
  (cd "$DIST_DIR" && shasum -a 256 "$TAR_NAME" > "${TAR_NAME}.sha256")
elif command -v sha256sum >/dev/null 2>&1; then
  (cd "$DIST_DIR" && sha256sum "$TAR_NAME" > "${TAR_NAME}.sha256")
fi
echo "[MemGit] Binary & tarball build complete."
