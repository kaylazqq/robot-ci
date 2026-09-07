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


class AuthApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "robot-ci.db"
        self.db_patch = patch.object(server, "DB_PATH", self.db_path)
        self.db_patch.start()
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
            cookie = (response.headers.get("Set-Cookie") or "").split(";", 1)[0]
        return cookie

    def test_login_me_password_and_protected_api(self) -> None:
        with self.assertRaises(HTTPError) as denied:
            self._open("/api/services")
        self.assertEqual(401, denied.exception.code)

        with self.assertRaises(HTTPError) as bad:
            self._open(
                "/api/auth/login",
                data=json.dumps({"username": "l30042018", "password": "wrong"}).encode(),
                method="POST",
            )
        self.assertEqual(401, bad.exception.code)

        with self._open(
            "/api/auth/login",
            data=json.dumps(
                {"username": server.DEFAULT_USERNAME, "password": server.DEFAULT_PASSWORD}
            ).encode(),
            method="POST",
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
            cookie = (response.headers.get("Set-Cookie") or "").split(";", 1)[0]
        self.assertEqual("l30042018", payload["username"])
        self.assertTrue(cookie.startswith(server.SESSION_COOKIE + "="))

        with self._open("/api/auth/me", cookie=cookie) as response:
            me = json.loads(response.read().decode("utf-8"))
        self.assertEqual("l30042018", me["username"])

        with self._open(
            "/api/auth/password",
            data=json.dumps(
                {"old_password": server.DEFAULT_PASSWORD, "new_password": "@l30042018x"}
            ).encode(),
            method="POST",
            cookie=cookie,
        ) as response:
            changed = json.loads(response.read().decode("utf-8"))
        self.assertTrue(changed["ok"])
        self.assertEqual(
            "l30042018",
            server.authenticate_user("l30042018", "@l30042018x"),
        )

    def test_second_default_user_can_login(self) -> None:
        self.assertEqual("l00855954", server.authenticate_user("l00855954", "@l00855954"))
        with self._open(
            "/api/auth/login",
            data=json.dumps({"username": "l00855954", "password": "@l00855954"}).encode(),
            method="POST",
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual("l00855954", payload["username"])

    def test_preset_roster_accounts_are_seeded(self) -> None:
        self.assertIn("c50065452", server.DEFAULT_USERNAMES)
        self.assertIn("z00987657", server.DEFAULT_USERNAMES)
        self.assertEqual(20, len(server.DEFAULT_USERNAMES))
        self.assertEqual(len(set(server.DEFAULT_USERNAMES)), len(server.DEFAULT_USERNAMES))
        for username, password in server.DEFAULT_USERS:
            self.assertEqual("@" + username, password)
            self.assertEqual(username, server.authenticate_user(username, password))
        self.assertEqual("c50065452", server.authenticate_user("c50065452", "@c50065452"))

    def test_session_survives_in_memory_restart(self) -> None:
        cookie = self._login()
        token = cookie.split("=", 1)[-1]
        self.assertEqual("l30042018", server.session_username(token))
        with server._sessions_lock:
            server._sessions.clear()
        self.assertEqual("l30042018", server.session_username(token))
        with self._open("/api/auth/me", cookie=cookie) as response:
            me = json.loads(response.read().decode("utf-8"))
        self.assertEqual("l30042018", me["username"])
        server.destroy_session(token)
        with server._sessions_lock:
            server._sessions.clear()
        self.assertEqual("", server.session_username(token))

    def test_run_template_is_saved_per_user_and_service(self) -> None:
        cookie = self._login()
        with self._open(
            "/api/run-templates",
            data=json.dumps({"service_id": "memory-service", "branch": "develop"}).encode(),
            method="POST",
            cookie=cookie,
        ) as response:
            saved = json.loads(response.read().decode("utf-8"))
        self.assertTrue(saved["ok"])
        self.assertEqual("develop", saved["branch"])

        with self._open("/api/run-templates?service_id=memory-service", cookie=cookie) as response:
            loaded = json.loads(response.read().decode("utf-8"))
        self.assertEqual("develop", loaded["branch"])

        with self._open(
            "/api/run-templates",
            data=json.dumps({"service_id": "memory-service", "branch": "release"}).encode(),
            method="POST",
            cookie=cookie,
        ) as response:
            json.loads(response.read().decode("utf-8"))
        with self._open("/api/run-templates?service_id=memory-service", cookie=cookie) as response:
            overwritten = json.loads(response.read().decode("utf-8"))
        self.assertEqual("release", overwritten["branch"])

    def test_missing_template_branch_falls_back_to_default(self) -> None:
        self.assertEqual(
            "main",
            server.resolve_template_branch("l30042018", "memory-service", ["main", "develop"], "main"),
        )
        server.save_run_template("l30042018", "memory-service", "gone")
        self.assertEqual(
            "main",
            server.resolve_template_branch("l30042018", "memory-service", ["main", "develop"], "main"),
        )
        server.save_run_template("l30042018", "memory-service", "develop")
        self.assertEqual(
            "develop",
            server.resolve_template_branch("l30042018", "memory-service", ["main", "develop"], "main"),
        )

    def test_legacy_users_json_is_imported_into_sqlite(self) -> None:
        self.db_patch.stop()
        self.tmp.cleanup()
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "robot-ci.db"
        users_path = Path(self.tmp.name) / "users.json"
        salt, digest = server._hash_password("imported-pass-1")
        users_path.write_text(
            json.dumps({"users": [{"username": "imported-user", "salt": salt, "password_hash": digest}]}),
            encoding="utf-8",
        )
        self.db_patch = patch.object(server, "DB_PATH", self.db_path)
        self.users_patch = patch.object(server, "USERS_PATH", users_path)
        self.db_patch.start()
        self.users_patch.start()
        try:
            server.init_store()
            self.assertEqual("imported-user", server.authenticate_user("imported-user", "imported-pass-1"))
            server.ensure_default_users()
            self.assertEqual(
                server.DEFAULT_USERNAME,
                server.authenticate_user(server.DEFAULT_USERNAME, server.DEFAULT_PASSWORD),
            )
        finally:
            self.users_patch.stop()
