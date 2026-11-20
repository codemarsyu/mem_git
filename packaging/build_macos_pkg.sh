#!/usr/bin/env bash
set -euo pipefail
export COPYFILE_DISABLE=1

# Build an installable macOS .pkg installer via Apple's native pkgbuild + productbuild.
# Installs:
#   /usr/local/bin/memgit
#   /usr/local/share/zsh/site-functions/_memgit
#   /usr/local/share/bash-completion/completions/memgit
#
# Optional signing:
#   Set APPLE_INSTALLER_CERT="Developer ID Installer: ..." to sign the final .pkg.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="${DIST_DIR:-$ROOT_DIR/dist}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "Error: build_macos_pkg.sh must be run on macOS." >&2
  exit 1
fi

VERSION="$("$PYTHON_BIN" -c 'import sys; sys.path.insert(0, "'"$ROOT_DIR"'"); import memgit; print(memgit.__version__)')"
ARCH_RAW="$(uname -m)"
ARCH="$([[ "$ARCH_RAW" == "arm64" ]] && echo "arm64" || echo "x86_64")"

# Build binary first if not already staged
if [[ ! -x "$DIST_DIR/memgit" ]]; then
  DIST_DIR="$DIST_DIR" "$ROOT_DIR/packaging/build_binary.sh"
fi

PKG_ROOT="$DIST_DIR/macos_pkg_root"
rm -rf "$PKG_ROOT"
mkdir -p \
  "$PKG_ROOT/usr/local/bin" \
  "$PKG_ROOT/usr/local/share/zsh/site-functions" \
  "$PKG_ROOT/usr/local/share/bash-completion/completions"

install -m 0755 "$DIST_DIR/memgit" "$PKG_ROOT/usr/local/bin/memgit"
"$DIST_DIR/memgit" completion zsh > "$PKG_ROOT/usr/local/share/zsh/site-functions/_memgit"
"$DIST_DIR/memgit" completion bash > "$PKG_ROOT/usr/local/share/bash-completion/completions/memgit"
chmod 0644 \
  "$PKG_ROOT/usr/local/share/zsh/site-functions/_memgit" \
  "$PKG_ROOT/usr/local/share/bash-completion/completions/memgit"
xattr -cr "$PKG_ROOT" 2>/dev/null || true

COMPONENT_PKG="$DIST_DIR/memgit-${VERSION}-component.pkg"
FINAL_PKG="$DIST_DIR/memgit-${VERSION}-darwin-${ARCH}.pkg"

pkgbuild \
  --root "$PKG_ROOT" \
  --identifier "io.memgit.cli" \
  --version "$VERSION" \
  --install-location "/" \
  "$COMPONENT_PKG"

if [[ -n "${APPLE_INSTALLER_CERT:-}" ]]; then
  productbuild \
    --package "$COMPONENT_PKG" \
    --sign "$APPLE_INSTALLER_CERT" \
    "$FINAL_PKG"
else
  productbuild \
    --package "$COMPONENT_PKG" \
    "$FINAL_PKG"
fi

rm -f "$COMPONENT_PKG"
(cd "$DIST_DIR" && shasum -a 256 "$(basename "$FINAL_PKG")" > "$(basename "$FINAL_PKG").sha256")
echo "[MemGit] Built macOS installer package: $FINAL_PKG"
