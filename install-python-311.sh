#!/usr/bin/env bash
# Install shared Python 3.11.15 for mattermost packaging on the CI host.
set -euo pipefail

PY_VER=3.11.15
ARCHIVE="Python-${PY_VER}.tgz"
AI_INSTALLERS=/opt/ai/installers
PREFIX=/opt/ai/python-${PY_VER}
BUILD_ROOT=/home/ci/python-build

mkdir -p "$AI_INSTALLERS" /opt/ai "$BUILD_ROOT"

echo "==> build deps for compiling Python"
if command -v dnf >/dev/null 2>&1; then
  dnf install -y openssl-devel bzip2-devel libffi-devel xz-devel sqlite-devel \
    readline-devel ncurses-devel zlib-devel libuuid-devel make gcc gcc-c++ tar gzip xz
elif command -v yum >/dev/null 2>&1; then
  yum install -y openssl-devel bzip2-devel libffi-devel xz-devel sqlite-devel \
    readline-devel ncurses-devel zlib-devel libuuid-devel make gcc gcc-c++ tar gzip xz
fi

if [[ ! -s "$AI_INSTALLERS/$ARCHIVE" ]]; then
  echo "==> download $ARCHIVE"
  curl -fL --retry 3 -o "$AI_INSTALLERS/$ARCHIVE" \
    "https://mirrors.huaweicloud.com/python/${PY_VER}/${ARCHIVE}" \
    || curl -fL --retry 3 -o "$AI_INSTALLERS/$ARCHIVE" \
    "https://www.python.org/ftp/python/${PY_VER}/${ARCHIVE}"
fi
ls -lh "$AI_INSTALLERS/$ARCHIVE"

if [[ -x "${PREFIX}/bin/python3.11" ]]; then
  "${PREFIX}/bin/python3.11" - <<PY
import sys
print(sys.version)
raise SystemExit(0 if sys.version_info[:3] == (3, 11, 15) else 1)
PY
  echo "Python already OK"
  exit 0
fi

echo "==> compile Python ${PY_VER} (may take several minutes)"
rm -rf "${BUILD_ROOT}/Python-${PY_VER}"
tar -C "$BUILD_ROOT" -xzf "$AI_INSTALLERS/$ARCHIVE"
cd "${BUILD_ROOT}/Python-${PY_VER}"
./configure --prefix="$PREFIX" --enable-optimizations --with-ensurepip=install
make -j"$(nproc)"
make altinstall
ln -sfn "${PREFIX}/bin/python3.11" /usr/local/bin/python3.11
ln -sfn "${PREFIX}/bin/pip3.11" /usr/local/bin/pip3.11 2>/dev/null || true

"${PREFIX}/bin/python3.11" -V
echo "==> Python host prep OK"
