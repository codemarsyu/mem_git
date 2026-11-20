#!/usr/bin/env bash
set -euo pipefail

# Build installable Linux .deb and .rpm packages using nfpm (used in CI Releases),
# with a fallback to dpkg-deb if nfpm is unavailable locally.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="${DIST_DIR:-$ROOT_DIR/dist}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

VERSION="$("$PYTHON_BIN" -c 'import sys; sys.path.insert(0, "'"$ROOT_DIR"'"); import memgit; print(memgit.__version__)')"
ARCH_RAW="$(uname -m)"
case "$ARCH_RAW" in
  x86_64|amd64)  ARCH="amd64"; RPM_ARCH="x86_64" ;;
  arm64|aarch64) ARCH="arm64"; RPM_ARCH="aarch64" ;;
  *)             ARCH="$ARCH_RAW"; RPM_ARCH="$ARCH_RAW" ;;
esac

if [[ ! -x "$DIST_DIR/memgit" ]]; then
  DIST_DIR="$DIST_DIR" "$ROOT_DIR/packaging/build_binary.sh"
fi

mkdir -p "$DIST_DIR/completions"
"$DIST_DIR/memgit" completion zsh  > "$DIST_DIR/completions/_memgit"
"$DIST_DIR/memgit" completion bash > "$DIST_DIR/completions/memgit.bash"
"$DIST_DIR/memgit" completion fish > "$DIST_DIR/completions/memgit.fish"

DEB_OUT="$DIST_DIR/memgit_${VERSION}_linux_${ARCH}.deb"
RPM_OUT="$DIST_DIR/memgit-${VERSION}-1.${RPM_ARCH}.rpm"

if command -v nfpm >/dev/null 2>&1; then
  echo "[MemGit] Building Linux .deb and .rpm via nfpm (v${VERSION}, ${ARCH}) ..."
  (
    cd "$ROOT_DIR"
    VERSION="$VERSION" ARCH="$ARCH" nfpm package -f packaging/nfpm.yaml -p deb -t "$DEB_OUT"
    VERSION="$VERSION" ARCH="$ARCH" nfpm package -f packaging/nfpm.yaml -p rpm -t "$RPM_OUT"
  )
  echo "[MemGit] Built: $DEB_OUT"
  echo "[MemGit] Built: $RPM_OUT"
elif command -v dpkg-deb >/dev/null 2>&1; then
  echo "[MemGit] nfpm not found; building .deb via dpkg-deb ..."
  DEB_ROOT="$DIST_DIR/deb_root"
  rm -rf "$DEB_ROOT"
  mkdir -p \
    "$DEB_ROOT/DEBIAN" \
    "$DEB_ROOT/usr/local/bin" \
    "$DEB_ROOT/usr/share/zsh/vendor-completions" \
    "$DEB_ROOT/usr/share/bash-completion/completions" \
    "$DEB_ROOT/usr/share/fish/vendor_completions.d"
  install -m 0755 "$DIST_DIR/memgit" "$DEB_ROOT/usr/local/bin/memgit"
  install -m 0644 "$DIST_DIR/completions/_memgit" "$DEB_ROOT/usr/share/zsh/vendor-completions/_memgit"
  install -m 0644 "$DIST_DIR/completions/memgit.bash" "$DEB_ROOT/usr/share/bash-completion/completions/memgit"
  install -m 0644 "$DIST_DIR/completions/memgit.fish" "$DEB_ROOT/usr/share/fish/vendor_completions.d/memgit.fish"
  cat > "$DEB_ROOT/DEBIAN/control" <<EOF
Package: memgit
Version: ${VERSION}
Section: devel
Priority: optional
Architecture: ${ARCH}
Maintainer: MemGit Team <maintainers@memgit.dev>
Description: Cross-Agent & Cross-Machine AI Coding Agent Memory CLI
 Discover, checkpoint, sanitize, transform, and resume sessions across
 Claude Desktop, Claude Code, Google Antigravity, Gemini CLI, Codex CLI, and Cursor.
EOF
  dpkg-deb --build --root-owner-group "$DEB_ROOT" "$DEB_OUT"
  echo "[MemGit] Built: $DEB_OUT"
else
  echo "Error: neither 'nfpm' nor 'dpkg-deb' is installed." >&2
  exit 1
fi

for pkg in "$DEB_OUT" "$RPM_OUT"; do
  if [[ -f "$pkg" ]]; then
    if command -v sha256sum >/dev/null 2>&1; then
      (cd "$DIST_DIR" && sha256sum "$(basename "$pkg")" > "$(basename "$pkg").sha256")
    elif command -v shasum >/dev/null 2>&1; then
      (cd "$DIST_DIR" && shasum -a 256 "$(basename "$pkg")" > "$(basename "$pkg").sha256")
    fi
  fi
done
