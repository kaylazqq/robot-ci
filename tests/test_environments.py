import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import server


class EnvironmentApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "robot-ci.db"
        self.users_path = Path(self.tmp.name) / "users.json"
        self.db_patch = patch.object(server, "DB_PATH", self.db_path)
        self.users_patch = patch.object(server, "USERS_PATH", self.users_path)
        self.cfg_port_patch = patch.dict(server.CFG, {"port": 18889})
        self.db_patch.start()
        self.users_patch.start()
        self.cfg_port_patch.start()
        with server._sessions_lock:
            server._sessions.clear()
        server.ensure_default_users()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.httpd.server_port}"
        self.opener = build_opener(ProxyHandler({}))

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        self.users_patch.stop()
        self.db_patch.stop()
        self.cfg_port_patch.stop()
        self.tmp.cleanup()

    def _open(self, path: str, data=None, method="GET", cookie=""):
        headers = {"Content-Type": "application/json"}
        if cookie:
            headers["Cookie"] = cookie
        request = Request(self.base_url + path, data=data, headers=headers, method=method)
        return self.opener.open(request, timeout=2)

    def _login(self) -> str:
        with self._open(
            "/api/auth/login",
            data=json.dumps(
                {"username": server.DEFAULT_USERNAME, "password": server.DEFAULT_PASSWORD}
            ).encode(),
            method="POST",
        ) as response:
            return (response.headers.get("Set-Cookie") or "").split(";", 1)[0]

    def test_environment_crud_and_guiyang_region(self) -> None:
        cookie = self._login()
        with self._open("/api/permissions?service_id=echoagent-router", cookie=cookie) as response:
            permissions = json.loads(response.read().decode())
        self.assertEqual(
            ["production_release", "environment_manage"],
            [item["permission_id"] for item in permissions["permissions"]],
        )
        self.assertIn(server.DEFAULT_USERNAME, permissions["accounts"])
        with self._open("/api/environments", cookie=cookie) as response:
            empty = json.loads(response.read().decode())
        self.assertEqual([], empty["environments"])
        self.assertEqual([{"id": "cn-southwest-2", "label": "贵阳一"}], empty["regions"])

        payload = {
            "name": "联调",
            "service_id": "echoagent-router",
            "region": "cn-southwest-2",
            "cluster_name": "AgentPlatform",
            "workload_name": "echoagent-router",
            "namespace": "echo-prod",
            "kubeconfig_path": "/etc/robot-ci/kubeconfigs/dev2.yaml",
            "jump_host": "root@122.9.139.49",
            "jump_password": "jump-secret",
            "node_password": "node-secret",
            "nodes": ["172.31.8.33", "172.31.22.203"],
        }
        with self._open(
            "/api/environments",
            data=json.dumps(payload).encode(),
            method="POST",
            cookie=cookie,
        ) as response:
            created = json.loads(response.read().decode())["environment"]
        self.assertEqual("联调", created["name"])
        self.assertEqual("echoagent-router", created["service_id"])
        self.assertEqual("贵阳一", created["region_label"])
        self.assertEqual(["172.31.8.33", "172.31.22.203"], created["nodes"])
        self.assertEqual("echo-prod", created["namespace"])
        self.assertEqual("/etc/robot-ci/kubeconfigs/dev2.yaml", created["kubeconfig_path"])
        self.assertNotIn("test_base_url", created)
        self.assertTrue(created["has_jump_password"])
        self.assertTrue(created["has_node_password"])
        self.assertNotIn("jump_password", created)
        self.assertNotIn("node_password", created)

        created["workload_name"] = "service-router"
        with self._open(
            "/api/environments/" + created["id"],
            data=json.dumps(created).encode(),
            method="POST",
            cookie=cookie,
        ) as response:
            updated = json.loads(response.read().decode())["environment"]
        self.assertEqual("service-router", updated["workload_name"])
        self.assertEqual("echoagent-router", updated["service_id"])
        self.assertNotIn("jump_password", updated)
        self.assertNotIn("node_password", updated)
        stored = server.get_environment(created["id"], include_secrets=True)
        self.assertEqual("jump-secret", stored["jump_password"])
        self.assertEqual("node-secret", stored["node_password"])

        with self._open("/api/environments", cookie=cookie) as response:
            listed = json.loads(response.read().decode())["environments"]
        self.assertEqual(1, len(listed))
        self.assertEqual("echoagent-router", listed[0]["service_id"])
        with self._open("/api/environments?service_id=echoagent-router", cookie=cookie) as response:
            scoped = json.loads(response.read().decode())["environments"]
        self.assertEqual(1, len(scoped))
        with self._open("/api/environments?service_id=org-admin", cookie=cookie) as response:
            other = json.loads(response.read().decode())["environments"]
        self.assertEqual([], other)
        self.assertNotIn("jump_password", listed[0])
        self.assertNotIn("node_password", listed[0])

        with self._open(
            "/api/environments/" + created["id"] + "/delete",
            data=b"{}",
            method="POST",
            cookie=cookie,
        ) as response:
            self.assertTrue(json.loads(response.read().decode())["ok"])

        with self._open("/api/environments", cookie=cookie) as response:
            after = json.loads(response.read().decode())
        self.assertEqual([], after["environments"])

    def test_deprecated_gamma_test_base_url_is_ignored(self) -> None:
        cookie = self._login()
        payload = {
            "name": "gamma", "service_id": "echoagent-router", "region": "cn-southwest-2",
            "cluster_name": "cluster", "workload_name": "echoagent-router", "jump_host": "jump",
            "jump_password": "jump-secret", "node_password": "node-secret", "nodes": [],
            "test_base_url": "http://semantic-schedule:8766/",
        }
        with self._open("/api/environments", data=json.dumps(payload).encode(), method="POST", cookie=cookie) as response:
            created = json.loads(response.read().decode())["environment"]
        self.assertNotIn("test_base_url", created)

    def test_runtime_config_crud_is_ci_only_and_scoped_to_environment(self) -> None:
        cookie = self._login()
        payload = {
            "name": "dev2", "service_id": "echoagent-router", "region": "cn-southwest-2",
            "cluster_name": "cluster", "workload_name": "echoagent-router", "jump_host": "jump",
            "jump_password": "jump-secret", "nodes": [],
        }
        with self._open(
            "/api/environments", data=json.dumps(payload).encode(), method="POST", cookie=cookie
        ) as response:
            env_id = json.loads(response.read().decode())["environment"]["id"]

        with self._open(
            f"/api/environments/{env_id}/runtime-config/variables",
            data=json.dumps({"name": "AGENT_MCP_PUBLIC_URL", "value": "http://example.test/api/mcp"}).encode(),
            method="POST", cookie=cookie,
        ) as response:
            created = json.loads(response.read().decode())
        self.assertTrue(created["ok"])
        self.assertEqual("AGENT_MCP_PUBLIC_URL", created["variables"][0]["name"])
        first_revision = created["revision"]

        with self._open(
            f"/api/environments/{env_id}/runtime-config/variables",
            data=json.dumps({"name": "AGENT_MCP_PUBLIC_URL", "value": "http://example.test/v2/mcp"}).encode(),
            method="POST", cookie=cookie,
        ) as response:
            updated = json.loads(response.read().decode())
        self.assertNotEqual(first_revision, updated["revision"])
        self.assertEqual("http://example.test/v2/mcp", updated["variables"][0]["value"])

        with self._open(f"/api/environments/{env_id}/runtime-config", cookie=cookie) as response:
            current = json.loads(response.read().decode())
        self.assertEqual(updated["revision"], current["revision"])
        self.assertEqual(1, len(current["variables"]))

        with self.assertRaises(HTTPError) as invalid:
            self._open(
                f"/api/environments/{env_id}/runtime-config/variables",
                data=json.dumps({"name": "INVALID-NAME", "value": "x"}).encode(),
                method="POST", cookie=cookie,
            )
        self.assertEqual(400, invalid.exception.code)

        with self._open(
            f"/api/environments/{env_id}/runtime-config/variables/AGENT_MCP_PUBLIC_URL/delete",
            data=b"{}", method="POST", cookie=cookie,
        ) as response:
            deleted = json.loads(response.read().decode())
        self.assertEqual([], deleted["variables"])
        self.assertEqual(["AGENT_MCP_PUBLIC_URL"], deleted["deleted_names"])

    def test_sync_runtime_config_imports_safe_literals_and_keeps_secret_values_out(self) -> None:
        cookie = self._login()
        payload = {
            "name": "dev2", "service_id": "echoagent-router", "region": "cn-southwest-2",
            "cluster_name": "cluster", "workload_name": "echoagent-router", "jump_host": "jump",
            "jump_password": "jump-secret", "nodes": [],
        }
        with self._open(
            "/api/environments", data=json.dumps(payload).encode(), method="POST", cookie=cookie
        ) as response:
            env_id = json.loads(response.read().decode())["environment"]["id"]
        snapshot = {
            "workload_kind": "deployment", "workload_name": "echoagent-router", "container_name": "router",
            "variables": {"PUBLIC_URL": "http://example.test", "EMPTY": ""},
            "skipped_value_from_names": ["DB_PASSWORD"], "skipped_sensitive_names": ["API_TOKEN"],
        }
        with patch.object(server.cce_rollout, "read_workload_runtime_environment", return_value=snapshot):
            with self._open(
                f"/api/environments/{env_id}/runtime-config/sync",
                data=b"{}", method="POST", cookie=cookie,
            ) as response:
                synced = json.loads(response.read().decode())
        self.assertEqual(["EMPTY", "PUBLIC_URL"], [item["name"] for item in synced["variables"]])
        self.assertTrue(all(item["source"] == "cluster" for item in synced["variables"]))
        self.assertEqual(["DB_PASSWORD"], synced["sync"]["skipped_value_from_names"])
        self.assertEqual(["API_TOKEN"], synced["sync"]["skipped_sensitive_names"])
        self.assertNotIn("jump-secret", json.dumps(synced))

    def test_create_rejects_unknown_region(self) -> None:
        cookie = self._login()
        with self.assertRaises(HTTPError) as failed:
            self._open(
                "/api/environments",
                data=json.dumps(
                    {
                        "name": "bad",
                        "region": "cn-north-4",
                        "cluster_name": "x",
                        "workload_name": "y",
                        "jump_host": "jump",
                    }
                ).encode(),
                method="POST",
                cookie=cookie,
            )
        self.assertEqual(400, failed.exception.code)

    def test_create_requires_jump_password(self) -> None:
        cookie = self._login()
        with self.assertRaises(HTTPError) as failed:
            self._open(
                "/api/environments",
                data=json.dumps(
                    {
                        "name": "no-pass",
                        "service_id": "echoagent-router",
                        "region": "cn-southwest-2",
                        "cluster_name": "x",
                        "workload_name": "y",
                        "jump_host": "jump",
                        "nodes": ["172.31.8.33"],
                    }
                ).encode(),
                method="POST",
                cookie=cookie,
            )
        self.assertEqual(400, failed.exception.code)
        self.assertIn("密码", json.loads(failed.exception.read().decode())["error"])

    def test_create_allows_direct_jump_mode_without_nodes_or_node_password(self) -> None:
        cookie = self._login()
        payload = {
            "name": "direct-jump",
            "service_id": "echoagent-router",
            "region": "cn-southwest-2",
            "cluster_name": "cluster",
            "workload_name": "echoagent-router",
            "namespace": "echo-prod",
            "kubeconfig_path": "/etc/robot-ci/kubeconfigs/prod.yaml",
            "jump_host": "root@jump",
            "jump_password": "jump-secret",
            "nodes": [],
        }
        with self._open(
            "/api/environments",
            data=json.dumps(payload).encode(),
            method="POST",
            cookie=cookie,
        ) as response:
            created = json.loads(response.read().decode())["environment"]

        self.assertEqual([], created["nodes"])
        self.assertTrue(created["has_jump_password"])
        self.assertFalse(created["has_node_password"])

    def test_kubeconfig_picker_lists_multiple_files_from_selected_jump_host(self) -> None:
        cookie = self._login()
        payload = {
            "service_id": "echoagent-router",
            "jump_host": "ops@jump.example:2222",
            "jump_password": "jump-secret",
        }
        expected = [
            "/etc/robot-ci/kubeconfigs/dev.yaml",
            "/etc/robot-ci/kubeconfigs/prod.yaml",
        ]
        with patch.object(server.cce_rollout, "list_jump_kubeconfigs", return_value=expected) as discover:
            with self._open(
                "/api/environments/kubeconfigs",
                data=json.dumps(payload).encode(),
                method="POST",
                cookie=cookie,
            ) as response:
                result = json.loads(response.read().decode())

        self.assertEqual(expected, result["kubeconfigs"])
        kwargs = discover.call_args.kwargs
        self.assertEqual("ops@jump.example:2222", kwargs["jump_host"])
        self.assertEqual("jump-secret", kwargs["creds"]["jump_password"])
        self.assertEqual("/etc/robot-ci/kubeconfigs", kwargs["directory"])
        self.assertNotIn("jump-secret", json.dumps(result))

    def test_kubeconfig_picker_reuses_saved_jump_password_when_editing(self) -> None:
        env, err = server.create_environment({
            "name": "saved",
            "service_id": "echoagent-router",
            "region": "cn-southwest-2",
            "cluster_name": "cluster",
            "workload_name": "echoagent-router",
            "jump_host": "root@jump",
            "jump_password": "saved-secret",
            "nodes": [],
        })
        self.assertEqual("", err)
        cookie = self._login()
        with patch.object(server.cce_rollout, "list_jump_kubeconfigs", return_value=[]) as discover:
            with self._open(
                "/api/environments/kubeconfigs",
                data=json.dumps({
                    "environment_id": env["id"],
                    "jump_host": "root@jump",
                    "jump_password": "",
                }).encode(),
                method="POST",
                cookie=cookie,
            ) as response:
                self.assertEqual([], json.loads(response.read().decode())["kubeconfigs"])

        self.assertEqual("saved-secret", discover.call_args.kwargs["creds"]["jump_password"])

    def test_create_requires_known_service(self) -> None:
        cookie = self._login()
        base = {
            "name": "no-svc",
            "region": "cn-southwest-2",
            "cluster_name": "x",
            "workload_name": "y",
            "jump_host": "jump",
            "jump_password": "j",
            "node_password": "n",
        }
        with self.assertRaises(HTTPError) as missing:
            self._open(
                "/api/environments",
                data=json.dumps(base).encode(),
                method="POST",
                cookie=cookie,
            )
        self.assertEqual(400, missing.exception.code)
        self.assertIn("微服务", json.loads(missing.exception.read().decode())["error"])
        with self.assertRaises(HTTPError) as unknown:
            self._open(
                "/api/environments",
                data=json.dumps({**base, "service_id": "not-a-service"}).encode(),
                method="POST",
                cookie=cookie,
            )
        self.assertEqual(400, unknown.exception.code)
        self.assertIn("未知", json.loads(unknown.exception.read().decode())["error"])

    def test_backfills_service_id_from_workload_name(self) -> None:
        server.init_store()
        now = "2026-09-10 14:26:00"
        with server._db_lock:
            conn = server._connect_db()
            try:
                conn.execute(
                    """
                    INSERT INTO environments (
                        id, name, service_id, region, region_label, cluster_name, workload_name,
                        jump_host, jump_password, node_password, nodes_json,
                        created_by, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "legacy1",
                        "liusong-dev-gamma",
                        "",
                        "cn-southwest-2",
                        "贵阳一",
                        "liusong-dev-gamma",
                        "echoagent-router",
                        "116.63.173.182",
                        "j",
                        "n",
                        "[]",
                        "l00855954",
                        now,
                        now,
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        server.init_store()
        stored = server.get_environment("legacy1")
        self.assertIsNotNone(stored)
        self.assertEqual("echoagent-router", stored["service_id"])
        self.assertEqual(1, len(server.list_environments("echoagent-router")))
        self.assertEqual([], server.list_environments("org-admin"))
