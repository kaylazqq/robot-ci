"""Huawei Cloud CCE + Kubernetes API helpers (AK/SK signed requests)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from huawei_sdk_http import HuaweiCloudHttpError, request_json

IAM_ENDPOINT = "https://iam.myhuaweicloud.com"

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
    access_key: str
    secret_key: str
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


def _friendly_iam_message(message: str, detail: Any) -> str:
    code = ""
    if isinstance(detail, dict):
        code = str(detail.get("error_code") or detail.get("code") or "")
    text = (message or "").strip()
    if code == "APIGW.0301" or "Incorrect IAM authentication information" in text:
        return (
            "华为云 IAM 认证失败（AK/SK 无效或不匹配）。"
            "请使用控制台「我的凭证 → 访问密钥」中的 AK/SK，"
            "不是 SWR docker login 的临时用户名/密码。"
            "若连续输错多次，请等待 5 分钟后再试。"
        )
    return text or "Huawei Cloud API error"


def _map_http_error(exc: HuaweiCloudHttpError) -> HuaweiCCEError:
    return HuaweiCCEError(
        _friendly_iam_message(str(exc), exc.detail),
        status=exc.status,
        detail=exc.detail,
    )


def _api(method: str, url: str, access_key: str, secret_key: str, **kwargs: Any) -> tuple[int, dict[str, Any], dict[str, str]]:
    try:
        return request_json(method, url, access_key, secret_key, **kwargs)
    except HuaweiCloudHttpError as exc:
        raise _map_http_error(exc) from exc


def list_projects(access_key: str, secret_key: str, *, region: str) -> list[dict[str, str]]:
    region = normalize_region(region)
    _status, payload, _headers = _api(
        "GET",
        f"{IAM_ENDPOINT}/v3/projects?name={region}",
        access_key,
        secret_key,
        region=region,
    )
    items = payload.get("projects") if isinstance(payload.get("projects"), list) else []
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
    out.sort(key=lambda row: row.get("name") or "")
    if out:
        return out
    return [{"id": "", "name": region, "region": region}]


def resolve_auth_context(
    access_key: str,
    secret_key: str,
    *,
    region: str,
    project_id: str = "",
) -> AuthContext:
    ak = (access_key or "").strip()
    sk = (secret_key or "").strip()
    if not ak or not sk:
        raise HuaweiCCEError("access_key and secret_key are required", status=400)
    region = normalize_region(region)
    pid = (project_id or "").strip()
    pname = region
    if not pid:
        projects = list_projects(ak, sk, region=region)
        match = next(
            (row for row in projects if (row.get("name") or row.get("region") or "") == region),
            projects[0] if projects else None,
        )
        if match and (match.get("id") or "").strip():
            pid = str(match["id"]).strip()
            pname = str(match.get("name") or pname).strip()
    if not pid:
        raise HuaweiCCEError(
            "could not resolve project_id; pass project_id explicitly",
            status=400,
        )
    return AuthContext(access_key=ak, secret_key=sk, project_id=pid, project_name=pname, region=region)


def list_clusters(
    access_key: str,
    secret_key: str,
    *,
    region: str,
    project_id: str = "",
) -> dict[str, Any]:
    ctx = resolve_auth_context(access_key, secret_key, region=region, project_id=project_id)
    url = f"{cce_base_url(region)}/api/v3/projects/{ctx.project_id}/clusters?detail=true"
    _status, payload, _headers = _api(
        "GET",
        url,
        ctx.access_key,
        ctx.secret_key,
        project_id=ctx.project_id,
        region=ctx.region,
    )
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
    ctx = resolve_auth_context(access_key, secret_key, region=region, project_id=project_id)
    _collection, list_path = _workload_paths(kind, namespace)
    url = f"{k8s_gateway_base_url(region, cluster_id)}{list_path}"
    _status, payload, _headers = _api(
        "GET",
        url,
        ctx.access_key,
        ctx.secret_key,
        project_id=ctx.project_id,
        region=ctx.region,
    )
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
    ctx = resolve_auth_context(access_key, secret_key, region=region, project_id=project_id)
    _collection, detail_path = _workload_paths(kind, namespace, workload_name)
    url = f"{k8s_gateway_base_url(region, cluster_id)}{detail_path}"
    _status, payload, _headers = _api(
        "GET",
        url,
        ctx.access_key,
        ctx.secret_key,
        project_id=ctx.project_id,
        region=ctx.region,
    )
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
