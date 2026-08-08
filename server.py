#!/usr/bin/env python3
"""SWR push helper for sharing: web login + GitHub branch → local build → push SWR."""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from copy import deepcopy
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse, urlunparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
LOG_DIR = ROOT / "logs"
CONFIG_PATH = ROOT / "config.json"
SERVICES_PATH = ROOT / "services.json"
TEST_PLANS_PATH = ROOT / "test-plans.json"
TEST_RUNNER_PATH = ROOT / "test_runner.py"
LAST_DAEMON_VERSION_PATH = LOG_DIR / "last-daemon-version.json"
LOG_DIR.mkdir(parents=True, exist_ok=True)

_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()
_login_ok = False
_login_lock = threading.Lock()
_login_probe_cache: tuple[float, bool] | None = None  # (ts, ok)
_token_cache: str | None = None
_docker_cache: tuple[float, dict[str, Any]] | None = None
_daemon_version_lock = threading.Lock()
_branch_cache: dict[str, tuple[float, list[str]]] = {}
_branch_cache_lock = threading.Lock()

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

JOB_PUBLIC_FIELDS = (
    "id",
    "service_id",
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
)

JOB_COMPACT_FIELDS = (
    "id",
    "service_id",
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
)


def _running_job_locked() -> dict[str, Any] | None:
    for job in _jobs.values():
        if job.get("status") == "running":
            return job
    return None


def active_job_summary() -> dict[str, Any] | None:
    """Return a lightweight snapshot without holding the lock during HTTP writes."""
    with _jobs_lock:
        job = _running_job_locked()
        if not job:
            return None
        return deepcopy({key: job.get(key) for key in JOB_COMPACT_FIELDS})


def register_job_if_idle(job: dict[str, Any]) -> dict[str, Any] | None:
    """Atomically register a job, or return the already-running job summary."""
    with _jobs_lock:
        active = _running_job_locked()
        if active:
            return deepcopy({key: active.get(key) for key in JOB_COMPACT_FIELDS})
        _jobs[str(job["id"])] = job
    return None


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


def job_payload(
    job: dict[str, Any],
    *,
    compact: bool = False,
    view_ui: bool = False,
    log_after: int = 0,
    test_revision: int = -1,
) -> dict[str, Any]:
    fields = JOB_COMPACT_FIELDS if compact else JOB_PUBLIC_FIELDS
    payload = deepcopy({key: job.get(key) for key in fields})
    if view_ui and "ui_log" in job:
        selected_log = list(job.get("ui_log") or [])
    else:
        raw_log = list(job.get("log") or [])
        selected_log = filter_ui_log(raw_log) if view_ui else raw_log
    cursor = max(0, min(int(log_after or 0), len(selected_log))) if compact else 0
    payload["log"] = selected_log[cursor:]
    payload["log_cursor"] = len(selected_log)
    revision = len(job.get("test_runs") or [])
    payload["test_revision"] = revision
    if compact and test_revision != revision:
        payload["test_runs"] = deepcopy(job.get("test_runs") or [])
    return payload


def job_meta_path(job_id: str) -> Path:
    return LOG_DIR / f"job-{job_id}.json"


def persist_job_meta(job_id: str) -> None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return
        meta = {
            k: job.get(k)
            for k in (
                "id",
                "service_id",
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
            )
        }
    try:
        job_meta_path(job_id).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


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
        "service_id": meta.get("service_id"),
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
        "log": log_lines,
    }


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
    cfg["port"] = int(cfg.get("port") or 18888)
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
    return cfg


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


def reload_cfg() -> None:
    global CFG, _token_cache
    CFG = load_config()
    _token_cache = None


def load_services() -> list[dict[str, Any]]:
    return json.loads(SERVICES_PATH.read_text(encoding="utf-8"))


def test_report_dir(job_id: str, service_id: str) -> Path:
    safe_service_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", service_id).strip("._") or "service"
    return LOG_DIR / "reports" / job_id / safe_service_id


def run_tests_nonblocking(job_id: str, service_id: str, repo_dir: Path, commit_sha: str) -> dict[str, Any]:
    """Run trusted UT/DT tests and always return a result that cannot block publishing."""
    report_dir = test_report_dir(job_id, service_id)
    report_dir.mkdir(parents=True, exist_ok=True)
    summary_path = report_dir / "summary.json"
    if not TEST_PLANS_PATH.is_file() or not TEST_RUNNER_PATH.is_file():
        result = {
            "status": "error",
            "total": 0,
            "passed": 0,
            "failed": 0,
            "errors": 1,
            "duration_ms": 0,
            "failures": [{"name": "test runner", "detail": "test-plans.json or test_runner.py is missing"}],
        }
    else:
        append_job_log(job_id, f"tests start service={service_id} sha={commit_sha}")
        command = " ".join(
            (
                "python3.11",
                shlex.quote(host_path(TEST_RUNNER_PATH)),
                "--plans",
                shlex.quote(host_path(TEST_PLANS_PATH)),
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
        append_job_log(job_id, "WARN tests did not pass; phase-1 policy continues to image build")
    return result


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


def run_cmd(
    args: list[str],
    timeout: int | None = 600,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> tuple[int, str]:
    try:
        p = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=cwd,
            env=env,
        )
        out = (p.stdout or "") + (("\n" + p.stderr) if p.stderr else "")
        return p.returncode, out.strip()
    except subprocess.TimeoutExpired as e:
        out = ((e.stdout or "") + "\n" + (e.stderr or "")).strip()
        return 124, out + "\nERROR: timeout"
    except FileNotFoundError:
        return 127, f"ERROR: not found: {args[0]}"


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


def append_job_log(job_id: str, line: str) -> None:
    ts = time.strftime("%H:%M:%S")
    text = f"[{ts}] {line}"
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return
        job["log"].append(text)
        _append_ui_log(job, text)
        Path(job["log_file"]).open("a", encoding="utf-8").write(text + "\n")


def set_job(job_id: str, **fields: Any) -> None:
    with _jobs_lock:
        if job_id in _jobs:
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


def clone_url_for(url: str) -> str:
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


def repo_dir(svc: dict[str, Any]) -> Path:
    return Path(CFG["workspace_root"]) / clone_dir_name(svc)


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
    source_dir = workspace / "deploy" / "runtime-images"
    contents: dict[str, str] = {}
    for marker in FLEET_RUNTIME_CACHE_MARKERS:
        content = _validated_fleet_cache_marker(source_dir / marker, marker)
        if content is None:
            return False
        contents[marker] = content

    cache_dir = fleet_runtime_cache_dir()
    try:
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
    for marker in FLEET_RUNTIME_CACHE_MARKERS:
        content = _validated_fleet_cache_marker(cache_dir / marker, marker)
        if content is None:
            return False
        contents[marker] = content

    target_dir = workspace / "deploy" / "runtime-images"
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
    """Make Fleet downloads nginx-readable and remove build-only metadata."""
    try:
        bundle.chmod(0o644)
        checksum = Path(str(bundle) + ".sha256")
        if checksum.is_file():
            checksum.chmod(0o644)
        metadata = Path(str(bundle) + ".meta")
        if metadata.is_file():
            metadata.unlink()
            append_job_log(job_id, f"removed build-only Fleet metadata: {metadata.name}")
    except OSError as exc:
        append_job_log(job_id, f"ERROR: Fleet archive permissions could not be finalized: {exc}")
        return False
    append_job_log(job_id, f"Fleet archive download permissions set: {bundle.name} mode=0644")
    return True


def check_docker(force: bool = False) -> dict[str, Any]:
    """Probe Docker via `docker info` (native Linux or WSL). Cached ~45s."""
    global _docker_cache
    now = time.time()
    if not force and _docker_cache and now - _docker_cache[0] < 45:
        return dict(_docker_cache[1])
    if use_wsl():
        pass
    elif shutil.which("docker") is None:
        st = {"ok": False, "detail": "docker not installed"}
        _docker_cache = (now, st)
        return dict(st)
    code, out = docker_cmd("info", timeout=8)
    if code != 0:
        st = {"ok": False, "detail": ((out or "")[-400:] or "docker unavailable")}
        _docker_cache = (now, st)
        return dict(st)
    where = f"WSL:{CFG['wsl_distro']}" if use_wsl() else "native"
    st = {"ok": True, "detail": f"Docker ok / {where}"}
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
    if _docker_cache:
        return dict(_docker_cache[1])
    return {"ok": True, "detail": "Docker (后台检测中)"}


def schedule_docker_probe() -> None:
    """Refresh docker cache in background if stale/missing."""
    now = time.time()
    if _docker_cache and now - _docker_cache[0] < 45:
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


def check_login(force: bool = False) -> bool:
    """Validate SWR auth. Uses short cache; force=True always hits registry."""
    global _login_ok, _login_probe_cache
    now = time.time()
    if not force and _login_probe_cache and now - _login_probe_cache[0] < 45:
        ok = bool(_login_probe_cache[1])
        with _login_lock:
            _login_ok = ok
        return ok

    if not docker_config_has_swr_auth():
        with _login_lock:
            _login_ok = False
            _login_probe_cache = (now, False)
        return False

    remote = f"{CFG['swr_registry']}/{CFG['swr_org']}/robot-ci-auth-probe-does-not-exist"
    code, out = docker_cmd("manifest", "inspect", remote, timeout=12)
    text = (out or "").lower()
    # A valid, authenticated registry request for this deliberately absent image
    # returns a precise manifest/name-missing response. Everything else fails
    # closed so a network/configuration error is never reported as logged in.
    if "unauthorized" in text or "authentication required" in text or "denied" in text:
        ok = False
    elif code == 0 or any(
        marker in text for marker in ("manifest unknown", "name unknown", "no such manifest")
    ):
        ok = True
    else:
        ok = False
    with _login_lock:
        _login_ok = ok
        _login_probe_cache = (now, ok)
    return ok


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
    reader.join(timeout=timeout)
    if reader.is_alive():
        p.kill()
        reader.join(timeout=10)
        append_job_log(job_id, "ERROR: timeout")
        return 124
    return p.wait() or 0


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

    dest = repo_dir(svc)
    public = public_github_url(url)
    clone_url = clone_url_for(url)
    if clone_url.startswith("https://") and "github.com" in clone_url and not gh_token():
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

    # Preserve only Fleet's validated cache metadata outside the disposable checkout.
    # The Fleet script independently verifies both the new source fingerprint and
    # the live Docker image IDs before it skips either Runtime image build.
    if svc.get("id") == "multica-fleet":
        persist_fleet_runtime_cache(job_id, dest)

    # Always wipe previous checkout (including build outputs) then fresh clone.
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
    if not (dest / "deploy.sh").is_file() and not (dest / "build-image.sh").is_file():
        return False, "missing deploy.sh/build-image.sh"
    if svc.get("id") == "multica-fleet":
        restore_fleet_runtime_cache(job_id, dest)
    code, head = run_cmd(git_args("-C", str(dest), "rev-parse", "--short=7", "HEAD"), timeout=30, env=genv)
    append_job_log(job_id, f"HEAD={head if code == 0 else '?'} @ {branch}")
    return True, str(dest)


def public_service_dir() -> Path:
    """Sibling of per-service clone dirs: <workspace_root>/public-service."""
    return Path(CFG["workspace_root"]) / "public-service"


def ensure_public_service(job_id: str) -> tuple[bool, str]:
    """
    Many microservice deploy.sh scripts source
    ../public-service/windows-deploy/lib/source-rrd.sh.
    Helper clones only the service repo, so keep a shared public-service checkout
    next to it (does not modify service source trees).
    """
    dest = public_service_dir()
    branch = (CFG.get("public_service_branch") or "main").strip() or "main"
    url = (CFG.get("public_service_github") or "https://github.com/rollingfruit/public-service.git").strip()
    public = public_github_url(url)
    clone_url = clone_url_for(url)
    genv = git_env()
    dest.parent.mkdir(parents=True, exist_ok=True)
    append_job_log(job_id, f"ensure shared public-service → {dest} @ {branch}")

    # Fresh clone each job so public-service helpers cannot accumulate junk either.
    wipe_workspace_dir(job_id, dest, label="public-service")
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
    rrd = dest / "windows-deploy" / "lib" / "source-rrd.sh"
    if not rrd.is_file():
        return False, f"missing {rrd}"
    code, head = run_cmd(git_args("-C", str(dest), "rev-parse", "--short=7", "HEAD"), timeout=30, env=genv)
    append_job_log(job_id, f"public-service HEAD={head if code == 0 else '?'} @ {branch}")
    return True, str(dest)


def mattermost_package_marker() -> Path:
    return Path("/opt/ai/mattermost/.swr_helper_git_hash")


def mattermost_package_matches(git_hash: str) -> bool:
    """True if /opt/ai/mattermost was packaged for this exact git short hash."""
    if not git_hash or git_hash.startswith("0000"):
        return False
    mm = Path("/opt/ai/mattermost")
    bin_ok = (mm / "mattermost" / "bin" / "mattermost").is_file() or (
        mm / "bin" / "mattermost"
    ).is_file()
    marker = mattermost_package_marker()
    try:
        return bin_ok and marker.is_file() and marker.read_text(encoding="utf-8").strip() == git_hash
    except OSError:
        return False


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
    src = repo_dir(svc)
    shell_src = host_path(src)
    ps_dir = host_path(public_service_dir())
    where = "WSL" if use_wsl() else "host"
    append_job_log(job_id, f"build on {where}: {src}")

    # Helper-only speedups (do not edit microservice source):
    # - CCE_SKIP_EXPORT=1: skip multi-hundred-MB docker save tar (we push from local image)
    # - mattermost SKIP_PACKAGE=1: skip webpack/go when package for this commit already exists
    extra_env = "CCE_SKIP_EXPORT=1 "
    if svc.get("id") == "mattermost" and mattermost_package_matches(git_hash):
        extra_env += "SKIP_PACKAGE=1 "
        append_job_log(
            job_id,
            f"mattermost optimize: SKIP_PACKAGE=1 (package already built for {git_hash})",
        )

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
            f"FLEET_GIT_HASH={shlex.quote(git_hash)} INCLUDE_RUNTIME_IMAGES=1 "
        )
        append_job_log(job_id, "Fleet bundle: control image + OpenCode + Hermes; SWR push disabled")

    deploy_args = f" {shlex.quote(action)}" if action else ""

    bash = (
        "set -euo pipefail; "
        f"cd '{shell_src}'; "
        "find . -maxdepth 3 -type f -name '*.sh' -exec sed -i 's/\\r$//' {} + 2>/dev/null || true; "
        f"export CCE_UPLOAD=0 DEPLOY_NO_PAUSE=1 SKIP_IMAGE_ARCHIVE=1 EXPORT_ARCHIVE=0 "
        f"CCE_GIT_HASH='{git_hash}' PUBLIC_SERVICE_DIR='{ps_dir}' {extra_env}; "
        f"if [[ -f ./deploy.sh ]]; then bash ./deploy.sh{deploy_args}; "
        "elif [[ -f ./build-image.sh ]]; then bash ./build-image.sh; "
        "else echo 'ERROR: no deploy.sh'; exit 1; fi"
    )
    output_tail: list[str] = []
    code = run_stream(
        job_id,
        bash_lc(bash),
        timeout=int(CFG.get("build_timeout_sec") or 7200),
        output_tail=output_tail,
    )
    if code != 0:
        append_job_log(job_id, f"ERROR build exit={code}")
        return False, summarize_command_failure(output_tail, f"build exited with code {code}")
    append_job_log(job_id, "build finished")
    if svc.get("id") == "mattermost" and git_hash and not git_hash.startswith("0000"):
        try:
            marker = mattermost_package_marker()
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(git_hash + "\n", encoding="utf-8")
            append_job_log(job_id, f"mattermost package marker -> {git_hash}")
        except OSError as e:
            append_job_log(job_id, f"WARN: could not write package marker: {e}")
    return True, ""


def find_latest_tar(svc: dict[str, Any]) -> Path | None:
    export_dir = repo_dir(svc) / "runtime-images" / "cce-export"
    if not export_dir.is_dir():
        return None
    cands = sorted(export_dir.glob(f"{svc['tar_prefix']}*.tar"), key=lambda p: p.stat().st_mtime, reverse=True)
    return cands[0] if cands else None


def parse_tag_from_tar(tar_path: Path, image: str) -> str:
    stem = tar_path.stem
    prefix = image + "_"
    if stem.startswith(prefix):
        return stem[len(prefix) :]
    parts = stem.split("_", 1)
    return parts[1] if len(parts) == 2 else stem


def disk_free_bytes(path: Path | str) -> int | None:
    try:
        return shutil.disk_usage(str(path)).free
    except OSError:
        return None


def prune_nginx_archives(job_id: str, keep_latest: int = 3, min_free_gb: float = 8.0) -> None:
    """Delete oldest timestamp dirs under archive_root when free space is low."""
    base = Path((CFG.get("archive_root") or "/usr/share/nginx/html/images").rstrip("/") or ".")
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    free = disk_free_bytes(base)
    if free is None:
        return
    min_free = int(min_free_gb * 1024**3)
    if free >= min_free:
        return

    dirs = sorted(
        [p for p in base.iterdir() if p.is_dir() and re.fullmatch(r"\d{8,14}", p.name)],
        key=lambda p: p.name,
    )
    if len(dirs) <= keep_latest:
        append_job_log(
            job_id,
            f"WARN: disk free={free // (1024**2)}MB < {min_free_gb:.0f}GB but only "
            f"{len(dirs)} archive dir(s) (keep_latest={keep_latest})",
        )
        return

    append_job_log(
        job_id,
        f"disk free={free // (1024**2)}MB < {min_free_gb:.0f}GB; pruning old nginx archives…",
    )
    for old in dirs[: max(0, len(dirs) - keep_latest)]:
        append_job_log(job_id, f"remove old archive dir: {old}")
        shutil.rmtree(old, ignore_errors=True)
        free = disk_free_bytes(base)
        if free is not None and free >= min_free:
            break
    free2 = disk_free_bytes(base)
    if free2 is not None:
        append_job_log(job_id, f"disk free after prune: {free2 // (1024**2)}MB")


def make_archive_dir(job_id: str | None = None) -> Path:
    base = (CFG.get("archive_root") or "/usr/share/nginx/html/images").rstrip("/")
    if job_id:
        prune_nginx_archives(job_id)
    out_dir = Path(base) / time.strftime("%Y%m%d%H%M%S")
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
    if not CFG.get("archive_enabled"):
        append_job_log(job_id, "local archive skipped (disabled)")
        return True, ""

    if out_dir is None:
        try:
            out_dir = make_archive_dir(job_id)
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


def resolve_local_image(job_id: str, svc: dict[str, Any]) -> tuple[str | None, str | None]:
    image = svc["image"]
    tar_path = find_latest_tar(svc)
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
    cmd = (login_command or "").strip()
    if cmd:
        append_job_log(job_id, "SWR login from page credentials…")
        ok_login, out_login = do_login(cmd)
        append_job_log(job_id, (out_login or "")[-800:])
        if not ok_login:
            set_job(job_id, status="failed", error="SWR login failed")
            append_job_log(job_id, "ERROR: paste a valid Huawei SWR temporary login command")
            return False
        if not check_login(force=True):
            set_job(job_id, status="failed", error="SWR login verification failed")
            append_job_log(job_id, "ERROR: SWR login command succeeded locally but registry verification failed")
            return False
        return True

    append_job_log(job_id, "reusing shared SWR login on this server…")
    if not check_login(force=True):
        set_job(job_id, status="failed", error="not logged in to SWR")
        append_job_log(
            job_id,
            "ERROR: 服务器上尚无有效 SWR 登录（或已过期）。请任一人在页面粘贴 docker login 并登录后再推送。",
        )
        return False
    append_job_log(job_id, "shared SWR login still valid")
    return True


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

    git = git_bin()
    code, head = run_cmd([git, "-C", detail, "rev-parse", "HEAD"], timeout=30)
    commit_sha = head if code == 0 else "0000000000000000000000000000000000000000"
    git_hash = commit_sha[:7]

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
    set_job(job_id, stage="building")

    # Always rebuild; never reuse a previous local image for the same git hash.
    built, build_error = build_from_source(job_id, svc, git_hash, version, archive_dir)
    if not built:
        result["error"] = build_error or "build failed"
        append_job_log(job_id, f"FAILED service={service_id} stage=building reason={result['error']}")
        return result

    if svc.get("id") == "multica-fleet":
        persist_fleet_runtime_cache(job_id, repo_dir(svc))

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
        append_job_log(job_id, f"OK archive-only {bundle} (SWR push skipped)")
        append_job_log(job_id, f"OK deployment env {required[0]}")
        append_job_log(job_id, f"OK deployment script {required[1]}")
        return result
    local_ref, tag = resolve_local_image(job_id, svc)
    by_hash_ref, by_hash_tag = find_local_image_by_git_hash(image, git_hash)
    if by_hash_ref and by_hash_tag:
        local_ref, tag = by_hash_ref, by_hash_tag
    if not local_ref or not tag:
        result["error"] = "no image/tar after build"
        return result

    set_job(job_id, stage="pushing")
    remote = f"{registry}/{org}/{image}:{tag}"
    append_job_log(job_id, f"docker tag {local_ref} -> {remote}")
    code, out = docker_cmd("tag", local_ref, remote, timeout=60)
    if code != 0:
        append_job_log(job_id, out)
        result["error"] = "docker tag failed"
        return result

    push_attempts = 4
    code, out = 1, ""
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
        time.sleep(wait_s)
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
    set_job(job_id, stage="archiving")
    prune_nginx_archives(job_id)
    ok_arc, arc_path = archive_image_locally(job_id, local_ref, image, tag, out_dir=archive_dir)
    if not ok_arc:
        result["remote"] = remote
        if CFG.get("archive_required"):
            result["error"] = "local archive failed"
            append_job_log(
                job_id,
                "ERROR: SWR 已推送成功，但本地归档镜像包失败。"
                f"请检查目录权限：{CFG.get('archive_root')}",
            )
            docker_cmd("rmi", remote, timeout=60)
            return result
        append_job_log(job_id, f"WARN: local archive failed (ignored): {arc_path}")

    docker_cmd("rmi", remote, timeout=60)
    result["ok"] = True
    result["remote"] = remote
    result["archive"] = arc_path or ""
    append_job_log(job_id, f"OK {remote}")
    if arc_path:
        append_job_log(job_id, f"OK archive {arc_path}")
    return result


def run_push_job(
    job_id: str,
    items: list[dict[str, str]],
    login_command: str = "",
) -> None:
    """Push one or more services; batch jobs share one archive timestamp directory."""
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
                archive_dir = make_archive_dir(job_id)
            except OSError as e:
                if CFG.get("archive_required") or bundle_required:
                    set_job(job_id, status="failed", error=f"cannot create archive dir: {e}")
                    append_job_log(job_id, f"ERROR cannot create archive dir: {e}")
                    return
                append_job_log(job_id, f"WARN: cannot create archive dir: {e}")
            else:
                append_job_log(job_id, f"shared archive dir={archive_dir}")
                set_job(job_id, archive_dir=str(archive_dir))

        results: list[dict[str, Any]] = []
        total = len(resolved)
        for idx, (svc, br, version) in enumerate(resolved, 1):
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
    except Exception as e:  # noqa: BLE001
        set_job(job_id, status="failed", error=str(e))
        append_job_log(job_id, f"ERROR {e}")


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

    def _json(self, code: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

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
            self._json(200, {"branches": branches, "default_branch": default})
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
            payload = None
            with _jobs_lock:
                job = _jobs.get(job_id)
                if job:
                    payload = job_payload(
                        job,
                        compact=compact,
                        view_ui=view_ui,
                        log_after=log_after,
                        test_revision=test_revision,
                    )
            if payload is not None:
                self._json(200, payload)
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
                    ) if compact or view_ui else disk,
                )
                return
            self._json(404, {"error": "job not found"})
            return

        # Lightweight global active-job discovery. Detailed logs are fetched
        # incrementally from /api/jobs/<id> after the page attaches.
        if path == "/api/running-job":
            payload = active_job_summary()
            if payload:
                self._json(200, payload)
                return
            self._json(200, {"id": None, "status": "idle"})
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
            if len(items) > 32:
                self._json(400, {"error": "too many services (max 32)"})
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
            active = active_job_summary()
            if active:
                self._json(
                    409,
                    {
                        "error": "another build is running",
                        "active_job_id": active.get("id"),
                        "active_job": active,
                    },
                )
                return
            docker = check_docker()
            if not docker["ok"]:
                self._json(503, {"error": "docker unavailable", "detail": docker["detail"]})
                return
            job_id = uuid.uuid4().hex[:12]
            log_file = LOG_DIR / f"job-{job_id}.log"
            ids = ",".join(it["service_id"] for it in items)
            branches = ",".join(it["branch"] for it in items)
            new_job = {
                "id": job_id,
                "service_id": ids,
                "branch": branches,
                "status": "running",
                "stage": "syncing",
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
                "_ui_test_running": False,
                "log_file": str(log_file),
            }
            active = register_job_if_idle(new_job)
            if active:
                self._json(
                    409,
                    {
                        "error": "another build is running",
                        "active_job_id": active.get("id"),
                        "active_job": active,
                    },
                )
                return
            for item in items:
                if catalog[item["service_id"]].get("requires_version"):
                    if not save_last_daemon_version(item.get("version") or ""):
                        append_job_log(job_id, "WARN: could not persist the last Daemon version on server")
            persist_job_meta(job_id)
            append_job_log(
                job_id,
                f"job start services={len(items)} [{ids}] branches=[{branches}]",
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
    print(f"[swr-push-helper] allow_remote={allow_remote}", flush=True)
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
