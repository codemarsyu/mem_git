#!/usr/bin/env bash
set -euo pipefail

# Universal macOS & Linux installer for MemGit CLI.
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/codemarsyu/mem_git/main/install.sh | sh
#   VERSION=0.2.0 INSTALL_DIR=/usr/local/bin sh install.sh

REPO="${MEMGIT_REPO:-codemarsyu/mem_git}"
INSTALL_DIR="${INSTALL_DIR:-$HOME/.local/bin}"
VERSION="${VERSION:-latest}"

OS_RAW="$(uname -s | tr '[:upper:]' '[:lower:]')"
case "$OS_RAW" in
  darwin|linux) OS="$OS_RAW" ;;
  *)
    echo "Unsupported OS: $OS_RAW (MemGit supports macOS and Linux)" >&2
    exit 1
    ;;
esac

ARCH_RAW="$(uname -m)"
case "$ARCH_RAW" in
  arm64|aarch64) ARCH="arm64" ;;
  x86_64|amd64)  ARCH="x86_64" ;;
  *)
    echo "Unsupported CPU architecture: $ARCH_RAW" >&2
    exit 1
    ;;
esac

if [[ "$VERSION" == "latest" ]]; then
  TAG="$(curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)"
  if [[ -z "$TAG" ]]; then
    echo "Could not resolve latest release tag from GitHub (${REPO}). Set VERSION=x.y.z explicitly or install via pipx." >&2
    exit 1
  fi
  VER_NUM="${TAG#v}"
else
  VER_NUM="${VERSION#v}"
  TAG="v${VER_NUM}"
fi

TAR_NAME="memgit-${VER_NUM}-${OS}-${ARCH}.tar.gz"
BASE_URL="https://github.com/${REPO}/releases/download/${TAG}"

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/memgit_install_XXXXXX")"
trap 'rm -rf "$TMP_DIR"' EXIT

echo "[MemGit] Downloading ${TAR_NAME} (${TAG}) ..."
curl -fsSL "${BASE_URL}/${TAR_NAME}" -o "${TMP_DIR}/${TAR_NAME}"

if curl -fsSL "${BASE_URL}/SHA256SUMS" -o "${TMP_DIR}/SHA256SUMS" 2>/dev/null; then
  EXPECTED="$(grep " ${TAR_NAME}\$" "${TMP_DIR}/SHA256SUMS" | awk '{print $1}' | head -n 1 || true)"
  if [[ -n "$EXPECTED" ]]; then
    if command -v shasum >/dev/null 2>&1; then
      ACTUAL="$(shasum -a 256 "${TMP_DIR}/${TAR_NAME}" | awk '{print $1}')"
    else
      ACTUAL="$(sha256sum "${TMP_DIR}/${TAR_NAME}" | awk '{print $1}')"
    fi
    if [[ "$ACTUAL" != "$EXPECTED" ]]; then
      echo "Checksum verification failed for ${TAR_NAME}!" >&2
      echo "  expected: ${EXPECTED}" >&2
      echo "  actual:   ${ACTUAL}" >&2
      exit 1
    fi
    echo "[MemGit] SHA256 checksum verified."
  fi
fi

tar -xzf "${TMP_DIR}/${TAR_NAME}" -C "${TMP_DIR}"
mkdir -p "${INSTALL_DIR}"
install -m 0755 "${TMP_DIR}/memgit-${VER_NUM}-${OS}-${ARCH}/memgit" "${INSTALL_DIR}/memgit"

echo "[MemGit] Installed to ${INSTALL_DIR}/memgit"
"${INSTALL_DIR}/memgit" --version
case ":${PATH}:" in
  *":${INSTALL_DIR}:"*) ;;
  *)
    echo ""
    echo "Note: Add ${INSTALL_DIR} to your PATH:"
    echo "  export PATH=\"${INSTALL_DIR}:\$PATH\""
    ;;
esac
