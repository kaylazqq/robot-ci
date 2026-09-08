#!/usr/bin/env python3
"""SWR push helper for sharing: web login + GitHub branch → local build → push SWR."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from copy import deepcopy
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse, urlunparse
from urllib.request import Request, urlopen

import huawei_cce
from cid_config import CidConfigError, build_test_plan, enabled_build_step, load_cid_config

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
LOG_DIR = ROOT / "logs"
CONFIG_PATH = ROOT / "config.json"
SERVICES_PATH = ROOT / "services.json"
TEST_PLANS_PATH = ROOT / "test-plans.json"
TEST_RUNNER_PATH = ROOT / "test_runner.py"
LAST_DAEMON_VERSION_PATH = LOG_DIR / "last-daemon-version.json"
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "robot-ci.db"
USERS_PATH = ROOT / "users.json"
LOG_DIR.mkdir(parents=True, exist_ok=True)
SESSION_COOKIE = "robot_ci_session"
SESSION_MAX_AGE_SEC = 7 * 24 * 3600
SESSION_SLIDE_PERSIST_SEC = 60
PBKDF2_ROUNDS = 120_000
DEFAULT_USERNAME = "l30042018"
DEFAULT_PASSWORD = "l30042018"
DEFAULT_USERNAMES = (
    "c50065452",
    "g50065646",
    "h00858007",
    "h00970575",
    "j00603704",
    "k30003632",
    "l00612085",
    "l00855954",
    "l00987661",
    "l30042018",
    "l50059896",
    "w00938605",
    "w30033098",
    "w50062658",
    "y00895149",
    "y30082836",
    "z00578775",
    "z00616552",
    "z00982866",
    "z00987657",
)
DEFAULT_USERS = tuple((name, name) for name in DEFAULT_USERNAMES)
AUTH_PUBLIC_GET = {"/api/auth/me", "/api/health"}
AUTH_PUBLIC_POST = {"/api/auth/login"}
_sessions: dict[str, dict[str, Any]] = {}
_sessions_lock = threading.Lock()
_db_lock = threading.Lock()

_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()
_job_procs: dict[str, list[subprocess.Popen]] = {}
_job_procs_lock = threading.Lock()
_job_ctx = threading.local()
STOPPED_JOB_ERROR = "stopped by user"


class JobStopped(Exception):
    """Raised when a running job is force-stopped."""


_login_ok = False
_login_lock = threading.Lock()
_login_probe_lock = threading.Lock()
_login_probe_cache: tuple[float, bool] | None = None  # (ts, ok)
_token_cache: str | None = None
_docker_cache: tuple[float, dict[str, Any]] | None = None
_docker_cache_lock = threading.Lock()
_daemon_version_lock = threading.Lock()
_branch_cache: dict[str, tuple[float, list[str]]] = {}
_branch_cache_lock = threading.Lock()
_artifacts_lock = threading.Lock()
_public_service_lock = threading.Lock()
_public_service_ready_at = 0.0
PUBLIC_SERVICE_REUSE_SEC = 120
_history_disk_cache: dict[str, tuple[float, int, dict[str, Any]]] = {}
_history_disk_cache_lock = threading.Lock()
_fleet_cache_lock = threading.Lock()
_slot_lock = threading.Lock()
_slot_cond = threading.Condition(_slot_lock)
_slot_queue: list[str] = []
ARTIFACTS_MAX_ENTRIES = 100
ARTIFACTS_TRIM_TO = 50
ARTIFACTS_DEFAULT_PAGE_SIZE = 10
HISTORY_DEFAULT_PAGE_SIZE = 10
HISTORY_MAX_ENTRIES = 100
HISTORY_TRIM_TO = 50
DISK_USAGE_PRUNE_RATIO = 0.80
CI_TMP_DEFAULT = "/home/ci"
BUILD_CACHE_DEFAULT = "/opt/ai/build-cache"
BUILD_SWAP_NAME = "build.swap"
WORKSPACE_KEEP_NAMES = ("public-service", ".robot-ci-cache")
CI_TMP_GC_PREFIXES = (
    "runtime-apt-debs.",
    "mattermost-build-cache.",
    "go-build",
    "tmp.",
    "ops-log-",
    "gmagent-pytest-",
)
CI_TMP_GC_NAMES = ("ops-router-build",)
PROTECTED_LOCAL_IMAGES = (
    "local/ai-go-toolchain",
    "local/ai-jdk-build",
    "local/ai-jdk-runtime",
    "local/ai-ubuntu-build",
    "local/ai-ubuntu-runtime",
    "local/ai-python-build",
    "local/ai-python-runtime",
    "local/ai-node-build",
    "local/ai-node-runtime",
    "local/ai-docker-cli",
    "multica-cloud-opencode",
    "multica-cloud-hermes",
)
# Any local/ai-* tag is a toolchain/base image and must survive prune.
PROTECTED_LOCAL_IMAGE_PREFIXES = ("local/ai-",)
PROTECTED_IMAGE_HOLD_PREFIX = "ci-protect-"
ARCHIVE_IMAGE_HOLD_PREFIX = "ci-archive-hold-"
# Mattermost compile uses a shared 8G build.swap and ~3.6G RAM; a second
# concurrent job swapoff/OOM-kills webpack. Override via services.json
# `max_concurrent` when needed.
DEFAULT_SERVICE_CONCURRENCY = {"mattermost": 1, "kibana-service": 1}
DEFAULT_SERVICE_MAX_CONCURRENT = 1
CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

DAEMON_VERSION_RE = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")
FLEET_RUNTIME_CACHE_MARKERS = (".build-source.sha256", ".build-image-ids")
FLEET_IMAGE_ID_LINE_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._/:+-]*\tsha256:[0-9a-fA-F]{64}$"
)


def load_last_daemon_version() -> str:
    with _daemon_version_lock:
        try:
            payload = json.loads(LAST_DAEMON_VERSION_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return ""
    value = str(payload.get("version") or "").strip() if isinstance(payload, dict) else ""
    return value if DAEMON_VERSION_RE.fullmatch(value) else ""


def save_last_daemon_version(version: str) -> bool:
    value = (version or "").strip()
    if not DAEMON_VERSION_RE.fullmatch(value):
        return False
    payload = {"version": value, "updated_at": int(time.time())}
    temporary = LAST_DAEMON_VERSION_PATH.with_suffix(".json.tmp")
    try:
        with _daemon_version_lock:
            LAST_DAEMON_VERSION_PATH.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(LAST_DAEMON_VERSION_PATH)
        return True
    except OSError:
        return False


def _hash_password(password: str, salt_hex: str = "") -> tuple[str, str]:
    salt = bytes.fromhex(salt_hex) if salt_hex else os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return salt.hex(), digest.hex()


def _password_ok(password: str, salt_hex: str, hash_hex: str) -> bool:
    if not salt_hex or not hash_hex:
        return False
    _, digest = _hash_password(password, salt_hex)
    return hmac.compare_digest(digest, hash_hex)


def _connect_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_store() -> None:
    with _db_lock:
        conn = _connect_db()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    username TEXT PRIMARY KEY,
                    salt TEXT NOT NULL,
                    password_hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS run_templates (
                    username TEXT NOT NULL,
                    service_id TEXT NOT NULL,
                    branch TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (username, service_id)
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    expires REAL NOT NULL
                );
                """
            )
            conn.commit()
            try:
                os.chmod(DB_PATH, 0o600)
            except OSError:
                pass
            _migrate_users_json_locked(conn)
        finally:
            conn.close()


def _migrate_users_json_locked(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
        return
    try:
        payload = json.loads(USERS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return
    users = payload.get("users") if isinstance(payload, dict) else payload
    if not isinstance(users, list):
        return
    for item in users:
        if not isinstance(item, dict):
            continue
        username = str(item.get("username") or "").strip()
        salt = str(item.get("salt") or "")
        digest = str(item.get("password_hash") or "")
        if not username or not salt or not digest:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO users (username, salt, password_hash) VALUES (?, ?, ?)",
            (username, salt, digest),
        )
    conn.commit()


def _load_users() -> list[dict[str, str]]:
    init_store()
    with _db_lock:
        conn = _connect_db()
        try:
            rows = conn.execute(
                "SELECT username, salt, password_hash FROM users ORDER BY username"
            ).fetchall()
        finally:
            conn.close()
    return [
        {"username": str(row["username"]), "salt": str(row["salt"]), "password_hash": str(row["password_hash"])}
        for row in rows
    ]


def _save_users(users: list[dict[str, str]]) -> None:
    init_store()
    with _db_lock:
        conn = _connect_db()
        try:
            conn.execute("DELETE FROM users")
            conn.executemany(
                "INSERT INTO users (username, salt, password_hash) VALUES (?, ?, ?)",
                [
                    (
                        str(item.get("username") or "").strip(),
                        str(item.get("salt") or ""),
                        str(item.get("password_hash") or ""),
                    )
                    for item in users
                    if str(item.get("username") or "").strip()
                ],
            )
            conn.commit()
        finally:
            conn.close()


def get_run_template(username: str, service_id: str) -> str:
    user = str(username or "").strip()
    sid = str(service_id or "").strip()
    if not user or not sid:
        return ""
    init_store()
    with _db_lock:
        conn = _connect_db()
        try:
            row = conn.execute(
                "SELECT branch FROM run_templates WHERE username = ? AND service_id = ?",
                (user, sid),
            ).fetchone()
        finally:
            conn.close()
    return str(row["branch"] or "").strip() if row else ""


def save_run_template(username: str, service_id: str, branch: str) -> str:
    user = str(username or "").strip()
    sid = str(service_id or "").strip()
    value = str(branch or "").strip()
    if not user:
        return "未登录"
    if not sid:
        return "缺少微服务"
    if not value or not re.match(r"^[\w./\-]+$", value):
        return "分支无效"
    init_store()
    with _db_lock:
        conn = _connect_db()
        try:
            conn.execute(
                """
                INSERT INTO run_templates (username, service_id, branch, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(username, service_id) DO UPDATE SET
                    branch = excluded.branch,
                    updated_at = excluded.updated_at
                """,
                (user, sid, value, time.strftime("%Y-%m-%d %H:%M:%S")),
            )
            conn.commit()
        finally:
            conn.close()
    return ""


def resolve_template_branch(
    username: str,
    service_id: str,
    branches: list[str],
    default: str = "main",
) -> str:
    names = [str(item).strip() for item in (branches or []) if str(item).strip()]
    fallback = str(default or "main").strip() or "main"
    preferred = get_run_template(username, service_id)
    if preferred and preferred in names:
        return preferred
    if fallback in names:
        return fallback
    return names[0] if names else fallback


def ensure_default_users() -> None:
    init_store()
    users = _load_users()
    names = {str(item.get("username") or "").strip() for item in users}
    roster = {username: password for username, password in DEFAULT_USERS}
    changed = False
    for username, password in DEFAULT_USERS:
        if username in names:
            continue
        salt, digest = _hash_password(password)
        users.append({"username": username, "salt": salt, "password_hash": digest})
        names.add(username)
        changed = True
    for item in users:
        username = str(item.get("username") or "").strip()
        password = roster.get(username)
        if not password:
            continue
        salt = str(item.get("salt") or "")
        digest = str(item.get("password_hash") or "")
        if _password_ok(password, salt, digest):
            continue
        if _password_ok("@" + username, salt, digest):
            item["salt"], item["password_hash"] = _hash_password(password)
            changed = True
    if changed:
        _save_users(users)


def authenticate_user(username: str, password: str) -> str:
    want = str(username or "").strip()
    if not want or not password:
        return ""
    for item in _load_users():
        if str(item.get("username") or "").strip() != want:
            continue
        if _password_ok(password, str(item.get("salt") or ""), str(item.get("password_hash") or "")):
            return want
        return ""
    return ""


def change_user_password(username: str, old_password: str, new_password: str) -> str:
    want = str(username or "").strip()
    new_value = str(new_password or "")
    if not want:
        return "用户不存在"
    if len(new_value) < 8:
        return "新密码至少 8 位"
    users = _load_users()
    for item in users:
        if str(item.get("username") or "").strip() != want:
            continue
        if not _password_ok(old_password, str(item.get("salt") or ""), str(item.get("password_hash") or "")):
            return "当前密码不正确"
        salt, digest = _hash_password(new_value)
        item["salt"] = salt
        item["password_hash"] = digest
        _save_users(users)
        return ""
    return "用户不存在"


def create_session(username: str) -> str:
    token = secrets.token_urlsafe(32)
    expires = time.time() + SESSION_MAX_AGE_SEC
    with _sessions_lock:
        _sessions[token] = {"username": username, "expires": expires, "persisted_at": time.time()}
    _persist_session(token, username, expires)
    return token


def destroy_session(token: str) -> None:
    with _sessions_lock:
        _sessions.pop(token or "", None)
    _delete_persisted_session(token)


def session_cookie_name() -> str:
    """Cookies are host-scoped, not port-scoped. Isolate :18889 from :80."""
    try:
        port = int(CFG.get("port") or 80)
    except (TypeError, ValueError, NameError):
        port = 80
    if port in (80, 443):
        return SESSION_COOKIE
    return f"{SESSION_COOKIE}_{port}"


def session_username(token: str) -> str:
    if not token:
        return ""
    now = time.time()
    persist: tuple[str, str, float] | None = None
    username = ""
    with _sessions_lock:
        item = _sessions.get(token)
        if item:
            if float(item.get("expires") or 0) < now:
                _sessions.pop(token, None)
            else:
                item["expires"] = now + SESSION_MAX_AGE_SEC
                username = str(item.get("username") or "")
                last = float(item.get("persisted_at") or 0)
                if now - last >= SESSION_SLIDE_PERSIST_SEC:
                    item["persisted_at"] = now
                    persist = (token, username, float(item["expires"]))
    if username:
        if persist:
            _persist_session(*persist)
        return username
    loaded = _load_persisted_session(token)
    if not loaded or float(loaded.get("expires") or 0) < now:
        _delete_persisted_session(token)
        return ""
    username = str(loaded.get("username") or "")
    expires = now + SESSION_MAX_AGE_SEC
    with _sessions_lock:
        _sessions[token] = {"username": username, "expires": expires, "persisted_at": now}
    _persist_session(token, username, expires)
    return username


def _persist_session(token: str, username: str, expires: float) -> None:
    if not token:
        return
    init_store()
    with _db_lock:
        conn = _connect_db()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO sessions (token, username, expires) VALUES (?, ?, ?)",
                (token, username, float(expires)),
            )
            conn.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
            conn.commit()
        finally:
            conn.close()


def _delete_persisted_session(token: str) -> None:
    if not token:
        return
    init_store()
    with _db_lock:
        conn = _connect_db()
        try:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            conn.commit()
        finally:
            conn.close()


def _load_persisted_session(token: str) -> dict[str, Any] | None:
    if not token:
        return None
    init_store()
    with _db_lock:
        conn = _connect_db()
        try:
            row = conn.execute(
                "SELECT username, expires FROM sessions WHERE token = ?",
                (token,),
            ).fetchone()
        finally:
            conn.close()
    if not row:
        return None
    return {"username": str(row["username"] or ""), "expires": float(row["expires"] or 0)}


ensure_default_users()

JOB_PUBLIC_FIELDS = (
    "id",
    "client_id",
    "operator",
    "created_at",
    "finished_at",
    "service_id",
    "service_ids",
    "branch",
    "status",
    "stage",
    "error",
    "remote",
    "archive",
    "archive_dir",
    "results",
    "progress",
    "current",
    "commit_sha",
    "test_status",
    "test_summary",
    "test_commands",
    "test_failures",
    "test_cases",
    "test_report",
    "test_runs",
    "test_kinds",
    "cancel_requested",
)

JOB_COMPACT_FIELDS = (
    "id",
    "client_id",
    "operator",
    "created_at",
    "finished_at",
    "service_id",
    "service_ids",
    "branch",
    "status",
    "stage",
    "error",
    "remote",
    "archive",
    "archive_dir",
    "results",
    "progress",
    "current",
    "commit_sha",
    "test_status",
    "test_summary",
    "test_report",
    "test_kinds",
    "cancel_requested",
    "slot_held",
    "queue_position",
)


def _job_service_ids(job: dict[str, Any]) -> list[str]:
    raw = job.get("service_ids")
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    text = str(job.get("service_id") or "")
    return [part.strip() for part in text.split(",") if part.strip()]


PIPELINE_STEP_DEFS: tuple[tuple[str, str, str], ...] = (
    ("sync", "拉代码", "git"),
    ("test", "测试执行", "test"),
    ("build", "构建镜像", "build"),
    ("push", "推送 SWR", "push"),
    ("archive", "本地归档", "archive"),
)

PIPELINE_PREPARE_DEFS: tuple[tuple[str, str, str], ...] = (
    ("env", "检查环境", "Docker / 工作区"),
    ("slot", "等待并发槽位", "全机并行上限"),
    ("adir", "归档目录", "nginx 产物路径"),
)

PIPELINE_SUBTASK_DEFS: dict[str, tuple[tuple[str, str], ...]] = {
    "env": (
        ("docker", "Docker daemon"),
        ("disk", "磁盘空间检查"),
    ),
    "slot": (
        ("wait", "排队等待"),
        ("acquire", "获得执行槽"),
    ),
    "adir": (
        ("mkdir", "创建时间戳目录"),
        ("perm", "目录权限"),
        ("nginx", "nginx 映射路径"),
    ),
    "sync": (
        ("clone", "克隆仓库"),
        ("sha", "记录提交"),
    ),
    "test": (
        ("plan", "加载build.yaml"),
        ("runner", "启动测试"),
        ("ut-cases", "执行UT"),
        ("dt-cases", "执行DT"),
    ),
    "build": (
        ("script", "执行构建脚本"),
        ("docker", "Docker构建"),
        ("verify", "产物校验"),
    ),
    "push": (
        ("tag", "标记镜像"),
        ("push", "推送镜像"),
        ("verify", "推送确认"),
    ),
    "archive": (
        ("save", "归档镜像"),
    ),
}

STAGE_TO_PIPELINE_STEP = {
    "starting": "prepare",
    "queued": "prepare",
    "syncing": "sync",
    "testing": "test",
    "building": "build",
    "pushing": "push",
    "archiving": "archive",
}
_TERMINAL_PIPELINE_STAGES = {"interrupted", "stopping", "done", ""}


def _infer_pipeline_stage(job: dict[str, Any]) -> str:
    test_status = str(job.get("test_status") or "")
    runs = job.get("test_runs") or []
    commands = (runs[0] or {}).get("commands") or [] if runs and isinstance(runs[0], dict) else []
    if test_status or commands:
        return "testing"
    sha = str(job.get("commit_sha") or "")
    if sha and not sha.startswith("0000000"):
        return "testing"
    if str(job.get("current") or "").strip():
        return "syncing"
    return "starting"


def _effective_pipeline_stage(job: dict[str, Any]) -> str:
    raw = str(job.get("stage") or "")
    before = str(job.get("stage_before_stop") or "")
    if raw in ("interrupted", "stopping") or job.get("cancel_requested"):
        if before and before not in _TERMINAL_PIPELINE_STAGES:
            return before
        return _infer_pipeline_stage(job)
    if raw in _TERMINAL_PIPELINE_STAGES:
        return _infer_pipeline_stage(job) if raw in ("interrupted", "stopping", "") else raw
    return raw


def _service_skip_push(svc: dict[str, Any] | None) -> bool:
    if not svc:
        return False
    return bool(svc.get("archive_only") or svc.get("host_package") or svc.get("bundle_archive"))


def _pipeline_steps_for_service(svc: dict[str, Any] | None) -> list[tuple[str, str, str]]:
    steps = list(PIPELINE_STEP_DEFS)
    if _service_skip_push(svc):
        steps = [item for item in steps if item[0] != "push"]
    return steps


def _spread_status_to_subtasks(status: str, count: int, *, fail_index: int | None = None) -> list[str]:
    if count <= 0:
        return []
    if status in ("pending", "skipped"):
        return [status] * count
    if status == "queued":
        if count == 1:
            return ["queued"]
        return ["queued"] + ["pending"] * (count - 1)
    if status == "done":
        return ["done"] * count
    if status == "failed":
        idx = fail_index if fail_index is not None else count - 1
        idx = max(0, min(idx, count - 1))
        out = ["done"] * count
        out[idx] = "failed"
        for pos in range(idx + 1, count):
            out[pos] = "skipped"
        return out
    if status == "running":
        idx = min(count - 1, max(0, count // 2))
        out = ["pending"] * count
        for pos in range(idx):
            out[pos] = "done"
        out[idx] = "running"
        return out
    return ["pending"] * count


def _test_subtask_defs(job: dict[str, Any] | None) -> tuple[tuple[str, str], ...]:
    job = job or {}
    if "test_kinds" not in job:
        return (
            ("plan", "加载build.yaml"),
            ("runner", "启动测试"),
        )
    kinds = [str(kind) for kind in (job.get("test_kinds") or []) if kind in {"ut", "dt"}]
    rows: list[tuple[str, str]] = [("plan", "加载build.yaml")]
    if not kinds:
        return tuple(rows)
    rows.append(("runner", "启动测试"))
    if "ut" in kinds:
        rows.append(("ut-cases", "执行 UT 用例"))
    if "dt" in kinds:
        rows.append(("dt-cases", "执行 DT 用例"))
    return tuple(rows)


def _test_fail_index(defs: tuple[tuple[str, str], ...], job: dict[str, Any] | None) -> int | None:
    if not defs:
        return None
    ids = [item[0] for item in defs]
    run = ((job or {}).get("test_runs") or [{}])
    commands = (run[0] or {}).get("commands") or []
    failed_kinds = {
        str(item.get("test_type") or "")
        for item in commands
        if item.get("test_type") in {"ut", "dt"} and int(item.get("exit_code") or 0) != 0
    }
    for kind, sub_id in (("ut", "ut-cases"), ("dt", "dt-cases")):
        if kind in failed_kinds and sub_id in ids:
            return ids.index(sub_id)
    for sub_id in ("ut-cases", "dt-cases", "runner"):
        if sub_id in ids:
            return ids.index(sub_id)
    return len(ids) - 1


def _job_progressed_past_prepare(job: dict[str, Any]) -> bool:
    sha = str(job.get("commit_sha") or "")
    if sha and not sha.startswith("0000000"):
        return True
    if job.get("current") or job.get("results") or job.get("archive_dir"):
        return True
    stage = str(job.get("stage") or "")
    before = str(job.get("stage_before_stop") or "")
    later = ("syncing", "testing", "building", "pushing", "archiving", "done")
    return stage in later or before in later


def _sync_fail_index(
    defs: tuple[tuple[str, str], ...],
    job: dict[str, Any] | None,
    result: dict[str, Any] | None = None,
) -> int:
    ids = [item[0] for item in defs]
    if not ids:
        return 0
    err = str((result or {}).get("error") or (job or {}).get("error") or "").lower()
    sha = str((result or {}).get("commit_sha") or (job or {}).get("commit_sha") or "")
    sha_ok = bool(sha) and not sha.startswith("0000000")
    clone_failed = any(
        token in err
        for token in (
            "clone",
            "checkout",
            "timed out",
            "could not read",
            "access rights",
            "repository",
            "git ",
        )
    )
    if "clone" in ids and (clone_failed or not sha_ok):
        return ids.index("clone")
    if "sha" in ids:
        return ids.index("sha")
    return len(ids) - 1


def _subtask_rows(
    step_id: str,
    status: str,
    job: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    defs = _test_subtask_defs(job) if step_id == "test" else PIPELINE_SUBTASK_DEFS.get(step_id, ())
    if status != "failed":
        fail_index = None
    elif step_id == "test":
        fail_index = _test_fail_index(defs, job)
    elif step_id == "sync":
        fail_index = _sync_fail_index(defs, job, result)
    else:
        fail_index = len(defs) - 1
    statuses = _spread_status_to_subtasks(status, len(defs), fail_index=fail_index)
    rows = [
        {"id": sub_id, "label": label, "status": statuses[idx] if idx < len(statuses) else "pending"}
        for idx, (sub_id, label) in enumerate(defs)
    ]
    if step_id != "test":
        return rows
    run = ((job or {}).get("test_runs") or [{}])
    commands = (run[0] or {}).get("commands") or []
    by_kind: dict[str, str] = {}
    for item in commands:
        kind = str(item.get("test_type") or "")
        if kind not in {"ut", "dt"}:
            continue
        by_kind[kind] = "failed" if int(item.get("exit_code") or 0) != 0 else "done"
    for row in rows:
        kind = "ut" if row["id"] == "ut-cases" else "dt" if row["id"] == "dt-cases" else ""
        if not kind:
            continue
        if kind in by_kind and status not in ("pending", "skipped"):
            row["status"] = by_kind[kind]
        elif status == "failed":
            row["status"] = "skipped"
    return rows


def _step_detail(step_id: str, status: str, result: dict[str, Any] | None, job: dict[str, Any]) -> str:
    if step_id == "sync":
        sha = str((result or {}).get("commit_sha") or job.get("commit_sha") or "")
        if sha and not sha.startswith("0000000"):
            return sha[:12]
        return "等待 clone 完成"
    if step_id == "test":
        test_status = str((result or {}).get("test_status") or job.get("test_status") or "")
        summary = (result or {}).get("test_summary") or job.get("test_summary") or {}
        if isinstance(summary, dict) and summary.get("total"):
            return f"{summary.get('passed', 0)}/{summary.get('total')} 通过 · {test_status or '—'}"
        return test_status or ("进行中" if status == "running" else "—")
    if step_id == "build":
        tag = str((result or {}).get("tag") or "")
        if tag:
            return tag
        return "CID / legacy Docker build"
    if step_id == "push":
        remote = str((result or {}).get("remote") or job.get("remote") or "")
        if remote and remote != "archive-only":
            return remote.split("/")[-1]
        return "swr.cn-southwest-2…"
    if step_id == "archive":
        archive = str((result or {}).get("archive") or job.get("archive") or job.get("archive_dir") or "")
        if archive:
            return Path(archive).name
        return "docker save → nginx"
    return ""


def _prepare_lane_for_job(
    job: dict[str, Any],
    service_ids: list[str],
    catalog: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    services = [catalog[sid] for sid in service_ids if sid in catalog]
    needs_archive = bool(CFG.get("archive_enabled")) or any(svc.get("bundle_archive") for svc in services)
    job_status = str(job.get("status") or "")
    error = str(job.get("error") or "")
    archive_dir = str(job.get("archive_dir") or "")
    active = STAGE_TO_PIPELINE_STEP.get(_effective_pipeline_stage(job))

    rows: list[dict[str, Any]] = []
    for step_id, label, hint in PIPELINE_PREPARE_DEFS:
        if step_id == "env":
            if any(token in error for token in ("SWR", "swr", "login", "鉴权", "docker login", "docker unavailable")):
                st = "failed"
                detail = "环境检查失败"
            elif "public-service" in error:
                st = "failed"
                detail = "依赖仓库同步失败"
            elif job_status == "failed" and active in (None, "prepare") and not _job_progressed_past_prepare(job):
                st = "failed"
                detail = (error[:80] if error else "前置准备失败")
            elif job_status == "queued":
                st = "done"
                detail = "Docker 可用 · 工作区就绪"
            elif job_status == "running" and str(job.get("stage") or "") in ("starting", "queued"):
                st = "done" if job.get("slot_held") else "running"
                detail = "Docker 可用 · 工作区就绪" if job.get("slot_held") else "检查 Docker / 工作区…"
            elif job_status:
                st = "done"
                detail = "Docker 可用 · 工作区就绪"
            else:
                st = "pending"
                detail = hint
        elif step_id == "slot":
            limit = max_concurrent_jobs()
            pos = int(job.get("queue_position") or 0)
            if job.get("slot_held"):
                st = "done"
                detail = f"已获得执行槽（并行上限 {limit}）"
            elif job_status == "queued":
                st = "queued"
                detail = f"排队中 · 第 {pos or 1} 位，并行上限 {limit}"
            elif job_status == "running" and str(job.get("stage") or "") in ("starting", "queued"):
                st = "running"
                detail = f"申请执行槽（并行上限 {limit}）"
            elif job_status == "failed" and active in (None, "prepare") and not (
                archive_dir or job.get("current") or job.get("results")
            ):
                st = "skipped"
                detail = "未执行"
            elif job_status in ("ok", "failed", "stopped") and (
                archive_dir
                or job.get("current")
                or job.get("results")
                or job.get("commit_sha")
                or job.get("test_status")
                or str(job.get("stage_before_stop") or "") not in ("", "starting", "queued")
            ):
                st = "done"
                detail = "已获得执行槽"
            elif job_status in ("stopped", "failed"):
                st = "skipped"
                detail = "已停止" if job_status == "stopped" else "未执行"
            else:
                st = "pending"
                detail = hint
        elif step_id == "adir":
            if not needs_archive:
                st = "skipped"
                detail = "归档已禁用"
            elif archive_dir:
                st = "done"
                detail = Path(archive_dir).name
            elif job_status == "queued":
                st = "pending"
                detail = hint
            elif job_status == "running":
                st = "running"
                detail = "创建目录…"
            elif job_status in ("stopped", "failed"):
                st = "skipped"
                detail = "已停止" if job_status == "stopped" else "未执行"
            else:
                st = "pending"
                detail = hint
        else:
            st = "pending"
            detail = hint

        rows.append(
            {
                "id": step_id,
                "label": label,
                "hint": hint,
                "status": st,
                "detail": detail,
                "subtasks": _subtask_rows(step_id, st),
            }
        )
    return rows


def _pipeline_step_rows(
    step_defs: list[tuple[str, str, str]],
    statuses: dict[str, str],
    *,
    result: dict[str, Any] | None = None,
    job: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    job = job or {}
    rows: list[dict[str, Any]] = []
    for step_id, label, icon in step_defs:
        status = statuses.get(step_id, "pending")
        rows.append(
            {
                "id": step_id,
                "label": label,
                "icon": icon,
                "status": status,
                "detail": _step_detail(step_id, status, result, job),
                "subtasks": _subtask_rows(step_id, status, job, result),
            }
        )
    return rows


def _failed_pipeline_step(result: dict[str, Any], step_ids: list[str]) -> str:
    err = str(result.get("error") or "").lower()
    # Do not match the substring "repo" — build errors like `/out/repo` would
    # otherwise be painted as 前置准备 / 克隆仓库.
    if any(
        token in err
        for token in (
            "clone",
            "checkout",
            "sync",
            "git ",
            "deploy.sh",
            "build-image.sh",
            "build.yaml",
            "unknown service",
            "repository not found",
            "missing deploy",
        )
    ):
        return "sync"
    if any(token in err for token in ("archive", "host package", "tar.gz", "bundle", "fleet")):
        return "archive"
    if "push" in step_ids and any(token in err for token in ("push", "swr", "docker login", "denied")):
        return "push"
    if any(token in err for token in ("build", "docker", "npm", "compile", "make", "solve")):
        return "build"
    commit_sha = str(result.get("commit_sha") or "")
    if err and (not commit_sha or commit_sha.startswith("0000000")):
        return "sync"
    return "build"


def _pipeline_summary(steps: list[dict[str, Any]], prepare: list[dict[str, Any]]) -> dict[str, Any]:
    all_steps = list(prepare) + list(steps)
    total = len(all_steps)
    counts = {"done": 0, "running": 0, "failed": 0, "pending": 0, "skipped": 0, "warn": 0, "queued": 0}
    for item in all_steps:
        key = str(item.get("status") or "pending")
        counts[key] = counts.get(key, 0) + 1
    weighted = counts["done"] + counts["warn"] + counts["skipped"] * 0.5
    percent = int(round((weighted / total) * 100)) if total else 0
    return {
        "total": total,
        "done": counts.get("done", 0),
        "running": counts.get("running", 0),
        "failed": counts.get("failed", 0),
        "pending": counts.get("pending", 0),
        "skipped": counts.get("skipped", 0),
        "warn": counts.get("warn", 0),
        "percent": min(100, max(0, percent)),
    }


def _pipeline_artifacts(result: dict[str, Any] | None, svc: dict[str, Any] | None) -> dict[str, str]:
    result = result or {}
    svc = svc or {}
    return {
        "service_id": str(result.get("service_id") or svc.get("id") or ""),
        "image": str(result.get("image") or svc.get("image") or ""),
        "tag": str(result.get("tag") or ""),
        "remote": str(result.get("remote") or ""),
        "package_name": str(result.get("package_name") or ""),
        "download_url": str(result.get("download_url") or ""),
        "archive": str(result.get("archive") or ""),
    }


def _running_pipeline_statuses(
    stage: str,
    step_ids: list[str],
    *,
    skip_push: bool,
) -> dict[str, str]:
    statuses: dict[str, str] = {}
    active = STAGE_TO_PIPELINE_STEP.get(str(stage or ""))
    if active not in step_ids:
        active = None

    build_idx = step_ids.index("build") if "build" in step_ids else -1
    active_idx = step_ids.index(active) if active in step_ids else -1

    for idx, step_id in enumerate(step_ids):
        if step_id == "push" and skip_push:
            if active_idx >= 0 and active_idx > build_idx:
                statuses[step_id] = "skipped"
            else:
                statuses[step_id] = "pending"
            continue
        if active_idx < 0:
            statuses[step_id] = "pending"
        elif idx < active_idx:
            statuses[step_id] = "done"
        elif idx == active_idx:
            statuses[step_id] = "running"
        else:
            statuses[step_id] = "pending"
    return statuses


def _apply_live_test_status(
    statuses: dict[str, str],
    job: dict[str, Any],
    result: dict[str, Any] | None = None,
) -> None:
    test_status = str((result or {}).get("test_status") or job.get("test_status") or "")
    if not test_status or test_status in ("passed", "not_configured"):
        return
    if statuses.get("test") in ("done", "running", "warn"):
        statuses["test"] = "failed"


def _completed_pipeline_statuses(
    result: dict[str, Any],
    step_defs: list[tuple[str, str, str]],
    *,
    skip_push: bool,
) -> dict[str, str]:
    step_ids = [step_id for step_id, _, _ in step_defs]
    statuses: dict[str, str] = {}
    if result.get("ok"):
        for step_id, _, _ in step_defs:
            if step_id == "push" and skip_push:
                statuses[step_id] = "skipped"
            else:
                statuses[step_id] = "done"
        test_status = str(result.get("test_status") or "")
        if test_status and test_status not in ("passed", "not_configured"):
            statuses["test"] = "failed"
        return statuses

    failed = _failed_pipeline_step(result, step_ids)
    failed_idx = step_ids.index(failed) if failed in step_ids else len(step_ids) - 1
    for idx, (step_id, _, _) in enumerate(step_defs):
        if step_id == "push" and skip_push:
            statuses[step_id] = "skipped"
            continue
        if idx < failed_idx:
            statuses[step_id] = "done"
        elif idx == failed_idx:
            statuses[step_id] = "failed"
        else:
            statuses[step_id] = "skipped"
    return statuses


def _failed_job_step_statuses(
    job: dict[str, Any],
    step_defs: list[tuple[str, str, str]],
    *,
    skip_push: bool,
) -> dict[str, str]:
    step_ids = [step_id for step_id, _, _ in step_defs]
    statuses = _running_pipeline_statuses(
        _effective_pipeline_stage(job),
        step_ids,
        skip_push=skip_push,
    )
    out: dict[str, str] = {}
    marked_failed = False
    for step_id in step_ids:
        st = statuses.get(step_id, "pending")
        if marked_failed:
            out[step_id] = "skipped"
        elif st == "running":
            out[step_id] = "failed"
            marked_failed = True
        elif st == "pending":
            out[step_id] = "skipped"
        else:
            out[step_id] = st
    return out


def build_job_pipeline(job: dict[str, Any]) -> dict[str, Any]:
    catalog = {str(item.get("id")): item for item in load_services()}
    service_ids = _job_service_ids(job)
    if not service_ids:
        fallback = str(job.get("service_id") or "").split(",")[0].strip()
        if fallback:
            service_ids = [fallback]

    results = job.get("results") or []
    results_by_id = {str(item.get("service_id")): item for item in results if item.get("service_id")}

    current_raw = str(job.get("current") or "")
    current_sid = current_raw.split("@", 1)[0].strip() if current_raw else ""

    progress_text = str(job.get("progress") or "")
    current_idx = 0
    progress_match = re.match(r"(\d+)/(\d+)", progress_text)
    if progress_match:
        current_idx = max(0, int(progress_match.group(1)) - 1)

    job_status = str(job.get("status") or "")
    stage = _effective_pipeline_stage(job)
    is_running = job_status in ("running", "queued", "unknown")

    services_out: list[dict[str, Any]] = []
    focus_sid = ""

    for idx, service_id in enumerate(service_ids):
        svc = catalog.get(service_id)
        title = str((svc or {}).get("title") or service_id)
        step_defs = _pipeline_steps_for_service(svc)
        skip_push = _service_skip_push(svc)

        if service_id in results_by_id:
            result = results_by_id[service_id]
            statuses = _completed_pipeline_statuses(result, step_defs, skip_push=skip_push)
            _apply_live_test_status(statuses, job, result)
            svc_status = "ok" if result.get("ok") else "failed"
            if not focus_sid and svc_status == "failed":
                focus_sid = service_id
        elif is_running and service_id == current_sid:
            statuses = _running_pipeline_statuses(
                stage,
                [step_id for step_id, _, _ in step_defs],
                skip_push=skip_push,
            )
            _apply_live_test_status(statuses, job)
            svc_status = "running"
            focus_sid = service_id
        elif is_running and idx < current_idx:
            statuses = {step_id: "done" for step_id, _, _ in step_defs}
            if skip_push:
                statuses["push"] = "skipped"
            svc_status = "done"
        elif job_status == "stopped" and service_id in {current_sid, str(job.get("service_id") or "")}:
            statuses = _running_pipeline_statuses(
                stage,
                [step_id for step_id, _, _ in step_defs],
                skip_push=skip_push,
            )
            _apply_live_test_status(statuses, job)
            for step_id, st in list(statuses.items()):
                if st in ("running", "pending"):
                    statuses[step_id] = "skipped"
            svc_status = "stopped"
            if not focus_sid:
                focus_sid = service_id
        elif job_status == "failed":
            statuses = _failed_job_step_statuses(job, step_defs, skip_push=skip_push)
            _apply_live_test_status(statuses, job)
            svc_status = "failed"
            if not focus_sid:
                focus_sid = service_id
        else:
            statuses = {step_id: "pending" for step_id, _, _ in step_defs}
            svc_status = "pending"

        result_row = results_by_id.get(service_id)
        services_out.append(
            {
                "service_id": service_id,
                "title": title,
                "status": svc_status,
                "archive_only": skip_push,
                "image": str((svc or {}).get("image") or ""),
                "branch": str(result_row.get("branch") if result_row else job.get("branch") or ""),
                "steps": _pipeline_step_rows(
                    step_defs,
                    statuses,
                    result=result_row,
                    job=job,
                ),
            }
        )

    if not focus_sid:
        failed = [item for item in services_out if item["status"] == "failed"]
        running = [item for item in services_out if item["status"] == "running"]
        if current_sid:
            focus_sid = current_sid
        elif failed:
            focus_sid = str(failed[0]["service_id"])
        elif running:
            focus_sid = str(running[0]["service_id"])
        elif services_out:
            focus_sid = str(services_out[0]["service_id"])

    focus = next((item for item in services_out if item["service_id"] == focus_sid), None)
    focus_result = results_by_id.get(focus_sid or "")
    focus_svc = catalog.get(focus_sid or "")
    prepare = _prepare_lane_for_job(job, service_ids, catalog)
    steps = list(focus.get("steps") or []) if focus else []
    return {
        "progress": progress_text,
        "current_service": current_sid,
        "focus_service": focus_sid,
        "meta": {
            "job_id": str(job.get("id") or ""),
            "status": job_status,
            "stage": stage,
            "branch": str(job.get("branch") or ""),
            "commit_sha": str(
                (focus_result or {}).get("commit_sha") or job.get("commit_sha") or ""
            ),
            "archive_dir": str(job.get("archive_dir") or ""),
            "service_count": len(service_ids),
        },
        "prepare": prepare,
        "steps": steps,
        "services": services_out,
        "summary": _pipeline_summary(steps, prepare),
        "artifacts": _pipeline_artifacts(focus_result, focus_svc),
    }


def service_concurrency_cap(service_id: str) -> int | None:
    """Per-service cap. Every microservice defaults to 1 concurrent job."""
    for item in load_services():
        if item.get("id") != service_id:
            continue
        raw = item.get("max_concurrent")
        if raw is None:
            break
        try:
            value = int(raw)
        except (TypeError, ValueError):
            break
        return value if value > 0 else DEFAULT_SERVICE_MAX_CONCURRENT
    return DEFAULT_SERVICE_CONCURRENCY.get(service_id, DEFAULT_SERVICE_MAX_CONCURRENT)


def other_job_uses_build_swap(job_id: str) -> bool:
    """True if another running job still needs the shared compile swap."""
    with _jobs_lock:
        return any(
            str(job.get("id") or "") != str(job_id)
            and job.get("status") == "running"
            and "mattermost" in _job_service_ids(job)
            for job in _jobs.values()
        )


def _running_jobs_locked() -> list[dict[str, Any]]:
    return [job for job in _jobs.values() if job.get("status") == "running"]


def _live_jobs_locked() -> list[dict[str, Any]]:
    """Jobs that occupy a microservice: running or queued."""
    return [job for job in _jobs.values() if job.get("status") in ("running", "queued")]


def _held_slot_count_locked() -> int:
    return sum(
        1
        for job in _jobs.values()
        if job.get("slot_held") and job.get("status") in ("running", "queued")
    )


def _running_job_locked() -> dict[str, Any] | None:
    running = _running_jobs_locked()
    return running[0] if running else None


def _normalize_client_id(value: Any) -> str:
    text = str(value or "").strip()
    if CLIENT_ID_RE.fullmatch(text):
        return text
    return ""


def max_concurrent_jobs() -> int:
    try:
        return max(1, min(int(CFG.get("max_concurrent_jobs") or 5), 16))
    except (TypeError, ValueError):
        return 5


def active_job_summary(client_id: str = "", service_id: str = "") -> dict[str, Any] | None:
    """Return the occupying job for a service (running or queued)."""
    want = _normalize_client_id(client_id)
    sid = str(service_id or "").strip()
    with _jobs_lock:
        live = _live_jobs_locked()
        if sid:
            live = [job for job in live if sid in _job_service_ids(job)]
        if not live:
            return None
        if want and not sid:
            mine = [job for job in live if _normalize_client_id(job.get("client_id")) == want]
            if mine:
                return deepcopy({key: mine[0].get(key) for key in JOB_COMPACT_FIELDS})
            return None
        return deepcopy({key: live[0].get(key) for key in JOB_COMPACT_FIELDS})


def list_running_job_summaries(client_id: str = "", service_id: str = "") -> list[dict[str, Any]]:
    want = _normalize_client_id(client_id)
    sid = str(service_id or "").strip()
    with _jobs_lock:
        live = _live_jobs_locked()
        if sid:
            live = [job for job in live if sid in _job_service_ids(job)]
        elif want:
            live = [job for job in live if _normalize_client_id(job.get("client_id")) == want]
        return [deepcopy({key: job.get(key) for key in JOB_COMPACT_FIELDS}) for job in live]


def register_concurrent_job(job: dict[str, Any]) -> dict[str, Any] | None:
    """
    Register a job. Each microservice may have at most one live (running/queued)
    job. Global parallelism is enforced later via FIFO slots, not by rejecting
    the request.
    """
    incoming = _job_service_ids(job)
    caps = {sid: service_concurrency_cap(sid) for sid in incoming}
    job.setdefault("slot_held", False)
    job.setdefault("queue_position", 0)
    with _jobs_lock:
        live = _live_jobs_locked()
        for service_id, cap in caps.items():
            if not cap:
                continue
            same = [item for item in live if service_id in _job_service_ids(item)]
            if len(same) >= cap:
                return {
                    "error": (
                        f"{service_id} 正在编译（{len(same)}/{cap}），"
                        "请等当前任务结束后再提交"
                    ),
                    "error_code": "service_busy",
                    "service": service_id,
                    "active_jobs": [
                        deepcopy({key: item.get(key) for key in JOB_COMPACT_FIELDS}) for item in same
                    ],
                }
        _jobs[str(job["id"])] = job
    return None


def register_job_if_idle(job: dict[str, Any]) -> dict[str, Any] | None:
    """Back-compat wrapper: register or return conflict shaped like an active job."""
    err = register_concurrent_job(job)
    if not err:
        return None
    active_jobs = err.get("active_jobs") or []
    if active_jobs:
        return active_jobs[0]
    aid = err.get("active_job_id")
    if aid:
        with _jobs_lock:
            job0 = _jobs.get(str(aid))
            if job0:
                return deepcopy({key: job0.get(key) for key in JOB_COMPACT_FIELDS})
    return {
        "id": None,
        "status": "running",
        "error": err.get("error"),
        "service_id": ",".join(err.get("busy_services") or []),
    }


def reset_job_scheduler() -> None:
    """Test helper: drop the FIFO wait queue."""
    with _slot_lock:
        _slot_queue.clear()
        _slot_cond.notify_all()


def _queue_position_locked(job_id: str) -> int:
    try:
        return _slot_queue.index(job_id) + 1
    except ValueError:
        return 0


def release_build_slot(job_id: str) -> None:
    if not job_id:
        return
    with _slot_lock:
        with _jobs_lock:
            job = _jobs.get(job_id)
            if job:
                job["slot_held"] = False
                if job.get("status") == "queued":
                    pass
        if job_id in _slot_queue:
            _slot_queue.remove(job_id)
        _slot_cond.notify_all()


def wait_for_build_slot(job_id: str) -> bool:
    """FIFO wait until this job may occupy a global execution slot."""
    logged_wait = False
    with log_substep("slot"):
        with _slot_lock:
            if job_id not in _slot_queue:
                _slot_queue.append(job_id)
            while True:
                if job_cancel_requested(job_id):
                    if job_id in _slot_queue:
                        _slot_queue.remove(job_id)
                    return False
                limit = max_concurrent_jobs()
                with _jobs_lock:
                    held = _held_slot_count_locked()
                    job = _jobs.get(job_id)
                    if not job or job.get("status") not in ("running", "queued"):
                        if job_id in _slot_queue:
                            _slot_queue.remove(job_id)
                        return False
                if held < limit and _slot_queue and _slot_queue[0] == job_id:
                    _slot_queue.pop(0)
                    set_job(
                        job_id,
                        status="running",
                        stage="starting",
                        slot_held=True,
                        queue_position=0,
                    )
                    append_job_log(job_id, f"acquired build slot ({held + 1}/{limit})")
                    _slot_cond.notify_all()
                    return True
                pos = _queue_position_locked(job_id)
                set_job(
                    job_id,
                    status="queued",
                    stage="queued",
                    slot_held=False,
                    queue_position=pos,
                )
                if not logged_wait:
                    append_job_log(
                        job_id,
                        f"build slot full ({held}/{limit}); queued at position {pos}",
                    )
                    logged_wait = True
                _slot_cond.wait(timeout=1.0)


def _quiet_dependency_log(line: str) -> bool:
    text = re.sub(r"^\[[^\]]+\]\s*", "", str(line or "")).strip().lower()
    return text.startswith(
        (
            "requirement already satisfied:",
            "collecting ",
            "downloading ",
            "using cached ",
            "building wheels for collected packages",
            "building wheel for ",
            "installing collected packages:",
            "successfully installed ",
            "looking in indexes:",
            "processing ",
            "defaulting to user installation",
        )
    )


def _append_ui_log(job: dict[str, Any], text: str) -> None:
    plain = re.sub(r"^\[[^\]]+\]\s*", "", str(text or "")).strip()
    if plain.startswith("tests start "):
        job["_ui_test_running"] = True
        job.setdefault("ui_log", []).append(text)
        return
    if plain.startswith(("@@TEST_STEP@@ ", "@@TEST_ERROR@@ ")):
        job.setdefault("ui_log", []).append(text)
        return
    if plain.startswith("TEST summary "):
        job["_ui_test_running"] = False
        # Keep the internal boundary in the incremental stream. The browser
        # consumes it to leave test-output filtering mode, but does not render it.
        job.setdefault("ui_log", []).append(text)
        return
    if "@@TEST_SUMMARY@@" in text or job.get("_ui_test_running"):
        return
    if _quiet_dependency_log(text):
        return
    job.setdefault("ui_log", []).append(text)


def filter_ui_log(lines: list[str]) -> list[str]:
    state: dict[str, Any] = {"ui_log": [], "_ui_test_running": False}
    for line in lines:
        _append_ui_log(state, line)
    return list(state["ui_log"])


def snapshot_job_for_payload(
    job: dict[str, Any],
    *,
    compact: bool = False,
    view_ui: bool = False,
    step: str = "",
) -> dict[str, Any]:
    """Copy job fields needed for HTTP payloads. Caller must hold `_jobs_lock`."""
    fields = JOB_COMPACT_FIELDS if compact else JOB_PUBLIC_FIELDS
    snap = {key: job.get(key) for key in fields}
    snap["stage_before_stop"] = job.get("stage_before_stop")
    snap["cancel_requested"] = job.get("cancel_requested")
    snap["_ui_test_running"] = job.get("_ui_test_running")
    snap["results"] = list(job.get("results") or [])
    snap["test_runs"] = list(job.get("test_runs") or [])
    if not compact:
        snap["test_cases"] = list(job.get("test_cases") or [])
        snap["test_failures"] = list(job.get("test_failures") or [])
        snap["test_commands"] = list(job.get("test_commands") or [])
    if view_ui:
        snap["ui_log"] = list(job.get("ui_log") or [])
        snap["log"] = []
    else:
        snap["log"] = list(job.get("log") or [])
        if "ui_log" in job:
            snap["ui_log"] = list(job.get("ui_log") or [])
    if step:
        snap["step_logs"] = {
            key: list(val or []) for key, val in (job.get("step_logs") or {}).items()
        }
    else:
        snap["step_logs"] = {}
    return snap


def job_payload(
    job: dict[str, Any],
    *,
    compact: bool = False,
    view_ui: bool = False,
    log_after: int = 0,
    test_revision: int = -1,
    step: str = "",
    sub: str = "",
) -> dict[str, Any]:
    fields = JOB_COMPACT_FIELDS if compact else JOB_PUBLIC_FIELDS
    payload = deepcopy({key: job.get(key) for key in fields})
    payload["duration_sec"] = _job_duration_sec(job)
    step_id = str(step or "").strip()
    sub_id = str(sub or "").strip()
    logs = job.get("step_logs") or {}
    if step_id:
        selected_log = _selected_step_logs(logs, step_id, sub_id)
    elif view_ui and "ui_log" in job:
        selected_log = list(job.get("ui_log") or [])
    else:
        raw_log = list(job.get("log") or [])
        selected_log = filter_ui_log(raw_log) if view_ui else raw_log
    cursor = max(0, min(int(log_after or 0), len(selected_log))) if compact else 0
    payload["log"] = selected_log[cursor:]
    payload["log_cursor"] = len(selected_log)
    payload["step"] = step_id or None
    payload["sub"] = sub_id or None
    revision = len(job.get("test_runs") or [])
    payload["test_revision"] = revision
    if compact and test_revision != revision:
        payload["test_runs"] = deepcopy(job.get("test_runs") or [])
    payload["pipeline"] = build_job_pipeline(job)
    return payload


def job_meta_path(job_id: str) -> Path:
    return LOG_DIR / f"job-{job_id}.json"


def delete_job_disk_files(job_id: str) -> None:
    """Remove persisted job meta, log, and test report files."""
    for name in (f"job-{job_id}.json", f"job-{job_id}.log"):
        try:
            (LOG_DIR / name).unlink(missing_ok=True)
        except OSError:
            pass
    for path in LOG_DIR.glob(f"job-{job_id}-*-test.json"):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _running_job_ids_for_client(client_id: str) -> set[str]:
    want = _normalize_client_id(client_id)
    if not want:
        return set()
    with _jobs_lock:
        return {
            str(job.get("id") or "")
            for job in _jobs.values()
            if _normalize_client_id(job.get("client_id")) == want and job.get("status") == "running"
            if str(job.get("id") or "")
        }


def prune_build_history(client_id: str, keep_job_id: str = "") -> int:
    """History metadata is kept in full; disk archive cleanup is separate."""
    return 0


def prune_all_build_histories() -> int:
    return 0


def prune_artifacts_log() -> int:
    path = artifacts_log_path()
    if not path.is_file():
        return 0
    with _artifacts_lock:
        try:
            entries = _parse_artifact_entries(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            return 0
        trimmed = trim_artifact_entries(entries)
        if len(trimmed) >= len(entries):
            return 0
        try:
            path.write_text(
                "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in trimmed),
                encoding="utf-8",
            )
        except OSError:
            return 0
        return len(entries) - len(trimmed)


def artifacts_log_path() -> Path:
    return LOG_DIR / "artifacts.jsonl"


def archive_root_path() -> Path:
    return Path(CFG.get("archive_root") or "/usr/share/nginx/html/images").expanduser()


def resolve_archive_file(rel_or_abs: str) -> Path | None:
    """Resolve a downloadable archive path that must stay under archive_root."""
    raw = (rel_or_abs or "").strip().replace("\\", "/")
    if not raw or "\x00" in raw:
        return None
    root = archive_root_path().resolve()
    candidate = Path(raw)
    if candidate.is_absolute():
        target = candidate
    else:
        # Allow "20260813.../svc_tag.tar" or accidental leading "images/"
        rel = raw.lstrip("/")
        if rel.startswith("images/"):
            rel = rel[len("images/") :]
        target = root / rel
    try:
        resolved = target.resolve()
        resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    if not resolved.is_file():
        return None
    return resolved


def public_archive_url(archive_path: str) -> str:
    """Download URL served by this helper (not nginx static files)."""
    resolved = resolve_archive_file(archive_path)
    if resolved is None:
        return ""
    try:
        rel = resolved.relative_to(archive_root_path().resolve()).as_posix()
    except (OSError, ValueError):
        return ""
    return f"/api/artifacts/download?file={quote(rel, safe='/')}"


def _parse_artifact_entries(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def trim_oldest_when_over(items: list[Any], max_entries: int, trim_to: int) -> list[Any]:
    """Keep append order; when over max_entries, drop oldest down to trim_to."""
    if len(items) <= max_entries:
        return items
    keep = max(0, int(trim_to))
    if keep <= 0:
        return []
    return items[-keep:]


def trim_artifact_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep full artifact metadata; disk tar cleanup is handled separately."""
    return list(entries)


def annotate_artifact_entry(item: dict[str, Any]) -> dict[str, Any]:
    """Mark a row expired when its archive file is gone (prune or manual delete)."""
    archive = str(item.get("archive") or "")
    url = public_archive_url(archive) if archive else ""
    available = bool(url)
    return {
        **item,
        "available": available,
        "expired": not available,
        "download_url": url,
    }


def _job_operator_name(job_id: str) -> str:
    with _jobs_lock:
        job = _jobs.get(job_id) or {}
        return str(job.get("operator") or "")


def record_build_artifact(entry: dict[str, Any]) -> None:
    """Append one build artifact record (forward-only; no historical backfill)."""
    path = artifacts_log_path()
    line = json.dumps(entry, ensure_ascii=False)
    with _artifacts_lock:
        try:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            text = path.read_text(encoding="utf-8", errors="replace").splitlines()
            trimmed = trim_artifact_entries(_parse_artifact_entries("\n".join(text)))
            if len(trimmed) < len(text):
                path.write_text(
                    "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in trimmed),
                    encoding="utf-8",
                )
        except OSError:
            pass


def sync_artifact_availability() -> int:
    """Persist expired flags after archive files disappear. Returns newly expired count."""
    path = artifacts_log_path()
    with _artifacts_lock:
        if not path.is_file():
            return 0
        try:
            entries = _parse_artifact_entries(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            return 0
        newly_expired = 0
        updated: list[dict[str, Any]] = []
        for item in entries:
            annotated = annotate_artifact_entry(item)
            if annotated["expired"] and not item.get("expired"):
                newly_expired += 1
            persisted = {
                **item,
                "available": annotated["available"],
                "expired": annotated["expired"],
                "download_url": annotated["download_url"],
            }
            updated.append(persisted)
        trimmed = trim_artifact_entries(updated)
        try:
            path.write_text(
                "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in trimmed),
                encoding="utf-8",
            )
        except OSError:
            return 0
        return newly_expired


def list_build_artifacts(
    page: int = 1,
    page_size: int = ARTIFACTS_DEFAULT_PAGE_SIZE,
    service_id: str = "",
) -> dict[str, Any]:
    path = artifacts_log_path()
    entries: list[dict[str, Any]] = []
    if path.is_file():
        try:
            entries = _parse_artifact_entries(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            entries = []
    annotated = [annotate_artifact_entry(item) for item in reversed(entries)]
    sid = str(service_id or "").strip()
    if sid:
        annotated = [item for item in annotated if str(item.get("service_id") or "") == sid]
    total = len(annotated)
    try:
        size = int(page_size or ARTIFACTS_DEFAULT_PAGE_SIZE)
    except (TypeError, ValueError):
        size = ARTIFACTS_DEFAULT_PAGE_SIZE
    size = max(1, min(size, 200))
    page_count = max(1, (total + size - 1) // size) if total else 1
    try:
        current = int(page or 1)
    except (TypeError, ValueError):
        current = 1
    current = max(1, min(current, page_count))
    start = (current - 1) * size
    return {
        "artifacts": annotated[start : start + size],
        "total": total,
        "page": current,
        "page_size": size,
        "page_count": page_count,
        "expired_count": sum(1 for item in annotated if item.get("expired")),
        "service_id": sid or None,
    }


def _history_created_at(meta: dict[str, Any], mtime: float = 0.0) -> str:
    created = str(meta.get("created_at") or "").strip()
    if created:
        return created
    if mtime > 0:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime))
    return ""


def _parse_job_ts(value: str) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return time.mktime(time.strptime(text[:19], fmt))
        except ValueError:
            continue
    return None


def _job_duration_sec(meta: dict[str, Any]) -> int | None:
    start = _parse_job_ts(str(meta.get("created_at") or ""))
    end = _parse_job_ts(str(meta.get("finished_at") or ""))
    if start is None or end is None:
        return None
    return max(0, int(end - start))


def _history_row(meta: dict[str, Any], mtime: float = 0.0) -> dict[str, Any]:
    return {
        "id": str(meta.get("id") or ""),
        "client_id": meta.get("client_id") or "",
        "operator": meta.get("operator") or "",
        "created_at": _history_created_at(meta, mtime),
        "finished_at": str(meta.get("finished_at") or ""),
        "duration_sec": _job_duration_sec(meta),
        "service_id": meta.get("service_id"),
        "service_ids": meta.get("service_ids") or _job_service_ids(meta),
        "branch": meta.get("branch"),
        "status": meta.get("status") or "unknown",
        "stage": meta.get("stage"),
        "error": meta.get("error"),
        "progress": meta.get("progress"),
        "current": meta.get("current"),
        "commit_sha": meta.get("commit_sha"),
        "remote": meta.get("remote"),
        "archive": meta.get("archive"),
    }


def list_build_history(
    client_id: str = "",
    service_id: str = "",
    page: int = 1,
    page_size: int = HISTORY_DEFAULT_PAGE_SIZE,
) -> dict[str, Any]:
    """Paginated jobs for the whole platform. Optional client_id / service_id filters."""
    want = _normalize_client_id(client_id)
    sid = str(service_id or "").strip()
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    with _jobs_lock:
        for job in _jobs.values():
            if want and _normalize_client_id(job.get("client_id")) != want:
                continue
            if sid and sid not in _job_service_ids(job):
                continue
            job_id = str(job.get("id") or "")
            if not job_id:
                continue
            rows.append(_history_row(job))
            seen.add(job_id)
    for path in LOG_DIR.glob("job-*.json"):
        match = re.fullmatch(r"job-([0-9a-fA-F]{8,32})\.json", path.name)
        if not match:
            continue
        job_id = match.group(1)
        if job_id in seen:
            continue
        row = _history_row_from_disk(path, job_id)
        if not row:
            continue
        if want and _normalize_client_id(row.get("client_id")) != want:
            continue
        if sid and sid not in _job_service_ids(row):
            continue
        rows.append(row)
        seen.add(job_id)
    rows.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("id") or "")), reverse=True)
    total = len(rows)
    try:
        size = int(page_size or HISTORY_DEFAULT_PAGE_SIZE)
    except (TypeError, ValueError):
        size = HISTORY_DEFAULT_PAGE_SIZE
    size = max(1, min(size, 200))
    page_count = max(1, (total + size - 1) // size) if total else 1
    try:
        current = int(page or 1)
    except (TypeError, ValueError):
        current = 1
    current = max(1, min(current, page_count))
    start = (current - 1) * size
    return {
        "jobs": rows[start : start + size],
        "total": total,
        "page": current,
        "page_size": size,
        "page_count": page_count,
        "client_id": want or None,
        "service_id": sid or None,
    }


def _history_row_from_disk(path: Path, job_id: str) -> dict[str, Any] | None:
    """Parse a job meta file into a slim history row, with mtime/size cache."""
    key = str(path)
    try:
        st = path.stat()
    except OSError:
        return None
    with _history_disk_cache_lock:
        cached = _history_disk_cache.get(key)
        if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
            return dict(cached[2])
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(meta, dict):
        return None
    meta.setdefault("id", job_id)
    row = _history_row(meta, st.st_mtime)
    with _history_disk_cache_lock:
        _history_disk_cache[key] = (st.st_mtime, st.st_size, row)
        if len(_history_disk_cache) > 400:
            extra = len(_history_disk_cache) - 300
            for old_key in list(_history_disk_cache.keys())[:extra]:
                _history_disk_cache.pop(old_key, None)
    return dict(row)


def persist_job_meta(job_id: str) -> None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return
        status = str(job.get("status") or "")
        keys = (
            "id",
            "client_id",
            "operator",
            "created_at",
            "finished_at",
            "service_id",
            "service_ids",
            "branch",
            "status",
            "stage",
            "error",
            "remote",
            "archive",
            "archive_dir",
            "results",
            "progress",
            "current",
            "commit_sha",
            "test_status",
            "test_summary",
            "test_commands",
            "test_failures",
            "test_cases",
            "test_report",
            "test_runs",
            "test_kinds",
            "cancel_requested",
            "stage_before_stop",
            "slot_held",
            "queue_position",
        )
        meta = {k: job.get(k) for k in keys}
        # Keep running-job JSON small so history listing stays cheap.
        if status in ("ok", "failed", "stopped"):
            meta["step_logs"] = job.get("step_logs")
    try:
        job_meta_path(job_id).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass
    else:
        client_id = _normalize_client_id(meta.get("client_id"))
        if client_id:
            prune_build_history(client_id, keep_job_id=job_id)


def load_job_from_disk(job_id: str) -> dict[str, Any] | None:
    if not re.fullmatch(r"[0-9a-f]{8,32}", job_id or ""):
        return None
    meta_path = job_meta_path(job_id)
    log_path = LOG_DIR / f"job-{job_id}.log"
    if not meta_path.is_file() and not log_path.is_file():
        return None
    meta: dict[str, Any] = {"id": job_id, "status": "unknown"}
    if meta_path.is_file():
        try:
            meta.update(json.loads(meta_path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass
    log_lines: list[str] = []
    if log_path.is_file():
        try:
            log_lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            pass
    return {
        "id": job_id,
        "client_id": meta.get("client_id") or "",
        "operator": meta.get("operator") or "",
        "created_at": meta.get("created_at") or "",
        "finished_at": meta.get("finished_at") or "",
        "service_id": meta.get("service_id"),
        "service_ids": meta.get("service_ids") or _job_service_ids(meta),
        "branch": meta.get("branch"),
        "status": meta.get("status") or "unknown",
        "error": meta.get("error"),
        "remote": meta.get("remote"),
        "archive": meta.get("archive"),
        "archive_dir": meta.get("archive_dir"),
        "results": meta.get("results") or [],
        "progress": meta.get("progress"),
        "current": meta.get("current"),
        "stage": meta.get("stage"),
        "commit_sha": meta.get("commit_sha"),
        "test_status": meta.get("test_status"),
        "test_summary": meta.get("test_summary"),
        "test_commands": meta.get("test_commands"),
        "test_failures": meta.get("test_failures"),
        "test_cases": meta.get("test_cases"),
        "test_report": meta.get("test_report"),
        "test_runs": meta.get("test_runs") or [],
        "test_kinds": meta.get("test_kinds") or [],
        "step_logs": meta.get("step_logs") or {},
        "cancel_requested": bool(meta.get("cancel_requested")),
        "stage_before_stop": meta.get("stage_before_stop"),
        "log": log_lines,
    }


INTERRUPTED_JOB_ERROR = "helper restarted while this job was running"


def reap_orphaned_running_jobs() -> int:
    """Mark disk jobs still status=running after a helper restart as failed."""
    marked = 0
    for path in LOG_DIR.glob("job-*.json"):
        match = re.fullmatch(r"job-([0-9a-fA-F]{8,32})\.json", path.name)
        if not match:
            continue
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            continue
        if not isinstance(meta, dict) or meta.get("status") != "running":
            continue
        prev_stage = str(meta.get("stage") or "")
        if prev_stage and prev_stage not in _TERMINAL_PIPELINE_STAGES and not meta.get("stage_before_stop"):
            meta["stage_before_stop"] = prev_stage
        meta["status"] = "failed"
        meta["stage"] = "interrupted"
        meta["error"] = INTERRUPTED_JOB_ERROR
        try:
            path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError:
            continue
        job_id = match.group(1)
        try:
            ts = time.strftime("%H:%M:%S")
            with (LOG_DIR / f"job-{job_id}.log").open("a", encoding="utf-8") as fh:
                fh.write(f"[{ts}] FAILED {INTERRUPTED_JOB_ERROR}\n")
        except OSError:
            pass
        marked += 1
    return marked


def load_config() -> dict[str, Any]:
    cfg = {}
    if CONFIG_PATH.is_file():
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    ws = (cfg.get("workspace_root") or "").strip()
    if ws:
        cfg["workspace_root"] = str(Path(ws).expanduser().resolve())
    else:
        # Prefer D: for WSL I/O; fall back to helper dir
        preferred = Path(r"D:\swr-workspaces")
        try:
            preferred.mkdir(parents=True, exist_ok=True)
            cfg["workspace_root"] = str(preferred.resolve())
        except OSError:
            cfg["workspace_root"] = str((ROOT / "workspaces").resolve())
    Path(cfg["workspace_root"]).mkdir(parents=True, exist_ok=True)
    cfg["host"] = cfg.get("host") or "127.0.0.1"
    cfg["port"] = int(cfg.get("port") or 80)
    cfg["allow_remote"] = bool(cfg.get("allow_remote")) or str(
        os.environ.get("SWR_ALLOW_REMOTE") or ""
    ).lower() in ("1", "true", "yes")
    cfg["swr_registry"] = cfg.get("swr_registry") or "swr.cn-southwest-2.myhuaweicloud.com"
    cfg["swr_org"] = cfg.get("swr_org") or "public_ai"
    cfg["wsl_distro"] = cfg.get("wsl_distro") or "Ubuntu"
    cfg["build_timeout_sec"] = int(cfg.get("build_timeout_sec") or 7200)
    cfg["github_token"] = (
        (cfg.get("github_token") or "").strip()
        or (os.environ.get("SWR_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    )
    ssh_key = (cfg.get("github_ssh_key") or os.environ.get("SWR_GITHUB_SSH_KEY") or "").strip()
    if not ssh_key:
        for cand in (
            Path.home() / ".ssh" / "id_ed25519_github",
            Path.home() / ".ssh" / "id_ed25519",
            Path.home() / ".ssh" / "id_rsa",
        ):
            if cand.is_file():
                ssh_key = str(cand)
                break
    cfg["github_ssh_key"] = ssh_key
    # Prefer SSH when a key is present (server shared deploy)
    if "github_use_ssh" in cfg:
        cfg["github_use_ssh"] = bool(cfg.get("github_use_ssh"))
    else:
        cfg["github_use_ssh"] = bool(ssh_key)
    cfg["helper_root"] = str(ROOT)

    # Optional: archive image tar locally for nginx static download.
    archive_root = (
        cfg.get("archive_root")
        or os.environ.get("SWR_ARCHIVE_ROOT")
        or "/usr/share/nginx/html/images"
    ).strip()
    if "archive_enabled" in cfg:
        archive_enabled = bool(cfg.get("archive_enabled"))
    else:
        env_en = (os.environ.get("SWR_ARCHIVE_ENABLED") or "").strip().lower()
        archive_enabled = env_en in ("1", "true", "yes") if env_en else True
    if "archive_required" in cfg:
        archive_required = bool(cfg.get("archive_required"))
    else:
        env_req = (os.environ.get("SWR_ARCHIVE_REQUIRED") or "").strip().lower()
        archive_required = env_req in ("1", "true", "yes") if env_req else True
    cfg["archive_root"] = archive_root.rstrip("/") or "/usr/share/nginx/html/images"
    cfg["archive_enabled"] = archive_enabled
    cfg["archive_required"] = archive_required
    test_policy = str(cfg.get("test_policy") or "report_only").strip().lower()
    if test_policy not in {"report_only", "blocking"}:
        raise ValueError("test_policy must be report_only or blocking")
    cfg["test_policy"] = test_policy
    try:
        cfg["max_concurrent_jobs"] = max(1, min(int(cfg.get("max_concurrent_jobs") or 5), 16))
    except (TypeError, ValueError):
        cfg["max_concurrent_jobs"] = 5
    ci_tmp = (cfg.get("ci_tmp_root") or os.environ.get("SWR_CI_TMP") or "").strip()
    if not ci_tmp:
        ci_tmp = CI_TMP_DEFAULT if os.name != "nt" else (os.environ.get("TEMP") or os.environ.get("TMP") or ".")
    cfg["ci_tmp_root"] = str(Path(ci_tmp).expanduser())
    build_cache = (cfg.get("build_cache_root") or os.environ.get("SWR_BUILD_CACHE") or "").strip()
    if not build_cache:
        build_cache = BUILD_CACHE_DEFAULT if os.name != "nt" else str(Path(cfg["ci_tmp_root"]) / "build-cache")
    cfg["build_cache_root"] = str(Path(build_cache).expanduser())
    hc = cfg.get("huawei_cloud") if isinstance(cfg.get("huawei_cloud"), dict) else {}
    cfg["huawei_cloud"] = {
        "access_key": (
            (hc.get("access_key") or os.environ.get("HW_ACCESS_KEY") or os.environ.get("HUAWEICLOUD_SDK_AK") or "")
            .strip()
        ),
        "secret_key": (
            (hc.get("secret_key") or os.environ.get("HW_SECRET_KEY") or os.environ.get("HUAWEICLOUD_SDK_SK") or "")
            .strip()
        ),
        "default_region": (hc.get("default_region") or "cn-southwest-2").strip() or "cn-southwest-2",
        "project_id": (hc.get("project_id") or "").strip(),
    }
    return cfg


def resolve_huawei_credentials(data: dict[str, Any] | None) -> dict[str, str]:
    body = data if isinstance(data, dict) else {}
    hc = CFG.get("huawei_cloud") if isinstance(CFG.get("huawei_cloud"), dict) else {}
    access_key = (body.get("access_key") or hc.get("access_key") or "").strip()
    secret_key = (body.get("secret_key") or hc.get("secret_key") or "").strip()
    region = (body.get("region") or hc.get("default_region") or "cn-southwest-2").strip() or "cn-southwest-2"
    project_id = (body.get("project_id") or hc.get("project_id") or "").strip()
    if not access_key or not secret_key:
        raise ValueError("access_key and secret_key are required")
    return {
        "access_key": access_key,
        "secret_key": secret_key,
        "region": region,
        "project_id": project_id,
    }


def log_cce_api_error(path: str, code: int, message: str) -> None:
    safe_path = (path or "").split("?", 1)[0]
    print(f"[cce-api] {safe_path} -> {code}: {message}", flush=True)


def json_huawei_cce_error(handler: SimpleHTTPRequestHandler, exc: Exception) -> bool:
    if isinstance(exc, huawei_cce.HuaweiCCEError):
        payload: dict[str, Any] = {"error": str(exc)}
        if exc.detail is not None:
            payload["detail"] = exc.detail
        log_cce_api_error(handler.path, exc.status, str(exc))
        handler._json(exc.status, payload)
        return True
    if isinstance(exc, ValueError):
        log_cce_api_error(handler.path, 400, str(exc))
        handler._json(400, {"error": str(exc)})
        return True
    return False


def save_config_value(key: str, value: Any) -> None:
    global _token_cache
    data = {}
    if CONFIG_PATH.is_file():
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    data[key] = value
    CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if key == "github_token":
        _token_cache = None


CFG = load_config()


def ci_tmp_root() -> Path:
    return Path(str(CFG.get("ci_tmp_root") or CI_TMP_DEFAULT))


def build_cache_root() -> Path:
    return Path(str(CFG.get("build_cache_root") or BUILD_CACHE_DEFAULT))


def apply_ci_tmp_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """Point Go/Python/npm/docker scratch files at /home/ci instead of tiny /tmp."""
    target = os.environ if env is None else env
    root = ci_tmp_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    root_s = str(root)
    for key in ("TMPDIR", "TMP", "TEMP", "GOTMPDIR", "DOCKER_TMPDIR", "NPM_CONFIG_TMP"):
        target[key] = root_s
    return target


def is_protected_base_image(ref: str) -> bool:
    name = (ref or "").strip()
    if not name or name == "archive-only":
        return True
    if any(name.startswith(prefix) for prefix in PROTECTED_LOCAL_IMAGE_PREFIXES):
        return True
    return any(name == prefix or name.startswith(prefix + ":") for prefix in PROTECTED_LOCAL_IMAGES)


def _protected_image_hold_name(ref: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", ref).strip("-._") or "image"
    return f"{PROTECTED_IMAGE_HOLD_PREFIX}{slug}"[:63]


def _archive_image_hold_name(ref: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", ref).strip("-._") or "image"
    return f"{ARCHIVE_IMAGE_HOLD_PREFIX}{slug}"[:63]


def pin_images_for_prune(refs: list[str] | tuple[str, ...]) -> list[str]:
    """Stopped containers so `docker image prune -af` cannot drop these tags."""
    holds: list[str] = []
    seen: set[str] = set()
    for ref in refs:
        name = (ref or "").strip()
        if not name or name in seen or name == "archive-only":
            continue
        seen.add(name)
        code, _ = docker_cmd("image", "inspect", name, timeout=30)
        if code != 0:
            continue
        hold = _archive_image_hold_name(name)
        docker_cmd("rm", "-f", hold, timeout=30)
        code, _ = docker_cmd("create", "--name", hold, name, "true", timeout=60)
        if code == 0:
            holds.append(hold)
    return holds


def unpin_archive_image_holds(holds: list[str]) -> None:
    for hold in holds:
        name = (hold or "").strip()
        if not name:
            continue
        docker_cmd("rm", "-f", name, timeout=30)


def list_local_image_refs() -> list[str]:
    code, out = docker_cmd("images", "--format", "{{.Repository}}:{{.Tag}}", timeout=120)
    if code != 0:
        return []
    refs: list[str] = []
    for line in (out or "").splitlines():
        ref = line.strip()
        if ref and "<none>" not in ref:
            refs.append(ref)
    return refs


def ensure_protected_image_holds() -> None:
    """Pin protected images with stopped containers so image prune keeps them."""
    for ref in list_local_image_refs():
        if not is_protected_base_image(ref):
            continue
        name = _protected_image_hold_name(ref)
        code, _ = docker_cmd("inspect", "-f", "{{.Id}}", name, timeout=30)
        if code == 0:
            continue
        docker_cmd("create", "--name", name, ref, "true", timeout=60)


def reclaim_unused_docker_images(job_id: str, keep_refs: list[str] | tuple[str, ...] = ()) -> None:
    """Remove unused Docker images. Protected toolchain / Fleet runtime images are kept."""
    append_job_log(job_id, "pruning unused docker images (protected base images kept)")
    keep = [r for r in keep_refs if (r or "").strip()]
    if keep:
        append_job_log(job_id, "keep for archive: " + ", ".join(keep))
    ensure_protected_image_holds()
    holds = pin_images_for_prune(keep)
    try:
        code, out = docker_cmd("image", "prune", "-af", timeout=300)
        for line in (out or "").splitlines()[-8:]:
            append_job_log(job_id, line)
        if code != 0:
            append_job_log(job_id, f"WARN: docker image prune failed: {(out or '')[-200:]}")
    finally:
        unpin_archive_image_holds(holds)


def remove_business_images(job_id: str, *refs: str) -> None:
    """Delete this job's compiled service images only. Never docker image prune."""
    seen: set[str] = set()
    for ref in refs:
        name = (ref or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        if is_protected_base_image(name):
            append_job_log(job_id, f"keep base image {name}")
            continue
        append_job_log(job_id, f"remove business image {name}")
        docker_cmd("rmi", "-f", name, timeout=60)


def reload_cfg() -> None:
    global CFG, _token_cache
    CFG = load_config()
    _token_cache = None
    apply_ci_tmp_env()


apply_ci_tmp_env()


def load_services() -> list[dict[str, Any]]:
    return json.loads(SERVICES_PATH.read_text(encoding="utf-8"))


def test_report_dir(job_id: str, service_id: str) -> Path:
    safe_service_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", service_id).strip("._") or "service"
    return LOG_DIR / "reports" / job_id / safe_service_id


def run_tests_nonblocking(job_id: str, service_id: str, repo_dir: Path, commit_sha: str) -> dict[str, Any]:
    """Run trusted UT/DT tests; the caller applies robot-ci's global test policy."""
    report_dir = test_report_dir(job_id, service_id)
    report_dir.mkdir(parents=True, exist_ok=True)
    summary_path = report_dir / "summary.json"
    plans_path = TEST_PLANS_PATH
    cid_error = ""
    try:
        cid = load_cid_config(repo_dir, service_id)
    except CidConfigError as exc:
        cid = None
        cid_error = str(exc)
    if cid_error:
        result = {
            "status": "error",
            "total": 0,
            "passed": 0,
            "failed": 0,
            "errors": 1,
            "duration_ms": 0,
            "failures": [{"name": "CID configuration", "detail": cid_error}],
        }
    elif cid is not None:
        plans_path = report_dir / "cid-test-plan.json"
        plans_path.write_text(
            json.dumps(build_test_plan(cid), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        append_job_log(job_id, "Loaded test stages from .cid/build.yaml")
    if cid_error:
        pass
    elif not plans_path.is_file() or not TEST_RUNNER_PATH.is_file():
        result = {
            "status": "error",
            "total": 0,
            "passed": 0,
            "failed": 0,
            "errors": 1,
            "duration_ms": 0,
            "failures": [{"name": "test runner", "detail": "test plan or test_runner.py is missing"}],
        }
    else:
        append_job_log(job_id, f"tests start service={service_id} sha={commit_sha}")
        command = " ".join(
            (
                "python3.11",
                shlex.quote(host_path(TEST_RUNNER_PATH)),
                "--plans",
                shlex.quote(host_path(plans_path)),
                "--service",
                shlex.quote(service_id),
                "--repo",
                shlex.quote(host_path(repo_dir)),
                "--report-dir",
                shlex.quote(host_path(report_dir)),
            )
        )
        exit_code = run_stream(job_id, bash_lc(command), timeout=int(CFG.get("test_timeout_sec") or 7200))
        try:
            result = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            result = {
                "status": "timeout" if exit_code == 124 else "error",
                "total": 0,
                "passed": 0,
                "failed": 0,
                "errors": 1,
                "duration_ms": 0,
                "failures": [{"name": "test runner", "detail": "test summary was not produced"}],
            }
    result["commit_sha"] = commit_sha
    result["report_dir"] = str(report_dir)
    try:
        safe_service_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", service_id).strip("._") or "service"
        (LOG_DIR / f"job-{job_id}-{safe_service_id}-test.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError:
        pass
    append_job_log(
        job_id,
        "TEST summary "
        f"status={result.get('status')} total={result.get('total', 0)} "
        f"passed={result.get('passed', 0)} failed={result.get('failed', 0)} "
        f"errors={result.get('errors', 0)} duration_ms={result.get('duration_ms', 0)}",
    )
    append_job_log(
        job_id,
        "Tests completed "
        f"service={service_id} status={result.get('status')} total={result.get('total', 0)} "
        f"passed={result.get('passed', 0)} failed={result.get('failed', 0)} "
        f"errors={result.get('errors', 0)} skipped={result.get('skipped', 0)} "
        f"duration_ms={result.get('duration_ms', 0)}",
    )
    for item in (result.get("failures") or [])[:20]:
        append_job_log(job_id, f"TEST FAIL {item.get('name')}: {item.get('detail')}")
    if result.get("status") not in ("passed", "not_configured"):
        if CFG.get("test_policy") == "blocking":
            append_job_log(job_id, "ERROR tests did not pass; blocking policy stops the build")
        else:
            append_job_log(job_id, "WARN tests did not pass; report_only policy continues to build")
    return result


def tests_block_build(result: dict[str, Any]) -> bool:
    """Whether the configured framework policy rejects this test result."""
    return (
        CFG.get("test_policy") == "blocking"
        and result.get("status") not in ("passed", "not_configured")
    )


def use_wsl() -> bool:
    """Windows helper uses WSL Docker; Linux server uses native docker/bash."""
    if os.name != "nt":
        return False
    return bool(shutil.which("wsl.exe") or shutil.which("wsl"))


def wsl_prefix() -> list[str]:
    return ["wsl.exe", "-d", CFG["wsl_distro"], "-u", "root", "--"]


def shell_prefix() -> list[str]:
    return wsl_prefix() if use_wsl() else []


def bash_lc(script: str) -> list[str]:
    return shell_prefix() + ["bash", "-lc", script]


def host_path(path: Path) -> str:
    """Path string usable inside the shell/docker host (WSL path on Windows)."""
    if use_wsl():
        return win_to_wsl(path)
    return str(path.resolve()).replace("\\", "/")


def current_job_id() -> str:
    return str(getattr(_job_ctx, "job_id", "") or "")


def job_cancel_requested(job_id: str) -> bool:
    if not job_id:
        return False
    with _jobs_lock:
        job = _jobs.get(job_id)
        return bool(job and job.get("cancel_requested"))


def _popen_session_kwargs() -> dict[str, Any]:
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return {"creationflags": flags} if flags else {}
    return {"start_new_session": True}


def _kill_process_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=15,
            )
        except Exception:
            pass
        try:
            proc.kill()
        except OSError:
            pass
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass


def attach_job_proc(job_id: str, proc: subprocess.Popen) -> None:
    if not job_id:
        return
    with _job_procs_lock:
        _job_procs.setdefault(job_id, []).append(proc)


def detach_job_proc(job_id: str, proc: subprocess.Popen) -> None:
    if not job_id:
        return
    with _job_procs_lock:
        procs = _job_procs.get(job_id) or []
        if proc in procs:
            procs.remove(proc)
        if not procs:
            _job_procs.pop(job_id, None)


def kill_job_procs(job_id: str) -> int:
    with _job_procs_lock:
        procs = list(_job_procs.get(job_id) or [])
    killed = 0
    for proc in procs:
        if proc.poll() is None:
            _kill_process_tree(proc)
            killed += 1
    return killed


def mark_job_stopped(job_id: str) -> None:
    set_job(job_id, status="stopped", stage="done", current="", error=STOPPED_JOB_ERROR)
    append_job_log(job_id, "STOPPED")


def request_job_stop(job_id: str) -> dict[str, Any]:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return {"ok": False, "error": "job not found", "http_status": 404}
        if job.get("status") not in ("running", "queued"):
            return {
                "ok": False,
                "error": f"任务已结束（{job.get('status')}）",
                "http_status": 409,
                "status": job.get("status"),
            }
        already = bool(job.get("cancel_requested"))
        if not already:
            job["stage_before_stop"] = job.get("stage")
        job["cancel_requested"] = True
        job["stage"] = "stopping"
    persist_job_meta(job_id)
    if not already:
        append_job_log(job_id, "STOP requested: killing current process")
    kill_job_procs(job_id)
    with _slot_lock:
        _slot_cond.notify_all()
    return {"ok": True, "id": job_id, "status": "stopping"}


def run_cmd(
    args: list[str],
    timeout: int | None = 600,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> tuple[int, str]:
    job_id = current_job_id()
    if job_id and job_cancel_requested(job_id):
        raise JobStopped()
    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=cwd,
            env=env,
            **_popen_session_kwargs(),
        )
    except FileNotFoundError:
        return 127, f"ERROR: not found: {args[0]}"
    attach_job_proc(job_id, proc)
    try:
        try:
            out, _err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_process_tree(proc)
            out, _err = proc.communicate(timeout=10)
            if job_id and job_cancel_requested(job_id):
                raise JobStopped()
            return 124, ((out or "").strip() + "\nERROR: timeout").strip()
        if job_id and job_cancel_requested(job_id):
            raise JobStopped()
        return proc.returncode, (out or "").strip()
    finally:
        detach_job_proc(job_id, proc)


def run_cmd_stdin(args: list[str], stdin_text: str, timeout: int | None = 120) -> tuple[int, str]:
    try:
        p = subprocess.run(
            args,
            input=stdin_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        out = (p.stdout or "") + (("\n" + p.stderr) if p.stderr else "")
        return p.returncode, out.strip()
    except Exception as e:  # noqa: BLE001
        return 1, str(e)


def docker_cmd(*docker_args: str, timeout: int | None = 600) -> tuple[int, str]:
    if use_wsl():
        return run_cmd(wsl_prefix() + ["docker", *docker_args], timeout=timeout)
    return run_cmd(["docker", *docker_args], timeout=timeout)


def win_to_wsl(path: Path) -> str:
    s = str(path.resolve())
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", s)
    if not m:
        return s.replace("\\", "/")
    return f"/mnt/{m.group(1).lower()}/{m.group(2).replace(chr(92), '/')}"


def interruptible_sleep(job_id: str, seconds: float, interval: float = 0.25) -> None:
    deadline = time.monotonic() + max(0.0, float(seconds or 0))
    while time.monotonic() < deadline:
        if job_id and job_cancel_requested(job_id):
            raise JobStopped()
        time.sleep(min(interval, max(0.0, deadline - time.monotonic())))


@contextmanager
def log_substep(sub_id: str):
    prev = str(getattr(_job_ctx, "log_substep", "") or "")
    _job_ctx.log_substep = str(sub_id or "")
    try:
        yield
    finally:
        _job_ctx.log_substep = prev


def _infer_test_log_substep(line: str) -> str:
    text = str(line or "")
    if "@@TEST_STEP@@" in text:
        type_match = re.search(r"@@TEST_TYPE@@\s*(ut|dt)\b", text, re.I)
        if type_match:
            return f"{type_match.group(1).lower()}-cases"
        name = re.split(r"@@TEST_TYPE@@", text.split("@@TEST_STEP@@", 1)[-1], maxsplit=1)[0].strip()
        tokens = set(re.findall(r"[a-z]+", name.lower()))
        has_ut = "ut" in tokens or "单元" in name
        has_dt = "dt" in tokens
        if has_ut and not has_dt:
            return "ut-cases"
        if has_dt and not has_ut:
            return "dt-cases"
        return "cases"
    if "Loaded test stages" in text or "加载 .cid/build.yaml" in text:
        return "plan"
    if "tests start" in text or "启动 test_runner" in text:
        return "runner"
    lowered = text.lower()
    if (
        "test summary" in lowered
        or "@@test_summary@@" in lowered
        or "tests completed" in lowered
        or "tests passed" in lowered
        or "tests failed" in lowered
    ):
        return "report"
    return ""


_TEST_LOG_SUBS = {"plan", "runner", "ut-cases", "dt-cases", "cases", "report"}


def _strip_log_prefix(line: str) -> str:
    text = str(line or "")
    if text.startswith("[") and "]" in text[:16]:
        text = text.split("]", 1)[-1].strip()
    return text


def _canonical_log_substep(step_id: str, sub_id: str) -> str:
    sub_id = str(sub_id or "").strip()
    if step_id == "prepare" and sub_id in {"swr", "ps"}:
        return "env"
    if step_id == "sync" and sub_id in {"fetch", "checkout"}:
        return "clone"
    if step_id == "build" and sub_id == "deps":
        return "script"
    if step_id == "test" and sub_id == "report":
        return "runner"
    return sub_id


def _infer_pipeline_log_substep(step_id: str, line: str) -> str:
    text = _strip_log_prefix(line)
    lowered = text.lower()
    if step_id == "test":
        inferred = _infer_test_log_substep(text) or _infer_test_log_substep(line)
        return _canonical_log_substep("test", inferred)
    if step_id == "prepare":
        if any(
            token in lowered
            for token in (
                "archive dir",
                "cannot create archive",
                "归档目录",
                "remove old archive dir",
                "pruning old nginx archives",
            )
        ):
            return "adir"
        return "env"
    if step_id == "sync":
        if lowered.startswith("head=") or "commit sha" in lowered or "记录 commit sha" in lowered:
            return "sha"
        return "clone"
    if step_id == "build":
        if any(token in lowered for token in ("build finished", "error build", "产物校验")):
            return "verify"
        if any(
            token in lowered
            for token in (
                "dockerfile",
                "docker build",
                "sending build context",
                "successfully tagged",
                "docker 多阶段",
            )
        ):
            return "docker"
        return "script"
    if step_id == "push":
        if "docker tag" in lowered or lowered.startswith("tag "):
            return "tag"
        if any(token in lowered for token in ("digest:", "推送确认")):
            return "verify"
        if lowered.startswith("ok ") and "archive" not in lowered and "host package" not in lowered:
            return "verify"
        return "push"
    if step_id == "archive":
        return "save"
    return ""


def _substep_alias_keys(step_id: str, sub_id: str) -> list[str]:
    want = _canonical_log_substep(step_id, sub_id)
    keys = [f"{step_id}:{want}"]
    if want == "env":
        keys.extend([f"{step_id}:swr", f"{step_id}:ps"])
    elif want == "clone":
        keys.extend([f"{step_id}:fetch", f"{step_id}:checkout"])
    elif want == "script":
        keys.append(f"{step_id}:deps")
    elif want == "runner":
        keys.append(f"{step_id}:report")
    return keys


def _selected_step_logs(logs: dict[str, Any], step_id: str, sub_id: str) -> list[str]:
    parent = list(logs.get(step_id) or [])
    if not sub_id:
        return parent
    want = _canonical_log_substep(step_id, sub_id)
    selected: list[str] = []
    seen: set[str] = set()
    for key in _substep_alias_keys(step_id, want):
        for line in logs.get(key) or []:
            if line in seen:
                continue
            seen.add(line)
            selected.append(line)
    if selected:
        return selected
    return [line for line in parent if _infer_pipeline_log_substep(step_id, line) == want]


def _current_log_step(job: dict[str, Any]) -> str:
    stage = str(job.get("stage") or "")
    if stage == "stopping":
        stage = str(job.get("stage_before_stop") or "")
    return STAGE_TO_PIPELINE_STEP.get(stage, "prepare")


def append_job_log(job_id: str, line: str) -> None:
    ts = time.strftime("%H:%M:%S")
    text = f"[{ts}] {line}"
    log_file = ""
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return
        job["log"].append(text)
        _append_ui_log(job, text)
        step_id = _current_log_step(job)
        job.setdefault("step_logs", {}).setdefault(step_id, []).append(text)
        inferred_test = _infer_test_log_substep(line)
        ctx_sub = str(getattr(_job_ctx, "log_substep", "") or "").strip()
        if step_id == "test":
            if inferred_test:
                _job_ctx.log_substep = inferred_test
                sub_id = inferred_test
            else:
                sub_id = ctx_sub
        else:
            if ctx_sub in _TEST_LOG_SUBS:
                ctx_sub = ""
            sub_id = ctx_sub or _infer_pipeline_log_substep(step_id, line)
        sub_id = _canonical_log_substep(step_id, sub_id)
        if sub_id:
            job["step_logs"].setdefault(f"{step_id}:{sub_id}", []).append(text)
            if sub_id in ("ut-cases", "dt-cases"):
                job["step_logs"].setdefault(f"{step_id}:cases", []).append(text)
        log_file = str(job.get("log_file") or "")
    if log_file:
        try:
            with Path(log_file).open("a", encoding="utf-8") as fh:
                fh.write(text + "\n")
        except OSError:
            pass


def set_job(job_id: str, **fields: Any) -> None:
    with _jobs_lock:
        if job_id not in _jobs:
            return
        status = str(fields.get("status") or "")
        if status in ("ok", "failed", "stopped") and not _jobs[job_id].get("finished_at"):
            fields.setdefault("finished_at", time.strftime("%Y-%m-%d %H:%M:%S"))
        _jobs[job_id].update(fields)
    persist_job_meta(job_id)


def record_test_run(job_id: str, test_run: dict[str, Any]) -> None:
    """Append one service result while retaining legacy single-service fields."""
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return
        runs = list(job.get("test_runs") or [])
        runs.append(test_run)
        job.update(
            {
                "test_runs": runs,
                "commit_sha": test_run.get("commit_sha"),
                "test_status": test_run.get("status"),
                "test_summary": test_run.get("summary"),
                "test_commands": test_run.get("commands") or [],
                "test_failures": test_run.get("failures") or [],
                "test_cases": test_run.get("test_cases") or [],
                "test_report": test_run.get("test_report"),
            }
        )
    persist_job_meta(job_id)


def git_bin() -> str:
    return shutil.which("git") or "git"


def git_env() -> dict[str, str]:
    """Non-interactive git env; attach SSH key for GitHub when configured."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    env["GIT_ASKPASS"] = ""
    env["SSH_ASKPASS"] = ""
    env["GCM_PRESERVE_CREDENTIALS"] = "true"
    key = (CFG.get("github_ssh_key") or "").strip()
    if key and Path(key).is_file():
        # BatchMode=yes → never prompt for passphrase / host confirmation hang
        env["GIT_SSH_COMMAND"] = (
            f'ssh -i "{key}" -o IdentitiesOnly=yes -o BatchMode=yes '
            f"-o StrictHostKeyChecking=accept-new"
        )
    return env


def git_args(*args: str) -> list[str]:
    """git with HTTPS credential helper disabled."""
    return [
        git_bin(),
        "-c",
        "credential.helper=",
        "-c",
        "credential.interactive=never",
        *args,
    ]


def to_github_ssh_url(url: str) -> str:
    """https://github.com/org/repo.git -> git@github.com:org/repo.git"""
    if url.startswith("git@"):
        return url
    pub = public_github_url(url)
    m = re.search(r"github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", pub)
    if not m:
        return url
    return f"git@github.com:{m.group(1)}/{m.group(2)}.git"


def clone_url_for(
    url: str, *, public_https: bool = False, prefer_token_https: bool = False
) -> str:
    if public_https:
        return public_github_url(url)
    if prefer_token_https and gh_token():
        return auth_github_url(public_github_url(url))
    if CFG.get("github_use_ssh") and (CFG.get("github_ssh_key") or ""):
        return to_github_ssh_url(url)
    if gh_token():
        return auth_github_url(public_github_url(url))
    return public_github_url(url)


def gh_token() -> str:
    """Token from page/env, or non-interactive `gh auth token` (no GCM GUI)."""
    global _token_cache
    if _token_cache:
        return _token_cache
    tok = (CFG.get("github_token") or "").strip()
    if tok:
        _token_cache = tok
        return tok
    # gh CLI can return a token without opening the account-picker GUI
    if shutil.which("gh"):
        code, out = run_cmd(
            ["gh", "auth", "token"],
            timeout=8,
            env={**os.environ, "GH_PROMPT_DISABLED": "1", "GIT_TERMINAL_PROMPT": "0"},
        )
        if code == 0 and out.strip():
            _token_cache = out.strip().splitlines()[0].strip()
            return _token_cache
    return ""


def auth_github_url(url: str) -> str:
    token = gh_token()
    if not token or not url:
        return url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or "github.com" not in (parsed.hostname or "").lower():
        return url
    if parsed.username:
        return url
    netloc = f"x-access-token:{quote(token, safe='')}@{parsed.hostname}"
    return urlunparse((parsed.scheme, netloc, parsed.path, "", "", ""))


def public_github_url(url: str) -> str:
    parsed = urlparse(url or "")
    if not parsed.hostname:
        return url or ""
    netloc = parsed.hostname + (f":{parsed.port}" if parsed.port else "")
    return urlunparse((parsed.scheme or "https", netloc, parsed.path, "", "", ""))


def repo_full_name(svc: dict[str, Any]) -> str:
    if svc.get("repo"):
        return svc["repo"]
    gh = public_github_url(svc.get("github") or "")
    m = re.search(r"github\.com/([^/]+/[^/]+?)(?:\.git)?$", gh)
    return m.group(1) if m else ""


def clone_dir_name(svc: dict[str, Any]) -> str:
    name = repo_full_name(svc).rsplit("/", 1)[-1] or svc["id"]
    return re.sub(r"[^\w.\-]+", "_", name)


def job_workspace_suffix(job_id: str | None) -> str:
    safe = re.sub(r"[^0-9a-fA-F]+", "", str(job_id or ""))
    return (safe[:32] or "job") if job_id else ""


def repo_dir(svc: dict[str, Any], job_id: str | None = None) -> Path:
    """Per-job checkout: <workspace_root>/<repo>--<job_id>.

    Sibling of shared public-service so deploy.sh `../public-service` still works.
    """
    root = Path(CFG["workspace_root"])
    name = clone_dir_name(svc)
    suffix = job_workspace_suffix(job_id)
    return root / (f"{name}--{suffix}" if suffix else name)


def existing_clone_dirs(svc: dict[str, Any]) -> list[Path]:
    root = Path(CFG["workspace_root"])
    if not root.is_dir():
        return []
    name = clone_dir_name(svc)
    found: list[Path] = []
    leftover = root / name
    if leftover.is_dir():
        found.append(leftover)
    prefix = f"{name}--"
    try:
        stamped = [
            path
            for path in root.iterdir()
            if path.is_dir() and path.name.startswith(prefix)
        ]
    except OSError:
        stamped = []
    found.extend(sorted(stamped, key=lambda path: path.stat().st_mtime, reverse=True))
    return found


def _kept_workspace_suffixes(job_id: str) -> set[str]:
    keep = {job_workspace_suffix(job_id)}
    with _jobs_lock:
        keep.update(job_workspace_suffix(str(item.get("id") or "")) for item in _live_jobs_locked())
    keep.discard("")
    return keep


def gc_idle_clone_dirs(job_id: str, svc: dict[str, Any]) -> None:
    """Remove finished per-job checkouts; keep dirs belonging to still-running jobs."""
    root = Path(CFG["workspace_root"])
    if not root.is_dir():
        return
    name = clone_dir_name(svc)
    prefix = f"{name}--"
    keep = _kept_workspace_suffixes(job_id)
    leftover = root / name
    if leftover.exists():
        wipe_workspace_dir(job_id, leftover, label=f"legacy workspace {name}")
    try:
        children = list(root.iterdir())
    except OSError:
        return
    for path in children:
        if not path.is_dir() or not path.name.startswith(prefix):
            continue
        if path.name[len(prefix) :] in keep:
            continue
        wipe_workspace_dir(job_id, path, label=f"idle workspace {path.name}")


def gc_all_idle_clone_dirs(job_id: str) -> None:
    """Drop every finished checkout. Keep public-service, fleet cache, running jobs."""
    root = Path(CFG.get("workspace_root") or "")
    if not root.is_dir():
        return
    keep = _kept_workspace_suffixes(job_id)
    try:
        children = list(root.iterdir())
    except OSError:
        return
    for path in children:
        if not path.is_dir() or path.is_symlink():
            continue
        if path.name in WORKSPACE_KEEP_NAMES:
            continue
        suffix = path.name.rsplit("--", 1)[-1] if "--" in path.name else ""
        if suffix and suffix in keep:
            continue
        wipe_workspace_dir(job_id, path, label=f"idle workspace {path.name}")


def reclaim_ci_tmp_leftovers(job_id: str) -> None:
    """Remove leftover compile scratch dirs under /home/ci. Keep the tmp root."""
    root = ci_tmp_root()
    if not root.is_dir():
        return
    try:
        children = list(root.iterdir())
    except OSError:
        return
    for path in children:
        name = path.name
        if name in CI_TMP_GC_NAMES or name.startswith(CI_TMP_GC_PREFIXES):
            wipe_workspace_dir(job_id, path, label=f"ci tmp leftover {name}")


def fleet_runtime_cache_dir() -> Path:
    """Persistent Fleet cache metadata that survives fresh workspace clones."""
    return Path(CFG["workspace_root"]) / ".robot-ci-cache" / "multica-fleet" / "runtime-images"


def _validated_fleet_cache_marker(path: Path, marker: str) -> str | None:
    """Read only the two small, strictly formatted cache markers Fleet produces."""
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
            return None
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None

    if marker == ".build-source.sha256":
        value = text.strip()
        return value + "\n" if re.fullmatch(r"[0-9a-fA-F]{64}", value) else None
    if marker == ".build-image-ids":
        lines = [line.strip("\r") for line in text.splitlines() if line.strip()]
        if len(lines) != 2 or any(not FLEET_IMAGE_ID_LINE_RE.fullmatch(line) for line in lines):
            return None
        return "\n".join(lines) + "\n"
    return None


def persist_fleet_runtime_cache(job_id: str, workspace: Path) -> bool:
    """Persist validated Fleet cache markers outside the disposable checkout."""
    source_dir = workspace / "build" / "deploy" / "runtime-images"
    contents: dict[str, str] = {}
    for marker in FLEET_RUNTIME_CACHE_MARKERS:
        content = _validated_fleet_cache_marker(source_dir / marker, marker)
        if content is None:
            return False
        contents[marker] = content

    cache_dir = fleet_runtime_cache_dir()
    try:
        with _fleet_cache_lock:
            cache_dir.mkdir(parents=True, exist_ok=True)
            for marker, content in contents.items():
                target = cache_dir / marker
                temporary = target.with_name(target.name + ".tmp")
                temporary.write_text(content, encoding="utf-8")
                temporary.replace(target)
    except OSError as exc:
        append_job_log(job_id, f"WARN: Fleet runtime cache metadata could not be saved: {exc}")
        return False
    append_job_log(job_id, f"Fleet runtime cache metadata saved: {cache_dir}")
    return True


def restore_fleet_runtime_cache(job_id: str, workspace: Path) -> bool:
    """Restore validated metadata; Fleet still verifies source hash and live image IDs."""
    cache_dir = fleet_runtime_cache_dir()
    contents: dict[str, str] = {}
    with _fleet_cache_lock:
        for marker in FLEET_RUNTIME_CACHE_MARKERS:
            content = _validated_fleet_cache_marker(cache_dir / marker, marker)
            if content is None:
                return False
            contents[marker] = content

    target_dir = workspace / "build" / "deploy" / "runtime-images"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        for marker, content in contents.items():
            (target_dir / marker).write_text(content, encoding="utf-8")
    except OSError as exc:
        append_job_log(job_id, f"WARN: Fleet runtime cache metadata could not be restored: {exc}")
        return False
    append_job_log(
        job_id,
        "Fleet runtime cache metadata restored; source fingerprint and Docker image IDs will be verified",
    )
    return True


def finalize_fleet_bundle_artifacts(job_id: str, bundle: Path) -> bool:
    """Make the Fleet bundle and checksum nginx-readable without altering metadata."""
    try:
        bundle.chmod(0o644)
        checksum = Path(str(bundle) + ".sha256")
        if checksum.is_file():
            checksum.chmod(0o644)
    except OSError as exc:
        append_job_log(job_id, f"ERROR: Fleet archive permissions could not be finalized: {exc}")
        return False
    append_job_log(job_id, f"Fleet archive download permissions set: {bundle.name} mode=0644")
    return True


def check_docker(force: bool = False) -> dict[str, Any]:
    """Probe Docker via `docker info` (native Linux or WSL).

    Successful probes are cached ~45s. Failures are cached only ~3s so a busy
    daemon during an in-flight build does not 503 subsequent /api/push calls.
    """
    global _docker_cache
    now = time.time()
    with _docker_cache_lock:
        cached = _docker_cache
    if not force and cached:
        ts, st = cached
        ttl = 45 if st.get("ok") else 3
        if now - ts < ttl:
            return dict(st)
    if use_wsl():
        pass
    elif shutil.which("docker") is None:
        st = {"ok": False, "detail": "docker not installed"}
        with _docker_cache_lock:
            _docker_cache = (now, st)
        return dict(st)
    code, out = docker_cmd("info", timeout=8)
    if code != 0:
        st = {"ok": False, "detail": ((out or "")[-400:] or "docker unavailable")}
        with _docker_cache_lock:
            _docker_cache = (now, st)
        return dict(st)
    where = f"WSL:{CFG['wsl_distro']}" if use_wsl() else "native"
    st = {"ok": True, "detail": f"Docker ok / {where}"}
    with _docker_cache_lock:
        _docker_cache = (now, st)
    return dict(st)


def docker_config_has_swr_auth() -> bool:
    """Fast local check: docker config.json contains auth for SWR registry."""
    registry = (CFG.get("swr_registry") or "").strip()
    if not registry:
        return False
    cfg_path = Path.home() / ".docker" / "config.json"
    if not cfg_path.is_file():
        return False
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    auths = data.get("auths") or {}
    if not isinstance(auths, dict):
        return False
    for key, val in auths.items():
        if registry in str(key) or str(key) in registry:
            if isinstance(val, dict) and (val.get("auth") or val.get("identitytoken")):
                return True
            if val:
                return True
    return False


def login_status_fast() -> bool:
    """UI/health: memory flag or local docker auth presence (no network)."""
    with _login_lock:
        if _login_ok:
            return True
        if _login_probe_cache and time.time() - _login_probe_cache[0] < 60:
            return bool(_login_probe_cache[1])
    return docker_config_has_swr_auth()


def docker_status_cached() -> dict[str, Any]:
    """Never block HTTP handlers on `docker info` (slow during builds)."""
    with _docker_cache_lock:
        cached = _docker_cache
    if cached:
        return dict(cached[1])
    return {"ok": True, "detail": "Docker (后台检测中)"}


def docker_ready_for_push() -> dict[str, Any]:
    """Decide whether /api/push may start a job without blocking on `docker info`.

    A live probe during a heavy build often times out; caching that failure used
    to 503 retries for 45s. If a job is already running, Docker is available.
    Otherwise trust a recent OK cache, reject only a confirmed missing binary,
    and otherwise allow the job (it will fail later if Docker is truly down).
    """
    with _jobs_lock:
        running = bool(_live_jobs_locked())
    cached = docker_status_cached()
    if running:
        return {"ok": True, "detail": cached.get("detail") or "Docker in use by a running job"}
    if cached.get("ok"):
        return dict(cached)
    if str(cached.get("detail") or "") == "docker not installed":
        return dict(cached)
    schedule_docker_probe()
    return {"ok": True, "detail": cached.get("detail") or "Docker (后台检测中)"}


def schedule_docker_probe() -> None:
    """Refresh docker cache in background if stale/missing."""
    now = time.time()
    with _docker_cache_lock:
        cached = _docker_cache
    if cached:
        ts, st = cached
        ttl = 45 if st.get("ok") else 3
        if now - ts < ttl:
            return

    def _run() -> None:
        try:
            check_docker(force=True)
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True).start()


def do_login(command: str) -> tuple[bool, str]:
    global _login_ok, _login_probe_cache
    cmd = command.strip()
    if not cmd:
        return False, "empty login command"
    m = re.search(r"docker\s+login\s+-u\s+(\S+)\s+-p\s+(\S+)\s+(\S+)", cmd, re.I)
    if m:
        user, password, registry = m.group(1), m.group(2), m.group(3)
        login_args = ["docker", "login", "-u", user, "--password-stdin", registry]
        if use_wsl():
            login_args = wsl_prefix() + login_args
        code, out = run_cmd_stdin(login_args, password, timeout=120)
    else:
        code, out = run_cmd(bash_lc(cmd), timeout=120)
    ok = code == 0 and "succeeded" in out.lower()
    with _login_lock:
        _login_ok = ok
        _login_probe_cache = (time.time(), ok)
    return ok, out


def do_logout() -> tuple[bool, str]:
    """Clear shared SWR docker credentials on this server."""
    global _login_ok, _login_probe_cache
    registry = (CFG.get("swr_registry") or "").strip()
    if not registry:
        return False, "missing swr_registry"
    code, out = docker_cmd("logout", registry, timeout=60)
    # Also drop local auth entry if docker logout left remnants
    cfg_path = Path.home() / ".docker" / "config.json"
    if cfg_path.is_file():
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            auths = data.get("auths") or {}
            if isinstance(auths, dict):
                for key in list(auths.keys()):
                    if registry in str(key) or str(key) in registry:
                        auths.pop(key, None)
                data["auths"] = auths
                cfg_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    with _login_lock:
        _login_ok = False
        _login_probe_cache = (time.time(), False)
    # docker logout returns 0 even when not logged in
    msg = (out or "").strip() or f"Logged out of {registry}"
    ok = code == 0 or "not logged in" in msg.lower() or "removing login" in msg.lower()
    return ok, msg


def _swr_probe_ok(code: int, out: str) -> tuple[bool, str]:
    text = (out or "").strip()
    low = text.lower()
    snippet = text.replace("\n", " ")[-240:] or f"exit={code}"
    # Missing-image responses mean the registry accepted our credentials.
    # Check these first: some SWR replies also contain the word "denied".
    if code == 0 or any(
        marker in low for marker in ("manifest unknown", "name unknown", "no such manifest")
    ):
        return True, snippet
    if "unauthorized" in low or "authentication required" in low:
        return False, snippet
    if "denied" in low:
        return False, snippet
    return False, snippet


def check_login(force: bool = False) -> bool:
    """Validate SWR auth. Uses short cache; force=True always hits registry."""
    return check_login_detail(force=force)[0]


def check_login_detail(force: bool = False) -> tuple[bool, str]:
    """Like check_login, but also return the probe snippet for job logs."""
    global _login_ok, _login_probe_cache
    now = time.time()
    with _login_lock:
        if not force and _login_probe_cache:
            ts, ok = _login_probe_cache
            ttl = 45 if ok else 5
            if now - ts < ttl:
                _login_ok = bool(ok)
                return bool(ok), "cached"

    with _login_probe_lock:
        now = time.time()
        with _login_lock:
            if not force and _login_probe_cache:
                ts, ok = _login_probe_cache
                ttl = 45 if ok else 5
                if now - ts < ttl:
                    _login_ok = bool(ok)
                    return bool(ok), "cached"
        if not docker_config_has_swr_auth():
            with _login_lock:
                _login_ok = False
                _login_probe_cache = (now, False)
            return False, "local docker config has no SWR auth"

        remote = f"{CFG['swr_registry']}/{CFG['swr_org']}/robot-ci-auth-probe-does-not-exist"
        code, out = docker_cmd("manifest", "inspect", remote, timeout=12)
        ok, snippet = _swr_probe_ok(code, out)
        with _login_lock:
            _login_ok = ok
            _login_probe_cache = (time.time(), ok)
        return ok, snippet


def mark_swr_login_invalid() -> None:
    global _login_ok, _login_probe_cache
    with _login_lock:
        _login_ok = False
        _login_probe_cache = (time.time(), False)


def run_stream(
    job_id: str,
    args: list[str],
    timeout: int = 7200,
    env: dict[str, str] | None = None,
    output_tail: list[str] | None = None,
) -> int:
    if job_cancel_requested(job_id):
        raise JobStopped()
    shown = [re.sub(r"x-access-token:[^@\s]+@", "x-access-token:***@", a) for a in args]
    append_job_log(job_id, "$ " + " ".join(shown))
    try:
        p = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            **_popen_session_kwargs(),
        )
    except FileNotFoundError:
        append_job_log(job_id, f"ERROR: not found: {args[0]}")
        return 127
    assert p.stdout is not None
    # Read stdout on a daemon thread so the main thread can enforce a real
    # wall-clock timeout even when the subprocess dies without flushing.
    read_done = threading.Event()

    def _reader() -> None:
        try:
            for line in p.stdout:
                clean = re.sub(r"x-access-token:[^@\s]+@", "x-access-token:***@", line.rstrip("\n"))
                append_job_log(job_id, clean)
                if output_tail is not None:
                    output_tail.append(clean)
                    del output_tail[:-80]
        except Exception:
            pass
        finally:
            read_done.set()

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()
    attach_job_proc(job_id, p)
    try:
        deadline = time.monotonic() + timeout
        while True:
            if job_cancel_requested(job_id):
                _kill_process_tree(p)
                reader.join(timeout=10)
                append_job_log(job_id, "ERROR: job stopped by user")
                raise JobStopped()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _kill_process_tree(p)
                reader.join(timeout=10)
                append_job_log(job_id, "ERROR: timeout")
                return 124
            reader.join(timeout=min(0.4, remaining))
            if not reader.is_alive():
                break
        if job_cancel_requested(job_id):
            append_job_log(job_id, "ERROR: job stopped by user")
            raise JobStopped()
        return p.wait() or 0
    finally:
        detach_job_proc(job_id, p)


def summarize_command_failure(lines: list[str], fallback: str) -> str:
    cleaned = [re.sub(r"\s+", " ", line).strip() for line in lines if line.strip()]
    preferred = (
        "error:", "failed", "failure", "denied", "unauthorized", "not found",
        "no such", "missing", "cannot", "could not", "exit status",
    )
    for line in reversed(cleaned):
        lower = line.lower()
        if any(token in lower for token in preferred):
            return line[-500:]
    return (cleaned[-1][-500:] if cleaned else fallback)


def _cache_branches(repo: str, names: list[str]) -> list[str]:
    unique = sorted({name.strip() for name in names if name and name.strip()})
    if unique:
        with _branch_cache_lock:
            _branch_cache[repo] = (time.time(), unique)
    return unique


def list_branches_api(repo: str, force: bool = False) -> tuple[bool, list[str] | str]:
    if not repo:
        return False, "missing repo"

    if not force:
        with _branch_cache_lock:
            cached = _branch_cache.get(repo)
        if cached and time.time() - cached[0] < 60:
            return True, list(cached[1])

    # 1) SSH ls-remote (preferred on shared server)
    if CFG.get("github_use_ssh") and (CFG.get("github_ssh_key") or ""):
        ssh_url = f"git@github.com:{repo}.git"
        code, out = run_cmd(git_args("ls-remote", "--heads", ssh_url), timeout=30, env=git_env())
        if code == 0:
            names = []
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[1].startswith("refs/heads/"):
                    names.append(parts[1][len("refs/heads/") :])
            names = _cache_branches(repo, names)
            if names:
                return True, names
            ssh_err = "git ls-remote returned no branch refs"
        else:
            ssh_err = re.sub(r"x-access-token:[^@\s]+@", "***@", out)[-500:]
    else:
        ssh_err = "ssh not configured"

    # 2) GitHub API with token
    token = gh_token()
    if token:
        try:
            names: list[str] = []
            for page in range(1, 11):
                url = f"https://api.github.com/repos/{repo}/branches?per_page=100&page={page}"
                req = Request(url)
                req.add_header("Accept", "application/vnd.github+json")
                req.add_header("Authorization", f"Bearer {token}")
                with urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                if not isinstance(data, list):
                    raise ValueError("GitHub branches response is not a list")
                names.extend(str(branch.get("name") or "") for branch in data if isinstance(branch, dict))
                if len(data) < 100:
                    break
            names = _cache_branches(repo, names)
            if names:
                return True, names
            return False, f"SSH: {ssh_err}; GitHub API returned no branches"
        except Exception as e:  # noqa: BLE001
            return False, f"SSH: {ssh_err}; GitHub API: {e}"

    return (
        False,
        "需要 GitHub SSH 密钥或 Token。"
        f" SSH: {ssh_err}",
    )


def wipe_workspace_dir(job_id: str, dest: Path, label: str = "workspace") -> None:
    """Delete a previous checkout so the next build always starts from a fresh clone."""
    if not dest.exists():
        return
    append_job_log(job_id, f"remove old {label}: {dest}")
    shutil.rmtree(dest, ignore_errors=True)
    if dest.exists():
        # Retry once — Windows/Docker sometimes briefly locks files.
        time.sleep(0.5)
        shutil.rmtree(dest, ignore_errors=True)


def sync_repo(job_id: str, svc: dict[str, Any], branch: str) -> tuple[bool, str]:
    url = (svc.get("github") or "").strip()
    if not url and svc.get("repo"):
        url = f"https://github.com/{svc['repo']}.git"
    if not url:
        return False, "missing github url"
    if not re.match(r"^[\w./\-]+$", branch or ""):
        return False, f"invalid branch: {branch!r}"

    dest = repo_dir(svc, job_id)
    public = public_github_url(url)
    public_https = bool(svc.get("public_https"))
    prefer_token_https = bool(svc.get("prefer_token_https"))
    clone_url = clone_url_for(
        url,
        public_https=public_https,
        prefer_token_https=prefer_token_https,
    )
    if (
        clone_url.startswith("https://")
        and "github.com" in clone_url
        and not gh_token()
        and not public_https
        and not prefer_token_https
    ):
        return (
            False,
            "私有仓需要配置 GitHub SSH 密钥（推荐）或 Token",
        )
    genv = git_env()
    dest.parent.mkdir(parents=True, exist_ok=True)
    append_job_log(job_id, f"github={public}")
    append_job_log(job_id, f"clone_via={'ssh' if clone_url.startswith('git@') else 'https'}")
    append_job_log(job_id, f"branch={branch}")
    append_job_log(job_id, f"workspace={dest}")

    # Harvest Fleet markers from a previous checkout before GC deletes it.
    if svc.get("id") == "multica-fleet":
        for candidate in existing_clone_dirs(svc):
            if persist_fleet_runtime_cache(job_id, candidate):
                break

    gc_idle_clone_dirs(job_id, svc)

    # Always wipe this job's checkout (including build outputs) then fresh clone.
    wipe_workspace_dir(job_id, dest, label=f"workspace {svc.get('id') or dest.name}")

    append_job_log(job_id, "git clone…")
    code = run_stream(
        job_id,
        git_args("clone", "--depth", "1", "--branch", branch, clone_url, str(dest)),
        timeout=1800,
        env=genv,
    )
    if code != 0:
        wipe_workspace_dir(job_id, dest, label="failed clone")
        code = run_stream(
            job_id,
            git_args("clone", "--depth", "1", clone_url, str(dest)),
            timeout=1800,
            env=genv,
        )
        if code != 0:
            return False, "git clone failed"
        run_stream(
            job_id,
            git_args("-C", str(dest), "fetch", "--depth", "1", "origin", branch),
            timeout=600,
            env=genv,
        )
        code = run_stream(
            job_id,
            git_args("-C", str(dest), "checkout", "-B", branch, "FETCH_HEAD"),
            timeout=120,
            env=genv,
        )
        if code != 0:
            return False, f"checkout {branch} failed"

    run_cmd(git_args("-C", str(dest), "remote", "set-url", "origin", public), timeout=30, env=genv)
    ok_contract, contract_error = validate_cloned_repo_contract(dest, svc)
    if not ok_contract:
        return False, contract_error
    if svc.get("id") == "multica-fleet":
        restore_fleet_runtime_cache(job_id, dest)
    code, head = run_cmd(git_args("-C", str(dest), "rev-parse", "--short=7", "HEAD"), timeout=30, env=genv)
    with log_substep("sha"):
        append_job_log(job_id, f"HEAD={head if code == 0 else '?'} @ {branch}")
    return True, str(dest)


def validate_cloned_repo_contract(dest: Path, svc: dict[str, Any]) -> tuple[bool, str]:
    """Require a CID contract or a legacy root/Docker build entry."""
    try:
        cid = load_cid_config(dest, str(svc.get("id") or ""))
    except CidConfigError as exc:
        return False, f"invalid .cid/build.yaml: {exc}"
    has_legacy = (dest / "deploy.sh").is_file() or (dest / "build-image.sh").is_file()
    has_package = (dest / "build" / "package" / "build.sh").is_file()
    has_dockerfile = (dest / "Dockerfile").is_file()
    if cid is None and not has_legacy and not has_package and not (
        svc.get("dockerfile_build") and has_dockerfile
    ):
        return False, "missing .cid/build.yaml or build/package/build.sh"
    return True, ""


def public_service_dir() -> Path:
    """Sibling of per-service clone dirs: <workspace_root>/public-service."""
    return Path(CFG["workspace_root"]) / "public-service"


def ensure_public_service(job_id: str) -> tuple[bool, str]:
    """
    Some legacy branches source shared helpers from the sibling public-service.
    Helper clones only the service repo, so keep a shared public-service checkout
    next to it (does not modify service source trees).

    Concurrent jobs share this checkout: update in place under a lock, never wipe
    an existing tree (wiping would break other in-flight builds).
    """
    global _public_service_ready_at
    with log_substep("env"), _public_service_lock:
        dest = public_service_dir()
        branch = (CFG.get("public_service_branch") or "main").strip() or "main"
        url = (CFG.get("public_service_github") or "https://github.com/rollingfruit/public-service.git").strip()
        public = public_github_url(url)
        clone_url = clone_url_for(url)
        genv = git_env()
        dest.parent.mkdir(parents=True, exist_ok=True)
        rrd = dest / "windows-deploy" / "lib" / "source-rrd.sh"
        now = time.time()
        append_job_log(job_id, f"ensure shared public-service → {dest} @ {branch}")

        if (dest / ".git").is_dir() and rrd.is_file() and now - _public_service_ready_at < PUBLIC_SERVICE_REUSE_SEC:
            code, head = run_cmd(git_args("-C", str(dest), "rev-parse", "--short=7", "HEAD"), timeout=30, env=genv)
            append_job_log(
                job_id,
                f"public-service recently updated; reuse HEAD={head if code == 0 else '?'} @ {branch}",
            )
            return True, str(dest)

        if (dest / ".git").is_dir():
            append_job_log(job_id, "public-service exists; fetch/update (no wipe, safe for concurrency)")
            run_cmd(git_args("-C", str(dest), "remote", "set-url", "origin", clone_url), timeout=30, env=genv)
            code = run_stream(
                job_id,
                git_args("-C", str(dest), "fetch", "--depth", "1", "origin", branch),
                timeout=600,
                env=genv,
            )
            if code != 0:
                if rrd.is_file():
                    append_job_log(job_id, "WARN git fetch public-service failed; reusing existing checkout")
                    _public_service_ready_at = now
                    return True, str(dest)
                return False, "git fetch public-service failed"
            code, old_head = run_cmd(git_args("-C", str(dest), "rev-parse", "HEAD"), timeout=30, env=genv)
            code_fh, new_head = run_cmd(git_args("-C", str(dest), "rev-parse", "FETCH_HEAD"), timeout=30, env=genv)
            if code == 0 and code_fh == 0 and (old_head or "").strip() == (new_head or "").strip():
                append_job_log(job_id, "public-service already at FETCH_HEAD; skip checkout")
            else:
                code = run_stream(
                    job_id,
                    git_args("-C", str(dest), "checkout", "-B", branch, "FETCH_HEAD"),
                    timeout=120,
                    env=genv,
                )
                if code != 0:
                    if rrd.is_file():
                        append_job_log(
                            job_id,
                            f"WARN checkout public-service {branch} failed; reusing existing checkout",
                        )
                        _public_service_ready_at = now
                        return True, str(dest)
                    return False, f"checkout public-service {branch} failed"
        else:
            append_job_log(job_id, "git clone public-service…")
            code = run_stream(
                job_id,
                git_args("clone", "--depth", "1", "--branch", branch, clone_url, str(dest)),
                timeout=1800,
                env=genv,
            )
            if code != 0:
                wipe_workspace_dir(job_id, dest, label="failed public-service clone")
                code = run_stream(
                    job_id,
                    git_args("clone", "--depth", "1", clone_url, str(dest)),
                    timeout=1800,
                    env=genv,
                )
                if code != 0:
                    return False, "git clone public-service failed"
                run_stream(
                    job_id,
                    git_args("-C", str(dest), "fetch", "--depth", "1", "origin", branch),
                    timeout=600,
                    env=genv,
                )
                code = run_stream(
                    job_id,
                    git_args("-C", str(dest), "checkout", "-B", branch, "FETCH_HEAD"),
                    timeout=120,
                    env=genv,
                )
                if code != 0:
                    return False, f"checkout public-service {branch} failed"

        run_cmd(git_args("-C", str(dest), "remote", "set-url", "origin", public), timeout=30, env=genv)
        if not rrd.is_file():
            rrd = dest / "windows-deploy" / "lib" / "source-rrd.sh"
        if not rrd.is_file():
            return False, f"missing {rrd}"
        code, head = run_cmd(git_args("-C", str(dest), "rev-parse", "--short=7", "HEAD"), timeout=30, env=genv)
        append_job_log(job_id, f"public-service HEAD={head if code == 0 else '?'} @ {branch}")
        _public_service_ready_at = time.time()
        return True, str(dest)


def find_local_image_by_git_hash(image: str, git_hash: str) -> tuple[str | None, str | None]:
    """Pick newest local/<image>:*_<git_hash> after a fresh build (tag is YYYYMMDDHHMM_<hash>)."""
    if not image or not git_hash or git_hash.startswith("0000"):
        return None, None
    code, out = docker_cmd(
        "images",
        f"local/{image}",
        "--format",
        "{{.Repository}}:{{.Tag}}",
        timeout=60,
    )
    if code != 0:
        return None, None
    suffix = f"_{git_hash}"
    matches: list[tuple[str, str]] = []
    for ln in (out or "").splitlines():
        ref = ln.strip()
        if not ref or ":" not in ref or ref.endswith(":none"):
            continue
        tag = ref.split(":", 1)[-1]
        if tag.endswith(suffix):
            matches.append((ref, tag))
    if not matches:
        return None, None
    # Prefer highest timestamp prefix so we push the image just built, not an older same-commit tag.
    matches.sort(key=lambda item: item[1], reverse=True)
    return matches[0]


def build_from_source(
    job_id: str,
    svc: dict[str, Any],
    git_hash: str,
    version: str = "",
    archive_dir: Path | None = None,
) -> tuple[bool, str]:
    src = repo_dir(svc, job_id)
    shell_src = host_path(src)
    ps_dir = host_path(public_service_dir())
    where = "WSL" if use_wsl() else "host"
    append_job_log(job_id, f"build on {where}: {src}")

    try:
        cid = load_cid_config(src, str(svc.get("id") or ""))
    except CidConfigError as exc:
        return False, f"invalid .cid/build.yaml: {exc}"
    cid_build = enabled_build_step(cid) if cid is not None else None
    if cid_build is not None:
        append_job_log(job_id, f"Loaded build stage from .cid/build.yaml: {cid_build.get('name') or cid_build['id']}")

    # Helper-only: CCE_SKIP_EXPORT=1 skips multi-hundred-MB docker save tar (we push from local image).
    extra_env = "CCE_SKIP_EXPORT=1 "

    build_ts = time.strftime("%Y%m%d%H%M")
    local_image = f"local/{svc.get('image')}:{build_ts}_{git_hash}"
    image_env_names = (
        "CID_IMAGE",
        "IMAGE",
        "IMAGE_NAME",
        "TEMPORAL_IMAGE",
        "SEMANTIC_GATEWAY_IMAGE",
        "RAG_SERVICE_IMAGE",
        "MCP_HUB_IMAGE",
        "SEMANTIC_SCHEDULE_IMAGE",
        "SEMANTIC_WORKER_IMAGE",
        "AGENTLINK_IMAGE",
        "AGENTOPS_IMAGE",
        "OPS_CONSOLE_IMAGE",
        "MULTICA_SERVER_IMAGE",
        "FLEET_IMAGE",
        "LLM_GATEWAY_IMAGE",
    )
    extra_env += " ".join(f"{name}={shlex.quote(local_image)}" for name in image_env_names) + " "

    action = str(svc.get("build_action") or "").strip()
    if svc.get("requires_version"):
        if not DAEMON_VERSION_RE.fullmatch(version):
            return False, f"invalid daemon version: {version or '(missing)'}"
        extra_env += f"DAEMON_RELEASE_VERSION={shlex.quote(version)} "
        append_job_log(job_id, f"daemon release version={version}")
    if svc.get("bundle_archive"):
        if archive_dir is None:
            return False, "archive directory is required for Fleet bundle"
        extra_env += (
            f"FLEET_OUTPUT_DIR={shlex.quote(host_path(archive_dir))} "
            f"FLEET_BUILD_TS={build_ts} FLEET_GIT_HASH={shlex.quote(git_hash)} INCLUDE_RUNTIME_IMAGES=1 "
        )
        append_job_log(job_id, "Fleet bundle: control image + OpenCode + Hermes; SWR push disabled")

    deploy_args = f" {shlex.quote(action)}" if action else ""
    svc_build_env = svc.get("build_env") if isinstance(svc.get("build_env"), dict) else {}
    build_env_exports = " ".join(
        f"{key}={shlex.quote(str(value))}" for key, value in svc_build_env.items() if key and value is not None
    )
    if build_env_exports:
        build_env_exports += " "

    image_tag = f"{time.strftime('%Y%m%d%H%M')}_{git_hash}"
    docker_args = ""
    if svc.get("dockerfile_build"):
        for key, value in svc_build_env.items():
            if key and value is not None:
                docker_args += f" --build-arg {shlex.quote(str(key))}={shlex.quote(str(value))}"
        append_job_log(job_id, f"dockerfile build local/{svc['image']}:{image_tag}")

    if cid_build is not None:
        build_invocation = str(cid_build["command"])
        build_timeout = int(cid_build.get("timeout_sec") or CFG.get("build_timeout_sec") or 7200)
    else:
        build_invocation = (
            f"if [[ -f ./build/package/build.sh ]]; then bash ./build/package/build.sh{deploy_args}; "
            f"elif [[ -f ./build/deploy/deploy.sh ]]; then bash ./build/deploy/deploy.sh{deploy_args}; "
            f"elif [[ -f ./deploy.sh ]]; then bash ./deploy.sh{deploy_args}; "
            "elif [[ -f ./build-image.sh ]]; then bash ./build-image.sh; "
            f"elif [[ -f ./Dockerfile ]]; then docker build{docker_args} "
            f"-t local/{shlex.quote(str(svc['image']))}:{shlex.quote(image_tag)} .; "
            "else echo 'ERROR: no build/package/build.sh'; exit 1; fi"
        )
        build_timeout = int(CFG.get("build_timeout_sec") or 7200)

    bash = (
        "set -euo pipefail; "
        f"cd '{shell_src}'; "
        "find . -maxdepth 5 -type f -name '*.sh' -exec sed -i 's/\\r$//' {} + 2>/dev/null || true; "
        "unset SKIP_PACKAGE MATTERMOST_FORCE_BUILD SKIP_WEBAPP_BUILD SKIP_SERVER_BUILD FORCE_WEBAPP_BUILD FORCE_REBUILD; "
        f"export {build_env_exports}CCE_UPLOAD=0 DEPLOY_NO_PAUSE=1 SKIP_IMAGE_ARCHIVE=1 EXPORT_ARCHIVE=0 "
        f"CCE_GIT_HASH='{git_hash}' PUBLIC_SERVICE_DIR='{ps_dir}' {extra_env}; "
        f"{build_invocation}"
    )
    output_tail: list[str] = []
    # Runtime credentials/configuration must never become implicit build inputs.
    # Deployment injects them later via Secrets and container environment variables.
    runtime_prefixes = (
        "GM_",
        "DEEPSEEK_",
        "MODELARTS_",
        "HUAWEI_MODELARTS_",
        "BAILIAN_",
        "DASHSCOPE_",
        "OPENAI_",
        "AWS_",
        "S3_",
    )
    runtime_exact = {
        "DATABASE_URL",
        "SQL_SERVER",
        "SQL_PASSWD",
        "MATTERMOST_SERVER",
        "MEMORY_SERVER",
        "MULTICA_SERVER",
        "SEMENTIC_GMAGENT_AUTH_TOKEN",
    }
    build_env = {
        key: value
        for key, value in os.environ.items()
        if key not in runtime_exact
        and key not in {
            "SKIP_PACKAGE",
            "MATTERMOST_FORCE_BUILD",
            "SKIP_WEBAPP_BUILD",
            "SKIP_SERVER_BUILD",
            "FORCE_WEBAPP_BUILD",
            "FORCE_REBUILD",
        }
        and not key.startswith(runtime_prefixes)
    }
    apply_ci_tmp_env(build_env)
    stream_sub = "docker" if svc.get("dockerfile_build") else "script"
    with log_substep(stream_sub):
        code = run_stream(
            job_id,
            bash_lc(bash),
            timeout=build_timeout,
            output_tail=output_tail,
            env=build_env,
        )
    if code != 0:
        with log_substep("verify"):
            append_job_log(job_id, f"ERROR build exit={code}")
        return False, summarize_command_failure(output_tail, f"build exited with code {code}")
    with log_substep("verify"):
        append_job_log(job_id, "build finished")
    return True, ""


def find_latest_tar(svc: dict[str, Any], workspace: Path | None = None) -> Path | None:
    export_dir = (workspace or repo_dir(svc)) / "runtime-images" / "cce-export"
    if not export_dir.is_dir():
        return None
    prefix = svc["tar_prefix"]
    cands = sorted(
        [*export_dir.glob(f"{prefix}*.tar"), *export_dir.glob(f"{prefix}*.tar.gz")],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return cands[0] if cands else None


def find_latest_host_package(svc: dict[str, Any], workspace: Path) -> Path | None:
    """Latest host-install tar.gz produced by services with host_package=true."""
    pattern = str(svc.get("package_pattern") or "").strip().replace("\\", "/")
    if pattern:
        cands = sorted(
            [package for package in workspace.glob(pattern) if package.is_file()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if cands:
            return cands[0]

    prefix = svc["tar_prefix"]
    export_dirs = (
        workspace / ".cid" / "output",
        workspace / "runtime-images" / "cce-export",
    )
    cands = sorted(
        [
            package
            for export_dir in export_dirs
            if export_dir.is_dir()
            for package in (
                *export_dir.glob(f"{prefix}*.tar.gz"),
                *export_dir.glob(f"{prefix}*.tar"),
            )
        ],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return cands[0] if cands else None


def finalize_host_package_artifact(
    job_id: str,
    svc: dict[str, Any],
    workspace: Path,
    archive_dir: Path | None,
    *,
    branch: str,
    commit_sha: str,
) -> dict[str, Any] | None:
    """Copy a host tar.gz into the nginx archive dir and return push_one_service fields."""
    pkg = find_latest_host_package(svc, workspace)
    if pkg is None:
        return None
    image = svc["image"]
    tag = parse_tag_from_tar(pkg, image) or pkg.stem
    created_at = time.strftime("%Y-%m-%d %H:%M:%S")
    archive_path = str(pkg)
    download_url = ""
    if archive_dir is not None:
        dest = archive_dir / pkg.name
        shutil.copy2(pkg, dest)
        try:
            archive_dir.chmod(0o755)
            dest.chmod(0o644)
        except OSError as e:
            append_job_log(job_id, f"WARN: chmod host package for nginx: {e}")
        archive_path = str(dest)
        download_url = public_archive_url(archive_path)
    return {
        "ok": True,
        "remote": "archive-only",
        "archive": archive_path,
        "tag": tag,
        "package_name": pkg.name,
        "download_url": download_url,
        "created_at": created_at,
    }


def parse_tag_from_tar(tar_path: Path, image: str) -> str:
    stem = tar_path.stem
    prefix = image + "_"
    if stem.startswith(prefix):
        return stem[len(prefix) :]
    parts = stem.split("_", 1)
    return parts[1] if len(parts) == 2 else stem


def disk_usage_ratio(path: Path | str) -> tuple[float, int, int] | None:
    """Return (df Use%, total_bytes, free_bytes), or None if unreadable.

    ``df`` Use% is (total - avail) / total. ``used / total`` ignores reserved
    blocks, so a disk that ``df`` shows as 84% can look like 79.9% and skip
    reclaim entirely.
    """
    try:
        usage = shutil.disk_usage(str(path))
    except OSError:
        return None
    if usage.total <= 0:
        return None
    return (usage.total - usage.free) / usage.total, usage.total, usage.free


def archive_timestamp_dirs(base: Path) -> list[Path]:
    return sorted(
        [
            p
            for p in base.iterdir()
            if p.is_dir() and re.fullmatch(r"\d{8,14}(?:-[0-9a-fA-F]{8,32})?", p.name)
        ],
        key=lambda p: p.name,
    )


def prune_nginx_archives(
    job_id: str,
    keep_latest: int = 3,
    min_keep: int = 1,
    max_usage_ratio: float = DISK_USAGE_PRUNE_RATIO,
) -> None:
    """Delete oldest timestamp dirs when disk usage reaches 80%.

    Prefers keeping `keep_latest` dirs. If usage is still over the ratio,
    continues until under the ratio or only `min_keep` newest dir remains.
    Removed files make matching artifact-management rows expired.
    """
    base = Path((CFG.get("archive_root") or "/usr/share/nginx/html/images").rstrip("/") or ".")
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    stats = disk_usage_ratio(base)
    if stats is None:
        return
    ratio, total, free = stats
    if ratio < max_usage_ratio:
        return

    floor = max(1, int(min_keep))
    dirs = archive_timestamp_dirs(base)
    if len(dirs) <= floor:
        append_job_log(
            job_id,
            f"WARN: disk usage={ratio:.0%} >= {max_usage_ratio:.0%} but only "
            f"{len(dirs)} archive dir(s) (min_keep={floor})",
        )
        return

    append_job_log(
        job_id,
        f"disk usage={ratio:.0%} >= {max_usage_ratio:.0%} "
        f"(free={free // (1024**2)}MB / total={total // (1024**2)}MB); "
        "pruning old nginx archives…",
    )
    removed = False
    while True:
        dirs = archive_timestamp_dirs(base)
        if len(dirs) <= floor:
            break
        stats = disk_usage_ratio(base)
        if stats is None:
            break
        ratio, total, free = stats
        if ratio < max_usage_ratio:
            break
        old = dirs[0]
        if len(dirs) <= keep_latest:
            append_job_log(
                job_id,
                f"still {ratio:.0%} used; remove extra archive {old.name} "
                f"(below keep_latest={keep_latest})",
            )
        else:
            append_job_log(job_id, f"remove old archive dir: {old}")
        shutil.rmtree(old, ignore_errors=True)
        removed = True
    if removed:
        expired = sync_artifact_availability()
        if expired:
            append_job_log(job_id, f"expired {expired} artifact record(s) after prune")
        stats = disk_usage_ratio(base)
        if stats is not None:
            ratio, total, free = stats
            append_job_log(
                job_id,
                f"disk after prune: usage={ratio:.0%} free={free // (1024**2)}MB",
            )


def iter_build_swap_files(max_depth: int = 3) -> list[Path]:
    """Find leftover compile swap files under build-cache and CI tmp."""
    found: list[Path] = []
    seen: set[str] = set()

    def walk(root: Path, depth: int) -> None:
        try:
            if not root.exists() or root.is_symlink():
                return
            if root.is_file():
                if root.name == BUILD_SWAP_NAME:
                    key = str(root)
                    if key not in seen:
                        seen.add(key)
                        found.append(root)
                return
            candidate = root / BUILD_SWAP_NAME
            if candidate.is_file() and not candidate.is_symlink():
                key = str(candidate)
                if key not in seen:
                    seen.add(key)
                    found.append(candidate)
            if depth <= 0:
                return
            for child in root.iterdir():
                if child.is_dir() and not child.is_symlink():
                    walk(child, depth - 1)
        except OSError:
            return

    for root in (build_cache_root(), ci_tmp_root()):
        walk(root, max_depth)
    return found


def drop_build_swap_file(path: Path, job_id: str) -> bool:
    """swapoff then delete a leftover build.swap. Never leave it mounted."""
    if not path.is_file():
        return False
    if os.name != "nt":
        code, out = run_cmd(["swapoff", str(path)], timeout=180)
        if code != 0 and out:
            append_job_log(job_id, f"swapoff {path}: {out.splitlines()[-1][:200]}")
    try:
        path.unlink()
    except OSError as e:
        append_job_log(job_id, f"WARN: cannot remove swap {path}: {e}")
        return False
    append_job_log(job_id, f"removed leftover swap {path}")
    return True


def reclaim_build_swap(job_id: str) -> int:
    """Compile swap is scratch only; drop it after the job so it does not occupy disk."""
    if other_job_uses_build_swap(job_id):
        append_job_log(
            job_id,
            "skipping build.swap reclaim: another mattermost job is still compiling",
        )
        return 0
    removed = 0
    for path in iter_build_swap_files():
        if drop_build_swap_file(path, job_id):
            removed += 1
    return removed


def reclaim_docker_builder_cache(job_id: str) -> None:
    """Free unused BuildKit layer cache. This is the usual 100G+ leak on the CI host."""
    append_job_log(job_id, "pruning docker builder cache")
    code, out = docker_cmd("builder", "prune", "-af", timeout=1800)
    for line in (out or "").splitlines()[-8:]:
        append_job_log(job_id, line)
    if code != 0:
        append_job_log(job_id, f"WARN: docker builder prune failed: {(out or '')[-200:]}")


def reclaim_ci_disk(
    job_id: str,
    keep_latest: int = 3,
    min_keep: int = 1,
    max_usage_ratio: float = DISK_USAGE_PRUNE_RATIO,
    keep_refs: list[str] | tuple[str, ...] = (),
) -> None:
    """CI disk reclaim used before archive and at job end.

    Always drop leftover build.swap, idle git workspaces, /home/ci scratch
    dirs, and unused BuildKit cache. Those are what filled the CI disk even
    while usage stayed under the 80% archive/image prune threshold.

    When usage is still >= 80% after that, prune nginx timestamp archives
    and unused docker images. Protected local/ai-* tags are never removed.
    """
    reclaim_build_swap(job_id)
    gc_all_idle_clone_dirs(job_id)
    reclaim_ci_tmp_leftovers(job_id)
    reclaim_docker_builder_cache(job_id)
    probe = Path((CFG.get("archive_root") or "/").rstrip("/") or "/")
    stats = disk_usage_ratio(probe)
    if stats is None:
        return
    ratio, total, free = stats
    if ratio < max_usage_ratio:
        append_job_log(
            job_id,
            f"disk after routine reclaim: usage={ratio:.0%} "
            f"free={free // (1024**2)}MB",
        )
        return
    append_job_log(
        job_id,
        f"disk usage={ratio:.0%} >= {max_usage_ratio:.0%} "
        f"(free={free // (1024**2)}MB / total={total // (1024**2)}MB); "
        "pruning archives and unused docker images…",
    )
    prune_nginx_archives(
        job_id,
        keep_latest=keep_latest,
        min_keep=min_keep,
        max_usage_ratio=max_usage_ratio,
    )
    stats = disk_usage_ratio(probe)
    if stats is not None:
        ratio, total, free = stats
        if ratio < max_usage_ratio:
            append_job_log(
                job_id,
                f"disk now {ratio:.0%} after archive prune; "
                "still reclaiming unused docker images",
            )
        else:
            append_job_log(
                job_id,
                f"disk still {ratio:.0%} after archive prune; "
                "reclaim unused docker images",
            )
    reclaim_unused_docker_images(job_id, keep_refs=keep_refs)
    stats = disk_usage_ratio(probe)
    if stats is not None:
        ratio, total, free = stats
        append_job_log(
            job_id,
            f"disk after reclaim: usage={ratio:.0%} free={free // (1024**2)}MB",
        )


def make_archive_dir(job_id: str | None = None, keep_refs: list[str] | tuple[str, ...] = ()) -> Path:
    base = (CFG.get("archive_root") or "/usr/share/nginx/html/images").rstrip("/")
    if job_id:
        reclaim_ci_disk(job_id, keep_refs=keep_refs)
    stamp = time.strftime("%Y%m%d%H%M%S")
    suffix = job_workspace_suffix(job_id)
    out_dir = Path(base) / (f"{stamp}-{suffix}" if suffix else stamp)
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        out_dir.chmod(0o755)
    except OSError:
        pass
    return out_dir


def archive_image_locally(
    job_id: str,
    local_ref: str,
    image: str,
    tag: str,
    out_dir: Path | None = None,
) -> tuple[bool, str]:
    """docker save image tar under archive dir (shared dir for batch jobs)."""
    with log_substep("save"):
        if not CFG.get("archive_enabled"):
            append_job_log(job_id, "local archive skipped (disabled)")
            return True, ""

    if out_dir is None:
        try:
            out_dir = make_archive_dir(job_id, keep_refs=(local_ref,) if local_ref else ())
        except OSError as e:
            msg = f"cannot create archive dir: {e}"
            append_job_log(job_id, f"ERROR: {msg}")
            return False, msg
    else:
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            msg = f"cannot create archive dir {out_dir}: {e}"
            append_job_log(job_id, f"ERROR: {msg}")
            return False, msg

    safe_tag = re.sub(r"[^\w.\-]+", "_", tag)
    tar_name = f"{image}_{safe_tag}.tar"
    out_file = out_dir / tar_name

    append_job_log(job_id, f"archive: docker save {local_ref} → {out_file}")

    # Save via docker host path (WSL-aware).
    code, out = docker_cmd("save", "-o", host_path(out_file), local_ref, timeout=3600)
    for line in (out or "").splitlines()[-40:]:
        append_job_log(job_id, line)
    if code != 0:
        return False, (out or "docker save failed")[-500:]

    # docker save creates 0600 files; nginx worker needs world-read to serve them.
    try:
        out_dir.chmod(0o755)
        out_file.chmod(0o644)
    except OSError as e:
        append_job_log(job_id, f"WARN: chmod archive for nginx: {e}")

    try:
        size = out_file.stat().st_size
        append_job_log(job_id, f"archive OK {out_file} ({size} bytes)")
    except OSError:
        append_job_log(job_id, f"archive OK {out_file}")
    return True, str(out_file)


def resolve_local_image(
    job_id: str,
    svc: dict[str, Any],
    workspace: Path | None = None,
) -> tuple[str | None, str | None]:
    image = svc["image"]
    tar_path = find_latest_tar(svc, workspace)
    if tar_path:
        tag = parse_tag_from_tar(tar_path, image)
        local_ref = f"local/{image}:{tag}"
        append_job_log(job_id, f"tar={tar_path.name}")
        code, _ = docker_cmd("image", "inspect", local_ref, timeout=30)
        if code != 0:
            append_job_log(job_id, "docker load…")
            code, out = docker_cmd("load", "-i", host_path(tar_path), timeout=1800)
            append_job_log(job_id, (out or "")[-1200:])
            if code != 0:
                return None, None
            m = re.search(r"Loaded image:\s*(\S+)", out or "")
            if m:
                local_ref = m.group(1)
                if ":" in local_ref:
                    tag = local_ref.split(":", 1)[-1]
        return local_ref, tag

    # Fallback: newest local/<image>:* from docker images
    append_job_log(job_id, "no tar; looking up docker images…")
    code, out = docker_cmd(
        "images",
        f"local/{image}",
        "--format",
        "{{.Repository}}:{{.Tag}}",
        timeout=60,
    )
    lines = [ln.strip() for ln in (out or "").splitlines() if ln.strip() and ":" in ln and not ln.endswith(":none")]
    dated = [ln for ln in lines if not ln.endswith(":latest")]
    pick = (dated or lines)
    if not pick:
        return None, None
    local_ref = pick[0]
    tag = local_ref.split(":", 1)[-1]
    append_job_log(job_id, f"using image {local_ref}")
    return local_ref, tag


def ensure_swr_login(job_id: str, login_command: str = "") -> bool:
    """Shared-server SWR login; returns False and sets job failed on error."""
    with log_substep("env"):
        cmd = (login_command or "").strip()
        if cmd:
            append_job_log(job_id, "SWR login from page credentials…")
            ok_login, out_login = do_login(cmd)
            append_job_log(job_id, (out_login or "")[-800:])
            if not ok_login:
                set_job(job_id, status="failed", error="SWR login failed")
                append_job_log(job_id, "ERROR: paste a valid Huawei SWR temporary login command")
                return False
            ok_probe, probe_detail = check_login_detail(force=True)
            if not ok_probe:
                set_job(job_id, status="failed", error="SWR login verification failed")
                append_job_log(
                    job_id,
                    "ERROR: SWR login command succeeded locally but registry verification failed: "
                    + probe_detail,
                )
                return False
            return True

        append_job_log(job_id, "reusing shared SWR login on this server…")
        ok_probe, probe_detail = check_login_detail(force=False)
        if ok_probe:
            append_job_log(job_id, "shared SWR login still valid")
            return True
        if docker_config_has_swr_auth():
            append_job_log(
                job_id,
                "WARN SWR registry probe failed; continuing with local docker auth: "
                + probe_detail,
            )
            return True
        set_job(job_id, status="failed", error="not logged in to SWR")
        append_job_log(
            job_id,
            "ERROR: 服务器上尚无有效 SWR 登录（或已过期）。请任一人在页面粘贴 docker login 并登录后再推送。"
            f" 校验: {probe_detail}",
        )
        return False


def push_one_service(
    job_id: str,
    svc: dict[str, Any],
    branch: str,
    archive_dir: Path | None,
    version: str = "",
) -> dict[str, Any]:
    """Build/push/archive one service. Does not set final job status."""
    service_id = svc["id"]
    branch = (branch or svc.get("default_branch") or "main").strip()
    image = svc["image"]
    registry, org = CFG["swr_registry"], CFG["swr_org"]
    result: dict[str, Any] = {
        "service_id": service_id,
        "title": svc.get("title") or service_id,
        "branch": branch,
        "ok": False,
        "remote": "",
        "archive": "",
        "error": "",
        "test_status": None,
        "test_summary": None,
        "version": version,
        "error_code": "",
        "image": image,
        "tag": "",
        "commit_sha": "",
        "created_at": "",
        "package_name": "",
        "download_url": "",
        "job_id": job_id,
    }
    append_job_log(job_id, f"service={svc['title']} image={image}")
    if svc.get("archive_only"):
        append_job_log(job_id, "target=local Fleet deployment bundle (SWR push skipped)")
    else:
        append_job_log(job_id, f"target={registry}/{org}/{image}:*")

    # Keep checkout after the job so failed builds can be inspected on disk.
    # The next build for this service wipes it in sync_repo() before re-cloning.
    ok, detail = sync_repo(job_id, svc, branch)
    if not ok:
        result["error"] = detail
        append_job_log(job_id, f"ERROR {detail}")
        return result

    try:
        cid = load_cid_config(Path(detail), service_id)
    except CidConfigError as exc:
        result["error"] = f"invalid .cid/build.yaml: {exc}"
        append_job_log(job_id, f"ERROR {result['error']}")
        return result
    if cid is not None:
        svc = dict(svc)
        declared_image = str(cid["service"]["image"])
        svc["image"] = declared_image
        image = declared_image
        result["image"] = declared_image
        image_artifact = (cid.get("artifacts") or {}).get("image") or {}
        delivery = str(image_artifact.get("delivery") or "swr")
        image_enabled = image_artifact.get("enabled") is True
        svc["archive_only"] = delivery == "archive-only"
        svc["archive_image"] = image_enabled and image_artifact.get("archive") is True
        package_artifact = (cid.get("artifacts") or {}).get("package") or {}
        package_enabled = package_artifact.get("enabled") is True
        if package_enabled:
            pattern = str(package_artifact.get("pattern") or "")
            svc["package_pattern"] = pattern
            svc["bundle_archive"] = "multica-fleet_bundle_" in pattern
            if not image_enabled:
                svc["host_package"] = True
        append_job_log(job_id, f"CID contract active image={declared_image} delivery={delivery}")
        test_kinds = [
            str(step.get("test_type") or "")
            for step in (cid.get("scripts") or [])
            if step.get("type") == "test"
            and step.get("enabled") is True
            and step.get("test_type") in {"ut", "dt"}
        ]
        set_job(job_id, test_kinds=test_kinds)

    git = git_bin()
    code, head = run_cmd([git, "-C", detail, "rev-parse", "HEAD"], timeout=30)
    commit_sha = head if code == 0 else "0000000000000000000000000000000000000000"
    git_hash = commit_sha[:7]
    result["commit_sha"] = commit_sha

    if job_cancel_requested(job_id):
        raise JobStopped()
    set_job(job_id, stage="testing", current=f"{service_id}@{branch}", commit_sha=commit_sha)
    test_result = run_tests_nonblocking(job_id, service_id, Path(detail), commit_sha)
    test_summary = {
        key: test_result.get(key, 0)
        for key in ("total", "passed", "failed", "errors", "skipped", "duration_ms")
    }
    safe_service_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", service_id).strip("._") or "service"
    test_run = {
        "service_id": service_id,
        "title": svc.get("title") or service_id,
        "branch": branch,
        "commit_sha": commit_sha,
        "status": test_result.get("status"),
        "summary": test_summary,
        "commands": test_result.get("commands") or [],
        "failures": test_result.get("failures") or [],
        "test_cases": test_result.get("test_cases") or [],
        "test_report": str(LOG_DIR / f"job-{job_id}-{safe_service_id}-test.json"),
    }
    record_test_run(job_id, test_run)
    result["test_status"] = test_run["status"]
    result["test_summary"] = test_summary
    if tests_block_build(test_result):
        result["error"] = f"tests failed under blocking policy: {test_result.get('status')}"
        append_job_log(job_id, f"FAILED service={service_id} stage=testing reason={result['error']}")
        return result
    if job_cancel_requested(job_id):
        raise JobStopped()
    set_job(job_id, stage="building")

    # Always rebuild; never reuse a previous local image for the same git hash.
    built, build_error = build_from_source(job_id, svc, git_hash, version, archive_dir)
    if not built:
        result["error"] = build_error or "build failed"
        append_job_log(job_id, f"FAILED service={service_id} stage=building reason={result['error']}")
        return result

    if svc.get("id") == "multica-fleet":
        persist_fleet_runtime_cache(job_id, Path(detail))

    if svc.get("host_package"):
        if archive_dir is None:
            result["error"] = "archive directory was not created"
            append_job_log(job_id, f"FAILED service={service_id} stage=archiving reason={result['error']}")
            return result
        host_result = finalize_host_package_artifact(
            job_id,
            svc,
            Path(detail),
            archive_dir,
            branch=branch,
            commit_sha=commit_sha,
        )
        if host_result is None:
            result["error"] = "host package tar.gz was not generated"
            append_job_log(job_id, f"FAILED service={service_id} stage=archiving reason={result['error']}")
            return result
        result.update(host_result)
        if job_cancel_requested(job_id):
            raise JobStopped()
        record_build_artifact(
            {
                "created_at": result["created_at"],
                "job_id": job_id,
                "operator": _job_operator_name(job_id),
                "service_id": service_id,
                "title": result["title"],
                "branch": branch,
                "commit_sha": commit_sha,
                "image": image,
                "tag": result["tag"],
                "image_ref": f"host-package:{image}:{result['tag']}",
                "remote": "archive-only",
                "package_name": result["package_name"],
                "archive": result["archive"],
                "download_url": result["download_url"],
            }
        )
        append_job_log(job_id, f"OK host package {result['archive']} (SWR push skipped)")
        return result

    if svc.get("bundle_archive"):
        if archive_dir is None:
            result["error"] = "Fleet archive directory was not created"
            return result
        bundles = sorted(
            archive_dir.glob("multica-fleet_bundle_*.tar"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        bundle = bundles[0] if bundles else None
        required = (
            archive_dir / "multica-fleet.env",
            archive_dir / "deploy-multica-fleet.sh",
        )
        if bundle is None or any(not path.is_file() for path in required):
            result["error"] = "Fleet bundle or deployment files were not generated"
            append_job_log(job_id, f"FAILED service={service_id} stage=archiving reason={result['error']}")
            return result
        if not finalize_fleet_bundle_artifacts(job_id, bundle):
            result["error"] = "Fleet archive permissions could not be finalized"
            append_job_log(job_id, f"FAILED service={service_id} stage=archiving reason={result['error']}")
            return result
        result["ok"] = True
        result["remote"] = "archive-only"
        result["archive"] = str(bundle)
        result["tag"] = parse_tag_from_tar(bundle, image) or bundle.name
        result["package_name"] = bundle.name
        result["download_url"] = public_archive_url(str(bundle))
        result["created_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        if job_cancel_requested(job_id):
            raise JobStopped()
        record_build_artifact(
            {
                "created_at": result["created_at"],
                "job_id": job_id,
                "operator": _job_operator_name(job_id),
                "service_id": service_id,
                "title": result["title"],
                "branch": branch,
                "commit_sha": commit_sha,
                "image": image,
                "tag": result["tag"],
                "image_ref": f"local/{image}:{result['tag']}",
                "remote": "archive-only",
                "package_name": result["package_name"],
                "archive": result["archive"],
                "download_url": result["download_url"],
            }
        )
        append_job_log(job_id, f"OK archive-only {bundle} (SWR push skipped)")
        append_job_log(job_id, f"OK deployment env {required[0]}")
        append_job_log(job_id, f"OK deployment script {required[1]}")
        return result
    local_ref, tag = resolve_local_image(job_id, svc, Path(detail))
    by_hash_ref, by_hash_tag = find_local_image_by_git_hash(image, git_hash)
    if by_hash_ref and by_hash_tag:
        local_ref, tag = by_hash_ref, by_hash_tag
    if not local_ref or not tag:
        result["error"] = "no image/tar after build"
        return result

    if job_cancel_requested(job_id):
        raise JobStopped()
    set_job(job_id, stage="pushing")
    remote = f"{registry}/{org}/{image}:{tag}"
    with log_substep("tag"):
        append_job_log(job_id, f"docker tag {local_ref} -> {remote}")
        code, out = docker_cmd("tag", local_ref, remote, timeout=60)
        if code != 0:
            append_job_log(job_id, out)
            result["error"] = "docker tag failed"
            return result

    push_attempts = 4
    code, out = 1, ""
    with log_substep("push"):
        for attempt in range(1, push_attempts + 1):
            append_job_log(job_id, f"docker push {remote} (try {attempt}/{push_attempts})")
            code, out = docker_cmd("push", remote, timeout=3600)
            for line in (out or "").splitlines()[-40:]:
                append_job_log(job_id, line)
            if code == 0:
                break
            text = (out or "").lower()
            retryable = any(
                x in text
                for x in (
                    "timeout",
                    "temporarily unavailable",
                    "connection reset",
                    "connection refused",
                    "tls handshake",
                    "i/o timeout",
                    "network is unreachable",
                    "request canceled",
                )
            )
            if not retryable or attempt >= push_attempts:
                break
            wait_s = min(30, 5 * attempt)
            append_job_log(job_id, f"push network error; retry in {wait_s}s…")
            for _ in range(wait_s):
                if job_cancel_requested(job_id):
                    raise JobStopped()
                time.sleep(1)
    if code != 0:
        result["remote"] = remote
        result["error"] = summarize_command_failure(
            (out or "").splitlines(),
            "docker push failed",
        )
        auth_failed = any(
            token in (out or "").lower()
            for token in ("authenticate", "authentication required", "unauthorized", "denied")
        )
        if auth_failed:
            result["error_code"] = "swr_auth_failed"
            mark_swr_login_invalid()
            append_job_log(
                job_id,
                "ERROR: SWR 鉴权失败。请重新复制华为云临时登录指令到页面后再推送；并确认组织 public_ai 有推送权限",
            )
        else:
            append_job_log(
                job_id,
                "ERROR: docker push failed（多为到 SWR 的网络超时）。镜像已在服务器构建完成，可重新登录 SWR 后再点一次推送。",
            )
        docker_cmd("rmi", remote, timeout=60)
        return result

    # Free space before large docker save when disk is tight.
    # Pin the just-built tags so prune cannot delete them before docker save.
    if job_cancel_requested(job_id):
        raise JobStopped()
    with log_substep("verify"):
        append_job_log(job_id, f"OK {remote}")
    set_job(job_id, stage="archiving")
    reclaim_ci_disk(job_id, keep_refs=(local_ref, remote))
    if svc.get("archive_image", True):
        ok_arc, arc_path = archive_image_locally(job_id, local_ref, image, tag, out_dir=archive_dir)
    else:
        append_job_log(job_id, "local image archive skipped by .cid/build.yaml")
        ok_arc, arc_path = True, ""
    if not ok_arc:
        result["remote"] = remote
        if CFG.get("archive_required"):
            result["error"] = "local archive failed"
            append_job_log(
                job_id,
                "ERROR: SWR 已推送成功，但本地归档镜像包失败："
                f"{arc_path}。目录：{CFG.get('archive_root')}",
            )
            docker_cmd("rmi", remote, timeout=60)
            return result
        append_job_log(job_id, f"WARN: local archive failed (ignored): {arc_path}")

    remove_business_images(job_id, remote, local_ref)
    result["ok"] = True
    result["remote"] = remote
    result["archive"] = arc_path or ""
    result["tag"] = tag or ""
    result["package_name"] = Path(arc_path).name if arc_path else ""
    result["download_url"] = public_archive_url(arc_path or "")
    result["created_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    if job_cancel_requested(job_id):
        raise JobStopped()
    record_build_artifact(
        {
            "created_at": result["created_at"],
            "job_id": job_id,
            "operator": _job_operator_name(job_id),
            "service_id": service_id,
            "title": result["title"],
            "branch": branch,
            "commit_sha": commit_sha,
            "image": image,
            "tag": result["tag"],
            "image_ref": f"local/{image}:{result['tag']}" if result["tag"] else f"local/{image}",
            "remote": remote,
            "package_name": result["package_name"],
            "archive": result["archive"],
            "download_url": result["download_url"],
        }
    )
    if arc_path:
        append_job_log(job_id, f"OK archive {arc_path}")
    return result


def run_push_job(
    job_id: str,
    items: list[dict[str, str]],
    login_command: str = "",
) -> None:
    """Push one or more services; batch jobs share one archive timestamp directory."""
    _job_ctx.job_id = job_id
    try:
        catalog = {s["id"]: s for s in load_services()}
        resolved: list[tuple[dict[str, Any], str, str]] = []
        for item in items:
            sid = (item.get("service_id") or "").strip()
            svc = catalog.get(sid)
            if not svc:
                set_job(job_id, status="failed", error=f"unknown service: {sid or '?'}")
                append_job_log(job_id, f"ERROR unknown service: {sid or '?'}")
                return
            br = (item.get("branch") or svc.get("default_branch") or "main").strip()
            version = (item.get("version") or "").strip()
            resolved.append((svc, br, version))

        if not resolved:
            set_job(job_id, status="failed", error="no services selected")
            return

        labels = ", ".join(
            f"{svc['id']}@{br}" + (f"[{version}]" if version else "")
            for svc, br, version in resolved
        )
        append_job_log(job_id, f"batch size={len(resolved)}: {labels}")
        set_job(
            job_id,
            service_id=",".join(svc["id"] for svc, _, _ in resolved),
            branch=",".join(br for _, br, _ in resolved),
        )

        with log_substep("env"):
            append_job_log(job_id, "environment check: docker/workspace ready")
        if not wait_for_build_slot(job_id):
            raise JobStopped()

        needs_swr = any(not svc.get("archive_only") for svc, _, _ in resolved)
        if needs_swr and not ensure_swr_login(job_id, login_command):
            return
        if not needs_swr:
            append_job_log(job_id, "SWR login skipped: all selected services are archive-only")

        if any(not svc.get("skip_public_service") for svc, _, _ in resolved):
            ok_ps, detail_ps = ensure_public_service(job_id)
            if not ok_ps:
                set_job(job_id, status="failed", error=detail_ps)
                append_job_log(job_id, f"ERROR {detail_ps}")
                return
        else:
            append_job_log(job_id, "shared public-service skipped: not required by selected services")

        archive_dir: Path | None = None
        bundle_required = any(svc.get("bundle_archive") for svc, _, _ in resolved)
        needs_archive_dir = bool(CFG.get("archive_enabled")) or bundle_required
        if needs_archive_dir:
            try:
                with log_substep("adir"):
                    archive_dir = make_archive_dir(job_id)
                    append_job_log(job_id, f"shared archive dir={archive_dir}")
                set_job(job_id, archive_dir=str(archive_dir))
            except OSError as e:
                if CFG.get("archive_required") or bundle_required:
                    set_job(job_id, status="failed", error=f"cannot create archive dir: {e}")
                    with log_substep("adir"):
                        append_job_log(job_id, f"ERROR cannot create archive dir: {e}")
                    return
                with log_substep("adir"):
                    append_job_log(job_id, f"WARN: cannot create archive dir: {e}")

        results: list[dict[str, Any]] = []
        total = len(resolved)
        for idx, (svc, br, version) in enumerate(resolved, 1):
            if job_cancel_requested(job_id):
                raise JobStopped()
            append_job_log(job_id, f"===== [{idx}/{total}] {svc['id']} @ {br} =====")
            set_job(job_id, stage="syncing", current=f"{svc['id']}@{br}", progress=f"{idx}/{total}")
            result = push_one_service(job_id, svc, br, archive_dir, version)
            results.append(result)
            set_job(job_id, results=results)
            if result.get("error_code") == "swr_auth_failed":
                append_job_log(job_id, "SWR authentication failed; stopping remaining SWR operations")
                for pending_svc, pending_br, pending_version in resolved[idx:]:
                    pending = {
                        "service_id": pending_svc["id"],
                        "title": pending_svc.get("title") or pending_svc["id"],
                        "branch": pending_br,
                        "version": pending_version,
                        "ok": False,
                        "remote": "",
                        "archive": "",
                        "error": "not attempted: SWR authentication failed",
                        "error_code": "swr_auth_failed",
                        "test_status": None,
                        "test_summary": None,
                    }
                    results.append(pending)
                    append_job_log(
                        job_id,
                        f"FAILED service={pending_svc['id']} reason={pending['error']}",
                    )
                set_job(job_id, results=results)
                break

        if job_cancel_requested(job_id):
            raise JobStopped()
        ok_n = sum(1 for r in results if r.get("ok"))
        fail_n = total - ok_n
        remotes = [r["remote"] for r in results if r.get("remote")]
        archives = [r["archive"] for r in results if r.get("archive")]
        archive_summary = str(archive_dir) if archive_dir else (archives[0] if archives else "")
        if fail_n == 0:
            set_job(
                job_id,
                status="ok",
                stage="done",
                current="",
                remote="; ".join(remotes),
                archive=archive_summary,
                results=results,
            )
            append_job_log(job_id, f"BATCH OK {ok_n}/{total}")
            if archive_dir:
                append_job_log(job_id, f"BATCH archive dir {archive_dir}")
        else:
            errs = "; ".join(
                f"{r['service_id']}:{r.get('error') or 'failed'}" for r in results if not r.get("ok")
            )
            set_job(
                job_id,
                status="failed",
                stage="done",
                current="",
                error=f"{fail_n}/{total} failed: {errs}",
                remote="; ".join(remotes),
                archive=archive_summary,
                results=results,
            )
            for failed in (r for r in results if not r.get("ok")):
                append_job_log(
                    job_id,
                    f"FAILED service={failed['service_id']} reason={failed.get('error') or 'failed'}",
                )
            append_job_log(job_id, f"BATCH DONE with failures ok={ok_n} fail={fail_n} errors=[{errs}]")
            if archive_dir:
                append_job_log(job_id, f"BATCH archive dir {archive_dir} (partial ok kept)")
    except JobStopped:
        mark_job_stopped(job_id)
    except Exception as e:  # noqa: BLE001
        if job_cancel_requested(job_id):
            mark_job_stopped(job_id)
        else:
            set_job(job_id, status="failed", error=str(e))
            append_job_log(job_id, f"ERROR {e}")
    finally:
        release_build_slot(job_id)
        _job_ctx.job_id = None
        reclaim_ci_disk(job_id)


def push_service(
    job_id: str,
    service_id: str,
    branch: str,
    login_command: str = "",
) -> None:
    """Back-compat wrapper for a single service push."""
    run_push_job(
        job_id,
        [{"service_id": service_id, "branch": branch}],
        login_command=login_command,
    )


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def log_message(self, fmt: str, *args: Any) -> None:
        if str(args[0]).startswith(("GET /api/", "POST /api/")):
            return
        super().log_message(fmt, *args)

    def end_headers(self) -> None:
        """Prevent browsers from retaining stale HTML, CSS, and JavaScript releases."""
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _json(self, code: int, payload: Any, extra_headers: dict[str, Any] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if extra_headers:
            for key, value in extra_headers.items():
                if isinstance(value, (list, tuple)):
                    for item in value:
                        self.send_header(key, str(item))
                else:
                    self.send_header(key, str(value))
        self.end_headers()
        self.wfile.write(body)

    def _session_token(self) -> str:
        raw = self.headers.get("Cookie") or ""
        cookies = SimpleCookie()
        try:
            cookies.load(raw)
        except Exception:
            return ""
        for name in (session_cookie_name(), SESSION_COOKIE):
            morsel = cookies.get(name)
            if morsel and morsel.value:
                return morsel.value
        return ""

    def _current_user(self) -> str:
        return session_username(self._session_token())

    def _session_cookie_header(self, token: str, *, clear: bool = False, name: str = "") -> str:
        cookie = name or session_cookie_name()
        if clear:
            return f"{cookie}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
        return (
            f"{cookie}={token}; Path=/; HttpOnly; SameSite=Lax; "
            f"Max-Age={SESSION_MAX_AGE_SEC}"
        )

    def _session_cookie_headers(self, token: str, *, clear: bool = False) -> list[str]:
        headers = [self._session_cookie_header(token, clear=clear)]
        # Stop sharing the default cookie with other ports on the same host.
        if session_cookie_name() != SESSION_COOKIE:
            headers.append(self._session_cookie_header("", clear=True, name=SESSION_COOKIE))
        return headers

    def _require_api_user(self, path: str, method: str) -> str | None:
        if not path.startswith("/api/"):
            return ""
        if method == "GET" and path in AUTH_PUBLIC_GET:
            return self._current_user()
        if method == "POST" and path in AUTH_PUBLIC_POST:
            return self._current_user()
        user = self._current_user()
        if user:
            return user
        self._json(401, {"error": "请先登录", "error_code": "auth_required"})
        return None

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return {}

    def do_GET(self) -> None:  # noqa: N802
        try:
            self._do_GET()
        except Exception as e:  # noqa: BLE001
            try:
                self._json(500, {"error": str(e)})
            except Exception:
                pass

    def _do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        user = self._require_api_user(path, "GET")
        if user is None:
            return
        if path == "/api/auth/me":
            self._json(200, {"user": user or None, "username": user or None})
            return
        if path == "/api/login/status":
            # Instant: no docker info / no SWR network round-trip.
            self._json(
                200,
                {
                    "ok": True,
                    "login_cached": login_status_fast(),
                    "registry": CFG["swr_registry"],
                    "org": CFG["swr_org"],
                },
            )
            return

        if path == "/api/cce/regions":
            regions = [{"id": rid, **meta} for rid, meta in huawei_cce.REGIONS.items()]
            self._json(200, {"regions": regions})
            return

        if path == "/api/cce/workload-kinds":
            kinds = [{"id": kid, **meta} for kid, meta in huawei_cce.WORKLOAD_KINDS.items()]
            self._json(200, {"kinds": kinds})
            return

        if path == "/api/health":
            schedule_docker_probe()
            logged = login_status_fast()
            self._json(
                200,
                {
                    "ok": True,
                    "agent": "swr-push-helper",
                    "mode": "local-github",
                    "docker": docker_status_cached(),
                    "login_cached": logged,
                    "registry": CFG["swr_registry"],
                    "org": CFG["swr_org"],
                    "workspace_root": CFG["workspace_root"],
                    "helper_root": CFG["helper_root"],
                    "github_token_configured": bool((CFG.get("github_token") or "").strip()),
                    "github_ssh_configured": bool(
                        CFG.get("github_use_ssh")
                        and (CFG.get("github_ssh_key") or "")
                        and Path(CFG.get("github_ssh_key") or "").is_file()
                    ),
                    "github_use_ssh": bool(CFG.get("github_use_ssh")),
                    "allow_remote": bool(CFG.get("allow_remote")),
                    "archive_enabled": bool(CFG.get("archive_enabled")),
                    "archive_root": (CFG.get("archive_root") or "").strip(),
                    "max_concurrent_jobs": max_concurrent_jobs(),
                },
            )
            return

        if path == "/api/services":
            items = []
            for svc in load_services():
                items.append(
                    {
                        "id": svc["id"],
                        "title": svc["title"],
                        "image": svc.get("image"),
                        "repo": repo_full_name(svc),
                        "github": public_github_url(svc.get("github") or (f"https://github.com/{repo_full_name(svc)}.git")),
                        "default_branch": svc.get("default_branch") or "main",
                        "cloned": (repo_dir(svc) / ".git").is_dir(),
                        "requires_version": bool(svc.get("requires_version")),
                        "version_label": svc.get("version_label") or "Version",
                        "version_example": svc.get("version_example") or "v1.2.3",
                        "archive_only": bool(svc.get("archive_only")),
                        "last_version": load_last_daemon_version() if svc.get("requires_version") else "",
                    }
                )
            self._json(200, {"services": items, "registry": CFG["swr_registry"], "org": CFG["swr_org"]})
            return

        if path == "/api/artifacts":
            def _query_int(name: str, default: int) -> int:
                try:
                    return int((query.get(name) or [str(default)])[0])
                except (TypeError, ValueError):
                    return default

            page = _query_int("page", 1)
            if "page_size" in query:
                page_size = _query_int("page_size", ARTIFACTS_DEFAULT_PAGE_SIZE)
            else:
                page_size = _query_int("limit", ARTIFACTS_DEFAULT_PAGE_SIZE)
            service_id = str((query.get("service_id") or [""])[0]).strip()
            payload = list_build_artifacts(page=page, page_size=page_size, service_id=service_id)
            payload["archive_root"] = (CFG.get("archive_root") or "").strip()
            payload["download_via"] = "helper"
            self._json(200, payload)
            return

        if path == "/api/artifacts/download":
            rel = (query.get("file") or [""])[0]
            target = resolve_archive_file(rel)
            if target is None:
                self._json(404, {"error": "archive not found"})
                return
            try:
                size = target.stat().st_size
            except OSError:
                self._json(404, {"error": "archive not readable"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{target.name}"',
            )
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                with target.open("rb") as fh:
                    shutil.copyfileobj(fh, self.wfile, length=1024 * 1024)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            return

        m = re.fullmatch(r"/api/services/([^/]+)/branches", path)
        if m:
            svc = next((s for s in load_services() if s["id"] == m.group(1)), None)
            if not svc:
                self._json(404, {"error": "unknown service"})
                return
            repo = repo_full_name(svc)
            force_refresh = (query.get("refresh") or [""])[0].lower() in ("1", "true", "yes")
            ok, result = list_branches_api(repo, force=force_refresh)
            default = svc.get("default_branch") or "main"
            if not ok:
                self._json(
                    503,
                    {
                        "error": "branch lookup failed",
                        "detail": str(result),
                        "default_branch": default,
                    },
                )
                return
            branches = list(result)
            if default in branches:
                branches.remove(default)
                branches.insert(0, default)
            preferred = get_run_template(user or "", str(svc.get("id") or ""))
            selected = resolve_template_branch(user or "", str(svc.get("id") or ""), branches, default)
            self._json(
                200,
                {
                    "branches": branches,
                    "default_branch": default,
                    "preferred_branch": preferred or None,
                    "selected_branch": selected,
                },
            )
            return

        if path == "/api/run-templates":
            service_id = str((query.get("service_id") or [""])[0]).strip()
            if not service_id:
                self._json(400, {"error": "缺少微服务"})
                return
            preferred = get_run_template(user or "", service_id)
            self._json(200, {"service_id": service_id, "branch": preferred or None})
            return

        if path == "/api/jobs":
            def _query_int(name: str, default: int) -> int:
                try:
                    return int((query.get(name) or [str(default)])[0])
                except (TypeError, ValueError):
                    return default

            client_id = _normalize_client_id((query.get("client_id") or [""])[0])
            service_id = str((query.get("service_id") or [""])[0]).strip()
            page = _query_int("page", 1)
            if "page_size" in query:
                page_size = _query_int("page_size", HISTORY_DEFAULT_PAGE_SIZE)
            else:
                page_size = _query_int("limit", HISTORY_DEFAULT_PAGE_SIZE)
            self._json(
                200,
                list_build_history(
                    client_id=client_id,
                    service_id=service_id,
                    page=page,
                    page_size=page_size,
                ),
            )
            return

        if path.startswith("/api/jobs/"):
            job_id = path[len("/api/jobs/") :].strip("/")
            compact = (query.get("compact") or [""])[0].lower() in ("1", "true", "yes")
            view_ui = (query.get("view") or [""])[0].lower() == "ui"
            try:
                log_after = max(0, int((query.get("log_after") or ["0"])[0]))
            except ValueError:
                log_after = 0
            try:
                test_revision = int((query.get("test_revision") or ["-1"])[0])
            except ValueError:
                test_revision = -1
            step = str((query.get("step") or [""])[0]).strip()
            sub = str((query.get("sub") or [""])[0]).strip()
            payload = None
            with _jobs_lock:
                job = _jobs.get(job_id)
                if job:
                    payload = snapshot_job_for_payload(
                        job,
                        compact=compact,
                        view_ui=view_ui,
                        step=step,
                    )
            if payload is not None:
                self._json(
                    200,
                    job_payload(
                        payload,
                        compact=compact,
                        view_ui=view_ui,
                        log_after=log_after,
                        test_revision=test_revision,
                        step=step,
                        sub=sub,
                    ),
                )
                return
            disk = load_job_from_disk(job_id)
            if disk:
                self._json(
                    200,
                    job_payload(
                        disk,
                        compact=compact,
                        view_ui=view_ui,
                        log_after=log_after,
                        test_revision=test_revision,
                        step=step,
                        sub=sub,
                    ) if compact or view_ui or step else disk,
                )
                return
            self._json(404, {"error": "job not found"})
            return

        # Active-job discovery. Prefer client_id so each browser only sees its jobs.
        if path == "/api/running-job":
            client_id = _normalize_client_id((query.get("client_id") or [""])[0])
            service_id = str((query.get("service_id") or [""])[0]).strip()
            payload = active_job_summary(client_id=client_id, service_id=service_id)
            if payload:
                self._json(200, payload)
                return
            self._json(
                200,
                {
                    "id": None,
                    "status": "idle",
                    "client_id": client_id or None,
                    "service_id": service_id or None,
                },
            )
            return

        if path == "/api/running-jobs":
            client_id = _normalize_client_id((query.get("client_id") or [""])[0])
            service_id = str((query.get("service_id") or [""])[0]).strip()
            jobs = list_running_job_summaries(client_id=client_id, service_id=service_id)
            self._json(
                200,
                {
                    "jobs": jobs,
                    "client_id": client_id or None,
                    "service_id": service_id or None,
                    "max_concurrent_jobs": max_concurrent_jobs(),
                },
            )
            return

        if path in ("/", "/index.html"):
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        try:
            self._do_POST()
        except Exception as e:  # noqa: BLE001
            try:
                self._json(500, {"error": str(e)})
            except Exception:
                pass

    def _do_POST(self) -> None:
        path = urlparse(self.path).path
        data = self._read_json()
        user = self._require_api_user(path, "POST")
        if user is None:
            return

        if path == "/api/auth/login":
            username = authenticate_user(str(data.get("username") or ""), str(data.get("password") or ""))
            if not username:
                self._json(401, {"ok": False, "error": "账号或密码错误", "error_code": "auth_failed"})
                return
            token = create_session(username)
            self._json(
                200,
                {"ok": True, "username": username, "user": username},
                extra_headers={"Set-Cookie": self._session_cookie_headers(token)},
            )
            return

        if path == "/api/auth/logout":
            destroy_session(self._session_token())
            self._json(
                200,
                {"ok": True},
                extra_headers={"Set-Cookie": self._session_cookie_headers("", clear=True)},
            )
            return

        if path == "/api/auth/password":
            err = change_user_password(
                user,
                str(data.get("old_password") or data.get("current_password") or ""),
                str(data.get("new_password") or ""),
            )
            if err:
                self._json(400, {"ok": False, "error": err})
                return
            self._json(200, {"ok": True})
            return

        if path == "/api/run-templates":
            service_id = str(data.get("service_id") or "").strip()
            branch = str(data.get("branch") or "").strip()
            catalog = {str(item.get("id")): item for item in load_services()}
            if service_id not in catalog:
                self._json(404, {"ok": False, "error": "未知微服务"})
                return
            err = save_run_template(user, service_id, branch)
            if err:
                self._json(400, {"ok": False, "error": err})
                return
            self._json(200, {"ok": True, "service_id": service_id, "branch": branch})
            return

        if path == "/api/login":
            ok, out = do_login((data.get("command") or "").strip())
            self._json(200 if ok else 400, {"ok": ok, "output": (out or "")[-2000:]})
            return

        if path == "/api/login/check":
            self._json(200, {"ok": check_login(force=True)})
            return

        if path == "/api/login/logout":
            ok, out = do_logout()
            self._json(200 if ok else 400, {"ok": ok, "output": (out or "")[-2000:]})
            return

        if path == "/api/config/token":
            save_config_value("github_token", (data.get("github_token") or "").strip())
            reload_cfg()
            self._json(200, {"ok": True, "github_token_configured": bool(CFG.get("github_token"))})
            return

        if path == "/api/cce/projects":
            try:
                creds = resolve_huawei_credentials(data)
                projects = huawei_cce.list_projects(
                    creds["access_key"],
                    creds["secret_key"],
                    region=creds["region"],
                )
                self._json(200, {"region": creds["region"], "projects": projects})
            except Exception as exc:  # noqa: BLE001
                if not json_huawei_cce_error(self, exc):
                    raise
            return

        if path == "/api/cce/clusters":
            try:
                creds = resolve_huawei_credentials(data)
                payload = huawei_cce.list_clusters(
                    creds["access_key"],
                    creds["secret_key"],
                    region=creds["region"],
                    project_id=creds["project_id"],
                )
                self._json(200, payload)
            except Exception as exc:  # noqa: BLE001
                if not json_huawei_cce_error(self, exc):
                    raise
            return

        if path == "/api/cce/workloads":
            try:
                creds = resolve_huawei_credentials(data)
                cluster_id = str(data.get("cluster_id") or "").strip()
                if not cluster_id:
                    log_cce_api_error(self.path, 400, "cluster_id is required")
                    self._json(400, {"error": "cluster_id is required"})
                    return
                payload = huawei_cce.list_workloads(
                    creds["access_key"],
                    creds["secret_key"],
                    region=creds["region"],
                    cluster_id=cluster_id,
                    project_id=creds["project_id"],
                    namespace=str(data.get("namespace") or "default"),
                    kind=str(data.get("kind") or "deployments"),
                )
                self._json(200, payload)
            except Exception as exc:  # noqa: BLE001
                if not json_huawei_cce_error(self, exc):
                    raise
            return

        if path == "/api/cce/workloads/detail":
            try:
                creds = resolve_huawei_credentials(data)
                cluster_id = str(data.get("cluster_id") or "").strip()
                name = str(data.get("name") or "").strip()
                if not cluster_id:
                    log_cce_api_error(self.path, 400, "cluster_id is required")
                    self._json(400, {"error": "cluster_id is required"})
                    return
                if not name:
                    log_cce_api_error(self.path, 400, "name is required")
                    self._json(400, {"error": "name is required"})
                    return
                payload = huawei_cce.get_workload_detail(
                    creds["access_key"],
                    creds["secret_key"],
                    region=creds["region"],
                    cluster_id=cluster_id,
                    name=name,
                    project_id=creds["project_id"],
                    namespace=str(data.get("namespace") or "default"),
                    kind=str(data.get("kind") or "deployments"),
                )
                self._json(200, payload)
            except Exception as exc:  # noqa: BLE001
                if not json_huawei_cce_error(self, exc):
                    raise
            return

        m_stop = re.fullmatch(r"/api/jobs/([^/]+)/stop", path)
        if m_stop:
            result = request_job_stop(m_stop.group(1))
            if result.get("ok"):
                self._json(200, result)
                return
            self._json(int(result.get("http_status") or 400), result)
            return

        if path == "/api/push":
            login_command = (data.get("login_command") or "").strip()
            raw_items = data.get("items")
            items: list[dict[str, str]] = []
            if isinstance(raw_items, list) and raw_items:
                for it in raw_items:
                    if not isinstance(it, dict):
                        continue
                    sid = (it.get("service_id") or "").strip()
                    if not sid:
                        continue
                    items.append(
                        {
                            "service_id": sid,
                            "branch": (it.get("branch") or "main").strip() or "main",
                            "version": (it.get("version") or "").strip(),
                        }
                    )
            else:
                service_id = (data.get("service_id") or "").strip()
                branch = (data.get("branch") or "main").strip() or "main"
                if service_id:
                    items = [
                        {
                            "service_id": service_id,
                            "branch": branch,
                            "version": (data.get("version") or "").strip(),
                        }
                    ]
            if not items:
                self._json(400, {"error": "service_id or items[] required"})
                return
            if len(items) != 1:
                self._json(
                    400,
                    {
                        "error": "一次只能构建一个微服务，请切换微服务后分别运行",
                        "error_code": "single_service_only",
                    },
                )
                return
            catalog = {svc["id"]: svc for svc in load_services()}
            for item in items:
                svc = catalog.get(item["service_id"])
                if not svc:
                    self._json(400, {"error": f"unknown service: {item['service_id']}"})
                    return
                if svc.get("requires_version") and not DAEMON_VERSION_RE.fullmatch(item.get("version") or ""):
                    self._json(
                        400,
                        {
                            "error": (
                                f"{item['service_id']} version is required and must match vMAJOR.MINOR.PATCH "
                                "(example: v1.2.3)"
                            )
                        },
                    )
                    return
            client_id = _normalize_client_id(data.get("client_id"))
            docker = docker_ready_for_push()
            if not docker["ok"]:
                self._json(503, {"error": "docker unavailable", "detail": docker["detail"]})
                return
            job_id = uuid.uuid4().hex[:12]
            log_file = LOG_DIR / f"job-{job_id}.log"
            ids = ",".join(it["service_id"] for it in items)
            service_ids = [it["service_id"] for it in items]
            branches = ",".join(it["branch"] for it in items)
            new_job = {
                "id": job_id,
                "client_id": client_id,
                "operator": user,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "finished_at": "",
                "service_id": ids,
                "service_ids": service_ids,
                "branch": branches,
                "status": "running",
                "stage": "starting",
                "error": None,
                "remote": None,
                "archive": None,
                "commit_sha": None,
                "test_status": None,
                "test_summary": None,
                "test_commands": [],
                "test_failures": [],
                "test_cases": [],
                "test_report": None,
                "archive_dir": None,
                "results": [],
                "progress": None,
                "current": None,
                "test_runs": [],
                "log": [],
                "ui_log": [],
                "step_logs": {},
                "_ui_test_running": False,
                "cancel_requested": False,
                "slot_held": False,
                "queue_position": 0,
                "log_file": str(log_file),
            }
            conflict = register_concurrent_job(new_job)
            if conflict:
                self._json(409, conflict)
                return
            for item in items:
                if catalog[item["service_id"]].get("requires_version"):
                    if not save_last_daemon_version(item.get("version") or ""):
                        append_job_log(job_id, "WARN: could not persist the last Daemon version on server")
            persist_job_meta(job_id)
            append_job_log(
                job_id,
                f"job start service={ids} branch={branches} operator={user or '-'}",
            )
            threading.Thread(
                target=run_push_job,
                args=(job_id, items, login_command),
                daemon=True,
            ).start()
            self._json(
                200,
                {
                    "job_id": job_id,
                    "client_id": client_id or None,
                    "operator": user,
                    "count": len(items),
                    "items": items,
                    "branch": branches,
                },
            )
            return

        self._json(404, {"error": "not found"})


def main() -> None:
    host, port = CFG["host"], CFG["port"]
    allow_remote = str(CFG.get("allow_remote") or os.environ.get("SWR_ALLOW_REMOTE") or "").lower() in (
        "1",
        "true",
        "yes",
    )
    if host not in ("127.0.0.1", "localhost", "::1") and not allow_remote:
        raise SystemExit("refusing non-localhost (set allow_remote=true in config.json for server deploy)")
    ThreadingHTTPServer.allow_reuse_address = True
    ThreadingHTTPServer.request_queue_size = 128
    ThreadingHTTPServer.daemon_threads = True
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"[swr-push-helper] http://{host}:{port}/", flush=True)
    print(f"[swr-push-helper] mode: web login + GitHub branch → local build → SWR", flush=True)
    print(f"[swr-push-helper] SWR: {CFG['swr_registry']}/{CFG['swr_org']}", flush=True)
    apply_ci_tmp_env()
    print(f"[swr-push-helper] ci_tmp={ci_tmp_root()}", flush=True)
    print(f"[swr-push-helper] allow_remote={allow_remote}", flush=True)
    ensure_default_users()
    reaped = reap_orphaned_running_jobs()
    if reaped:
        print(f"[swr-push-helper] marked {reaped} interrupted job(s) after restart", flush=True)
    pruned_artifacts = prune_artifacts_log()
    pruned_history = prune_all_build_histories()
    if pruned_artifacts or pruned_history:
        print(
            f"[swr-push-helper] trimmed records: artifacts={pruned_artifacts} history_jobs={pruned_history}",
            flush=True,
        )
    # Fast local signal for UI; full registry probe in background (don't block startup).
    fast = login_status_fast()
    print(f"[swr-push-helper] shared SWR login (local)={fast}", flush=True)

    def _bg_login_probe() -> None:
        try:
            ok = check_login(force=True)
            print(f"[swr-push-helper] shared SWR login (probe)={ok}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[swr-push-helper] shared SWR login probe skipped: {e}", flush=True)

    threading.Thread(target=_bg_login_probe, daemon=True).start()
    httpd.serve_forever()


if __name__ == "__main__":
    main()
