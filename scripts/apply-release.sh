#!/usr/bin/env bash
# Overlay a release tarball onto /opt/swr-push-helper without wiping runtime state.
#
# KEEP (do not replace, do not leave behind in a .bak tree):
#   data/          robot-ci.db — users, favorites, environments, sessions
#   config.json    tokens / SWR / GitHub
#   logs/          job history
#
# Do NOT: mv /opt/swr-push-helper /opt/swr-push-helper.bak.* && untar a fresh tree.
# That is what emptied favorites and environments on 2026-09-10.
set -euo pipefail

TAR="${1:-}"
APP_DIR="${APP_DIR:-/opt/swr-push-helper}"

if [[ -z "$TAR" || ! -s "$TAR" ]]; then
  echo "Usage: $0 /path/to/robot-ci.tar.gz" >&2
  exit 1
fi

recover_db_from_bak() {
  local live="$APP_DIR/data/robot-ci.db"
  local bak
  bak="$(ls -dt "$APP_DIR".bak.*/data/robot-ci.db 2>/dev/null | head -1 || true)"
  [[ -n "$bak" && -s "$bak" ]] || return 0
  python3 - "$live" "$bak" <<'PY'
import os, sqlite3, sys
live, bak = sys.argv[1], sys.argv[2]

def counts(path):
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        return 0, 0
    conn = sqlite3.connect(path)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        env = conn.execute("SELECT COUNT(*) FROM environments").fetchone()[0] if "environments" in tables else 0
        fav = conn.execute("SELECT COUNT(*) FROM service_favorites").fetchone()[0] if "service_favorites" in tables else 0
        return env, fav
    finally:
        conn.close()

live_env, live_fav = counts(live)
bak_env, bak_fav = counts(bak)
if (live_env + live_fav) == 0 and (bak_env + bak_fav) > 0:
    os.makedirs(os.path.dirname(live), exist_ok=True)
    if os.path.isfile(live):
        os.replace(live, live + ".empty-before-recover")
    with open(bak, "rb") as src, open(live, "wb") as dst:
        dst.write(src.read())
    os.chmod(live, 0o600)
    print(f"recovered db from {bak} (env={bak_env} fav={bak_fav})")
else:
    print(f"db ok live env={live_env} fav={live_fav}; bak env={bak_env} fav={bak_fav}")
PY
}

echo "overlay $TAR -> $APP_DIR (keeping data/ config.json logs/)"
systemctl stop swr-push-helper.service 2>/dev/null || true

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT
tar -xzf "$TAR" -C "$tmpdir"
inner="$(find "$tmpdir" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
test -n "$inner"

mkdir -p "$APP_DIR" "$APP_DIR/data" "$APP_DIR/logs"

# Portable overlay: copy release files, skip runtime state.
( cd "$inner" && tar cf - \
    --exclude=data \
    --exclude=config.json \
    --exclude=logs \
    --exclude=.git \
    . ) | ( cd "$APP_DIR" && tar xf - )

if [[ ! -f "$APP_DIR/config.json" ]]; then
  echo "WARN: no config.json in $APP_DIR; copy from backup or run deploy-linux.sh once" >&2
fi

recover_db_from_bak

PYTHON_BIN="$(command -v python3.11 || command -v python3 || true)"
if [[ -n "$PYTHON_BIN" && -f "$APP_DIR/requirements.txt" ]]; then
  "$PYTHON_BIN" -m pip install --disable-pip-version-check -q -r "$APP_DIR/requirements.txt" || true
fi

systemctl daemon-reload || true
systemctl start swr-push-helper.service 2>/dev/null || true
sleep 2
systemctl is-active swr-push-helper.service 2>/dev/null || echo "service-not-active"
echo "release applied; runtime state kept in $APP_DIR/data $APP_DIR/config.json $APP_DIR/logs"
