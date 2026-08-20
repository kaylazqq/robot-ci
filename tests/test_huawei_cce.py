"""Tests for Huawei CCE API helper module."""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import huawei_cce


class _MockResp:
    def __init__(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self._body = body
        self.headers = headers or {"Content-Type": "application/json"}

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_MockResp":
        return self

    def __exit__(self, *args: object) -> None:
        return None


class HuaweiCCETests(unittest.TestCase):
    def test_normalize_region_rejects_unknown(self) -> None:
        with self.assertRaises(huawei_cce.HuaweiCCEError):
            huawei_cce.normalize_region("invalid-region")

    def test_auth_token_parses_project(self) -> None:
        token_body = {
            "token": {
                "project": {"id": "proj-1", "name": "cn-southwest-2"},
                "user": {"domain": {"id": "dom-1", "name": "example"}},
            }
        }

        def fake_urlopen(req, timeout=0):  # noqa: ARG001
            payload = json.dumps(token_body).encode("utf-8")
            return _MockResp(201, payload, {"Content-Type": "application/json", "X-Subject-Token": "token-abc"})

        with patch("huawei_cce.urlopen", side_effect=fake_urlopen):
            ctx = huawei_cce.auth_token("ak", "sk", region="cn-southwest-2")
        self.assertEqual("token-abc", ctx.token)
        self.assertEqual("proj-1", ctx.project_id)
        self.assertEqual("cn-southwest-2", ctx.project_name)

    def test_list_clusters_maps_items(self) -> None:
        calls: list[str] = []

        def fake_urlopen(req, timeout=0):  # noqa: ARG001
            calls.append(req.full_url)
            if req.full_url.endswith("/v3/auth/tokens"):
                payload = json.dumps(
                    {"token": {"project": {"id": "proj-1", "name": "cn-southwest-2"}}}
                ).encode("utf-8")
                return _MockResp(201, payload, {"X-Subject-Token": "token-abc"})
            cluster_payload = json.dumps(
                {
                    "items": [
                        {
                            "metadata": {"uid": "cluster-uid", "name": "prod", "creationTimestamp": "2026-01-01T00:00:00Z"},
                            "spec": {"version": "v1.29", "type": "VirtualMachine"},
                            "status": {"phase": "Available"},
                        }
                    ]
                }
            ).encode("utf-8")
            return _MockResp(200, cluster_payload)

        with patch("huawei_cce.urlopen", side_effect=fake_urlopen):
            result = huawei_cce.list_clusters("ak", "sk", region="cn-southwest-2")
        self.assertEqual(1, result["count"])
        self.assertEqual("cluster-uid", result["clusters"][0]["id"])
        self.assertEqual("prod", result["clusters"][0]["name"])
        self.assertTrue(any("/api/v3/projects/proj-1/clusters" in url for url in calls))

    def test_list_workloads_summarizes_deployment(self) -> None:
        def fake_urlopen(req, timeout=0):  # noqa: ARG001
            if req.full_url.endswith("/v3/auth/tokens"):
                payload = json.dumps(
                    {"token": {"project": {"id": "proj-1", "name": "cn-southwest-2"}}}
                ).encode("utf-8")
                return _MockResp(201, payload, {"X-Subject-Token": "token-abc"})
            workload_payload = json.dumps(
                {
                    "items": [
                        {
                            "metadata": {"name": "agent-link", "uid": "uid-1", "creationTimestamp": "2026-01-02T00:00:00Z"},
                            "spec": {
                                "replicas": 1,
                                "strategy": {"type": "RollingUpdate"},
                                "template": {
                                    "spec": {
                                        "containers": [{"name": "c1", "image": "swr.example/agent-link:v1"}]
                                    }
                                },
                            },
                            "status": {"readyReplicas": 1, "availableReplicas": 1},
                        }
                    ]
                }
            ).encode("utf-8")
            return _MockResp(200, workload_payload)

        with patch("huawei_cce.urlopen", side_effect=fake_urlopen):
            result = huawei_cce.list_workloads(
                "ak",
                "sk",
                region="cn-southwest-2",
                cluster_id="cluster-uid",
            )
        self.assertEqual(1, result["count"])
        self.assertEqual("agent-link", result["workloads"][0]["name"])
        self.assertEqual(["swr.example/agent-link:v1"], result["workloads"][0]["images"])


if __name__ == "__main__":
    unittest.main()
