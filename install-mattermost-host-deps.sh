#!/usr/bin/env bash
# Host prep for mattermost packaging on the shared build server.
# Does not modify microservice source trees.
set -euo pipefail

GO_VERSION=1.25.9
NODE_VERSION=24.11.1
GO_ARCHIVE="go${GO_VERSION}.linux-amd64.tar.gz"
NODE_ARCHIVE="node-v${NODE_VERSION}-linux-x64.tar.xz"
PS_DEPS=/var/lib/swr-workspaces/public-service/deps
MM_INSTALLERS=/var/lib/swr-workspaces/mattermost/deploy/installers
AI_INSTALLERS=/opt/ai/installers

echo "==> yum/dnf packages"
if command -v dnf >/dev/null 2>&1; then
  dnf install -y make gcc gcc-c++ rsync curl tar gzip xz which git
elif command -v yum >/dev/null 2>&1; then
  yum install -y make gcc gcc-c++ rsync curl tar gzip xz which git
fi

mkdir -p "$PS_DEPS" "$AI_INSTALLERS" /usr/local

echo "==> Go ${GO_VERSION}"
if [[ ! -x /usr/local/go/bin/go ]] || ! /usr/local/go/bin/go version 2>/dev/null | grep -q "go${GO_VERSION}"; then
  if [[ ! -s "$PS_DEPS/$GO_ARCHIVE" ]]; then
    echo "download $GO_ARCHIVE"
    curl -fL --retry 3 -o "$PS_DEPS/$GO_ARCHIVE" \
      "https://mirrors.aliyun.com/golang/${GO_ARCHIVE}" \
      || curl -fL --retry 3 -o "$PS_DEPS/$GO_ARCHIVE" \
      "https://go.dev/dl/${GO_ARCHIVE}"
  fi
  cp -f "$PS_DEPS/$GO_ARCHIVE" "$AI_INSTALLERS/$GO_ARCHIVE"
  rm -rf /usr/local/go
  tar -C /usr/local -xzf "$PS_DEPS/$GO_ARCHIVE"
fi
ln -sfn /usr/local/go/bin/go /usr/local/bin/go
ln -sfn /usr/local/go/bin/gofmt /usr/local/bin/gofmt
cat >/etc/profile.d/go.sh <<EOF
export PATH=/usr/local/go/bin:\$PATH
export GOPROXY=https://goproxy.cn,direct
EOF
/usr/local/go/bin/go version

echo "==> Node ${NODE_VERSION}"
NODE_HOME="/usr/local/node-v${NODE_VERSION}-linux-x64"
if [[ ! -x "$NODE_HOME/bin/node" ]]; then
  SRC=""
  for c in "$MM_INSTALLERS/$NODE_ARCHIVE" "$AI_INSTALLERS/$NODE_ARCHIVE"; do
    [[ -s "$c" ]] && SRC="$c" && break
  done
  if [[ -z "$SRC" ]]; then
    echo "download $NODE_ARCHIVE"
    curl -fL --retry 3 -o "$AI_INSTALLERS/$NODE_ARCHIVE" \
      "https://npmmirror.com/mirrors/node/v${NODE_VERSION}/${NODE_ARCHIVE}" \
      || curl -fL --retry 3 -o "$AI_INSTALLERS/$NODE_ARCHIVE" \
      "https://nodejs.org/dist/v${NODE_VERSION}/${NODE_ARCHIVE}"
    SRC="$AI_INSTALLERS/$NODE_ARCHIVE"
  fi
  rm -rf "$NODE_HOME"
  mkdir -p "$NODE_HOME"
  tar -xJf "$SRC" -C "$NODE_HOME" --strip-components=1
fi
ln -sfn "$NODE_HOME/bin/node" /usr/local/bin/node
ln -sfn "$NODE_HOME/bin/npm" /usr/local/bin/npm
ln -sfn "$NODE_HOME/bin/npx" /usr/local/bin/npx
cat >/etc/profile.d/node.sh <<EOF
export PATH=${NODE_HOME}/bin:\$PATH
EOF
node --version
npm --version

echo "==> PATH for swr-push-helper systemd"
mkdir -p /etc/systemd/system/swr-push-helper.service.d
cat >/etc/systemd/system/swr-push-helper.service.d/path.conf <<EOF
[Service]
Environment=PATH=/usr/local/go/bin:${NODE_HOME}/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
Environment=GOPROXY=https://goproxy.cn,direct
Environment=DOCKER_BUILDKIT=1
Environment=BUILDKIT_PROGRESS=plain
EOF
systemctl daemon-reload
systemctl restart swr-push-helper

echo "==> verify"
for c in go node npm make gcc rsync git docker python3 tar sha256sum curl; do
  printf "%-10s " "$c"
  command -v "$c" && "$c" --version 2>&1 | head -1 || echo MISSING
done
systemctl is-active swr-push-helper
echo "==> host prep OK"
