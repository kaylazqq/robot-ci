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
        self.db_patch.start()
        self.users_patch.start()
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
        with self._open("/api/environments", cookie=cookie) as response:
            empty = json.loads(response.read().decode())
        self.assertEqual([], empty["environments"])
        self.assertEqual([{"id": "cn-southwest-2", "label": "贵阳一"}], empty["regions"])

        payload = {
            "name": "联调",
            "service_id": "semantic-schedule",
            "region": "cn-southwest-2",
            "cluster_name": "AgentPlatform",
            "workload_name": "semantic-schedule",
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
        self.assertEqual("semantic-schedule", created["service_id"])
        self.assertEqual("贵阳一", created["region_label"])
        self.assertEqual(["172.31.8.33", "172.31.22.203"], created["nodes"])
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
        self.assertEqual("semantic-schedule", updated["service_id"])
        self.assertNotIn("jump_password", updated)
        self.assertNotIn("node_password", updated)
        stored = server.get_environment(created["id"], include_secrets=True)
        self.assertEqual("jump-secret", stored["jump_password"])
        self.assertEqual("node-secret", stored["node_password"])

        with self._open("/api/environments", cookie=cookie) as response:
            listed = json.loads(response.read().decode())["environments"]
        self.assertEqual(1, len(listed))
        self.assertEqual("semantic-schedule", listed[0]["service_id"])
        with self._open("/api/environments?service_id=semantic-schedule", cookie=cookie) as response:
            scoped = json.loads(response.read().decode())["environments"]
        self.assertEqual(1, len(scoped))
        with self._open("/api/environments?service_id=memory-service", cookie=cookie) as response:
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

    def test_create_requires_passwords_and_never_echoes_them(self) -> None:
        cookie = self._login()
        with self.assertRaises(HTTPError) as failed:
            self._open(
                "/api/environments",
                data=json.dumps(
                    {
                        "name": "no-pass",
                        "service_id": "semantic-schedule",
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
                        "semantic-schedule",
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
        self.assertEqual("semantic-schedule", stored["service_id"])
        self.assertEqual(1, len(server.list_environments("semantic-schedule")))
        self.assertEqual([], server.list_environments("memory-service"))
