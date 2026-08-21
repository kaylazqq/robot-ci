#!/usr/bin/env bash
# Build shared Ubuntu / JDK / Go / Node bases.
# Compatible with Docker 18.09 (no buildx / BuildKit mounts required).
if grep -q $'\r' "$0" 2>/dev/null; then
  sed -i 's/\r$//' "$0"
  exec bash "$0" "$@"
fi
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=/dev/null
source "$ROOT/versions.env"

BUILD_UBUNTU=0
BUILD_JDK=0
BUILD_GO=0
BUILD_NODE=0

log() { printf '[ops-bases] %s\n' "$*"; }
die() { printf '[ops-bases] ERROR: %s\n' "$*" >&2; exit 1; }

image_exists() { docker image inspect "$1" >/dev/null 2>&1; }

ensure_local_base() {
  local local_tag="$1" source_tag="$2"
  image_exists "$local_tag" && return 0
  if ! image_exists "$source_tag"; then
    log "pulling $source_tag"
    docker pull "$source_tag"
  fi
  docker tag "$source_tag" "$local_tag"
}

build_image() {
  local tag="$1" dockerfile="$2"
  shift 2
  log "building $tag"
  docker build --network=host --tag "$tag" --file "$dockerfile" "$@" "$ROOT"
}

select_targets() {
  [[ $# -gt 0 ]] || set -- all
  local target
  for target in "$@"; do
    case "$target" in
      all) BUILD_UBUNTU=1; BUILD_JDK=1; BUILD_GO=1; BUILD_NODE=1 ;;
      ubuntu) BUILD_UBUNTU=1 ;;
      jdk) BUILD_UBUNTU=1; BUILD_JDK=1 ;;
      go) BUILD_UBUNTU=1; BUILD_GO=1 ;;
      node) BUILD_UBUNTU=1; BUILD_NODE=1 ;;
      *) die "unknown target: $target (all|ubuntu|jdk|go|node)" ;;
    esac
  done
}

main() {
  select_targets "$@"
  command -v docker >/dev/null || die "docker missing"
  docker info >/dev/null 2>&1 || die "docker daemon unavailable"

  if [[ "$BUILD_UBUNTU" -eq 1 ]]; then
    ensure_local_base "$UBUNTU_BUILD_IMAGE" "$UBUNTU_SOURCE_IMAGE"
    ensure_local_base "$UBUNTU_RUNTIME_IMAGE" "$UBUNTU_SOURCE_IMAGE"
    log "ubuntu bases ready: $UBUNTU_BUILD_IMAGE $UBUNTU_RUNTIME_IMAGE"
  fi

  if [[ "$BUILD_JDK" -eq 1 ]]; then
    if image_exists "$JDK_BUILD_IMAGE" && image_exists "$JDK_RUNTIME_IMAGE"; then
      log "reuse JDK bases: $JDK_BUILD_IMAGE $JDK_RUNTIME_IMAGE"
    else
      OPS_DEPS_ONLY_FILES="OpenJDK21U-jdk_x64_linux_hotspot_${JDK_VERSION}_${JDK_BUILD}.tar.gz" \
        bash "$SCRIPT_DIR/download-deps.sh"
      build_image "$JDK_BUILD_IMAGE" "$ROOT/docker/jdk-runtime.Dockerfile" \
        --target jdk-build \
        --build-arg "BUILD_BASE_IMAGE=$UBUNTU_BUILD_IMAGE" \
        --build-arg "RUNTIME_BASE_IMAGE=$UBUNTU_RUNTIME_IMAGE" \
        --build-arg "UBUNTU_APT_MIRROR=$UBUNTU_APT_MIRROR" \
        --build-arg "BOOT_JDK_ARCHIVE=deps/OpenJDK21U-jdk_x64_linux_hotspot_${JDK_VERSION}_${JDK_BUILD}.tar.gz"
      build_image "$JDK_RUNTIME_IMAGE" "$ROOT/docker/jdk-runtime.Dockerfile" \
        --target jdk-runtime \
        --build-arg "BUILD_BASE_IMAGE=$UBUNTU_BUILD_IMAGE" \
        --build-arg "RUNTIME_BASE_IMAGE=$UBUNTU_RUNTIME_IMAGE" \
        --build-arg "UBUNTU_APT_MIRROR=$UBUNTU_APT_MIRROR" \
        --build-arg "BOOT_JDK_ARCHIVE=deps/OpenJDK21U-jdk_x64_linux_hotspot_${JDK_VERSION}_${JDK_BUILD}.tar.gz"
    fi
  fi

  if [[ "$BUILD_GO" -eq 1 ]]; then
    if image_exists "$GO_TOOLCHAIN_IMAGE"; then
      log "reuse Go toolchain: $GO_TOOLCHAIN_IMAGE"
    else
      OPS_DEPS_ONLY_FILES="go${GO_BOOTSTRAP_VERSION}.linux-amd64.tar.gz,go${GO_VERSION}.src.tar.gz" \
        bash "$SCRIPT_DIR/download-deps.sh"
      build_image "$GO_TOOLCHAIN_IMAGE" "$ROOT/docker/go-toolchain.Dockerfile" \
        --target go-toolchain \
        --build-arg "BUILD_BASE_IMAGE=$UBUNTU_BUILD_IMAGE" \
        --build-arg "UBUNTU_APT_MIRROR=$UBUNTU_APT_MIRROR" \
        --build-arg "GO_VERSION=$GO_VERSION" \
        --build-arg "GO_BOOTSTRAP_ARCHIVE=deps/go${GO_BOOTSTRAP_VERSION}.linux-amd64.tar.gz" \
        --build-arg "GO_SOURCE_ARCHIVE=deps/go${GO_VERSION}.src.tar.gz"
    fi
  fi

  if [[ "$BUILD_NODE" -eq 1 ]]; then
    if image_exists "$NODE_BUILD_IMAGE" && image_exists "$NODE_RUNTIME_IMAGE"; then
      log "reuse Node bases: $NODE_BUILD_IMAGE $NODE_RUNTIME_IMAGE"
    else
      OPS_DEPS_ONLY_FILES="node-v${NODE_VERSION}-linux-x64.tar.gz" \
        bash "$SCRIPT_DIR/download-deps.sh"
      build_image "$NODE_BUILD_IMAGE" "$ROOT/docker/node-runtime.Dockerfile" \
        --target node-build \
        --build-arg "BUILD_BASE_IMAGE=$UBUNTU_BUILD_IMAGE" \
        --build-arg "RUNTIME_BASE_IMAGE=$UBUNTU_RUNTIME_IMAGE" \
        --build-arg "UBUNTU_APT_MIRROR=$UBUNTU_APT_MIRROR" \
        --build-arg "NODE_VERSION=$NODE_VERSION" \
        --build-arg "NODE_BINARY_ARCHIVE=deps/node-v${NODE_VERSION}-linux-x64.tar.gz"
      build_image "$NODE_RUNTIME_IMAGE" "$ROOT/docker/node-runtime.Dockerfile" \
        --target node-runtime \
        --build-arg "BUILD_BASE_IMAGE=$UBUNTU_BUILD_IMAGE" \
        --build-arg "RUNTIME_BASE_IMAGE=$UBUNTU_RUNTIME_IMAGE" \
        --build-arg "UBUNTU_APT_MIRROR=$UBUNTU_APT_MIRROR" \
        --build-arg "NODE_VERSION=$NODE_VERSION" \
        --build-arg "NODE_BINARY_ARCHIVE=deps/node-v${NODE_VERSION}-linux-x64.tar.gz"
    fi
  fi

  log "done"
  docker images 'local/ai-*'
}

main "$@"
