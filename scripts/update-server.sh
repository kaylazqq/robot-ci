#!/usr/bin/env bash
# In-place upgrade of an existing CI install. Never replaces the app directory.
# Runtime state stays on disk: data/ (SQLite), config.json, logs/.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/swr-push-helper}"
cd "$APP_DIR"

HOME_GIT="${HOME_GIT:-/tmp/robot-ci-git-home}"
mkdir -p "$HOME_GIT"
printf '[safe]\n\tdirectory = *\n' > "$HOME_GIT/.gitconfig"
export HOME="$HOME_GIT"

if [[ ! -d .git ]]; then
  echo "ERROR: $APP_DIR is not a git checkout." >&2
  echo "First install: git clone the repo into $APP_DIR (keep an existing data/ and config.json)." >&2
  echo "Tarball overlay: bash scripts/apply-release.sh /path/to/robot-ci.tar.gz" >&2
  exit 1
fi

git fetch origin main
git pull --ff-only origin main

PYTHON_BIN="$(command -v python3.11 || command -v python3)"
if [[ -f requirements.txt ]]; then
  "$PYTHON_BIN" -m pip install --disable-pip-version-check -q -r requirements.txt
fi

"$PYTHON_BIN" - <<'PY'
import json
from pathlib import Path
plans = json.loads(Path("test-plans.json").read_text(encoding="utf-8"))
profile = plans.get("services", {}).get("config-service")
print("config-service profile:", profile)
assert profile == "config-service-go", "config-service not registered in test-plans.json"
PY

if systemctl is-active --quiet swr-push-helper.service 2>/dev/null; then
  systemctl restart swr-push-helper.service
  sleep 1
  systemctl --no-pager --full status swr-push-helper.service || true
else
  echo "WARN: swr-push-helper.service not found; restart server.py manually"
fi

echo "updated $(git rev-parse --short HEAD); kept data/ config.json logs/"
