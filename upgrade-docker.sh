#!/usr/bin/env bash
# Upgrade HCE docker-engine 18.09 → modern Docker static binaries + BuildKit/buildx
set -euo pipefail

DOCKER_VER="${DOCKER_VER:-27.5.1}"
ARCH=x86_64
TMP=/tmp/docker-upgrade-$$
mkdir -p "$TMP"
cd "$TMP"

echo "==> stop old docker"
systemctl stop docker 2>/dev/null || true
systemctl stop docker.socket 2>/dev/null || true

echo "==> remove HCE docker-engine 18.09 (keep images/volumes under /var/lib/docker)"
dnf remove -y docker-engine docker-runc 2>/dev/null || yum remove -y docker-engine docker-runc 2>/dev/null || true

echo "==> download docker ${DOCKER_VER} static"
curl -fL --retry 3 -o docker.tgz \
  "https://download.docker.com/linux/static/stable/${ARCH}/docker-${DOCKER_VER}.tgz"
tar xzf docker.tgz
install -m 755 docker/docker docker/dockerd docker/containerd docker/containerd-shim-runc-v2 \
  docker/ctr docker/runc docker/docker-init docker/docker-proxy /usr/bin/

# buildx plugin
mkdir -p /usr/local/lib/docker/cli-plugins
BUILDX_VER="${BUILDX_VER:-v0.21.1}"
curl -fL --retry 3 -o /usr/local/lib/docker/cli-plugins/docker-buildx \
  "https://github.com/docker/buildx/releases/download/${BUILDX_VER}/buildx-${BUILDX_VER}.linux-amd64"
chmod +x /usr/local/lib/docker/cli-plugins/docker-buildx
mkdir -p /usr/libexec/docker/cli-plugins /usr/lib/docker/cli-plugins
ln -sfn /usr/local/lib/docker/cli-plugins/docker-buildx /usr/libexec/docker/cli-plugins/docker-buildx
ln -sfn /usr/local/lib/docker/cli-plugins/docker-buildx /usr/lib/docker/cli-plugins/docker-buildx

# docker group
getent group docker >/dev/null || groupadd docker

mkdir -p /etc/docker
cat >/etc/docker/daemon.json <<'EOF'
{
  "features": { "buildkit": true },
  "log-driver": "json-file",
  "log-opts": { "max-size": "50m", "max-file": "3" }
}
EOF

# systemd unit
cat >/etc/systemd/system/docker.service <<'EOF'
[Unit]
Description=Docker Application Container Engine
Documentation=https://docs.docker.com
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
ExecStart=/usr/bin/dockerd -H unix:///var/run/docker.sock
ExecReload=/bin/kill -s HUP $MAINPID
TimeoutStartSec=0
Restart=on-failure
RestartSec=5
LimitNOFILE=1048576
LimitNPROC=infinity
LimitCORE=infinity
TasksMax=infinity
Delegate=yes
KillMode=process
OOMScoreAdjust=-500

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now docker.service
sleep 2
docker version
docker buildx version
docker info >/tmp/docker-info.txt 2>&1 || true
head -20 /tmp/docker-info.txt || true

# helper: ensure BuildKit for builds from systemd
if [[ -f /etc/systemd/system/swr-push-helper.service ]]; then
  mkdir -p /etc/systemd/system/swr-push-helper.service.d
  cat >/etc/systemd/system/swr-push-helper.service.d/docker.conf <<'EOF'
[Service]
Environment=DOCKER_BUILDKIT=1
Environment=BUILDKIT_PROGRESS=plain
EOF
  systemctl daemon-reload
  systemctl restart swr-push-helper.service || true
fi

# quick parse test for --mount
cat >/tmp/Dockerfile.mounttest <<'EOF'
# syntax=docker/dockerfile:1
FROM alpine:3.20
RUN --mount=type=cache,target=/var/cache/apk apk add --no-cache curl
EOF
echo "==> smoke: docker build with --mount"
docker build -t mounttest:ok -f /tmp/Dockerfile.mounttest /tmp
docker rmi mounttest:ok >/dev/null || true
rm -rf "$TMP" /tmp/Dockerfile.mounttest
echo "==> Docker upgrade OK"
