"""SSH jump → CCE worker → in-place or parallel Deployment rollout.

Used by gamma deploy after a successful SWR push. Credentials come from
config / process env / an optional secrets file — never from git.
"""
from __future__ import annotations

import base64
import copy
import json
import os
import re
import shlex
import time
from pathlib import Path
from typing import Any, Callable, Mapping

# CI service_id -> (Deployment, business container)
SERVICE_MAP: dict[str, tuple[str, str]] = {
    "agent-governance-gw": ("governance", "container-1"),
    "service-router": ("service-router", "container1"),
    "semantic-gateway": ("semantic-gateway", "container-1"),
    "multica-server": ("multica-server", "container-1"),
    "semantic-schedule": ("semantic-schedule", "container-1"),
    "agentops": ("agent-ops", "container-1"),
    "config-service": ("config-server", "container-1"),
    "memory-service": ("memory-service", "container-1"),
    "agentlink": ("agentlink", "container-1"),
}

_SIDECAR_TOKENS = ("filebeat", "fluent", "fluent-bit", "istio-proxy", "vault-agent")
_DEFAULT_SECRET_NAMES = (
    "cce-aiwelink.env",
    "cce.env",
)
RemoteFn = Callable[[str, int], tuple[int, str, str]]


class CceRolloutError(RuntimeError):
    """User-visible gamma deploy failure (no secrets in the message)."""


def parse_ssh_target(value: str, default_user: str = "root") -> tuple[str, str, int]:
    text = str(value or "").strip()
    if not text:
        raise CceRolloutError("SSH 目标为空")
    user = default_user
    if "@" in text:
        user, text = text.rsplit("@", 1)
        user = user.strip() or default_user
    text = text.strip()
    if text.startswith("[") and "]" in text:
        host, rest = text[1:].split("]", 1)
        if rest.startswith(":") and rest[1:].isdigit():
            return user, host.strip(), int(rest[1:])
        return user, host.strip(), 22
    if text.count(":") == 1:
        host, port_s = text.rsplit(":", 1)
        if port_s.isdigit():
            return user, host.strip(), int(port_s)
    return user, text, 22


def image_ref_name(image: str) -> str:
    last = str(image or "").rstrip("/").split("/")[-1]
    return last.rsplit(":", 1)[0] if ":" in last else last


def image_tag(image: str) -> str:
    last = str(image or "").rstrip("/").split("/")[-1]
    if ":" not in last:
        return ""
    return last.rsplit(":", 1)[-1]


def validate_image_ref(image: str) -> str:
    text = str(image or "").strip()
    if not text:
        raise CceRolloutError("没有可部署的 SWR 镜像")
    tag = image_tag(text)
    if not tag or tag == "latest":
        raise CceRolloutError(f"拒绝部署 latest 或无 tag 镜像: {text}")
    return text


def parse_secret_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, value = text.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def _first_nonempty(*values: str) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def default_secret_paths(root: Path | None = None) -> list[Path]:
    paths: list[Path] = []
    here = root or Path(__file__).resolve().parent
    for name in _DEFAULT_SECRET_NAMES:
        paths.append(here / "secrets" / name)
    paths.append(Path.home() / ".cursor" / "secrets" / "cce-aiwelink.env")
    return paths


def resolve_credentials(
    cfg: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
    *,
    secrets_file: str | Path | None = None,
    load_default_files: bool = True,
    helper_root: Path | None = None,
) -> dict[str, str]:
    cfg = cfg or {}
    env = environ if environ is not None else os.environ
    file_vals: dict[str, str] = {}
    configured = _first_nonempty(
        str(secrets_file or ""),
        str(cfg.get("cce_secrets_file") or ""),
        str(env.get("CCE_SECRETS_FILE") or ""),
    )
    search: list[Path] = []
    if configured:
        search.append(Path(configured).expanduser())
    if load_default_files:
        search.extend(default_secret_paths(helper_root))
    for path in search:
        file_vals = parse_secret_file(path)
        if file_vals:
            break

    shared = _first_nonempty(
        str(env.get("CCE_SSH_PASSWORD") or ""),
        str(cfg.get("cce_ssh_password") or ""),
        file_vals.get("CCE_SSH_PASSWORD", ""),
    )
    jump_password = _first_nonempty(
        str(env.get("CCE_JUMP_PASSWORD") or ""),
        str(cfg.get("cce_jump_password") or ""),
        file_vals.get("CCE_JUMP_PASSWORD", ""),
        shared,
    )
    node_password = _first_nonempty(
        str(env.get("CCE_NODE_PASSWORD") or ""),
        str(cfg.get("cce_node_password") or ""),
        file_vals.get("CCE_NODE_PASSWORD", ""),
        shared,
    )
    ssh_key = _first_nonempty(
        str(env.get("CCE_SSH_KEY") or ""),
        str(cfg.get("cce_ssh_key") or ""),
        file_vals.get("CCE_SSH_KEY", ""),
    )
    node_user = _first_nonempty(
        str(env.get("CCE_NODE_USER") or ""),
        str(cfg.get("cce_node_user") or ""),
        file_vals.get("CCE_NODE_USER", ""),
        "root",
    )
    return {
        "jump_password": jump_password,
        "node_password": node_password,
        "ssh_key": ssh_key,
        "node_user": node_user or "root",
    }


def default_key_candidates() -> list[Path]:
    home = Path.home() / ".ssh"
    return [home / "id_ed25519", home / "id_rsa", home / "id_ecdsa"]


def overlay_environment_passwords(
    creds: Mapping[str, str],
    environment: Mapping[str, Any] | None,
) -> dict[str, str]:
    out = {
        "jump_password": str(creds.get("jump_password") or ""),
        "node_password": str(creds.get("node_password") or ""),
        "ssh_key": str(creds.get("ssh_key") or ""),
        "node_user": str(creds.get("node_user") or "root") or "root",
    }
    if not environment:
        return out
    jump = str(environment.get("jump_password") or "").strip()
    node = str(environment.get("node_password") or "").strip()
    if jump:
        out["jump_password"] = jump
    if node:
        out["node_password"] = node
    return out


def has_ssh_auth(creds: Mapping[str, str]) -> bool:
    if str(creds.get("jump_password") or "").strip():
        return True
    if str(creds.get("node_password") or "").strip():
        return True
    key = str(creds.get("ssh_key") or "").strip()
    if key and Path(key).expanduser().is_file():
        return True
    return any(path.is_file() for path in default_key_candidates())


def image_matches_workload(image: str, workload: str) -> bool:
    repo = image_ref_name(image).lower().replace("_", "-")
    name = str(workload or "").strip().lower().replace("_", "-")
    if not repo or not name:
        return False
    compact_repo = repo.replace("-", "")
    compact_name = name.replace("-", "")
    return repo == name or compact_repo == compact_name or name in repo or repo in name


def resolve_deploy_target(
    service_id: str,
    image: str,
    workload_name: str,
) -> tuple[str, str | None]:
    workload = str(workload_name or "").strip()
    mapped = SERVICE_MAP.get(str(service_id or "").strip())
    if mapped:
        mapped_deploy, mapped_container = mapped
        deploy = workload or mapped_deploy
        if deploy in {mapped_deploy, str(service_id).strip()}:
            return deploy, mapped_container
        return deploy, None
    if not workload:
        raise CceRolloutError(f"服务 {service_id} 没有负载名称，无法部署")
    return workload, None


def should_rollout_result(
    service_id: str,
    image: str,
    workload_name: str,
    *,
    result_count: int,
) -> bool:
    if result_count <= 1:
        return True
    workload = str(workload_name or "").strip()
    if str(service_id or "").strip() == workload:
        return True
    mapped = SERVICE_MAP.get(str(service_id or "").strip())
    if mapped and mapped[0] == workload:
        return True
    return image_matches_workload(image, workload)


def parse_container_table(text: str) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for line in str(text or "").splitlines():
        parts = re.split(r"[\t]+", line.strip(), maxsplit=1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            continue
        rows.append((parts[0].strip(), parts[1].strip()))
    return rows


def containers_from_deploy_json(payload: Any) -> list[tuple[str, str]]:
    if not isinstance(payload, dict):
        return []
    containers = (
        payload.get("spec", {})
        .get("template", {})
        .get("spec", {})
        .get("containers", [])
    )
    rows: list[tuple[str, str]] = []
    if not isinstance(containers, list):
        return rows
    for item in containers:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        image = str(item.get("image") or "").strip()
        if name:
            rows.append((name, image))
    return rows


def is_sidecar_container(name: str) -> bool:
    lowered = str(name or "").lower()
    return any(token in lowered for token in _SIDECAR_TOKENS)


def pick_container(
    containers: list[tuple[str, str]],
    *,
    image: str,
    preferred: str | None = None,
) -> str:
    if not containers:
        raise CceRolloutError("Deployment 没有容器")
    names = {name for name, _ in containers}
    if preferred and preferred in names:
        return preferred
    repo = image_ref_name(image)
    mains = [(name, img) for name, img in containers if not is_sidecar_container(name)]
    pool = mains or containers
    if repo:
        for name, img in pool:
            if repo in img:
                return name
    if len(pool) == 1:
        return pool[0][0]
    if preferred:
        raise CceRolloutError(f"容器 {preferred} 不在 Deployment 中: {', '.join(names)}")
    return pool[0][0]


def rollout_timeout_seconds(text: str, default: int = 180) -> int:
    raw = str(text or "").strip().lower()
    if raw.endswith("s") and raw[:-1].isdigit():
        return int(raw[:-1])
    if raw.endswith("m") and raw[:-1].isdigit():
        return int(raw[:-1]) * 60
    if raw.isdigit():
        return int(raw)
    return default


def _import_paramiko():
    try:
        import paramiko  # type: ignore
    except ImportError as exc:  # pragma: no cover - exercised only when missing
        raise CceRolloutError("gamma 部署需要 paramiko，请在 CI 机器执行 pip install paramiko") from exc
    return paramiko


def hop_exec(
    command: str,
    *,
    jump_user: str,
    jump_host: str,
    jump_port: int = 22,
    node_user: str,
    node_host: str,
    node_port: int = 22,
    jump_password: str = "",
    node_password: str = "",
    ssh_key: str = "",
    timeout: int = 120,
) -> tuple[int, str, str]:
    paramiko = _import_paramiko()
    jump = paramiko.SSHClient()
    jump.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    node = paramiko.SSHClient()
    node.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    key = str(ssh_key or "").strip()
    connect_kw: dict[str, Any] = {
        "timeout": 20,
        "banner_timeout": 20,
        "auth_timeout": 20,
        "allow_agent": True,
        "look_for_keys": True,
    }
    if key:
        connect_kw["key_filename"] = str(Path(key).expanduser())
    try:
        jump_kw = dict(connect_kw)
        if jump_password:
            jump_kw["password"] = jump_password
        jump.connect(jump_host, port=int(jump_port or 22), username=jump_user, **jump_kw)
        transport = jump.get_transport()
        if transport is None:
            raise CceRolloutError(f"跳板机 {jump_host} 连接后无 transport")
        channel = transport.open_channel(
            "direct-tcpip",
            (node_host, int(node_port or 22)),
            ("127.0.0.1", 0),
        )
        node_kw = dict(connect_kw)
        if node_password:
            node_kw["password"] = node_password
        node.connect(node_host, port=int(node_port or 22), username=node_user, sock=channel, **node_kw)
        _stdin, stdout, stderr = node.exec_command(command, timeout=max(30, int(timeout or 120)))
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        code = int(stdout.channel.recv_exit_status())
        return code, out, err
    except CceRolloutError:
        raise
    except Exception as exc:
        raise CceRolloutError(f"SSH {jump_host} → {node_host} 失败: {exc}") from exc
    finally:
        try:
            node.close()
        except Exception:
            pass
        try:
            jump.close()
        except Exception:
            pass


def exec_via_nodes(
    command: str,
    *,
    jump_host: str,
    nodes: list[str],
    creds: Mapping[str, str],
    timeout: int,
    hop: Callable[..., tuple[int, str, str]] | None = None,
) -> tuple[int, str, str]:
    jump_user, jump_addr, jump_port = parse_ssh_target(jump_host)
    if not nodes:
        raise CceRolloutError("环境未配置 CCE 节点")
    errors: list[str] = []
    runner = hop or hop_exec
    node_user_default = str(creds.get("node_user") or "root")
    for raw_node in nodes:
        node_user, node_addr, node_port = parse_ssh_target(raw_node, default_user=node_user_default)
        try:
            return runner(
                command,
                jump_user=jump_user,
                jump_host=jump_addr,
                jump_port=jump_port,
                node_user=node_user,
                node_host=node_addr,
                node_port=node_port,
                jump_password=str(creds.get("jump_password") or ""),
                node_password=str(creds.get("node_password") or ""),
                ssh_key=str(creds.get("ssh_key") or ""),
                timeout=timeout,
            )
        except CceRolloutError as exc:
            errors.append(f"{node_addr}: {exc}")
    raise CceRolloutError("所有节点 SSH 失败: " + " | ".join(errors))


def inspect_containers(run_remote: RemoteFn, namespace: str, deploy: str) -> list[tuple[str, str]]:
    ns = shlex.quote(namespace)
    name = shlex.quote(deploy)
    code, out, err = run_remote(f"kubectl -n {ns} get deploy {name} -o json", 60)
    if code != 0:
        detail = (err or out or "").strip() or f"exit {code}"
        raise CceRolloutError(f"读取 Deployment {deploy} 失败: {detail}")
    try:
        payload = json.loads(out)
    except json.JSONDecodeError as exc:
        raise CceRolloutError(f"Deployment {deploy} 返回的不是 JSON") from exc
    rows = containers_from_deploy_json(payload)
    if rows:
        return rows
    raise CceRolloutError(f"Deployment {deploy} 没有 containers")


def set_image_and_rollout(
    run_remote: RemoteFn,
    *,
    namespace: str,
    deploy: str,
    container: str,
    image: str,
    timeout: str,
) -> tuple[int, str, str]:
    ns = shlex.quote(namespace)
    name = shlex.quote(deploy)
    wait = shlex.quote(timeout)
    set_cmd = (
        f"kubectl -n {ns} set image deploy/{name} {shlex.quote(container)}={shlex.quote(image)} && "
        f"kubectl -n {ns} rollout status deploy/{name} --timeout={wait} && "
        f"kubectl -n {ns} get deploy {name} -o wide && "
        f"kubectl -n {ns} get pods -o wide | grep -i {shlex.quote(deploy)} | head -20"
    )
    return run_remote(set_cmd, rollout_timeout_seconds(timeout) + 60)


PARALLEL_RELEASE_LABEL = "robot-ci.io/release"
PARALLEL_SOURCE_LABEL = "robot-ci.io/source-workload"


def parallel_deployment_name(source: str, release_id: str) -> str:
    """Build a deterministic DNS label no longer than a Deployment name permits."""
    base = re.sub(r"[^a-z0-9-]+", "-", str(source or "").lower()).strip("-")
    match = re.search(r"(20\d{10})", str(release_id or ""))
    token = match.group(1) if match else time.strftime("%Y%m%d%H%M")
    if not base or not token:
        raise CceRolloutError("无法生成新版本负载名称")
    suffix = f"-v-{token}"
    return (base[: 63 - len(suffix)].rstrip("-") + suffix)[:63]


def deployment_manifest_for_parallel_release(
    source_payload: Mapping[str, Any],
    *,
    source_name: str,
    base_name: str = "",
    release_id: str,
    image: str,
    container: str,
    replicas: int = 1,
) -> dict[str, Any]:
    """Clone a Deployment safely, adding a selector unique to this release.

    The stable Service continues to select the copied application labels.  The
    release label is deliberately *not* added to that Service's selector.
    """
    if not isinstance(source_payload.get("spec"), Mapping):
        raise CceRolloutError(f"Deployment {source_name} 返回的格式不正确")
    if not isinstance(replicas, int) or replicas < 0 or replicas > 1000:
        raise CceRolloutError("实例数必须是 0 到 1000 的整数")
    validate_image_ref(image)
    spec = copy.deepcopy(dict(source_payload["spec"]))
    template = spec.get("template") if isinstance(spec.get("template"), dict) else {}
    pod_spec = template.get("spec") if isinstance(template.get("spec"), dict) else {}
    containers = pod_spec.get("containers") if isinstance(pod_spec.get("containers"), list) else []
    if not any(isinstance(row, dict) and row.get("name") == container for row in containers):
        raise CceRolloutError(f"容器 {container} 不在 Deployment {source_name} 中")
    for row in containers:
        if isinstance(row, dict) and row.get("name") == container:
            row["image"] = image

    name = parallel_deployment_name(base_name or source_name, release_id)
    release = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(release_id)).strip("-_.")[:63]
    if not release:
        raise CceRolloutError("发布标识不合法")
    labels = copy.deepcopy((source_payload.get("metadata") or {}).get("labels") or {})
    labels[PARALLEL_RELEASE_LABEL] = release
    labels[PARALLEL_SOURCE_LABEL] = source_name
    pod_labels = copy.deepcopy((template.get("metadata") or {}).get("labels") or {})
    pod_labels[PARALLEL_RELEASE_LABEL] = release
    pod_labels[PARALLEL_SOURCE_LABEL] = source_name
    selector = copy.deepcopy(spec.get("selector") or {})
    match_labels = copy.deepcopy(selector.get("matchLabels") or {})
    match_labels[PARALLEL_RELEASE_LABEL] = release
    selector["matchLabels"] = match_labels
    spec["selector"] = selector
    template["metadata"] = {**copy.deepcopy(template.get("metadata") or {}), "labels": pod_labels}
    template["spec"] = pod_spec
    spec["template"] = template
    spec["replicas"] = replicas
    return {
        "apiVersion": str(source_payload.get("apiVersion") or "apps/v1"),
        "kind": "Deployment",
        "metadata": {
            "name": name,
            "namespace": str((source_payload.get("metadata") or {}).get("namespace") or ""),
            "labels": labels,
            "annotations": {
                PARALLEL_SOURCE_LABEL: source_name,
                PARALLEL_RELEASE_LABEL: release,
            },
        },
        "spec": spec,
    }


def deployment_summary(payload: Mapping[str, Any], *, name: str = "") -> dict[str, Any]:
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), Mapping) else {}
    spec = payload.get("spec") if isinstance(payload.get("spec"), Mapping) else {}
    status = payload.get("status") if isinstance(payload.get("status"), Mapping) else {}
    template = spec.get("template") if isinstance(spec.get("template"), Mapping) else {}
    labels = metadata.get("labels") if isinstance(metadata.get("labels"), Mapping) else {}
    pod_spec = template.get("spec") if isinstance(template.get("spec"), Mapping) else {}
    containers = pod_spec.get("containers") if isinstance(pod_spec.get("containers"), list) else []
    images = [str(row.get("image") or "") for row in containers if isinstance(row, Mapping) and str(row.get("image") or "")]
    return {
        "name": str(metadata.get("name") or name),
        "exists": True,
        "desired_replicas": int(spec.get("replicas") or 0),
        "ready_replicas": int(status.get("readyReplicas") or 0),
        "available_replicas": int(status.get("availableReplicas") or 0),
        "images": images,
        "release_id": str(labels.get(PARALLEL_RELEASE_LABEL) or ""),
        "source_workload": str(labels.get(PARALLEL_SOURCE_LABEL) or ""),
    }


def get_deployment_payload(run_remote: RemoteFn, *, namespace: str, deploy: str) -> dict[str, Any] | None:
    code, out, err = run_remote(
        f"kubectl -n {shlex.quote(namespace)} get deploy {shlex.quote(deploy)} -o json", 60
    )
    if code != 0:
        text = (err or out or "").lower()
        if "notfound" in text or "not found" in text:
            return None
        detail = (err or out or "").strip() or f"exit {code}"
        raise CceRolloutError(f"读取 Deployment {deploy} 失败: {detail}")
    try:
        payload = json.loads(out)
    except json.JSONDecodeError as exc:
        raise CceRolloutError(f"Deployment {deploy} 返回的不是 JSON") from exc
    if not isinstance(payload, dict):
        raise CceRolloutError(f"Deployment {deploy} 返回的不是对象")
    return payload


def list_namespace_deployments(run_remote: RemoteFn, *, namespace: str) -> list[dict[str, Any]]:
    code, out, err = run_remote(
        f"kubectl -n {shlex.quote(namespace)} get deploy -o json", 90
    )
    if code != 0:
        detail = (err or out or "").strip() or f"exit {code}"
        raise CceRolloutError(f"列出 Deployment 失败: {detail}")
    try:
        payload = json.loads(out)
    except json.JSONDecodeError as exc:
        raise CceRolloutError("Deployment 列表返回的不是 JSON") from exc
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _deployment_name(payload: Mapping[str, Any]) -> str:
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), Mapping) else {}
    return str(metadata.get("name") or "").strip()


def discover_related_deployments(
    run_remote: RemoteFn,
    *,
    namespace: str,
    baseline: str,
) -> list[dict[str, Any]]:
    """Approximate CCE Deployments related to a baseline workload name.

    Matches parallel-release naming (``baseline-v-*``) and the
    ``robot-ci.io/source-workload`` label. Exact baseline name is excluded —
    callers should already have tried that lookup.
    """
    base = str(baseline or "").strip()
    if not base:
        return []
    related: list[dict[str, Any]] = []
    for item in list_namespace_deployments(run_remote, namespace=namespace):
        name = _deployment_name(item)
        if not name or name == base:
            continue
        labels = ((item.get("metadata") or {}).get("labels") or {}) if isinstance(item.get("metadata"), Mapping) else {}
        source = str(labels.get(PARALLEL_SOURCE_LABEL) or "").strip()
        if source == base or name.startswith(base + "-v-"):
            related.append(item)
    related.sort(key=_deployment_name)
    return related


def resolve_source_deployment(
    run_remote: RemoteFn,
    *,
    namespace: str,
    active: str,
    baseline: str,
    log: Callable[[str], None] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Resolve the source Deployment for a release.

    Order:
    1. exact ``active_workload_name``
    2. exact baseline ``workload_name``
    3. approximate CCE query by baseline — use only when exactly one match
    """
    write = log or (lambda _line: None)
    active_name = str(active or "").strip()
    baseline_name = str(baseline or "").strip()
    tried: list[str] = []

    for name in (active_name, baseline_name):
        if not name or name in tried:
            continue
        tried.append(name)
        payload = get_deployment_payload(run_remote, namespace=namespace, deploy=name)
        if payload is not None:
            if active_name and name != active_name:
                write(f"活动负载 {active_name} 不存在，改用基准负载 {name}")
            return name, payload
        if name == active_name and baseline_name and baseline_name != active_name:
            write(f"活动负载 {active_name} 不存在，尝试基准负载 {baseline_name}")
        else:
            write(f"负载 {name} 不存在")

    discover_base = baseline_name or active_name
    if not discover_base:
        raise CceRolloutError("环境未配置负载名称，无法定位源负载")

    related = discover_related_deployments(run_remote, namespace=namespace, baseline=discover_base)
    names = [_deployment_name(item) for item in related]
    if len(related) == 1:
        name = names[0]
        write(f"基准负载不存在，近似查询唯一命中 {name}，用作源负载")
        return name, related[0]
    if len(related) > 1:
        raise CceRolloutError(
            f"活动负载与基准负载均不存在，按基准名 {discover_base} 近似查询到多个负载："
            f"{', '.join(names)}。请更新环境中的负载名称后重试"
        )

    hint = " / ".join(tried) if tried else discover_base
    raise CceRolloutError(
        f"源负载不存在（已查：{hint}），按基准名 {discover_base} 近似查询也无结果。"
        "请更新环境中的负载名称后重试"
    )


def create_parallel_deployment(
    run_remote: RemoteFn,
    *,
    namespace: str,
    source: str,
    base_name: str = "",
    release_id: str,
    image: str,
    container: str,
    replicas: int,
    timeout: str,
    source_payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload = dict(source_payload) if isinstance(source_payload, Mapping) else get_deployment_payload(
        run_remote, namespace=namespace, deploy=source
    )
    if payload is None:
        raise CceRolloutError(f"源 Deployment 不存在: {source}")
    manifest = deployment_manifest_for_parallel_release(
        payload, source_name=source, base_name=base_name, release_id=release_id,
        image=image, container=container, replicas=replicas,
    )
    name = str(manifest["metadata"]["name"])
    encoded = base64.b64encode(json.dumps(manifest, ensure_ascii=False).encode("utf-8")).decode("ascii")
    command = (
        f"printf %s {shlex.quote(encoded)} | base64 -d | kubectl -n {shlex.quote(namespace)} apply -f - && "
        f"kubectl -n {shlex.quote(namespace)} rollout status deploy/{shlex.quote(name)} "
        f"--timeout={shlex.quote(timeout)}"
    )
    code, out, err = run_remote(command, rollout_timeout_seconds(timeout) + 60)
    if code != 0:
        detail = (err or out or "").strip() or f"exit {code}"
        raise CceRolloutError(f"创建新版本负载 {name} 失败: {detail}")
    created = get_deployment_payload(run_remote, namespace=namespace, deploy=name)
    if created is None:
        raise CceRolloutError(f"新版本负载创建后未找到: {name}")
    return deployment_summary(created, name=name)


def scale_deployment(
    run_remote: RemoteFn, *, namespace: str, deploy: str, replicas: int
) -> dict[str, Any]:
    if not isinstance(replicas, int) or replicas < 0 or replicas > 1000:
        raise CceRolloutError("实例数必须是 0 到 1000 的整数")
    code, out, err = run_remote(
        f"kubectl -n {shlex.quote(namespace)} scale deploy/{shlex.quote(deploy)} --replicas={replicas}", 60
    )
    if code != 0:
        detail = (err or out or "").strip() or f"exit {code}"
        raise CceRolloutError(f"设置 {deploy} 实例数失败: {detail}")
    payload = get_deployment_payload(run_remote, namespace=namespace, deploy=deploy)
    if payload is None:
        raise CceRolloutError(f"设置实例数后未找到负载: {deploy}")
    return deployment_summary(payload)


def delete_deployment(run_remote: RemoteFn, *, namespace: str, deploy: str) -> None:
    code, out, err = run_remote(
        f"kubectl -n {shlex.quote(namespace)} delete deploy/{shlex.quote(deploy)} --wait=true", 180
    )
    if code != 0:
        detail = (err or out or "").strip() or f"exit {code}"
        raise CceRolloutError(f"删除旧版本负载 {deploy} 失败: {detail}")


def deploy_job_results(
    *,
    environment: Mapping[str, Any],
    results: list[Mapping[str, Any]],
    creds: Mapping[str, str],
    namespace: str = "default",
    rollout_timeout: str = "180s",
    hop: Callable[..., tuple[int, str, str]] | None = None,
    log: Callable[[str], None] | None = None,
    mode: str = "inplace",
    release_id: str = "",
    on_parallel_created: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[bool, str]:
    write = log or (lambda _line: None)
    jump_host = str(environment.get("jump_host") or "").strip()
    nodes = [str(item).strip() for item in (environment.get("nodes") or []) if str(item).strip()]
    workload = str(environment.get("active_workload_name") or environment.get("workload_name") or "").strip()
    base_workload = str(environment.get("workload_name") or workload).strip()
    env_name = str(environment.get("name") or workload or "environment")
    cluster = str(environment.get("cluster_name") or "").strip()
    if not jump_host:
        return False, "环境未配置跳板机"
    if not nodes:
        return False, "环境未配置 CCE 节点"
    if not workload:
        return False, "环境未配置负载名称"
    if not has_ssh_auth(creds):
        return False, (
            "未配置跳板机 SSH 凭据，请在 CI 的 config.json 设置 cce_ssh_password，"
            "或设置环境变量 CCE_SSH_PASSWORD / CCE_JUMP_PASSWORD"
        )
    if mode not in {"inplace", "parallel"}:
        return False, "不支持的部署方式"
    if mode == "parallel" and not release_id:
        return False, "平滑部署缺少发布标识"

    candidates: list[tuple[str, str]] = []
    for row in results:
        if not row.get("ok"):
            continue
        image = str(row.get("remote") or "").strip()
        if not image or image == "archive-only":
            continue
        candidates.append((str(row.get("service_id") or "").strip(), image))
    if not candidates:
        return False, "构建结果里没有可部署的 SWR 镜像（archive-only 不能上 CCE）"

    planned: list[tuple[str, str, str, str | None]] = []
    for service_id, image in candidates:
        if not should_rollout_result(service_id, image, workload, result_count=len(candidates)):
            write(f"gamma部署跳过 {service_id}: 镜像与负载 {workload} 不匹配")
            continue
        try:
            validate_image_ref(image)
            deploy, container = resolve_deploy_target(service_id, image, workload)
        except CceRolloutError as exc:
            return False, str(exc)
        planned.append((service_id, deploy, image, container))

    if not planned:
        return False, f"构建镜像与环境负载 {workload} 不匹配，未执行 kubectl"

    write(
        f"gamma部署 环境={env_name} 集群={cluster or '-'} 跳板={jump_host} "
        f"节点={','.join(nodes)} 负载={workload}"
    )

    def run_remote(command: str, timeout: int) -> tuple[int, str, str]:
        return exec_via_nodes(
            command,
            jump_host=jump_host,
            nodes=nodes,
            creds=creds,
            timeout=timeout,
            hop=hop,
        )

    for service_id, deploy, image, preferred in planned:
        write(f"gamma部署 {service_id} → deploy/{deploy} image={image} mode={mode}")
        try:
            source_name, source_payload = resolve_source_deployment(
                run_remote,
                namespace=namespace,
                active=deploy,
                baseline=base_workload,
                log=write,
            )
            if source_name != deploy:
                write(f"实际源负载切换为 deploy/{source_name}")
                deploy = source_name
            containers = containers_from_deploy_json(source_payload)
            if not containers:
                raise CceRolloutError(f"Deployment {deploy} 没有 containers")
            container = pick_container(containers, image=image, preferred=preferred)
            current = next((img for name, img in containers if name == container), "")
            if current:
                write(f"当前容器 {container}={current}")
            if mode == "parallel":
                write(f"创建新版本 Deployment，旧版本 {deploy} 保持不变")
                created = create_parallel_deployment(
                    run_remote,
                    namespace=namespace,
                    source=deploy,
                    base_name=base_workload,
                    release_id=release_id,
                    image=image,
                    container=container,
                    replicas=1,
                    timeout=rollout_timeout,
                    source_payload=source_payload,
                )
                created.update({
                    "service_id": service_id, "source_workload": deploy,
                    "container": container, "image": image,
                })
                if on_parallel_created:
                    on_parallel_created(created)
                write(
                    f"新版本负载已就绪 deploy/{created['name']} "
                    f"实例={created['desired_replicas']}"
                )
                continue
            write(f"kubectl set image deploy/{deploy} {container}={image}")
            code, out, err = set_image_and_rollout(
                run_remote,
                namespace=namespace,
                deploy=deploy,
                container=container,
                image=image,
                timeout=rollout_timeout,
            )
        except CceRolloutError as exc:
            return False, str(exc)
        text = ((out or "") + ("\n" + err if err and err.strip() else "")).strip()
        if text:
            for line in text.splitlines()[:80]:
                write(line)
        if code != 0:
            return False, f"kubectl 升级 {deploy} 失败 (exit {code})"
        write(f"gamma部署完成 {service_id} deploy/{deploy} container={container}")
    return True, ""
