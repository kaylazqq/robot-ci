#!/usr/bin/env bash
# Server bootstrap for Huawei Cloud EulerOS / common Linux.
# Run on server after files are in /opt/swr-push-helper
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/swr-push-helper}"
PORT="${SWR_PORT:-18888}"
KEY="${GITHUB_SSH_KEY:-/root/.ssh/id_ed25519_github}"
ARCHIVE_ROOT="${SWR_ARCHIVE_ROOT:-/usr/share/nginx/html/images}"

cd "$APP_DIR"

echo "==> install deps"
if command -v dnf >/dev/null 2>&1; then
  dnf install -y python3 git curl tar gzip which
elif command -v yum >/dev/null 2>&1; then
  yum install -y python3 git curl tar gzip which
elif command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq python3 git curl ca-certificates
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "==> install docker (HCE/native packages preferred)"
  if command -v dnf >/dev/null 2>&1; then
    dnf install -y docker-engine docker-runc || dnf install -y docker || true
  elif command -v yum >/dev/null 2>&1; then
    yum install -y docker-engine docker-runc || yum install -y docker || true
  fi
  if ! command -v docker >/dev/null 2>&1; then
    # Fallback for common Linux; HCE may not be supported by get.docker.com
    curl -fsSL https://get.docker.com | sh || true
  fi
fi
if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker not installed" >&2
  exit 1
fi
systemctl enable --now docker 2>/dev/null || service docker start || true
# wait briefly for daemon
for i in 1 2 3 4 5; do
  docker info >/dev/null 2>&1 && break
  sleep 2
done
docker info >/dev/null

PYTHON_BIN="$(command -v python3.11 || true)"
if [[ -z "$PYTHON_BIN" ]]; then
  echo "ERROR: python3.11 is required; run install-python-311.sh first" >&2
  exit 1
fi

echo "==> GitHub SSH key"
mkdir -p /root/.ssh
chmod 700 /root/.ssh
if [[ ! -f "$KEY" ]]; then
  ssh-keygen -t ed25519 -C "swr-push-helper@$(hostname)" -N "" -f "$KEY"
fi
chmod 600 "$KEY"
chmod 644 "${KEY}.pub"
# github known_hosts
ssh-keyscan -t ed25519,rsa github.com >> /root/.ssh/known_hosts 2>/dev/null || true
sort -u /root/.ssh/known_hosts -o /root/.ssh/known_hosts
chmod 644 /root/.ssh/known_hosts

cat > /root/.ssh/config <<EOF
Host github.com
  HostName github.com
  User git
  IdentityFile ${KEY}
  IdentitiesOnly yes
  BatchMode yes
EOF
chmod 600 /root/.ssh/config

mkdir -p /var/lib/swr-workspaces
chmod 700 /var/lib/swr-workspaces

echo "==> nginx image archive dir"
mkdir -p "$ARCHIVE_ROOT"
chmod 755 "$ARCHIVE_ROOT" || true

export APP_DIR PORT KEY ARCHIVE_ROOT
python3 <<'PY'
import json, os
from pathlib import Path

app_dir = Path(os.environ["APP_DIR"])
cfg = {
    "host": "0.0.0.0",
    "port": int(os.environ.get("PORT") or 18888),
    "allow_remote": True,
    "swr_registry": "swr.cn-southwest-2.myhuaweicloud.com",
    "swr_org": "public_ai",
    "workspace_root": "/var/lib/swr-workspaces",
    "github_use_ssh": True,
    "github_ssh_key": os.environ.get("KEY") or "",
    "github_token": "",
    "build_timeout_sec": 7200,
    "archive_enabled": True,
    "archive_required": True,
    "archive_root": os.environ.get("ARCHIVE_ROOT") or "/usr/share/nginx/html/images",
}
(app_dir / "config.json").write_text(
    json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
print("wrote config.json")
PY
chmod 600 "$APP_DIR/config.json"

cat > /etc/systemd/system/swr-push-helper.service <<EOF
[Unit]
Description=SWR Push Helper
After=network.target docker.service
Wants=docker.service

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
ExecStart=${PYTHON_BIN} ${APP_DIR}/server.py
Restart=on-failure
RestartSec=3
Environment=SWR_ALLOW_REMOTE=1

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now swr-push-helper.service
sleep 1
systemctl --no-pager --full status swr-push-helper.service || true

# open firewall if firewalld present
if command -v firewall-cmd >/dev/null 2>&1; then
  firewall-cmd --permanent --add-port=${PORT}/tcp || true
  firewall-cmd --reload || true
fi

echo
echo "======== ADD THIS SSH PUBLIC KEY TO GITHUB ========"
echo "GitHub → Settings → SSH and GPG keys → New SSH key"
echo
cat "${KEY}.pub"
echo
echo "===================================================="
echo "Then test: ssh -T git@github.com"
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo "Web UI: http://${IP:-YOUR_SERVER_IP}:${PORT}/"
echo "Image archive: ${ARCHIVE_ROOT}/YYYYMMDDHHMMSS/<image>_<tag>.tar"
