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


class PipelineTemplateApiTests(unittest.TestCase):
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

    def _login(self, username: str = "", password: str = "") -> str:
        with self._open(
            "/api/auth/login",
            data=json.dumps(
                {
                    "username": username or server.DEFAULT_USERNAME,
                    "password": password or server.DEFAULT_PASSWORD,
                }
            ).encode(),
            method="POST",
        ) as response:
            return (response.headers.get("Set-Cookie") or "").split(";", 1)[0]

    def _list(self, cookie: str, service_id: str = "memory-service") -> list[dict]:
        with self._open(
            "/api/pipeline-templates?service_id=" + service_id,
            cookie=cookie,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload["templates"]

    def test_defaults_per_user_and_service_and_crud(self) -> None:
        cookie = self._login()
        templates = self._list(cookie)
        self.assertEqual(["个人构建流水线", "生产发布"], [item["name"] for item in templates])
        personal = next(item for item in templates if item["kind"] == "personal")
        release = next(item for item in templates if item["kind"] == "release")
        self.assertTrue(personal["builtin"])
        self.assertFalse(personal["gamma_deploy"])
        self.assertFalse(personal["gamma_test"])
        self.assertTrue(release["builtin"])
        self.assertTrue(release["gamma_deploy"])
        self.assertTrue(release["gamma_test"])

        with self._open(
            "/api/pipeline-templates/" + personal["id"],
            data=json.dumps(
                {
                    "name": "我的日常构建",
                    "branch": "develop",
                    "gamma_deploy": False,
                    "gamma_test": False,
                    "environment_id": "",
                }
            ).encode(),
            method="POST",
            cookie=cookie,
        ) as response:
            updated = json.loads(response.read().decode("utf-8"))
        self.assertTrue(updated["ok"])
        self.assertEqual("我的日常构建", updated["template"]["name"])
        self.assertEqual("develop", updated["template"]["branch"])

        with self._open("/api/run-templates?service_id=memory-service", cookie=cookie) as response:
            last_branch = json.loads(response.read().decode("utf-8"))
        self.assertEqual("develop", last_branch["branch"])

        with self._open(
            "/api/pipeline-templates/" + release["id"] + "/copy",
            data=json.dumps({"name": "生产发布 克隆"}).encode(),
            method="POST",
            cookie=cookie,
        ) as response:
            copied = json.loads(response.read().decode("utf-8"))["template"]
        self.assertEqual("生产发布 克隆", copied["name"])
        self.assertEqual("custom", copied["kind"])
        self.assertFalse(copied["builtin"])
        self.assertTrue(copied["gamma_deploy"])

        with self.assertRaises(HTTPError) as denied:
            self._open(
                "/api/pipeline-templates/" + personal["id"] + "/delete",
                data=b"{}",
                method="POST",
                cookie=cookie,
            )
        self.assertEqual(400, denied.exception.code)

        with self._open(
            "/api/pipeline-templates/" + copied["id"] + "/delete",
            data=b"{}",
            method="POST",
            cookie=cookie,
        ) as response:
            deleted = json.loads(response.read().decode("utf-8"))
        self.assertTrue(deleted["ok"])
        names = [item["name"] for item in self._list(cookie)]
        self.assertEqual(["我的日常构建", "生产发布"], names)

        other = self._login("c00985465", "c00985465")
        other_names = [item["name"] for item in self._list(other)]
        self.assertEqual(["个人构建流水线", "生产发布"], other_names)
        other_ids = {item["id"] for item in self._list(other)}
        self.assertNotIn(personal["id"], other_ids)

    def test_unknown_service_and_auth(self) -> None:
        with self.assertRaises(HTTPError) as denied:
            self._open("/api/pipeline-templates?service_id=memory-service")
        self.assertEqual(401, denied.exception.code)

        cookie = self._login()
        with self.assertRaises(HTTPError) as missing:
            self._open("/api/pipeline-templates?service_id=not-a-service", cookie=cookie)
        self.assertEqual(404, missing.exception.code)


if __name__ == "__main__":
    unittest.main()
