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
        self.assertNotIn("jump_password", updated)
        self.assertNotIn("node_password", updated)
        stored = server.get_environment(created["id"], include_secrets=True)
        self.assertEqual("jump-secret", stored["jump_password"])
        self.assertEqual("node-secret", stored["node_password"])

        with self._open("/api/environments", cookie=cookie) as response:
            listed = json.loads(response.read().decode())["environments"]
        self.assertEqual(1, len(listed))
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
