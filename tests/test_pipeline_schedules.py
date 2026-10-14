import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.request import ProxyHandler, Request, build_opener

import server


class PipelineScheduleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(server, "DB_PATH", Path(self.tmp.name) / "robot-ci.db")
        self.users_patch = patch.object(server, "USERS_PATH", Path(self.tmp.name) / "users.json")
        self.db_patch.start()
        self.users_patch.start()
        with server._sessions_lock:
            server._sessions.clear()
        server.ensure_default_users()

    def tearDown(self) -> None:
        self.users_patch.stop()
        self.db_patch.stop()
        self.tmp.cleanup()

    def _template(self) -> dict:
        username = server.DEFAULT_USERNAME
        template = next(
            item
            for item in server.list_pipeline_templates(username, "echoagent-backend")
            if item["kind"] == "personal"
        )
        updated, error = server.update_pipeline_template(
            username, template["id"], {**template, "branch": "main"}
        )
        self.assertEqual("", error)
        return updated

    def test_cron_validation_and_timezone(self) -> None:
        spec, error = server.validate_cron_expression("30 9 * * 1-5")
        self.assertEqual("", error)
        self.assertIn(30, spec["minute"])
        after = datetime(2026, 10, 16, 2, 0, tzinfo=timezone.utc)  # Friday 10:00 Shanghai
        next_at = server.next_cron_time("30 9 * * 1-5", "Asia/Shanghai", after)
        self.assertEqual("2026-10-19T01:30:00+00:00", next_at.isoformat())
        self.assertTrue(server.validate_cron_expression("bad cron")[1])
        with self.assertRaises(ValueError):
            server.next_cron_time("0 2 * * *", "Invalid/Zone", after)

    def test_multiple_schedules_crud_and_claim_once(self) -> None:
        template = self._template()
        first, error = server.save_pipeline_schedule(
            template["id"],
            {"name": "daily", "cron_expression": "0 2 * * *", "timezone": "Asia/Shanghai", "enabled": True},
            server.DEFAULT_USERNAME,
        )
        self.assertEqual("", error)
        second, error = server.save_pipeline_schedule(
            template["id"],
            {"name": "hourly", "cron_expression": "0 * * * *", "timezone": "UTC", "enabled": True},
            server.DEFAULT_USERNAME,
        )
        self.assertEqual("", error)
        self.assertEqual(2, len(server.list_pipeline_schedules(template["id"])))

        now = datetime(2026, 10, 14, 0, 0, tzinfo=timezone.utc)
        conn = server._connect_db()
        try:
            conn.execute("UPDATE pipeline_schedules SET next_run_at=?", (server._utc_iso(now),))
            conn.commit()
        finally:
            conn.close()
        claimed = server.claim_due_pipeline_schedules(now)
        self.assertEqual({first["id"], second["id"]}, {item[0] for item in claimed})
        self.assertEqual([], server.claim_due_pipeline_schedules(now))

        changed, error = server.save_pipeline_schedule(
            template["id"], {**first, "name": "daily changed", "enabled": False},
            server.DEFAULT_USERNAME, first["id"],
        )
        self.assertEqual("", error)
        self.assertFalse(changed["enabled"])
        self.assertTrue(server.delete_pipeline_schedule(second["id"]))
        self.assertEqual([first["id"]], [item["id"] for item in server.list_pipeline_schedules(template["id"])])

    def test_schedule_requires_template_branch_and_cascades_on_delete(self) -> None:
        username = server.DEFAULT_USERNAME
        original = next(
            item for item in server.list_pipeline_templates(username, "echoagent-backend")
            if item["kind"] == "personal"
        )
        item, error = server.save_pipeline_schedule(
            original["id"], {"name": "invalid", "cron_expression": "0 2 * * *"}, username
        )
        self.assertIsNone(item)
        self.assertIn("固定分支", error)

        copied, error = server.copy_pipeline_template(username, self._template()["id"], "scheduled copy")
        self.assertEqual("", error)
        schedule, error = server.save_pipeline_schedule(
            copied["id"], {"name": "daily", "cron_expression": "0 2 * * *"}, username
        )
        self.assertEqual("", error)
        server._record_schedule_result(schedule["id"], server._utc_iso(), status="skipped")
        self.assertEqual("", server.delete_pipeline_template(username, copied["id"]))
        self.assertIsNone(server.get_pipeline_schedule(schedule["id"]))
        self.assertEqual([], server.list_pipeline_schedule_runs(schedule["id"]))

    def test_trigger_uses_existing_push_api_and_records_job(self) -> None:
        template = self._template()
        schedule, error = server.save_pipeline_schedule(
            template["id"], {"name": "daily", "cron_expression": "0 2 * * *"}, server.DEFAULT_USERNAME
        )
        self.assertEqual("", error)

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"job_id":"scheduled-job"}'

        captured = {}

        def fake_open(request, timeout=0):
            captured["url"] = request.full_url
            captured["payload"] = json.loads(request.data.decode())
            captured["cookie"] = request.headers.get("Cookie")
            captured["timeout"] = timeout
            return Response()

        with patch.object(server, "urlopen", side_effect=fake_open):
            result = server.trigger_pipeline_schedule(schedule["id"], "http://127.0.0.1:18888")
        self.assertTrue(result["ok"])
        self.assertEqual("http://127.0.0.1:18888/api/push", captured["url"])
        self.assertEqual("schedule", captured["payload"]["trigger_type"])
        self.assertEqual(schedule["id"], captured["payload"]["trigger_id"])
        self.assertEqual("scheduled-job", server.get_pipeline_schedule(schedule["id"])["last_job_id"])


class PipelineScheduleApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(server, "DB_PATH", Path(self.tmp.name) / "robot-ci.db")
        self.users_patch = patch.object(server, "USERS_PATH", Path(self.tmp.name) / "users.json")
        self.db_patch.start()
        self.users_patch.start()
        with server._sessions_lock:
            server._sessions.clear()
        server.ensure_default_users()
        template = next(
            item for item in server.list_pipeline_templates(server.DEFAULT_USERNAME, "echoagent-backend")
            if item["kind"] == "personal"
        )
        self.template, _ = server.update_pipeline_template(
            server.DEFAULT_USERNAME, template["id"], {**template, "branch": "main"}
        )
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"
        self.opener = build_opener(ProxyHandler({}))
        login = Request(
            self.base + "/api/auth/login",
            data=json.dumps({"username": server.DEFAULT_USERNAME, "password": server.DEFAULT_PASSWORD}).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with self.opener.open(login) as response:
            self.cookie = response.headers["Set-Cookie"].split(";", 1)[0]

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        self.users_patch.stop()
        self.db_patch.stop()
        self.tmp.cleanup()

    def _request(self, path: str, payload=None, method="GET") -> dict:
        request = Request(
            self.base + path,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Cookie": self.cookie}, method=method,
        )
        with self.opener.open(request, timeout=3) as response:
            return json.loads(response.read().decode())

    def test_schedule_api_lifecycle_and_run_now(self) -> None:
        created = self._request(
            f"/api/pipeline-templates/{self.template['id']}/schedules",
            {"name": "nightly", "cron_expression": "0 2 * * *", "timezone": "Asia/Shanghai"}, "POST",
        )["schedule"]
        listed = self._request(f"/api/pipeline-templates/{self.template['id']}/schedules")
        self.assertEqual([created["id"]], [item["id"] for item in listed["schedules"]])
        updated = self._request(
            f"/api/pipeline-schedules/{created['id']}", {**created, "enabled": False}, "POST"
        )["schedule"]
        self.assertFalse(updated["enabled"])
        with patch.object(server, "trigger_pipeline_schedule", return_value={"ok": True, "job_id": "job123"}):
            run = self._request(f"/api/pipeline-schedules/{created['id']}/run-now", {}, "POST")
        self.assertEqual("job123", run["job_id"])
        self.assertTrue(self._request(f"/api/pipeline-schedules/{created['id']}/delete", {}, "POST")["ok"])


if __name__ == "__main__":
    unittest.main()
