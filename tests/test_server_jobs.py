import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import server


def make_job(job_id: str, status: str = "running") -> dict:
    return {
        "id": job_id,
        "service_id": "memory-service",
        "branch": "main",
        "status": status,
        "stage": "testing",
        "error": None,
        "remote": None,
        "archive": None,
        "archive_dir": None,
        "results": [],
        "progress": "1/1",
        "current": "memory-service",
        "commit_sha": "abc123",
        "test_status": "passed",
        "test_summary": {"total": 1, "passed": 1},
        "test_report": None,
        "test_runs": [{"service_id": "memory-service", "status": "passed"}],
        "log": [],
        "ui_log": [],
    }


class JobCoordinationTests(unittest.TestCase):
    def setUp(self) -> None:
        with server._jobs_lock:
            self.saved_jobs = dict(server._jobs)
            server._jobs.clear()

    def tearDown(self) -> None:
        with server._jobs_lock:
            server._jobs.clear()
            server._jobs.update(self.saved_jobs)

    def test_only_one_concurrent_job_is_registered(self) -> None:
        barrier = threading.Barrier(8)
        outcomes: list[tuple[str, dict | None]] = []

        def register(index: int) -> None:
            job_id = f"job-{index}"
            barrier.wait()
            outcomes.append((job_id, server.register_job_if_idle(make_job(job_id))))

        threads = [threading.Thread(target=register, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        accepted = [job_id for job_id, active in outcomes if active is None]
        self.assertEqual(1, len(accepted))
        self.assertEqual(1, len(server._jobs))
        self.assertEqual(accepted[0], server.active_job_summary()["id"])

    def test_completed_saved_job_does_not_block_the_next_job(self) -> None:
        first = make_job("first-job")
        self.assertIsNone(server.register_job_if_idle(first))
        first["status"] = "ok"

        second = make_job("second-job")
        self.assertIsNone(server.register_job_if_idle(second))
        self.assertEqual("second-job", server.active_job_summary()["id"])

    def test_active_summary_does_not_copy_logs_or_test_cases(self) -> None:
        job = make_job("active-job")
        job["log"] = ["large raw log"]
        job["ui_log"] = ["small ui log"]
        job["test_cases"] = [{"name": "case"}]
        self.assertIsNone(server.register_job_if_idle(job))

        summary = server.active_job_summary()
        self.assertNotIn("log", summary)
        self.assertNotIn("ui_log", summary)
        self.assertNotIn("test_cases", summary)
        self.assertNotIn("test_runs", summary)


class JobPayloadTests(unittest.TestCase):
    def test_compact_payload_returns_only_log_delta(self) -> None:
        job = make_job("delta-job")
        job["ui_log"] = ["step 1", "step 2", "step 3"]

        payload = server.job_payload(
            job,
            compact=True,
            view_ui=True,
            log_after=1,
            test_revision=-1,
        )
        self.assertEqual(["step 2", "step 3"], payload["log"])
        self.assertEqual(3, payload["log_cursor"])
        self.assertEqual(1, payload["test_revision"])
        self.assertEqual(job["test_runs"], payload["test_runs"])

        unchanged = server.job_payload(
            job,
            compact=True,
            view_ui=True,
            log_after=payload["log_cursor"],
            test_revision=payload["test_revision"],
        )
        self.assertEqual([], unchanged["log"])
        self.assertNotIn("test_runs", unchanged)

    def test_ui_log_keeps_steps_and_errors_but_hides_runner_noise(self) -> None:
        raw = [
            "[10:00:00] git fetch…",
            "[10:00:01] Requirement already satisfied: pytest",
            "[10:00:02] tests start service=memory-service sha=abc123",
            "[10:00:03] @@TEST_STEP@@ CellMem pure UT/DT",
            "[10:00:04] {\"Action\":\"pass\",\"Test\":\"TestPayload\"}",
            "[10:00:05] @@TEST_ERROR@@ test command timed out",
            "[10:00:06] @@TEST_SUMMARY@@ {\"status\":\"failed\"}",
            "[10:00:07] TEST summary status=failed total=1",
            "[10:00:08] Tests completed service=memory-service status=failed",
        ]

        visible = server.filter_ui_log(raw)
        self.assertIn(raw[0], visible)
        self.assertIn(raw[2], visible)
        self.assertIn(raw[3], visible)
        self.assertIn(raw[5], visible)
        self.assertIn(raw[8], visible)
        self.assertNotIn(raw[1], visible)
        self.assertNotIn(raw[4], visible)
        self.assertNotIn(raw[6], visible)
        self.assertNotIn(raw[7], visible)


class JobEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        with server._jobs_lock:
            self.saved_jobs = dict(server._jobs)
            server._jobs.clear()
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
            server._jobs.update(self.saved_jobs)

    def test_running_endpoint_is_lightweight_and_push_is_globally_locked(self) -> None:
        job = make_job("active-job")
        job["log"] = ["large raw log"]
        self.assertIsNone(server.register_job_if_idle(job))

        with self.opener.open(self.base_url + "/api/running-job", timeout=2) as response:
            active = json.loads(response.read().decode("utf-8"))
        self.assertEqual("active-job", active["id"])
        self.assertNotIn("log", active)
        self.assertNotIn("test_runs", active)

        request = Request(
            self.base_url + "/api/push",
            data=json.dumps({"items": [{"service_id": "memory-service", "branch": "main"}]}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as raised:
            self.opener.open(request, timeout=2)
        self.assertEqual(409, raised.exception.code)
        payload = json.loads(raised.exception.read().decode("utf-8"))
        self.assertEqual("active-job", payload["active_job_id"])


if __name__ == "__main__":
    unittest.main()
