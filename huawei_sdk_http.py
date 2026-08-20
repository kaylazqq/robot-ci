"""Huawei Cloud HTTP calls via huaweicloudsdkcore (AK/SK signing)."""
from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urlparse

from huaweicloudsdkcore.auth.credentials import BasicCredentials
from huaweicloudsdkcore.exceptions.exception_handler import DefaultExceptionHandler
from huaweicloudsdkcore.exceptions.exceptions import ServiceResponseException
from huaweicloudsdkcore.http.http_client import HttpClient
from huaweicloudsdkcore.http.http_config import HttpConfig
from huaweicloudsdkcore.http.http_handler import HttpHandler
from huaweicloudsdkcore.sdk_request import SdkRequest


class HuaweiCloudHttpError(RuntimeError):
    def __init__(self, message: str, *, status: int = 401, detail: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.detail = detail


_USER_AGENT = "robot-ci/1.0"
_LOGGER = logging.getLogger("robot-ci.huawei")


def _http_client(timeout: int = 30) -> HttpClient:
    config = HttpConfig.get_default_config()
    config.user_agent = _USER_AGENT
    config.timeout = timeout
    return HttpClient(config, HttpHandler(), DefaultExceptionHandler(), _LOGGER)


def _parse_url(url: str) -> tuple[str, str, str, list[tuple[str, str]]]:
    parsed = urlparse(url)
    query: list[tuple[str, str]] = []
    if parsed.query:
        for part in parsed.query.split("&"):
            if not part:
                continue
            if "=" in part:
                key, value = part.split("=", 1)
            else:
                key, value = part, ""
            query.append((key, value))
    return parsed.scheme, parsed.netloc, parsed.path, query


def _credentials(
    access_key: str,
    secret_key: str,
    *,
    project_id: str = "",
    region: str = "",
    client: HttpClient | None = None,
) -> BasicCredentials:
    ak = (access_key or "").strip()
    sk = (secret_key or "").strip()
    if not ak or not sk:
        raise HuaweiCloudHttpError("access_key and secret_key are required", status=400)
    creds = BasicCredentials(ak, sk, (project_id or "").strip() or None)
    if not creds.project_id and (region or "").strip():
        creds.process_auth_params(client or _http_client(), region.strip())
    return creds


def request_json(
    method: str,
    url: str,
    access_key: str,
    secret_key: str,
    *,
    project_id: str = "",
    region: str = "",
    body: dict[str, Any] | None = None,
    timeout: int = 30,
) -> tuple[int, dict[str, Any], dict[str, str]]:
    client = _http_client(timeout=timeout)
    creds = _credentials(
        access_key,
        secret_key,
        project_id=project_id,
        region=region,
        client=client,
    )
    schema, host, path, query = _parse_url(url)
    header_params: dict[str, str] = {"User-Agent": _USER_AGENT}
    payload = json.dumps(body, ensure_ascii=False) if body is not None else None
    if payload is not None:
        header_params["Content-Type"] = "application/json;charset=UTF-8"

    req = SdkRequest(
        method=method.upper(),
        schema=schema,
        host=host,
        resource_path=path,
        query_params=query,
        header_params=header_params,
        body=payload,
    )
    signed = creds.sign_request(req)
    try:
        resp = client.do_request_sync(signed)
    except ServiceResponseException as exc:
        detail: Any = exc.detail if hasattr(exc, "detail") else None
        if detail is None and exc.error_msg:
            try:
                detail = json.loads(exc.error_msg)
            except json.JSONDecodeError:
                detail = {"error_msg": exc.error_msg}
        if detail is None:
            detail = {"error_msg": str(exc.error_msg or exc), "error_code": getattr(exc, "error_code", "")}
        elif isinstance(detail, dict) and getattr(exc, "error_code", None):
            detail.setdefault("error_code", exc.error_code)
        message = str(exc.error_msg or exc)
        if isinstance(detail, dict):
            message = str(detail.get("error_msg") or detail.get("message") or message)
        raise HuaweiCloudHttpError(message, status=int(getattr(exc, "status_code", None) or 401), detail=detail) from exc

    raw = resp.content.decode("utf-8", errors="replace") if resp.content else ""
    parsed = json.loads(raw) if raw.strip() else {}
    hdrs = {k.lower(): v for k, v in (resp.headers or {}).items()}
    return int(resp.status_code), parsed if isinstance(parsed, dict) else {"data": parsed}, hdrs
