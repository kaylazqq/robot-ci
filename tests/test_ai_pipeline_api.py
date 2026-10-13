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


class AiPipelineApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.patches = [
            patch.object(server, "DB_PATH", root / "robot-ci.db"),
            patch.object(server, "USERS_PATH", root / "users.json"),
            patch.object(server, "LOG_DIR", root / "logs"),
        ]
        for item in self.patches:
            item.start()
        server.LOG_DIR.mkdir()
        with server._sessions_lock:
            server._sessions.clear()
        with server._jobs_lock:
            server._jobs.clear()
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
        with server._jobs_lock:
            server._jobs.clear()
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def open(self, path, *, payload=None, cookie="", bearer="", headers=None, method="GET"):
        request_headers = {"Content-Type": "application/json", **(headers or {})}
        if cookie:
            request_headers["Cookie"] = cookie
        if bearer:
            request_headers["Authorization"] = "Bearer " + bearer
        body = json.dumps(payload).encode() if payload is not None else None
        return self.opener.open(Request(self.base_url + path, data=body, headers=request_headers, method=method), timeout=3)

    def login(self) -> str:
        with self.open("/api/auth/login", payload={"username": server.DEFAULT_USERNAME,
                       "password": server.DEFAULT_PASSWORD}, method="POST") as response:
            return (response.headers.get("Set-Cookie") or "").split(";", 1)[0]

    def token(self, cookie: str) -> str:
        with self.open("/api/ai/tokens", payload={"name": "test", "expires_in_days": 1},
                       cookie=cookie, method="POST") as response:
            return json.load(response)["token"]["token"]

    def enabled_pipeline(self) -> dict:
        template = server.personal_builtin_template(server.DEFAULT_USERNAME, "memory-service")
        updated, error = server.update_pipeline_template(
            server.DEFAULT_USERNAME, template["id"], {**template, "api_enabled": True}
        )
        self.assertEqual("", error)
        return updated

    def test_token_is_shown_once_and_bearer_lists_enabled_pipeline(self) -> None:
        cookie = self.login()
        token = self.token(cookie)
        self.enabled_pipeline()
        with self.open("/api/ai/tokens", cookie=cookie) as response:
            stored = json.load(response)["tokens"][0]
        self.assertNotIn("token", stored)
        with self.open("/api/ai/pipelines", bearer=token) as response:
            pipelines = json.load(response)["pipelines"]
        self.assertEqual(["memory-service"], [item["service_id"] for item in pipelines])

    def test_run_pins_sha_is_idempotent_and_can_be_polled(self) -> None:
        cookie = self.login()
        token = self.token(cookie)
        pipeline = self.enabled_pipeline()
        sha = "a" * 40
        payload = {"pipeline_id": pipeline["id"], "branch": "feature/api", "commit_sha": sha,
                   "reason": "local API test"}
        with patch.object(server, "resolve_service_branch_sha", return_value=sha), \
             patch.object(server, "docker_ready_for_push", return_value={"ok": True, "detail": ""}), \
             patch.object(server, "run_push_job"):
            with self.open("/api/ai/pipeline-runs", payload=payload, bearer=token, method="POST",
                           headers={"Idempotency-Key": "same-request-1"}) as response:
                first = json.load(response)
            with self.open("/api/ai/pipeline-runs", payload=payload, bearer=token, method="POST",
                           headers={"Idempotency-Key": "same-request-1"}) as response:
                second = json.load(response)
        self.assertEqual(first["run_id"], second["run_id"])
        self.assertTrue(second["reused"])
        job = server._job_copy(first["run_id"])
        self.assertEqual(sha, job["frozen_inputs"]["memory-service"])
        self.assertTrue(job["api_approval_exempt"])
        with self.open("/api/ai/pipeline-runs/" + first["run_id"], bearer=token) as response:
            status = json.load(response)
        self.assertEqual("running", status["status"])
        self.assertEqual(sha, status["commit_sha"])

    def test_commit_mismatch_is_rejected_before_start(self) -> None:
        token = self.token(self.login())
        pipeline = self.enabled_pipeline()
        with patch.object(server, "resolve_service_branch_sha", return_value="b" * 40):
            with self.assertRaises(HTTPError) as denied:
                self.open("/api/ai/pipeline-runs", payload={"pipeline_id": pipeline["id"],
                          "branch": "feature/api", "commit_sha": "a" * 40}, bearer=token,
                          method="POST", headers={"Idempotency-Key": "mismatch-1"})
        self.assertEqual(409, denied.exception.code)
        self.assertFalse(server._jobs)

    def test_bearer_token_cannot_mint_or_revoke_tokens(self) -> None:
        token = self.token(self.login())
        with self.assertRaises(HTTPError) as denied:
            self.open("/api/ai/tokens", payload={"name": "nested"}, bearer=token, method="POST")
        self.assertEqual(403, denied.exception.code)


if __name__ == "__main__":
    unittest.main()
