#!/usr/bin/env bash
# Pull latest robot-ci on the build server and restart the helper service.
# Run on the Linux CI host (default install: /opt/swr-push-helper).
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/swr-push-helper}"
cd "$APP_DIR"

if [[ -d .git ]]; then
  git pull --ff-only origin main
else
  echo "ERROR: $APP_DIR is not a git checkout; copy or clone robot-ci first" >&2
  exit 1
fi

if systemctl is-active --quiet swr-push-helper.service 2>/dev/null; then
  systemctl restart swr-push-helper.service
  systemctl --no-pager --full status swr-push-helper.service || true
else
  echo "WARN: swr-push-helper.service not found; restart server.py manually"
fi

python3 - <<'PY'
import json
from pathlib import Path
plans = json.loads(Path("test-plans.json").read_text(encoding="utf-8"))
profile = plans.get("services", {}).get("config-service")
print("config-service profile:", profile)
assert profile == "config-service-go", "config-service not registered in test-plans.json"
PY

echo "robot-ci updated; config-service tests are registered."
