"""Tests for Huawei CCE API helper module."""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import huawei_cce


class HuaweiCCETests(unittest.TestCase):
    def test_normalize_region_rejects_unknown(self) -> None:
        with self.assertRaises(huawei_cce.HuaweiCCEError):
            huawei_cce.normalize_region("invalid-region")

    def test_resolve_auth_context_uses_project_list(self) -> None:
        def fake_api(method, url, access_key, secret_key, **kwargs):  # noqa: ARG001
            if url.endswith("/v3/projects?name=cn-southwest-2"):
                payload = {"projects": [{"id": "proj-1", "name": "cn-southwest-2"}]}
                return 200, payload, {}
            raise AssertionError(f"unexpected url: {url}")

        with patch("huawei_cce._api", side_effect=fake_api):
            ctx = huawei_cce.resolve_auth_context("ak", "sk", region="cn-southwest-2")
        self.assertEqual("proj-1", ctx.project_id)
        self.assertEqual("cn-southwest-2", ctx.project_name)

    def test_list_clusters_maps_items(self) -> None:
        calls: list[str] = []

        def fake_api(method, url, access_key, secret_key, **kwargs):  # noqa: ARG001
            calls.append(url)
            if "/v3/projects?" in url:
                return 200, {"projects": [{"id": "proj-1", "name": "cn-southwest-2"}]}, {}
            return 200, {
                "items": [
                    {
                        "metadata": {
                            "uid": "cluster-uid",
                            "name": "prod",
                            "creationTimestamp": "2026-01-01T00:00:00Z",
                        },
                        "spec": {"version": "v1.29", "type": "VirtualMachine"},
                        "status": {"phase": "Available"},
                    }
                ]
            }, {}

        with patch("huawei_cce._api", side_effect=fake_api):
            result = huawei_cce.list_clusters("ak", "sk", region="cn-southwest-2")
        self.assertEqual(1, result["count"])
        self.assertEqual("cluster-uid", result["clusters"][0]["id"])
        self.assertTrue(any("/api/v3/projects/proj-1/clusters" in url for url in calls))

    def test_list_workloads_summarizes_deployment(self) -> None:
        def fake_api(method, url, access_key, secret_key, **kwargs):  # noqa: ARG001
            if "/v3/projects?" in url:
                return 200, {"projects": [{"id": "proj-1", "name": "cn-southwest-2"}]}, {}
            return 200, {
                "items": [
                    {
                        "metadata": {
                            "name": "agent-link",
                            "uid": "uid-1",
                            "creationTimestamp": "2026-01-02T00:00:00Z",
                        },
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
            }, {}

        with patch("huawei_cce._api", side_effect=fake_api):
            result = huawei_cce.list_workloads(
                "ak",
                "sk",
                region="cn-southwest-2",
                cluster_id="cluster-uid",
            )
        self.assertEqual(1, result["count"])
        self.assertEqual("agent-link", result["workloads"][0]["name"])


if __name__ == "__main__":
    unittest.main()
