"""Huawei Cloud CCE + Kubernetes API helpers (AK/SK → IAM token → cluster/workload APIs)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

IAM_ENDPOINT = "https://iam.myhuaweicloud.com"

# Common Huawei Cloud regions (extend as needed).
REGIONS: dict[str, dict[str, str]] = {
    "cn-southwest-2": {"label": "西南-贵阳一", "cce_endpoint": "cce.cn-southwest-2.myhuaweicloud.com"},
    "cn-north-4": {"label": "华北-北京四", "cce_endpoint": "cce.cn-north-4.myhuaweicloud.com"},
    "cn-east-3": {"label": "华东-上海一", "cce_endpoint": "cce.cn-east-3.myhuaweicloud.com"},
    "cn-south-1": {"label": "华南-广州", "cce_endpoint": "cce.cn-south-1.myhuaweicloud.com"},
}

WORKLOAD_KINDS: dict[str, dict[str, str]] = {
    "deployments": {
        "api_version": "apps/v1",
        "collection": "deployments",
        "label": "无状态负载 (Deployment)",
    },
    "statefulsets": {
        "api_version": "apps/v1",
        "collection": "statefulsets",
        "label": "有状态负载 (StatefulSet)",
    },
    "daemonsets": {
        "api_version": "apps/v1",
        "collection": "daemonsets",
        "label": "守护进程集 (DaemonSet)",
    },
}


class HuaweiCCEError(RuntimeError):
    def __init__(self, message: str, *, status: int = 400, detail: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class AuthContext:
    token: str
    project_id: str
    project_name: str
    region: str


def normalize_region(region: str) -> str:
    value = (region or "").strip()
    if not value:
        raise HuaweiCCEError("region is required", status=400)
    if value not in REGIONS:
        known = ", ".join(sorted(REGIONS))
        raise HuaweiCCEError(f"unsupported region: {value}; known: {known}", status=400)
    return value


def cce_base_url(region: str) -> str:
    normalize_region(region)
    return f"https://{REGIONS[region]['cce_endpoint']}"


def k8s_gateway_base_url(region: str, cluster_id: str) -> str:
    region = normalize_region(region)
    cluster_id = (cluster_id or "").strip()
    if not cluster_id:
        raise HuaweiCCEError("cluster_id is required", status=400)
    host = REGIONS[region]["cce_endpoint"]
    return f"https://{cluster_id}.{host}"


def _request_json(
    method: str,
    url: str,
    *,
    token: str | None = None,
    body: dict[str, Any] | None = None,
    timeout: int = 30,
) -> tuple[int, dict[str, Any], dict[str, str]]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["X-Auth-Token"] = token
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            payload = json.loads(raw) if raw.strip() else {}
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
            return int(resp.status), payload if isinstance(payload, dict) else {"data": payload}, hdrs
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            payload = {"error": raw[:500]}
        if not isinstance(payload, dict):
            payload = {"error": str(payload)}
        message = (
            str(payload.get("error_msg") or payload.get("message") or payload.get("error") or exc.reason)
            or f"HTTP {exc.code}"
        )
        raise HuaweiCCEError(message, status=int(exc.code), detail=payload) from exc
    except json.JSONDecodeError as exc:
        raise HuaweiCCEError("invalid JSON response from upstream", status=502) from exc


def auth_token(
    access_key: str,
    secret_key: str,
    *,
    region: str,
    project_id: str = "",
    project_name: str = "",
) -> AuthContext:
    ak = (access_key or "").strip()
    sk = (secret_key or "").strip()
    if not ak or not sk:
        raise HuaweiCCEError("access_key and secret_key are required", status=400)
    region = normalize_region(region)

    scope: dict[str, Any]
    if (project_id or "").strip():
        scope = {"project": {"id": project_id.strip()}}
    elif (project_name or "").strip():
        scope = {"project": {"name": project_name.strip()}}
    else:
        # Most tenant projects are named after the region id (e.g. cn-southwest-2).
        scope = {"project": {"name": region}}

    body = {
        "auth": {
            "identity": {
                "methods": ["ak_sk"],
                "ak_sk": {
                    "access": {"key": ak},
                    "secret": {"key": sk},
                },
            },
            "scope": scope,
        }
    }
    _status, payload, headers = _request_json(
        "POST",
        f"{IAM_ENDPOINT}/v3/auth/tokens",
        body=body,
        timeout=30,
    )
    token = headers.get("x-subject-token") or headers.get("X-Subject-Token".lower())
    if not token:
        raise HuaweiCCEError("IAM did not return X-Subject-Token", status=502)

    project = payload.get("token", {}).get("project") if isinstance(payload.get("token"), dict) else {}
    pid = str((project or {}).get("id") or project_id or "").strip()
    pname = str((project or {}).get("name") or project_name or region).strip()
    if not pid:
        raise HuaweiCCEError(
            "could not resolve project_id; pass project_id explicitly",
            status=400,
            detail=payload,
        )
    return AuthContext(token=token, project_id=pid, project_name=pname, region=region)


def _domain_scoped_token(access_key: str, secret_key: str, domain_id: str) -> str:
    body = {
        "auth": {
            "identity": {
                "methods": ["ak_sk"],
                "ak_sk": {
                    "access": {"key": access_key.strip()},
                    "secret": {"key": secret_key.strip()},
                },
            },
            "scope": {"domain": {"id": domain_id.strip()}},
        }
    }
    _status, _payload, headers = _request_json("POST", f"{IAM_ENDPOINT}/v3/auth/tokens", body=body)
    token = headers.get("x-subject-token") or ""
    if not token:
        raise HuaweiCCEError("IAM did not return domain-scoped token", status=502)
    return token


def list_projects(access_key: str, secret_key: str, *, region: str) -> list[dict[str, str]]:
    region = normalize_region(region)
    _status, payload, _headers = _request_json(
        "POST",
        f"{IAM_ENDPOINT}/v3/auth/tokens",
        body={
            "auth": {
                "identity": {
                    "methods": ["ak_sk"],
                    "ak_sk": {
                        "access": {"key": access_key.strip()},
                        "secret": {"key": secret_key.strip()},
                    },
                },
                "scope": {"project": {"name": region}},
            }
        },
    )
    token_obj = payload.get("token") if isinstance(payload.get("token"), dict) else {}
    project = token_obj.get("project") if isinstance(token_obj.get("project"), dict) else {}
    user = token_obj.get("user") if isinstance(token_obj.get("user"), dict) else {}
    domain = user.get("domain") if isinstance(user.get("domain"), dict) else {}
    domain_id = str(domain.get("id") or "").strip()
    fallback = [
        {
            "id": str(project.get("id") or ""),
            "name": str(project.get("name") or region),
            "region": str(project.get("name") or region),
        }
    ]
    if not domain_id:
        return fallback

    domain_token = _domain_scoped_token(access_key, secret_key, domain_id)
    _status, projects_payload, _headers = _request_json(
        "GET",
        f"{IAM_ENDPOINT}/v3/projects",
        token=domain_token,
    )
    items = projects_payload.get("projects") if isinstance(projects_payload.get("projects"), list) else []
    out: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "id": str(item.get("id") or ""),
                "name": str(item.get("name") or ""),
                "region": str(item.get("name") or ""),
            }
        )
    if not out:
        return fallback
    out.sort(key=lambda row: row.get("name") or "")
    return out


def list_clusters(
    access_key: str,
    secret_key: str,
    *,
    region: str,
    project_id: str = "",
) -> dict[str, Any]:
    ctx = auth_token(access_key, secret_key, region=region, project_id=project_id)
    url = f"{cce_base_url(region)}/api/v3/projects/{ctx.project_id}/clusters?detail=true"
    _status, payload, _headers = _request_json("GET", url, token=ctx.token)
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    clusters: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        spec = item.get("spec") if isinstance(item.get("spec"), dict) else {}
        status = item.get("status") if isinstance(item.get("status"), dict) else {}
        clusters.append(
            {
                "id": str(metadata.get("uid") or ""),
                "name": str(metadata.get("name") or ""),
                "cluster_type": str(spec.get("type") or spec.get("clusterType") or ""),
                "version": str(spec.get("version") or ""),
                "status": str(status.get("phase") or status.get("status") or ""),
                "region": region,
                "project_id": ctx.project_id,
                "created_at": str(metadata.get("creationTimestamp") or ""),
            }
        )
    clusters.sort(key=lambda row: row.get("name") or "")
    return {
        "region": region,
        "project_id": ctx.project_id,
        "project_name": ctx.project_name,
        "count": len(clusters),
        "clusters": clusters,
    }


def _workload_paths(kind: str, namespace: str, name: str = "") -> tuple[str, str]:
    kind_norm = (kind or "deployments").strip().lower()
    meta = WORKLOAD_KINDS.get(kind_norm)
    if not meta:
        known = ", ".join(sorted(WORKLOAD_KINDS))
        raise HuaweiCCEError(f"unsupported workload kind: {kind_norm}; known: {known}", status=400)
    ns = (namespace or "default").strip() or "default"
    base = f"/apis/{meta['api_version']}/namespaces/{ns}/{meta['collection']}"
    if name:
        return base, f"{base}/{name}"
    return base, base


def _container_images(template: dict[str, Any]) -> list[str]:
    spec = template.get("spec") if isinstance(template.get("spec"), dict) else {}
    containers = spec.get("containers") if isinstance(spec.get("containers"), list) else []
    images: list[str] = []
    for container in containers:
        if not isinstance(container, dict):
            continue
        image = str(container.get("image") or "").strip()
        if image:
            images.append(image)
    return images


def summarize_workload(kind: str, item: dict[str, Any], *, namespace: str) -> dict[str, Any]:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    spec = item.get("spec") if isinstance(item.get("spec"), dict) else {}
    status = item.get("status") if isinstance(item.get("status"), dict) else {}
    template = spec.get("template") if isinstance(spec.get("template"), dict) else {}
    strategy = spec.get("strategy") if isinstance(spec.get("strategy"), dict) else {}
    return {
        "kind": kind,
        "namespace": namespace,
        "name": str(metadata.get("name") or ""),
        "uid": str(metadata.get("uid") or ""),
        "labels": metadata.get("labels") if isinstance(metadata.get("labels"), dict) else {},
        "replicas": spec.get("replicas"),
        "ready_replicas": status.get("readyReplicas"),
        "available_replicas": status.get("availableReplicas"),
        "updated_replicas": status.get("updatedReplicas"),
        "images": _container_images(template),
        "strategy": strategy.get("type") or "",
        "created_at": str(metadata.get("creationTimestamp") or ""),
        "conditions": status.get("conditions") if isinstance(status.get("conditions"), list) else [],
    }


def list_workloads(
    access_key: str,
    secret_key: str,
    *,
    region: str,
    cluster_id: str,
    project_id: str = "",
    namespace: str = "default",
    kind: str = "deployments",
) -> dict[str, Any]:
    ctx = auth_token(access_key, secret_key, region=region, project_id=project_id)
    _collection, list_path = _workload_paths(kind, namespace)
    url = f"{k8s_gateway_base_url(region, cluster_id)}{list_path}"
    _status, payload, _headers = _request_json("GET", url, token=ctx.token)
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    workloads = [summarize_workload(kind, item, namespace=namespace) for item in items if isinstance(item, dict)]
    workloads.sort(key=lambda row: row.get("name") or "")
    return {
        "region": region,
        "project_id": ctx.project_id,
        "cluster_id": cluster_id,
        "namespace": namespace,
        "kind": kind,
        "count": len(workloads),
        "workloads": workloads,
    }


def get_workload_detail(
    access_key: str,
    secret_key: str,
    *,
    region: str,
    cluster_id: str,
    name: str,
    project_id: str = "",
    namespace: str = "default",
    kind: str = "deployments",
) -> dict[str, Any]:
    workload_name = (name or "").strip()
    if not workload_name:
        raise HuaweiCCEError("workload name is required", status=400)
    ctx = auth_token(access_key, secret_key, region=region, project_id=project_id)
    _collection, detail_path = _workload_paths(kind, namespace, workload_name)
    url = f"{k8s_gateway_base_url(region, cluster_id)}{detail_path}"
    _status, payload, _headers = _request_json("GET", url, token=ctx.token)
    if not isinstance(payload, dict):
        raise HuaweiCCEError("unexpected workload detail payload", status=502)
    summary = summarize_workload(kind, payload, namespace=namespace)
    spec = payload.get("spec") if isinstance(payload.get("spec"), dict) else {}
    template = spec.get("template") if isinstance(spec.get("template"), dict) else {}
    pod_spec = template.get("spec") if isinstance(template.get("spec"), dict) else {}
    containers = pod_spec.get("containers") if isinstance(pod_spec.get("containers"), list) else []
    container_details: list[dict[str, Any]] = []
    for container in containers:
        if not isinstance(container, dict):
            continue
        container_details.append(
            {
                "name": str(container.get("name") or ""),
                "image": str(container.get("image") or ""),
                "image_pull_policy": str(container.get("imagePullPolicy") or ""),
                "resources": container.get("resources") if isinstance(container.get("resources"), dict) else {},
                "env_count": len(container.get("env") or []) if isinstance(container.get("env"), list) else 0,
            }
        )
    return {
        "region": region,
        "project_id": ctx.project_id,
        "cluster_id": cluster_id,
        "namespace": namespace,
        "kind": kind,
        "summary": summary,
        "selector": spec.get("selector") if isinstance(spec.get("selector"), dict) else {},
        "strategy": spec.get("strategy") if isinstance(spec.get("strategy"), dict) else {},
        "containers": container_details,
        "raw": payload,
    }
