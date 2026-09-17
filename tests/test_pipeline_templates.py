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
        self.assertTrue(release["production_release"])

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
        self.assertFalse(copied["production_release"])

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
        other_templates = self._list(other)
        other_names = [item["name"] for item in other_templates]
        self.assertEqual(["我的日常构建", "生产发布"], other_names)
        other_ids = {item["id"] for item in self._list(other)}
        self.assertIn(personal["id"], other_ids)

        other_personal = next(item for item in other_templates if item["kind"] == "personal")
        self.assertEqual(personal["id"], other_personal["id"])
        self.assertTrue(server._job_matches_template_filter(
            {"template_id": personal["id"], "template_name": personal["name"]},
            other_personal["id"],
        ))
        self.assertFalse(server._job_matches_template_filter(
            {"template_id": release["id"], "template_name": release["name"]},
            other_personal["id"],
        ))

    def test_unknown_service_and_auth(self) -> None:
        with self.assertRaises(HTTPError) as denied:
            self._open("/api/pipeline-templates?service_id=memory-service")
        self.assertEqual(401, denied.exception.code)

        cookie = self._login()
        with self.assertRaises(HTTPError) as missing:
            self._open("/api/pipeline-templates?service_id=not-a-service", cookie=cookie)
        self.assertEqual(404, missing.exception.code)

    def test_account_scoped_builtin_ids_and_history_are_migrated(self) -> None:
        service_id = "memory-service"
        templates = self._list(self._login(), service_id)
        canonical = next(item for item in templates if item["kind"] == "personal")
        old_id = "oldpersonal1"
        now = server._now_stamp()
        with server._connect_db() as conn:
            server._insert_pipeline_template(conn, {
                **canonical, "id": old_id, "username": "other-user", "created_at": now, "updated_at": now,
            })
        log_dir = Path(self.tmp.name) / "logs"
        log_dir.mkdir()
        history = log_dir / "job-abcdef123456.json"
        history.write_text(json.dumps({
            "id": "abcdef123456", "service_id": service_id, "service_ids": [service_id],
            "template_id": old_id, "template_name": "个人构建流水线",
        }), encoding="utf-8")
        with patch.object(server, "LOG_DIR", log_dir):
            result = server.migrate_pipeline_templates_to_service_scope()
        migrated = json.loads(history.read_text(encoding="utf-8"))
        self.assertEqual(canonical["id"], migrated["template_id"])
        self.assertGreaterEqual(result["updated"], 1)
        with server._connect_db() as conn:
            rows = conn.execute(
                "SELECT id, username FROM pipeline_templates WHERE service_id=? AND kind='personal' AND builtin=1",
                (service_id,),
            ).fetchall()
        self.assertEqual([(canonical["id"], "")], [(row["id"], row["username"]) for row in rows])


if __name__ == "__main__":
    unittest.main()
