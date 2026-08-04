#!/usr/bin/env bash
# Install shared Python 3.11.15 for mattermost packaging (public-service contract).
set -euo pipefail

PY_VER=3.11.15
ARCHIVE="Python-${PY_VER}.tgz"
PS_DEPS=/var/lib/swr-workspaces/public-service/deps
AI_INSTALLERS=/opt/ai/installers
PS_ROOT=/var/lib/swr-workspaces/public-service

mkdir -p "$PS_DEPS" "$AI_INSTALLERS"

echo "==> build deps for compiling Python"
if command -v dnf >/dev/null 2>&1; then
  dnf install -y openssl-devel bzip2-devel libffi-devel xz-devel sqlite-devel \
    readline-devel ncurses-devel zlib-devel libuuid-devel make gcc gcc-c++ tar gzip xz
elif command -v yum >/dev/null 2>&1; then
  yum install -y openssl-devel bzip2-devel libffi-devel xz-devel sqlite-devel \
    readline-devel ncurses-devel zlib-devel libuuid-devel make gcc gcc-c++ tar gzip xz
fi

if [[ ! -s "$PS_DEPS/$ARCHIVE" ]]; then
  echo "==> download $ARCHIVE"
  curl -fL --retry 3 -o "$PS_DEPS/$ARCHIVE" \
    "https://mirrors.huaweicloud.com/python/${PY_VER}/${ARCHIVE}" \
    || curl -fL --retry 3 -o "$PS_DEPS/$ARCHIVE" \
    "https://www.python.org/ftp/python/${PY_VER}/${ARCHIVE}"
fi
cp -f "$PS_DEPS/$ARCHIVE" "$AI_INSTALLERS/$ARCHIVE"
ls -lh "$PS_DEPS/$ARCHIVE"

if [[ -x /opt/ai/python-${PY_VER}/bin/python3.11 ]]; then
  /opt/ai/python-${PY_VER}/bin/python3.11 - <<PY
import sys
print(sys.version)
raise SystemExit(0 if sys.version_info[:3] == (3, 11, 15) else 1)
PY
  echo "Python already OK"
  exit 0
fi

echo "==> ensure-python.sh (compile may take several minutes)"
export PUBLIC_SERVICE_ROOT="$PS_ROOT"
export AI_ROOT=/opt/ai
bash "$PS_ROOT/scripts/ensure-python.sh"

/opt/ai/python-${PY_VER}/bin/python3.11 -V
echo "==> Python host prep OK"
