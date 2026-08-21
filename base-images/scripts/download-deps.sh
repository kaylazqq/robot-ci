#!/usr/bin/env bash
# Fetch toolchain archives into base-images/deps/, reuse ${INSTALLERS:-/opt/ai/installers}.
if grep -q $'\r' "$0" 2>/dev/null; then
  sed -i 's/\r$//' "$0"
  exec bash "$0" "$@"
fi
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=/dev/null
source "$ROOT/versions.env"
DEPS="${OPS_DEPS_DIR:-$ROOT/deps}"
INSTALLERS="${INSTALLERS:-/opt/ai/installers}"
ONLY_FILES="${OPS_DEPS_ONLY_FILES:-}"

log() { printf '[download-deps] %s\n' "$*"; }
die() { printf '[download-deps] ERROR: %s\n' "$*" >&2; exit 1; }

validate_dependency() {
  local path="$1"
  [[ -s "$path" ]] || return 1
  tar -tf "$path" >/dev/null 2>&1
}

want_file() {
  local file="$1"
  [[ -z "$ONLY_FILES" ]] && return 0
  case ",${ONLY_FILES}," in
    *",${file},"*) return 0 ;;
    *) return 1 ;;
  esac
}

place_file() {
  local src="$1" dest="$2"
  [[ -n "$src" && -n "$dest" && -f "$src" ]] || return 1
  mkdir -p "$(dirname "$dest")"
  if [[ -e "$dest" ]] && [[ "$src" -ef "$dest" ]]; then
    return 0
  fi
  rm -f "$dest"
  ln "$src" "$dest" 2>/dev/null || cp -f "$src" "$dest"
}

cache_to_installers() {
  local file="$1"
  mkdir -p "$INSTALLERS" 2>/dev/null || return 0
  place_file "$DEPS/$file" "$INSTALLERS/$file" || true
}

reuse_local() {
  local file="$1"
  local dest="$DEPS/$file"
  mkdir -p "$DEPS"
  if validate_dependency "$dest"; then
    log "skip (deps): $file"
    cache_to_installers "$file"
    return 0
  fi
  if validate_dependency "$INSTALLERS/$file"; then
    place_file "$INSTALLERS/$file" "$dest"
    log "reuse installers: $INSTALLERS/$file"
    return 0
  fi
  return 1
}

download_one() {
  local file="$1"
  shift
  want_file "$file" || return 0
  if reuse_local "$file"; then
    return 0
  fi
  rm -f "$DEPS/$file"
  mkdir -p "$DEPS"
  local url download_ok dest="$DEPS/$file"
  for url in "$@"; do
    log "downloading $file <- $url"
    download_ok=0
    if command -v curl >/dev/null 2>&1; then
      if curl -fL --retry 3 --retry-all-errors --connect-timeout 30 \
          --speed-time 60 --speed-limit 10240 --max-time 3600 \
          --continue-at - -o "$dest.part" "$url"; then
        download_ok=1
      fi
    elif command -v wget >/dev/null 2>&1; then
      if wget -c -O "$dest.part" "$url"; then
        download_ok=1
      fi
    else
      die "need curl or wget"
    fi
    if [[ "$download_ok" == 1 ]] && validate_dependency "$dest.part"; then
      mv "$dest.part" "$dest"
      log "ok: $file ($(du -h "$dest" | awk '{print $1}'))"
      cache_to_installers "$file"
      return 0
    fi
    rm -f "$dest.part"
    log "failed: $url"
  done
  die "could not download $file"
}

main() {
  mkdir -p "$DEPS"
  download_one "OpenJDK21U-jdk_x64_linux_hotspot_${JDK_VERSION}_${JDK_BUILD}.tar.gz" \
    "https://ghfast.top/https://github.com/adoptium/temurin21-binaries/releases/download/jdk-${JDK_VERSION}%2B${JDK_BUILD}/OpenJDK21U-jdk_x64_linux_hotspot_${JDK_VERSION}_${JDK_BUILD}.tar.gz" \
    "https://github.com/adoptium/temurin21-binaries/releases/download/${JDK_TAG}/OpenJDK21U-jdk_x64_linux_hotspot_${JDK_VERSION}_${JDK_BUILD}.tar.gz"
  download_one "go${GO_BOOTSTRAP_VERSION}.linux-amd64.tar.gz" \
    "https://mirrors.aliyun.com/golang/go${GO_BOOTSTRAP_VERSION}.linux-amd64.tar.gz" \
    "https://go.dev/dl/go${GO_BOOTSTRAP_VERSION}.linux-amd64.tar.gz"
  download_one "go${GO_VERSION}.src.tar.gz" \
    "https://mirrors.aliyun.com/golang/go${GO_VERSION}.src.tar.gz" \
    "https://go.dev/dl/go${GO_VERSION}.src.tar.gz"
  download_one "node-v${NODE_VERSION}.tar.gz" \
    "https://npmmirror.com/mirrors/node/v${NODE_VERSION}/node-v${NODE_VERSION}.tar.gz" \
    "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}.tar.gz"
  log "deps ready (installers=$INSTALLERS):"
  ls -lh "$DEPS"
}

main "$@"
