import json
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import call, patch
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import server


def make_job(job_id: str, status: str = "running", service_id: str = "memory-service") -> dict:
    return {
        "id": job_id,
        "service_id": service_id,
        "service_ids": [service_id],
        "branch": "main",
        "status": status,
        "stage": "testing",
        "error": None,
        "remote": None,
        "archive": None,
        "archive_dir": None,
        "results": [],
        "progress": "1/1",
        "current": service_id,
        "commit_sha": "abc123",
        "test_status": "passed",
        "test_summary": {"total": 1, "passed": 1},
        "test_report": None,
        "test_runs": [{"service_id": service_id, "status": "passed"}],
        "log": [],
        "ui_log": [],
    }


class JobCoordinationTests(unittest.TestCase):
    def setUp(self) -> None:
        with server._jobs_lock:
            self.saved_jobs = dict(server._jobs)
            server._jobs.clear()
        server.reset_job_scheduler()

    def tearDown(self) -> None:
        server.reset_job_scheduler()
        with server._job_procs_lock:
            server._job_procs.clear()
        with server._jobs_lock:
            server._jobs.clear()
            server._jobs.update(self.saved_jobs)

    def test_request_job_stop_marks_running_job(self) -> None:
        job = make_job("stop-job-aa")
        self.assertIsNone(server.register_job_if_idle(job))
        result = server.request_job_stop("stop-job-aa")
        self.assertTrue(result["ok"])
        self.assertEqual("stopping", result["status"])
        with server._jobs_lock:
            stored = server._jobs["stop-job-aa"]
        self.assertTrue(stored["cancel_requested"])
        self.assertEqual("stopping", stored["stage"])
        self.assertEqual("testing", stored["stage_before_stop"])
        self.assertTrue(server.request_job_stop("stop-job-aa")["ok"])
        missing = server.request_job_stop("missing-job")
        self.assertFalse(missing["ok"])
        self.assertEqual(404, missing["http_status"])

    def test_interruptible_sleep_raises_after_stop(self) -> None:
        job = make_job("sleep-stop-aa")
        self.assertIsNone(server.register_job_if_idle(job))
        self.assertTrue(server.request_job_stop("sleep-stop-aa")["ok"])
        with self.assertRaises(server.JobStopped):
            server.interruptible_sleep("sleep-stop-aa", 3, interval=0.01)

    def test_request_job_stop_rejects_finished_job(self) -> None:
        job = make_job("done-job-aa", status="ok")
        self.assertIsNone(server.register_job_if_idle(job))
        rejected = server.request_job_stop("done-job-aa")
        self.assertFalse(rejected["ok"])
        self.assertEqual(409, rejected["http_status"])

    def test_run_cmd_raises_when_job_is_stopped(self) -> None:
        job = make_job("stop-run-aa")
        self.assertIsNone(server.register_job_if_idle(job))
        started = threading.Event()
        raised = []

        def _run() -> None:
            server._job_ctx.job_id = "stop-run-aa"
            try:
                started.set()
                server.run_cmd([sys.executable, "-c", "import time; time.sleep(30)"], timeout=30)
            except server.JobStopped:
                raised.append(True)
            finally:
                server._job_ctx.job_id = None

        worker = threading.Thread(target=_run)
        worker.start()
        self.assertTrue(started.wait(2))
        time.sleep(0.4)
        self.assertTrue(server.request_job_stop("stop-run-aa")["ok"])
        worker.join(timeout=15)
        self.assertFalse(worker.is_alive())
        self.assertTrue(raised)

    def test_concurrent_jobs_are_accepted_for_distinct_services(self) -> None:
        barrier = threading.Barrier(8)
        outcomes: list[tuple[str, dict | None]] = []
        catalog = [item["id"] for item in server.load_services()]
        self.assertGreaterEqual(len(catalog), 8)

        def register(index: int) -> None:
            job_id = f"job-{index}"
            barrier.wait()
            outcomes.append(
                (
                    job_id,
                    server.register_job_if_idle(make_job(job_id, service_id=catalog[index])),
                )
            )

        threads = [threading.Thread(target=register, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        accepted = [job_id for job_id, active in outcomes if active is None]
        self.assertEqual(8, len(accepted))
        self.assertEqual(8, len(server._jobs))
        self.assertIn(server.active_job_summary()["id"], accepted)

    def test_jobs_queue_fifo_when_slots_full(self) -> None:
        with patch.object(server, "max_concurrent_jobs", return_value=1):
            first = make_job("slot-a000000", service_id="temporal")
            first["slot_held"] = True
            self.assertIsNone(server.register_job_if_idle(first))
            second = make_job("slot-b000000", service_id="agentlink")
            self.assertIsNone(server.register_job_if_idle(second))
            acquired: list[bool] = []

            def waiter() -> None:
                with patch.object(server, "append_job_log"), patch.object(server, "persist_job_meta"):
                    acquired.append(server.wait_for_build_slot("slot-b000000"))

            worker = threading.Thread(target=waiter)
            worker.start()
            time.sleep(0.4)
            with server._jobs_lock:
                self.assertEqual("queued", server._jobs["slot-b000000"]["status"])
            first["status"] = "ok"
            first["slot_held"] = False
            server.release_build_slot("slot-a000000")
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
            self.assertEqual([True], acquired)
            with server._jobs_lock:
                self.assertTrue(server._jobs["slot-b000000"]["slot_held"])
                self.assertEqual("running", server._jobs["slot-b000000"]["status"])

    def test_queued_job_pipeline_shows_waiting_slot(self) -> None:
        job = make_job("pipe-queue000", status="queued")
        job["stage"] = "queued"
        job["slot_held"] = False
        job["queue_position"] = 2
        job["current"] = ""
        job["results"] = []
        job["test_status"] = ""
        job["test_runs"] = []
        payload = server.job_payload(job, compact=True)
        prepare = {item["id"]: item["status"] for item in payload["pipeline"]["prepare"]}
        self.assertEqual("done", prepare["env"])
        self.assertEqual("queued", prepare["slot"])
        self.assertEqual("pending", prepare["adir"])

    def test_same_service_second_job_is_rejected(self) -> None:
        first = make_job("job-a")
        second = make_job("job-b")
        self.assertIsNone(server.register_job_if_idle(first))
        conflict = server.register_concurrent_job(second)
        self.assertEqual("service_busy", conflict["error_code"])
        self.assertEqual("memory-service", conflict["service"])
        ids = {item["id"] for item in server.list_running_job_summaries()}
        self.assertEqual({"job-a"}, ids)

    def test_mattermost_second_job_is_rejected(self) -> None:
        first = make_job("mm-a", service_id="mattermost")
        second = make_job("mm-b", service_id="mattermost")
        self.assertIsNone(server.register_job_if_idle(first))
        conflict = server.register_concurrent_job(second)
        self.assertEqual("service_busy", conflict["error_code"])
        self.assertEqual("mattermost", conflict["service"])
        self.assertEqual(["mm-a"], [item["id"] for item in conflict["active_jobs"]])
        self.assertIsNone(server._jobs.get("mm-b"))

    def test_batch_including_mattermost_is_rejected_while_mattermost_runs(self) -> None:
        self.assertIsNone(server.register_job_if_idle(make_job("mm-a", service_id="mattermost")))
        mixed = make_job("batch-b")
        mixed["service_id"] = "memory-service,mattermost"
        mixed["service_ids"] = ["memory-service", "mattermost"]
        conflict = server.register_concurrent_job(mixed)
        self.assertEqual("service_busy", conflict["error_code"])
        self.assertEqual("mattermost", conflict["service"])
        self.assertIsNone(server._jobs.get("batch-b"))

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


class JobWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        with server._jobs_lock:
            self.saved_jobs = dict(server._jobs)
            server._jobs.clear()

    def tearDown(self) -> None:
        with server._jobs_lock:
            server._jobs.clear()
            server._jobs.update(self.saved_jobs)

    def test_repo_dir_is_isolated_per_job(self) -> None:
        svc = {
            "id": "memory-service",
            "repo": "rollingfruit/CellMem",
            "github": "https://github.com/rollingfruit/CellMem.git",
        }
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(server.CFG, {"workspace_root": tmp}):
                left = server.repo_dir(svc, "aaa111bbb222")
                right = server.repo_dir(svc, "ccc333ddd444")
                self.assertNotEqual(left, right)
                self.assertEqual(left.parent, right.parent)
                self.assertTrue(left.name.endswith("--aaa111bbb222"))
                self.assertTrue(right.name.endswith("--ccc333ddd444"))
                self.assertEqual(left.parent, server.public_service_dir().parent)

    def test_gc_keeps_running_job_workspace_and_drops_idle_ones(self) -> None:
        svc = {
            "id": "memory-service",
            "repo": "rollingfruit/CellMem",
            "github": "https://github.com/rollingfruit/CellMem.git",
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = server.Path(tmp)
            with patch.dict(server.CFG, {"workspace_root": tmp}):
                live = server.repo_dir(svc, "livejob00aaaa")
                idle = server.repo_dir(svc, "idlejob00bbbb")
                legacy = root / server.clone_dir_name(svc)
                live.mkdir()
                idle.mkdir()
                legacy.mkdir()
                (live / "keep.txt").write_text("live", encoding="utf-8")
                (idle / "gone.txt").write_text("idle", encoding="utf-8")
                job = make_job("livejob00aaaa")
                self.assertIsNone(server.register_job_if_idle(job))
                with patch.object(server, "append_job_log"):
                    server.gc_idle_clone_dirs("livejob00aaaa", svc)
                self.assertTrue(live.is_dir())
                self.assertFalse(idle.exists())
                self.assertFalse(legacy.exists())

    def test_gc_all_drops_other_service_workspaces(self) -> None:
        svc = {
            "id": "memory-service",
            "repo": "rollingfruit/CellMem",
            "github": "https://github.com/rollingfruit/CellMem.git",
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = server.Path(tmp)
            with patch.dict(server.CFG, {"workspace_root": tmp}):
                live = server.repo_dir(svc, "livejob00aaaa")
                other = root / "kibana-service--idlejob00cccc"
                public = root / "public-service"
                live.mkdir()
                other.mkdir()
                public.mkdir()
                job = make_job("livejob00aaaa")
                self.assertIsNone(server.register_job_if_idle(job))
                try:
                    with patch.object(server, "append_job_log"):
                        server.gc_all_idle_clone_dirs("livejob00aaaa")
                    self.assertTrue(live.is_dir())
                    self.assertTrue(public.is_dir())
                    self.assertFalse(other.exists())
                finally:
                    with server._jobs_lock:
                        server._jobs.pop("livejob00aaaa", None)


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

    def test_step_sub_logs_are_isolated(self) -> None:
        job = make_job("sub-log-aa")
        job["step_logs"] = {
            "sync": ["[all] clone and fetch"],
            "sync:clone": ["[clone] git clone"],
            "sync:fetch": ["[fetch] git fetch"],
        }
        clone = server.job_payload(job, step="sync", sub="clone")
        self.assertEqual(["[clone] git clone", "[fetch] git fetch"], clone["log"])
        self.assertEqual("clone", clone["sub"])
        whole = server.job_payload(job, step="sync")
        self.assertEqual(["[all] clone and fetch"], whole["log"])
        missing = server.job_payload(job, step="sync", sub="missing")
        self.assertEqual([], missing["log"])

    def test_parent_step_logs_are_split_when_sub_logs_missing(self) -> None:
        job = make_job("sub-log-infer")
        job["step_logs"] = {
            "prepare": [
                "[11:30:24] reusing shared SWR login on this server…",
                "[11:30:25] shared SWR login still valid",
                "[11:30:25] ensure shared public-service → /tmp/public-service @ main",
                "[11:30:26] shared archive dir=/tmp/archives/x",
            ],
            "sync": [
                "[11:30:27] git clone…",
                "[11:30:40] HEAD=abc1234 @ release",
            ],
            "build": [
                "[11:31:01] build on WSL: /tmp/src",
                "[11:31:02] dockerfile build local/service:tag",
                "[11:32:00] build finished",
            ],
        }
        env = server.job_payload(job, step="prepare", sub="env")
        self.assertEqual(3, len(env["log"]))
        self.assertIn("reusing shared SWR login", env["log"][0])
        adir = server.job_payload(job, step="prepare", sub="adir")
        self.assertEqual(["[11:30:26] shared archive dir=/tmp/archives/x"], adir["log"])
        clone = server.job_payload(job, step="sync", sub="clone")
        self.assertEqual(["[11:30:27] git clone…"], clone["log"])
        sha = server.job_payload(job, step="sync", sub="sha")
        self.assertEqual(["[11:30:40] HEAD=abc1234 @ release"], sha["log"])
        script = server.job_payload(job, step="build", sub="script")
        self.assertEqual(["[11:31:01] build on WSL: /tmp/src"], script["log"])
        docker = server.job_payload(job, step="build", sub="docker")
        self.assertEqual(["[11:31:02] dockerfile build local/service:tag"], docker["log"])
        verify = server.job_payload(job, step="build", sub="verify")
        self.assertEqual(["[11:32:00] build finished"], verify["log"])

    def test_partial_legacy_docker_sublog_includes_buildkit_failure(self) -> None:
        job = make_job("sub-log-buildkit")
        failure = (
            "[09:45:30] ERROR: failed to solve: local/ai-python-build:3.11.15-v1: "
            "failed to resolve source metadata for docker.io/local/ai-python-build:3.11.15-v1: "
            "pull access denied: insufficient_scope"
        )
        job["step_logs"] = {
            "build": [
                "[09:45:29] #1 [internal] load build definition from Dockerfile",
                failure,
                "[09:45:30] ERROR build exit=1",
            ],
            "build:docker": ["[09:45:29] #1 [internal] load build definition from Dockerfile"],
            "build:verify": ["[09:45:30] ERROR build exit=1"],
        }

        docker = server.job_payload(job, step="build", sub="docker")
        self.assertIn(failure, docker["log"])
        verify = server.job_payload(job, step="build", sub="verify")
        self.assertNotIn("[09:45:30] ERROR build exit=1", verify["log"])

    def test_test_step_markers_route_to_ut_or_dt_sublogs(self) -> None:
        self.assertEqual("ut-cases", server._infer_test_log_substep("@@TEST_STEP@@ Temporal shell UT @@TEST_TYPE@@ ut"))
        self.assertEqual("dt-cases", server._infer_test_log_substep("@@TEST_STEP@@ Service Router DT @@TEST_TYPE@@ dt"))
        self.assertEqual("ut-cases", server._infer_test_log_substep("@@TEST_STEP@@ Temporal shell UT"))
        self.assertEqual("cases", server._infer_test_log_substep("@@TEST_STEP@@ CellMem pure UT/DT"))
        self.assertEqual("plan", server._infer_test_log_substep("Loaded test stages from .cid/build.yaml"))
        self.assertEqual("runner", server._infer_test_log_substep("tests start service=service-router sha=abc"))
        self.assertEqual("report", server._infer_test_log_substep("TEST summary status=passed total=3"))

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
        # The browser needs this non-rendered marker to leave test filtering
        # mode before showing build, push, and archive logs.
        self.assertIn(raw[7], visible)


class JobPipelineTests(unittest.TestCase):
    def test_running_job_marks_active_stage(self) -> None:
        job = make_job("pipe-run")
        job["stage"] = "building"
        job["current"] = "memory-service@main"
        payload = server.job_payload(job, compact=True)
        pipeline = payload["pipeline"]
        self.assertEqual("memory-service", pipeline["focus_service"])
        statuses = {step["id"]: step["status"] for step in pipeline["steps"]}
        self.assertEqual("done", statuses["sync"])
        self.assertEqual("done", statuses["test"])
        self.assertEqual("running", statuses["build"])
        self.assertEqual("pending", statuses["push"])

    def test_failed_tests_turn_red_while_later_steps_run(self) -> None:
        job = make_job("pipe-test-fail")
        job["stage"] = "building"
        job["current"] = "memory-service@main"
        job["test_status"] = "failed"
        payload = server.job_payload(job, compact=True)
        statuses = {step["id"]: step["status"] for step in payload["pipeline"]["steps"]}
        self.assertEqual("failed", statuses["test"])
        self.assertEqual("running", statuses["build"])
        job["test_kinds"] = ["ut", "dt"]
        job["test_runs"] = [{"commands": [{"test_type": "ut", "exit_code": 1}]}]
        test = next(step for step in server.job_payload(job, compact=True)["pipeline"]["steps"] if step["id"] == "test")
        sub = {item["id"]: item["status"] for item in test["subtasks"]}
        self.assertEqual("failed", sub["ut-cases"])
        self.assertEqual("skipped", sub["dt-cases"])

    def test_test_subtasks_follow_cid_kinds(self) -> None:
        job = make_job("pipe-kinds")
        job["test_kinds"] = ["ut"]
        payload = server.job_payload(job, compact=True)
        test = next(step for step in payload["pipeline"]["steps"] if step["id"] == "test")
        self.assertEqual("测试执行", test["label"])
        self.assertEqual(["plan", "runner", "ut-cases"], [item["id"] for item in test["subtasks"]])
        self.assertTrue(all("兼容" not in item["label"] for item in test["subtasks"]))

        job["test_kinds"] = ["ut", "dt"]
        test = next(step for step in server.job_payload(job, compact=True)["pipeline"]["steps"] if step["id"] == "test")
        self.assertEqual(["plan", "runner", "ut-cases", "dt-cases"], [item["id"] for item in test["subtasks"]])

        job["test_kinds"] = []
        test = next(step for step in server.job_payload(job, compact=True)["pipeline"]["steps"] if step["id"] == "test")
        self.assertEqual(["plan"], [item["id"] for item in test["subtasks"]])
        self.assertEqual("加载build.yaml", test["subtasks"][0]["label"])

    def test_pipeline_subtasks_hide_retry_and_cleanup(self) -> None:
        job = make_job("pipe-display")
        payload = server.job_payload(job, compact=True)
        by_id = {step["id"]: step for step in payload["pipeline"]["steps"]}
        self.assertEqual(["clone", "sha"], [item["id"] for item in by_id["sync"]["subtasks"]])
        self.assertEqual("执行构建脚本", next(item["label"] for item in by_id["build"]["subtasks"] if item["id"] == "script"))
        self.assertEqual(["tag", "push", "verify"], [item["id"] for item in by_id["push"]["subtasks"]])
        self.assertEqual(["save"], [item["id"] for item in by_id["archive"]["subtasks"]])
        self.assertEqual("归档镜像", by_id["archive"]["subtasks"][0]["label"])
        self.assertEqual("检查环境", payload["pipeline"]["prepare"][0]["label"])
        self.assertEqual(["env", "slot", "adir"], [item["id"] for item in payload["pipeline"]["prepare"]])
        env = next(item for item in payload["pipeline"]["prepare"] if item["id"] == "env")
        self.assertEqual(["docker", "disk"], [item["id"] for item in env["subtasks"]])
        self.assertEqual(["script", "docker", "verify"], [item["id"] for item in by_id["build"]["subtasks"]])

    def test_stopped_job_keeps_completed_steps(self) -> None:
        job = make_job("pipe-stop", status="stopped")
        job["stage"] = "done"
        job["stage_before_stop"] = "building"
        job["cancel_requested"] = True
        job["current"] = ""
        payload = server.job_payload(job, compact=True)
        statuses = {step["id"]: step["status"] for step in payload["pipeline"]["steps"]}
        self.assertEqual("done", statuses["sync"])
        self.assertEqual("done", statuses["test"])
        self.assertEqual("skipped", statuses["build"])
        self.assertEqual("skipped", statuses["push"])
        prepare = {item["id"]: item["status"] for item in payload["pipeline"]["prepare"]}
        self.assertEqual("skipped", prepare["adir"])
        self.assertEqual("done", prepare["slot"])

    def test_archive_only_service_skips_push_step(self) -> None:
        job = make_job("pipe-archive")
        job["stage"] = "archiving"
        job["service_id"] = "ops-router"
        job["service_ids"] = ["ops-router"]
        job["current"] = "ops-router@main"
        payload = server.job_payload(job, compact=True)
        step_ids = [step["id"] for step in payload["pipeline"]["steps"]]
        self.assertNotIn("push", step_ids)
        statuses = {step["id"]: step["status"] for step in payload["pipeline"]["steps"]}
        self.assertEqual("running", statuses["archive"])
        self.assertEqual("skipped", statuses.get("gamma", "skipped"))

    def test_gamma_optional_steps_default_skipped(self) -> None:
        job = make_job("pipe-gamma", status="ok")
        job["stage"] = "done"
        job["results"] = [{"service_id": "memory-service", "ok": True}]
        payload = server.job_payload(job, compact=True)
        steps = {step["id"]: step for step in payload["pipeline"]["steps"]}
        self.assertNotIn("gamma", steps)

    def test_gamma_hidden_while_running_if_not_selected(self) -> None:
        job = make_job("pipe-gamma-hidden", status="running")
        job["stage"] = "pushing"
        payload = server.job_payload(job, compact=True)
        steps = {step["id"]: step for step in payload["pipeline"]["steps"]}
        self.assertNotIn("gamma", steps)

    def test_gamma_optional_steps_selected_done(self) -> None:
        job = make_job("pipe-gamma-on", status="ok")
        job["stage"] = "done"
        job["optional_steps"] = {"gamma_deploy": True, "gamma_test": False}
        job["results"] = [{"service_id": "memory-service", "ok": True}]
        payload = server.job_payload(job, compact=True)
        steps = {step["id"]: step for step in payload["pipeline"]["steps"]}
        self.assertEqual("done", steps["gamma"]["status"])
        sub = {item["id"]: item["status"] for item in steps["gamma"]["subtasks"]}
        self.assertEqual(["deploy"], list(sub))
        self.assertEqual("done", sub["deploy"])

    def test_gamma_running_after_push_results(self) -> None:
        job = make_job("pipe-gamma-live", status="running")
        job["stage"] = "gamma"
        job["optional_steps"] = {"gamma_deploy": True, "gamma_test": True}
        job["results"] = [
            {
                "service_id": "memory-service",
                "ok": True,
                "remote": "swr.cn-southwest-2.myhuaweicloud.com/public_ai/memory-service:202609101200_abc",
            }
        ]
        payload = server.job_payload(job, compact=True)
        steps = {step["id"]: step for step in payload["pipeline"]["steps"]}
        self.assertEqual("running", steps["gamma"]["status"])
        self.assertEqual("done", steps["push"]["status"])
        sub = {item["id"]: item["status"] for item in steps["gamma"]["subtasks"]}
        self.assertEqual("running", sub["deploy"])
        self.assertEqual("pending", sub["test"])

    def test_gamma_failed_after_successful_push(self) -> None:
        job = make_job("pipe-gamma-fail", status="failed")
        job["stage"] = "gamma"
        job["error"] = "kubectl 升级 memory-service 失败"
        job["optional_steps"] = {"gamma_deploy": True, "gamma_test": False}
        job["results"] = [{"service_id": "memory-service", "ok": True}]
        payload = server.job_payload(job, compact=True)
        self.assertEqual("failed", payload["status"])
        steps = {step["id"]: step for step in payload["pipeline"]["steps"]}
        self.assertEqual("failed", steps["gamma"]["status"])
        self.assertEqual("done", steps["push"]["status"])
        sub = {item["id"]: item["status"] for item in steps["gamma"]["subtasks"]}
        self.assertEqual(["deploy"], list(sub))
        self.assertEqual("failed", sub["deploy"])

    def test_failed_sync_marks_first_step_failed(self) -> None:
        job = make_job("pipe-fail", status="failed")
        job["stage"] = "done"
        job["results"] = [
            {
                "service_id": "memory-service",
                "ok": False,
                "error": "git clone failed: repository not found",
                "commit_sha": "0000000000000000000000000000000000000000",
            }
        ]
        payload = server.job_payload(job, compact=True)
        statuses = {step["id"]: step["status"] for step in payload["pipeline"]["steps"]}
        self.assertEqual("failed", statuses["sync"])
        self.assertEqual("skipped", statuses["build"])
        self.assertTrue(payload["pipeline"]["prepare"])
        self.assertTrue(all("subtasks" in step for step in payload["pipeline"]["steps"]))
        prepare = {item["id"]: item["status"] for item in payload["pipeline"]["prepare"]}
        self.assertEqual("done", prepare["env"])
        sync = next(step for step in payload["pipeline"]["steps"] if step["id"] == "sync")
        sub = {item["id"]: item["status"] for item in sync["subtasks"]}
        self.assertEqual("failed", sub["clone"])
        self.assertEqual("skipped", sub["sha"])

    def test_failed_build_out_repo_is_not_prepare(self) -> None:
        job = make_job("pipe-out-repo", status="failed", service_id="agentlink")
        job["stage"] = "done"
        job["error"] = "1/1 failed: agentlink:mkdir: cannot create directory '/out/repo': No such file or directory"
        job["commit_sha"] = "f554aa18d5d60b35dd1c80ab4710e57fe6ae306e"
        job["test_status"] = "passed"
        job["results"] = [
            {
                "service_id": "agentlink",
                "ok": False,
                "error": "mkdir: cannot create directory '/out/repo': No such file or directory",
                "commit_sha": "f554aa18d5d60b35dd1c80ab4710e57fe6ae306e",
                "test_status": "passed",
            }
        ]
        payload = server.job_payload(job, compact=True)
        statuses = {step["id"]: step["status"] for step in payload["pipeline"]["steps"]}
        self.assertEqual("done", statuses["sync"])
        self.assertEqual("done", statuses["test"])
        self.assertEqual("failed", statuses["build"])
        self.assertEqual("skipped", statuses["push"])
        prepare = {item["id"]: item["status"] for item in payload["pipeline"]["prepare"]}
        self.assertEqual("done", prepare["env"])
        sync = next(step for step in payload["pipeline"]["steps"] if step["id"] == "sync")
        sub = {item["id"]: item["status"] for item in sync["subtasks"]}
        self.assertEqual("done", sub["clone"])
        self.assertEqual("done", sub["sha"])

    def test_buildkit_base_image_pull_denial_marks_build_not_push(self) -> None:
        job = make_job("pipe-buildkit-pull", status="failed", service_id="agentlink")
        job["stage"] = "done"
        job["commit_sha"] = "25e8855662c833938f96696dc720db5aefc80c71"
        job["test_status"] = "passed"
        job["error"] = (
            "ERROR: failed to solve: local/ai-python-build:3.11.15-v1: "
            "failed to resolve source metadata for docker.io/local/ai-python-build:3.11.15-v1: "
            "pull access denied: insufficient_scope"
        )
        job["results"] = [{
            "service_id": "agentlink",
            "ok": False,
            "error": job["error"],
            "commit_sha": job["commit_sha"],
            "test_status": "passed",
        }]

        statuses = {
            step["id"]: step["status"]
            for step in server.job_payload(job, compact=True)["pipeline"]["steps"]
        }
        self.assertEqual("failed", statuses["build"])
        self.assertEqual("skipped", statuses["push"])

    def test_failed_job_without_results_marks_active_step(self) -> None:
        job = make_job("pipe-interrupt", status="failed")
        job["stage"] = "interrupted"
        job["stage_before_stop"] = "testing"
        job["error"] = server.INTERRUPTED_JOB_ERROR
        job["results"] = []
        job["test_kinds"] = ["ut"]
        job["test_runs"] = [{"commands": [{"test_type": "ut", "exit_code": 0}]}]
        payload = server.job_payload(job, compact=True)
        statuses = {step["id"]: step["status"] for step in payload["pipeline"]["steps"]}
        self.assertEqual("done", statuses["sync"])
        self.assertEqual("failed", statuses["test"])
        self.assertEqual("skipped", statuses["build"])
        self.assertEqual("skipped", statuses["push"])
        test = next(step for step in payload["pipeline"]["steps"] if step["id"] == "test")
        sub = {item["id"]: item["status"] for item in test["subtasks"]}
        self.assertEqual("done", sub["plan"])
        self.assertEqual("done", sub["ut-cases"])
        prepare = {item["id"]: item["status"] for item in payload["pipeline"]["prepare"]}
        self.assertEqual("done", prepare["env"])
        self.assertEqual("done", prepare["slot"])
        self.assertEqual("skipped", prepare["adir"])

    def test_failed_prepare_job_marks_env_and_skips_steps(self) -> None:
        job = make_job("pipe-prep-fail", status="failed")
        job["stage"] = "interrupted"
        job["error"] = server.INTERRUPTED_JOB_ERROR
        job["results"] = []
        job["commit_sha"] = ""
        job["current"] = ""
        job["test_status"] = ""
        job["test_runs"] = []
        job["test_kinds"] = []
        payload = server.job_payload(job, compact=True)
        statuses = {step["id"]: step["status"] for step in payload["pipeline"]["steps"]}
        self.assertTrue(all(status == "skipped" for status in statuses.values()))
        prepare = {item["id"]: item["status"] for item in payload["pipeline"]["prepare"]}
        self.assertEqual("failed", prepare["env"])
        self.assertEqual("skipped", prepare["slot"])

    def test_ut_success_does_not_override_pending_test_step(self) -> None:
        job = {
            "test_kinds": ["ut"],
            "test_runs": [{"commands": [{"test_type": "ut", "exit_code": 0}]}],
        }
        pending = {item["id"]: item["status"] for item in server._subtask_rows("test", "pending", job)}
        self.assertEqual("pending", pending["ut-cases"])
        failed = {item["id"]: item["status"] for item in server._subtask_rows("test", "failed", job)}
        self.assertEqual("done", failed["ut-cases"])

    def test_build_fail_subtask_follows_error_class(self) -> None:
        jdk = {"error": "[es-build] ERROR: JDK bases missing"}
        rows = {item["id"]: item["status"] for item in server._subtask_rows("build", "failed", result=jdk)}
        self.assertEqual("failed", rows["script"])
        self.assertEqual("skipped", rows["docker"])
        self.assertEqual("skipped", rows["verify"])

        docker = {"error": "ERROR: failed to solve: process \"/bin/sh -c command -v logrotate\" exit code: 127"}
        rows = {item["id"]: item["status"] for item in server._subtask_rows("build", "failed", result=docker)}
        self.assertEqual("done", rows["script"])
        self.assertEqual("failed", rows["docker"])
        self.assertEqual("skipped", rows["verify"])

        verify = {"error": "build/package/build-image.sh did not produce local/mattermost:tag"}
        rows = {item["id"]: item["status"] for item in server._subtask_rows("build", "failed", result=verify)}
        self.assertEqual("done", rows["script"])
        self.assertEqual("done", rows["docker"])
        self.assertEqual("failed", rows["verify"])

    def test_pipeline_includes_meta_and_summary(self) -> None:
        job = make_job("pipe-meta")
        job["id"] = "pipe-meta"
        job["archive_dir"] = "/usr/share/nginx/html/images/demo-job"
        payload = server.job_payload(job, compact=True)
        pipeline = payload["pipeline"]
        self.assertEqual("pipe-meta", pipeline["meta"]["job_id"])
        self.assertIn("percent", pipeline["summary"])
        self.assertIn("artifacts", pipeline)


class SwrLoginProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.saved_cache = server._login_probe_cache
        self.saved_login = server._login_ok

    def tearDown(self) -> None:
        server._login_probe_cache = self.saved_cache
        server._login_ok = self.saved_login

    @patch.object(server, "docker_cmd", return_value=(1, "denied: no such manifest: demo"))
    def test_missing_manifest_wins_over_denied_word(self, _docker_cmd) -> None:
        with patch.object(server, "docker_config_has_swr_auth", return_value=True):
            self.assertTrue(server.check_login(force=True))

    @patch.object(server, "docker_cmd", return_value=(1, "manifest unknown"))
    def test_precise_missing_manifest_means_authenticated(self, docker_cmd) -> None:
        with patch.object(server, "docker_config_has_swr_auth", return_value=True):
            self.assertTrue(server.check_login(force=True))
        remote = docker_cmd.call_args.args[2]
        self.assertIn("robot-ci-auth-probe-does-not-exist", remote)
        self.assertNotIn("_", remote.rsplit("/", 1)[-1])

    @patch.object(server, "docker_cmd", return_value=(1, "dial tcp: i/o timeout"))
    def test_ambiguous_network_failure_is_not_login_success(self, _docker_cmd) -> None:
        with patch.object(server, "docker_config_has_swr_auth", return_value=True):
            self.assertFalse(server.check_login(force=True))

    @patch.object(server, "docker_cmd", return_value=(1, "unauthorized: authentication required"))
    def test_authentication_failure_is_rejected(self, _docker_cmd) -> None:
        with patch.object(server, "docker_config_has_swr_auth", return_value=True):
            self.assertFalse(server.check_login(force=True))

    @patch.object(server, "docker_config_has_swr_auth", return_value=False)
    @patch.object(server, "docker_cmd")
    def test_missing_local_credentials_skips_registry_probe(self, docker_cmd, _has_auth) -> None:
        self.assertFalse(server.check_login(force=True))
        docker_cmd.assert_not_called()


class BranchLookupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(server, "DB_PATH", server.Path(self.tmp.name) / "robot-ci.db")
        self.db_patch.start()
        with server._branch_cache_lock:
            server._branch_cache.clear()

    def tearDown(self) -> None:
        with server._branch_cache_lock:
            server._branch_cache.clear()
        self.db_patch.stop()
        self.tmp.cleanup()

    @patch.object(server, "gh_token", return_value="")
    @patch.object(server, "run_cmd", return_value=(0, ""))
    def test_empty_ssh_result_is_an_error_not_a_fake_main_branch(self, _run_cmd, _token) -> None:
        with patch.dict(server.CFG, {"github_use_ssh": True, "github_ssh_key": __file__}):
            ok, detail = server.list_branches_api("owner/repo", force=True)
        self.assertFalse(ok)
        self.assertIn("no branch refs", detail)

    @patch.object(
        server,
        "run_cmd",
        return_value=(0, "a refs/heads/main\nb refs/heads/feature/test\n"),
    )
    def test_success_is_cached_and_force_bypasses_cache(self, run_cmd) -> None:
        with patch.dict(server.CFG, {"github_use_ssh": True, "github_ssh_key": __file__}):
            first = server.list_branches_api("owner/repo")
            second = server.list_branches_api("owner/repo")
            refreshed = server.list_branches_api("owner/repo", force=True)
        self.assertEqual((True, ["feature/test", "main"]), first)
        self.assertEqual(first, second)
        self.assertEqual(first, refreshed)
        self.assertEqual(2, run_cmd.call_count)

    @patch.object(server, "gh_token", return_value="token")
    @patch.object(server, "urlopen")
    def test_github_api_fetches_more_than_one_page(self, urlopen, _token) -> None:
        class Response:
            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps(self.payload).encode("utf-8")

        urlopen.side_effect = [
            Response([{"name": f"branch-{index:03d}"} for index in range(100)]),
            Response([{"name": "branch-100"}]),
        ]
        with patch.dict(server.CFG, {"github_use_ssh": False, "github_ssh_key": ""}):
            ok, branches = server.list_branches_api("owner/repo", force=True)
        self.assertTrue(ok)
        self.assertEqual(101, len(branches))
        self.assertEqual(2, urlopen.call_count)


class BuildCommandTests(unittest.TestCase):
    def test_host_package_discovery_supports_cid_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = server.Path(temporary)
            output = workspace / ".cid" / "output"
            output.mkdir(parents=True)
            package = output / "ops-router_202609031800_abcdef1.tar.gz"
            package.write_bytes(b"archive")

            found = server.find_latest_host_package(
                {"tar_prefix": "ops-router_"},
                workspace,
            )

        self.assertEqual(package, found)

    def test_host_package_discovery_uses_cid_pattern(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = server.Path(temporary)
            output = workspace / ".cid" / "output"
            output.mkdir(parents=True)
            package = output / "custom-package.tar.gz"
            package.write_bytes(b"archive")

            found = server.find_latest_host_package(
                {
                    "tar_prefix": "legacy_",
                    "package_pattern": ".cid/output/custom-*.tar.gz",
                },
                workspace,
            )

        self.assertEqual(package, found)

    def test_report_only_test_policy_does_not_block_build(self) -> None:
        with patch.dict(server.CFG, {"test_policy": "report_only"}):
            self.assertFalse(server.tests_block_build({"status": "failed"}))
            self.assertFalse(server.tests_block_build({"status": "error"}))

    def test_blocking_test_policy_stops_failed_tests(self) -> None:
        with patch.dict(server.CFG, {"test_policy": "blocking"}):
            self.assertTrue(server.tests_block_build({"status": "failed"}))
            self.assertFalse(server.tests_block_build({"status": "passed"}))

    def test_cid_contract_does_not_require_root_build_scripts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = server.Path(temporary)
            (workspace / ".cid").mkdir()
            (workspace / ".cid" / "build.yaml").write_text(
                """version: 1
service:
  id: demo
  name: Demo
  language: shell
  image: demo
scripts:
  - id: ut
    name: Demo UT
    type: test
    test_type: ut
    enabled: false
    reason: none
  - id: dt
    name: Demo DT
    type: test
    test_type: dt
    enabled: false
    reason: none
  - id: build
    name: CID build
    type: build
    enabled: true
    command: bash build/package/build.sh
    timeout_sec: 60
artifacts:
  image:
    enabled: true
    delivery: swr
""",
                encoding="utf-8",
            )
            ok, detail = server.validate_cloned_repo_contract(
                workspace, {"id": "demo", "image": "demo"}
            )
        self.assertTrue(ok, detail)

    def test_missing_cid_and_build_entry_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = server.Path(temporary)
            ok, detail = server.validate_cloned_repo_contract(
                workspace, {"id": "demo", "image": "demo"}
            )
        self.assertFalse(ok)
        self.assertIn("missing .cid/build.yaml", detail)

    @patch.object(server, "run_stream", return_value=0)
    def test_cid_build_command_and_timeout_override_legacy_entrypoint(self, run_stream) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = server.Path(temporary)
            (workspace / ".cid").mkdir()
            (workspace / ".cid" / "build.yaml").write_text(
                """version: 1
service:
  id: demo
  name: Demo
  language: shell
  image: demo
scripts:
  - id: ut
    name: Demo UT
    type: test
    test_type: ut
    enabled: false
    reason: none
  - id: dt
    name: Demo DT
    type: test
    test_type: dt
    enabled: false
    reason: none
  - id: build
    name: CID build
    type: build
    enabled: true
    command: bash build/package/build.sh
    timeout_sec: 321
artifacts:
  image:
    enabled: true
    delivery: swr
""",
                encoding="utf-8",
            )
            svc = {"id": "demo", "image": "demo", "repo": "owner/demo"}
            with patch.object(server, "repo_dir", return_value=workspace):
                ok, detail = server.build_from_source("no-job", svc, "abcdef1")
        self.assertTrue(ok, detail)
        command = " ".join(run_stream.call_args.args[1])
        self.assertIn("bash build/package/build.sh", command)
        self.assertIn("CID_IMAGE=local/demo:", command)
        self.assertIn("find . -maxdepth 5", command)
        self.assertEqual(321, run_stream.call_args.kwargs["timeout"])

    def test_failure_summary_prefers_actionable_error(self) -> None:
        detail = server.summarize_command_failure(
            ["Step 8/10", "ERROR: fetch-lfs.sh missing cache", "build exited"],
            "build failed",
        )
        self.assertEqual("ERROR: fetch-lfs.sh missing cache", detail)

    @patch.object(server, "run_stream", return_value=0)
    def test_multica_server_passes_valid_version_to_build_cce(self, run_stream) -> None:
        svc = {
            "id": "multica-server",
            "image": "multica-server",
            "repo": "rollingfruit/multica-aiwelink",
            "build_action": "build-cce",
            "requires_version": True,
        }
        ok, detail = server.build_from_source("no-job", svc, "abcdef1", "v1.2.3")
        self.assertTrue(ok, detail)
        command = " ".join(run_stream.call_args.args[1])
        self.assertIn("DAEMON_RELEASE_VERSION=v1.2.3", command)
        self.assertIn("bash ./deploy.sh build-cce", command)

    @patch.object(server, "run_stream")
    def test_multica_server_rejects_invalid_version_before_shell(self, run_stream) -> None:
        svc = {"id": "multica-server", "requires_version": True}
        ok, detail = server.build_from_source("no-job", svc, "abcdef1", "latest")
        self.assertFalse(ok)
        self.assertIn("invalid daemon version", detail)
        run_stream.assert_not_called()

    @patch.object(server, "run_stream", return_value=0)
    def test_fleet_pack_receives_archive_directory_and_no_push_action(self, run_stream) -> None:
        svc = {
            "id": "multica-fleet",
            "image": "multica-fleet",
            "repo": "censong574-spec/multica-fleet",
            "build_action": "pack",
            "bundle_archive": True,
        }
        ok, detail = server.build_from_source(
            "no-job", svc, "abcdef1", archive_dir=server.Path("/tmp/fleet-output")
        )
        self.assertTrue(ok, detail)
        command = " ".join(run_stream.call_args.args[1])
        self.assertRegex(command, r"FLEET_OUTPUT_DIR=\S*[/\\]tmp[/\\]fleet-output")
        self.assertIn("INCLUDE_RUNTIME_IMAGES=1", command)
        self.assertIn("bash ./deploy.sh pack", command)

    @patch.object(server, "run_stream", return_value=0)
    def test_mattermost_build_never_sets_skip_package(self, run_stream) -> None:
        svc = {
            "id": "mattermost",
            "image": "mattermost",
            "repo": "rollingfruit/mattermost",
        }
        with patch.dict(
            server.os.environ,
            {"SKIP_PACKAGE": "1", "SKIP_WEBAPP_BUILD": "1", "SKIP_SERVER_BUILD": "1"},
            clear=False,
        ):
            ok, detail = server.build_from_source("no-job", svc, "1dd288a")
        self.assertTrue(ok, detail)
        command = " ".join(run_stream.call_args.args[1])
        self.assertNotIn("SKIP_PACKAGE=1", command)
        self.assertNotIn("SKIP_WEBAPP_BUILD=1", command)
        self.assertNotIn("SKIP_SERVER_BUILD=1", command)
        self.assertIn("unset SKIP_PACKAGE", command)
        env = run_stream.call_args.kwargs["env"]
        self.assertNotIn("SKIP_PACKAGE", env)
        self.assertNotIn("SKIP_WEBAPP_BUILD", env)
        self.assertNotIn("SKIP_SERVER_BUILD", env)


class FleetRuntimeCacheTests(unittest.TestCase):
    @staticmethod
    def write_valid_markers(workspace: server.Path, fingerprint: str = "a" * 64) -> None:
        marker_dir = workspace / "build" / "deploy" / "runtime-images"
        marker_dir.mkdir(parents=True, exist_ok=True)
        (marker_dir / ".build-source.sha256").write_text(fingerprint + "\n", encoding="utf-8")
        (marker_dir / ".build-image-ids").write_text(
            "multica-cloud-opencode:demo\tsha256:" + "b" * 64 + "\n"
            "multica-cloud-hermes:demo\tsha256:" + "c" * 64 + "\n",
            encoding="utf-8",
        )

    def test_markers_survive_disposable_workspace_and_are_restored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace_root = server.Path(tmp)
            workspace = workspace_root / "multica-fleet"
            self.write_valid_markers(workspace)
            with patch.dict(server.CFG, {"workspace_root": str(workspace_root)}):
                self.assertTrue(server.persist_fleet_runtime_cache("no-job", workspace))
                cache_dir = server.fleet_runtime_cache_dir()
                self.assertNotEqual(workspace, cache_dir.parent)

                server.shutil.rmtree(workspace)
                workspace.mkdir()
                self.assertTrue(server.restore_fleet_runtime_cache("no-job", workspace))

            restored = workspace / "build" / "deploy" / "runtime-images"
            self.assertEqual("a" * 64, (restored / ".build-source.sha256").read_text().strip())
            image_ids = (restored / ".build-image-ids").read_text(encoding="utf-8")
            self.assertIn("multica-cloud-opencode:demo\tsha256:", image_ids)
            self.assertIn("multica-cloud-hermes:demo\tsha256:", image_ids)

    def test_invalid_or_partial_metadata_never_replaces_good_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace_root = server.Path(tmp)
            workspace = workspace_root / "multica-fleet"
            self.write_valid_markers(workspace)
            with patch.dict(server.CFG, {"workspace_root": str(workspace_root)}):
                self.assertTrue(server.persist_fleet_runtime_cache("no-job", workspace))
                cached_fingerprint = (
                    server.fleet_runtime_cache_dir() / ".build-source.sha256"
                ).read_text(encoding="utf-8")

                (workspace / "build" / "deploy" / "runtime-images" / ".build-image-ids").write_text(
                    "not trusted metadata\n", encoding="utf-8"
                )
                self.assertFalse(server.persist_fleet_runtime_cache("no-job", workspace))
                self.assertEqual(
                    cached_fingerprint,
                    (server.fleet_runtime_cache_dir() / ".build-source.sha256").read_text(
                        encoding="utf-8"
                    ),
                )


class FleetBundleArtifactTests(unittest.TestCase):
    def test_bundle_is_downloadable_and_auxiliary_files_are_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = server.Path(tmp) / "multica-fleet_bundle_test.tar"
            bundle.write_bytes(b"bundle")
            bundle.chmod(0o600)
            metadata = server.Path(str(bundle) + ".meta")
            metadata.write_text("FLEET_IMAGE=local/test\n", encoding="utf-8")
            checksum = server.Path(str(bundle) + ".sha256")
            checksum.write_text("0" * 64 + "  " + bundle.name + "\n", encoding="utf-8")

            with patch.object(server.Path, "chmod", autospec=True) as chmod:
                self.assertTrue(server.finalize_fleet_bundle_artifacts("no-job", bundle))

            self.assertEqual([call(bundle, 0o644), call(checksum, 0o644)], chmod.call_args_list)
            self.assertTrue(metadata.is_file())
            self.assertTrue(checksum.is_file())


class DaemonVersionStateTests(unittest.TestCase):
    def test_server_state_round_trip_and_format_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = server.Path(tmp) / "last-daemon-version.json"
            with patch.object(server, "LAST_DAEMON_VERSION_PATH", state_path):
                self.assertEqual("", server.load_last_daemon_version())
                self.assertFalse(server.save_last_daemon_version("1.2.3"))
                self.assertTrue(server.save_last_daemon_version("v1.2.3"))
                self.assertEqual("v1.2.3", server.load_last_daemon_version())


class JobEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        with server._jobs_lock:
            self.saved_jobs = dict(server._jobs)
            server._jobs.clear()
        server.reset_job_scheduler()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.httpd.server_port}"
        self.opener = build_opener(ProxyHandler({}))
        server.ensure_default_users()
        login = Request(
            self.base_url + "/api/auth/login",
            data=json.dumps(
                {"username": server.DEFAULT_USERNAME, "password": server.DEFAULT_PASSWORD}
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.opener.open(login, timeout=2) as response:
            cookie = response.headers.get("Set-Cookie") or ""
        self.cookie = cookie.split(";", 1)[0]

    def _open(self, path: str, data: bytes | None = None, method: str = "GET"):
        headers = {"Content-Type": "application/json", "Cookie": self.cookie}
        request = Request(
            self.base_url + path,
            data=data,
            headers=headers,
            method=method,
        )
        return self.opener.open(request, timeout=2)

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        server.reset_job_scheduler()
        with server._job_procs_lock:
            server._job_procs.clear()
        with server._jobs_lock:
            server._jobs.clear()
            server._jobs.update(self.saved_jobs)

    def test_stop_endpoint_stops_running_job(self) -> None:
        job = make_job("active-stop")
        self.assertIsNone(server.register_job_if_idle(job))
        with self._open("/api/jobs/active-stop/stop", data=b"{}", method="POST") as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertTrue(payload["ok"])
        self.assertEqual("stopping", payload["status"])
        with server._jobs_lock:
            self.assertTrue(server._jobs["active-stop"]["cancel_requested"])

    def test_stop_endpoint_rejects_idle_and_missing_jobs(self) -> None:
        job = make_job("done-stop", status="failed")
        self.assertIsNone(server.register_job_if_idle(job))
        with self.assertRaises(HTTPError) as raised:
            self._open("/api/jobs/done-stop/stop", data=b"{}", method="POST")
        self.assertEqual(409, raised.exception.code)
        with self.assertRaises(HTTPError) as missing_err:
            self._open("/api/jobs/deadbeefdead/stop", data=b"{}", method="POST")
        self.assertEqual(404, missing_err.exception.code)

    def test_running_endpoint_is_lightweight_and_push_queues_past_slot_cap(self) -> None:
        catalog = [item["id"] for item in server.load_services()]
        job = make_job("active-job", service_id=catalog[0])
        job["log"] = ["large raw log"]
        job["slot_held"] = True
        self.assertIsNone(server.register_job_if_idle(job))

        with self._open("/api/running-job") as response:
            active = json.loads(response.read().decode("utf-8"))
        self.assertEqual("active-job", active["id"])
        self.assertNotIn("log", active)
        self.assertNotIn("test_runs", active)

        for index in range(1, server.max_concurrent_jobs()):
            extra = make_job(f"active-job-{index}", service_id=catalog[index])
            extra["slot_held"] = True
            self.assertIsNone(server.register_job_if_idle(extra))

        sixth = catalog[server.max_concurrent_jobs()]
        with patch.object(server, "run_push_job"):
            with self._open(
                "/api/push",
                data=json.dumps({"items": [{"service_id": sixth, "branch": "main"}]}).encode(),
                method="POST",
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        self.assertTrue(payload.get("job_id"))
        with server._jobs_lock:
            created = server._jobs[payload["job_id"]]
        self.assertEqual(sixth, created["service_id"])
        self.assertIn(created["status"], ("running", "queued"))

    def test_push_hits_mattermost_service_busy(self) -> None:
        job = make_job("mm-active", service_id="mattermost")
        self.assertIsNone(server.register_job_if_idle(job))
        with patch.object(server, "check_docker", return_value={"ok": True, "detail": ""}):
            with self.assertRaises(HTTPError) as raised:
                self._open(
                    "/api/push",
                    data=json.dumps({"items": [{"service_id": "mattermost", "branch": "main"}]}).encode(),
                    method="POST",
                )
        self.assertEqual(409, raised.exception.code)
        payload = json.loads(raised.exception.read().decode("utf-8"))
        self.assertEqual("service_busy", payload["error_code"])
        self.assertEqual("mattermost", payload["service"])

    def test_multica_server_version_is_validated_before_build(self) -> None:
        with patch.object(server, "save_last_daemon_version") as save_version:
            with self.assertRaises(HTTPError) as raised:
                self._open(
                    "/api/push",
                    data=json.dumps(
                        {"items": [{"service_id": "multica-server", "branch": "main", "version": "1.2.3"}]}
                    ).encode(),
                    method="POST",
                )
            save_version.assert_not_called()
        self.assertEqual(400, raised.exception.code)
        payload = json.loads(raised.exception.read().decode("utf-8"))
        self.assertIn("vMAJOR.MINOR.PATCH", payload["error"])

    def test_services_expose_version_and_archive_capabilities(self) -> None:
        with self._open("/api/services") as response:
            payload = json.loads(response.read().decode("utf-8"))
        services = {item["id"]: item for item in payload["services"]}
        self.assertTrue(services["multica-server"]["requires_version"])
        self.assertEqual("v1.2.3", services["multica-server"]["version_example"])
        self.assertIn("last_version", services["multica-server"])
        self.assertTrue(services["multica-fleet"]["archive_only"])
        self.assertEqual("censong574-spec/multica-fleet", services["multica-fleet"]["repo"])

    @patch.object(server, "list_branches_api", return_value=(False, "SSH timeout"))
    def test_branch_lookup_uses_default_until_user_refreshes(self, lookup) -> None:
        with patch.object(server, "cached_branches", return_value=[]):
            with self._open("/api/services/memory-service/branches") as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(["main"], payload["branches"])
            self.assertFalse(payload["cached"])
            lookup.assert_not_called()

        with self.assertRaises(HTTPError) as raised:
            self._open("/api/services/memory-service/branches?refresh=1")
        self.assertEqual(503, raised.exception.code)
        payload = json.loads(raised.exception.read().decode("utf-8"))
        self.assertEqual("branch lookup failed", payload["error"])
        self.assertEqual("SSH timeout", payload["detail"])
        lookup.assert_called_once_with("rollingfruit/CellMem", force=True)

    @patch.object(server, "list_branches_api", return_value=(True, ["main", "feature/latest"]))
    def test_branch_retry_forces_backend_refresh(self, lookup) -> None:
        with self._open("/api/services/memory-service/branches?refresh=1") as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(["main", "feature/latest"], payload["branches"])
        self.assertEqual("main", payload["selected_branch"])
        lookup.assert_called_once_with("rollingfruit/CellMem", force=True)

    @patch.object(server, "list_branches_api", return_value=(False, "SSH timeout"))
    @patch.object(server, "cached_branches", return_value=["main", "feature/cached"])
    def test_branch_refresh_falls_back_to_persisted_cache(self, cached, lookup) -> None:
        with self._open("/api/services/memory-service/branches?refresh=1") as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(["main", "feature/cached"], payload["branches"])
        self.assertTrue(payload["cached"])
        self.assertIn("远程刷新失败", payload["refresh_error"])
        cached.assert_called_once_with("rollingfruit/CellMem")
        lookup.assert_called_once_with("rollingfruit/CellMem", force=True)


class DiskPruneAndArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        with server._ci_disk_reclaim_lock:
            server._ci_disk_reclaimed_jobs.clear()
        with server._jobs_lock:
            self.saved_jobs = dict(server._jobs)
            server._jobs.clear()
        self.tmp = tempfile.TemporaryDirectory()
        root = server.Path(self.tmp.name)
        self.archive_root = root / "images"
        self.archive_root.mkdir()
        self.workspace_root = root / "workspaces"
        self.workspace_root.mkdir()
        self.ci_tmp_root = root / "ci-tmp"
        self.ci_tmp_root.mkdir()
        self.log_dir = root / "logs"
        self.log_dir.mkdir()
        self.cfg = patch.dict(
            server.CFG,
            {
                "archive_root": str(self.archive_root),
                "workspace_root": str(self.workspace_root),
                "ci_tmp_root": str(self.ci_tmp_root),
            },
        )
        self.log = patch.object(server, "LOG_DIR", self.log_dir)
        self.job_log = patch.object(server, "append_job_log")
        self.cfg.start()
        self.log.start()
        self.job_log.start()

    def tearDown(self) -> None:
        self.job_log.stop()
        self.log.stop()
        self.cfg.stop()
        self.tmp.cleanup()
        with server._jobs_lock:
            server._jobs.clear()
            server._jobs.update(self.saved_jobs)

    def _disk(self, used: int, total: int = 1000):
        return type("usage", (), {"total": total, "used": used, "free": total - used})()

    def _stamp_dir(self, name: str, filename: str = "pkg.tar") -> server.Path:
        folder = self.archive_root / name
        folder.mkdir()
        payload = folder / filename
        payload.write_bytes(b"tar")
        return payload

    def test_disk_usage_ratio_matches_df_when_blocks_are_reserved(self) -> None:
        # Same shape as the CI host: df Size=493G Used=394G Avail=79G Use%=84%.
        usage = type("usage", (), {"total": 493, "used": 394, "free": 79})()
        with patch.object(server.shutil, "disk_usage", return_value=usage):
            ratio, total, free = server.disk_usage_ratio("/")
        self.assertEqual(total, 493)
        self.assertEqual(free, 79)
        self.assertAlmostEqual(ratio, (493 - 79) / 493)
        self.assertGreaterEqual(ratio, 0.80)
        self.assertLess(394 / 493, 0.80)

    def test_prune_skips_when_usage_below_80_percent(self) -> None:
        for name in ("20260801000000", "20260802000000", "20260803000000", "20260804000000"):
            self._stamp_dir(name)
        with patch.object(server.shutil, "disk_usage", return_value=self._disk(700)):
            server.prune_nginx_archives("job-1")
        remaining = sorted(p.name for p in self.archive_root.iterdir())
        self.assertEqual(
            ["20260801000000", "20260802000000", "20260803000000", "20260804000000"],
            remaining,
        )

    def test_prune_deletes_oldest_until_usage_drops(self) -> None:
        for name in ("20260801000000", "20260802000000", "20260803000000", "20260804000000"):
            self._stamp_dir(name)

        def fake_usage(_path):
            count = len(list(self.archive_root.iterdir()))
            used = 900 if count >= 4 else 700
            return self._disk(used)

        with patch.object(server.shutil, "disk_usage", side_effect=fake_usage):
            server.prune_nginx_archives("job-1")
        remaining = sorted(p.name for p in self.archive_root.iterdir())
        self.assertEqual(["20260802000000", "20260803000000", "20260804000000"], remaining)

    def test_missing_archive_is_expired_and_paginated(self) -> None:
        kept = self._stamp_dir("20260804000000", "new.tar")
        gone = self.archive_root / "20260801000000" / "old.tar"
        server.record_build_artifact(
            {
                "created_at": "2026-08-01 10:00:00",
                "service_id": "old-svc",
                "title": "old",
                "archive": str(gone),
                "package_name": "old.tar",
            }
        )
        server.record_build_artifact(
            {
                "created_at": "2026-08-04 10:00:00",
                "service_id": "new-svc",
                "title": "new",
                "archive": str(kept),
                "package_name": "new.tar",
            }
        )
        page = server.list_build_artifacts(page=1, page_size=1)
        self.assertEqual(2, page["total"])
        self.assertEqual(2, page["page_count"])
        self.assertEqual(1, page["expired_count"])
        self.assertEqual("new-svc", page["artifacts"][0]["service_id"])
        self.assertFalse(page["artifacts"][0]["expired"])
        self.assertTrue(page["artifacts"][0]["download_url"])

        page2 = server.list_build_artifacts(page=2, page_size=1)
        self.assertEqual("old-svc", page2["artifacts"][0]["service_id"])
        self.assertTrue(page2["artifacts"][0]["expired"])
        self.assertEqual("", page2["artifacts"][0]["download_url"])

        filtered = server.list_build_artifacts(page=1, page_size=10, service_id="old-svc")
        self.assertEqual(1, filtered["total"])
        self.assertEqual("old-svc", filtered["artifacts"][0]["service_id"])
        self.assertEqual("old-svc", filtered["service_id"])

    def test_prune_expires_artifact_records(self) -> None:
        old = self._stamp_dir("20260801000000", "old.tar")
        new = self._stamp_dir("20260804000000", "new.tar")
        extra = self._stamp_dir("20260802000000", "mid.tar")
        server.record_build_artifact(
            {"created_at": "a", "service_id": "old-svc", "archive": str(old), "package_name": "old.tar"}
        )
        server.record_build_artifact(
            {"created_at": "b", "service_id": "mid-svc", "archive": str(extra), "package_name": "mid.tar"}
        )
        server.record_build_artifact(
            {"created_at": "c", "service_id": "new-svc", "archive": str(new), "package_name": "new.tar"}
        )
        with patch.object(server.shutil, "disk_usage", return_value=self._disk(900)):
            server.prune_nginx_archives("job-1", keep_latest=3, min_keep=1)
        page = server.list_build_artifacts(page=1, page_size=10)
        by_id = {item["service_id"]: item for item in page["artifacts"]}
        self.assertTrue(by_id["old-svc"]["expired"])
        self.assertTrue(by_id["mid-svc"]["expired"])
        self.assertFalse(by_id["new-svc"]["expired"])
        self.assertEqual(2, page["expired_count"])
        self.assertTrue((self.archive_root / "20260804000000").is_dir())
        self.assertFalse((self.archive_root / "20260801000000").exists())

    def test_reclaim_swap_removes_cache_and_ci_tmp_files(self) -> None:
        root = server.Path(self.tmp.name)
        cache = root / "build-cache"
        ci_tmp = root / "ci-tmp"
        nested = ci_tmp / "mattermost-build-cache.abc"
        cache.mkdir()
        nested.mkdir(parents=True)
        (cache / "build.swap").write_bytes(b"swap-a")
        (nested / "build.swap").write_bytes(b"swap-b")
        (ci_tmp / "keep.txt").write_text("ok", encoding="utf-8")
        with patch.dict(server.CFG, {"build_cache_root": str(cache), "ci_tmp_root": str(ci_tmp)}):
            with patch.object(server, "run_cmd", return_value=(0, "")) as run_cmd:
                removed = server.reclaim_build_swap("job-1")
        self.assertEqual(2, removed)
        self.assertFalse((cache / "build.swap").exists())
        self.assertFalse((nested / "build.swap").exists())
        self.assertTrue((ci_tmp / "keep.txt").is_file())
        if server.os.name != "nt":
            self.assertTrue(any(args[0][0] == "swapoff" for args, _ in run_cmd.call_args_list))

    def test_reclaim_swap_skips_when_other_mattermost_running(self) -> None:
        with server._jobs_lock:
            saved = dict(server._jobs)
            server._jobs.clear()
            server._jobs["mm-run"] = make_job("mm-run", service_id="mattermost")
        try:
            root = server.Path(self.tmp.name)
            cache = root / "build-cache-busy"
            cache.mkdir()
            (cache / "build.swap").write_bytes(b"swap-live")
            with patch.dict(server.CFG, {"build_cache_root": str(cache), "ci_tmp_root": str(root / "empty-ci")}):
                removed = server.reclaim_build_swap("other-job")
            self.assertEqual(0, removed)
            self.assertTrue((cache / "build.swap").exists())
        finally:
            with server._jobs_lock:
                server._jobs.clear()
                server._jobs.update(saved)

    def test_reclaim_swap_runs_when_finishing_job_is_the_mattermost_job(self) -> None:
        with server._jobs_lock:
            saved = dict(server._jobs)
            server._jobs.clear()
            server._jobs["mm-run"] = make_job("mm-run", service_id="mattermost")
        try:
            root = server.Path(self.tmp.name)
            cache = root / "build-cache-self"
            cache.mkdir()
            (cache / "build.swap").write_bytes(b"swap-done")
            with patch.dict(server.CFG, {"build_cache_root": str(cache), "ci_tmp_root": str(root / "empty-ci-self")}):
                with patch.object(server, "run_cmd", return_value=(0, "")):
                    removed = server.reclaim_build_swap("mm-run")
            self.assertEqual(1, removed)
            self.assertFalse((cache / "build.swap").exists())
        finally:
            with server._jobs_lock:
                server._jobs.clear()
                server._jobs.update(saved)

    @patch.object(server, "docker_cmd", return_value=(0, "Total: 1GB"))
    def test_reclaim_preserves_builder_cache_below_80_percent(self, docker_cmd) -> None:
        with patch.object(server.shutil, "disk_usage", return_value=self._disk(700)):
            with patch.object(server, "reclaim_build_swap", return_value=0):
                server.reclaim_ci_disk("job-1")
        prune_args = [call.args for call in docker_cmd.call_args_list if len(call.args) >= 3]
        self.assertNotIn(("builder", "prune", "-af"), prune_args)
        self.assertNotIn(("image", "prune", "-af"), prune_args)

    @patch.object(server, "ensure_protected_image_holds")
    @patch.object(server, "docker_cmd", return_value=(0, "Total: 32GB"))
    def test_reclaim_prunes_builder_then_images_when_still_over_80(
        self, docker_cmd, ensure_holds
    ) -> None:
        self._stamp_dir("20260804000000")
        with patch.object(server.shutil, "disk_usage", return_value=self._disk(960)):
            with patch.object(server, "reclaim_build_swap", return_value=0):
                server.reclaim_ci_disk("job-1")
        ensure_holds.assert_called()
        prune_args = [call.args for call in docker_cmd.call_args_list if len(call.args) >= 3]
        self.assertIn(("image", "prune", "-af"), prune_args)
        self.assertIn(
            ("builder", "prune", "-af", "--filter", "until=168h", "--keep-storage", "50GB"),
            prune_args,
        )
        builder_index = prune_args.index(
            ("builder", "prune", "-af", "--filter", "until=168h", "--keep-storage", "50GB")
        )
        image_index = prune_args.index(("image", "prune", "-af"))
        self.assertLess(builder_index, image_index)

    @patch.object(server, "ensure_protected_image_holds")
    @patch.object(server, "docker_cmd", return_value=(0, "Total: 32GB"))
    def test_reclaim_prunes_images_even_when_archives_drop_below_80(
        self, docker_cmd, ensure_holds
    ) -> None:
        for name in ("20260801000000", "20260802000000", "20260803000000", "20260804000000"):
            self._stamp_dir(name)

        def fake_usage(_path):
            count = len(list(self.archive_root.iterdir()))
            used = 850 if count >= 4 else 700
            return self._disk(used)

        with patch.object(server.shutil, "disk_usage", side_effect=fake_usage):
            with patch.object(server, "reclaim_build_swap", return_value=0):
                server.reclaim_ci_disk("job-1")
        ensure_holds.assert_called()
        prune_args = [call.args for call in docker_cmd.call_args_list if len(call.args) >= 3]
        self.assertIn(
            ("builder", "prune", "-af", "--filter", "until=168h", "--keep-storage", "50GB"),
            prune_args,
        )
        self.assertIn(("image", "prune", "-af"), prune_args)

    def test_gc_all_idle_clone_dirs_keeps_public_service_and_running_jobs(self) -> None:
        svc = {
            "id": "memory-service",
            "repo": "rollingfruit/CellMem",
            "github": "https://github.com/rollingfruit/CellMem.git",
        }
        live = server.repo_dir(svc, "livejob00aaaa")
        idle = self.workspace_root / "kibana-service--deadjob00bbbb"
        public = self.workspace_root / "public-service"
        cache = self.workspace_root / ".robot-ci-cache"
        leftover_tmp = self.ci_tmp_root / "runtime-apt-debs.abc123"
        keep_tmp = self.ci_tmp_root / "node-compile-cache"
        for path in (live, idle, public, cache, leftover_tmp, keep_tmp):
            path.mkdir()
        job = make_job("livejob00aaaa")
        self.assertIsNone(server.register_job_if_idle(job))
        server.gc_all_idle_clone_dirs("livejob00aaaa")
        server.reclaim_ci_tmp_leftovers("livejob00aaaa")
        self.assertTrue(live.is_dir())
        self.assertTrue(public.is_dir())
        self.assertTrue(cache.is_dir())
        self.assertFalse(idle.exists())
        self.assertFalse(leftover_tmp.exists())
        self.assertTrue(keep_tmp.is_dir())

    def test_reclaim_ci_tmp_skips_when_other_job_live(self) -> None:
        go_work = self.ci_tmp_root / "go-build12345"
        ops_build = self.ci_tmp_root / "ops-router-build"
        go_work.mkdir()
        ops_build.mkdir()
        with server._jobs_lock:
            saved = dict(server._jobs)
            server._jobs.clear()
            server._jobs["other"] = make_job("other", service_id="temporal")
        try:
            server.reclaim_ci_tmp_leftovers("finishing-job")
            self.assertTrue(go_work.is_dir())
            self.assertTrue(ops_build.is_dir())
        finally:
            with server._jobs_lock:
                server._jobs.clear()
                server._jobs.update(saved)

    def test_gc_keeps_workspaces_marked_live_by_other_instance(self) -> None:
        other_job_id = "abcdef01bbbb"
        other = self.workspace_root / ("mattermost--" + other_job_id)
        idle = self.workspace_root / "mattermost--deadjob02cccc"
        other.mkdir(parents=True)
        idle.mkdir(parents=True)
        server.register_live_job_marker(other_job_id, ["mattermost"])
        server.gc_all_idle_clone_dirs("livejob00aaaa")
        self.assertTrue(other.is_dir())
        self.assertFalse(idle.exists())
        server.unregister_live_job_marker(other_job_id)


class BuildHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.log_dir = server.Path(self.tmp.name)
        self.saved_jobs = dict(server._jobs)
        with server._jobs_lock:
            server._jobs.clear()
        self.log = patch.object(server, "LOG_DIR", self.log_dir)
        self.log.start()

    def tearDown(self) -> None:
        self.log.stop()
        with server._jobs_lock:
            server._jobs.clear()
            server._jobs.update(self.saved_jobs)
        self.tmp.cleanup()

    def _write_meta(
        self,
        job_id: str,
        client_id: str,
        created_at: str,
        service_id: str = "temporal",
        finished_at: str = "",
    ) -> None:
        payload = {
            "id": job_id,
            "client_id": client_id,
            "created_at": created_at,
            "service_id": service_id,
            "service_ids": [service_id],
            "branch": "main",
            "status": "ok",
        }
        if finished_at:
            payload["finished_at"] = finished_at
        (self.log_dir / f"job-{job_id}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    def test_history_is_filtered_by_client_and_paginated(self) -> None:
        mine = "cmsqxcxcesgairws0"
        other = "otherclientid0001"
        self._write_meta("aaaaaaaaaaaa", mine, "2026-08-15 10:00:00", "temporal")
        self._write_meta("bbbbbbbbbbbb", mine, "2026-08-15 11:00:00", "agentlink")
        self._write_meta("cccccccccccc", other, "2026-08-15 12:00:00", "mattermost")
        empty = server.list_build_history(client_id="", page=1, page_size=10)
        self.assertEqual(3, empty["total"])
        self.assertEqual("cccccccccccc", empty["jobs"][0]["id"])
        page = server.list_build_history(client_id=mine, page=1, page_size=1)
        self.assertEqual(2, page["total"])
        self.assertEqual(2, page["page_count"])
        self.assertEqual("bbbbbbbbbbbb", page["jobs"][0]["id"])
        self.assertEqual("agentlink", page["jobs"][0]["service_id"])
        page2 = server.list_build_history(client_id=mine, page=2, page_size=1)
        self.assertEqual("aaaaaaaaaaaa", page2["jobs"][0]["id"])

    def test_history_is_filtered_by_service_id(self) -> None:
        mine = "cmsqxcxcesgairws0"
        self._write_meta("aaaaaaaaaaaa", mine, "2026-08-15 10:00:00", "temporal")
        self._write_meta("bbbbbbbbbbbb", mine, "2026-08-15 11:00:00", "agentlink")
        page = server.list_build_history(page=1, page_size=10, service_id="agentlink")
        self.assertEqual(1, page["total"])
        self.assertEqual("bbbbbbbbbbbb", page["jobs"][0]["id"])
        self.assertEqual("agentlink", page["service_id"])

    def test_history_row_includes_duration_for_finished_jobs(self) -> None:
        self._write_meta(
            "aaaaaaaaaaaa",
            "cmsqxcxcesgairws0",
            "2026-08-15 10:00:00",
            "temporal",
            finished_at="2026-08-15 10:05:30",
        )
        page = server.list_build_history(page=1, page_size=10)
        self.assertEqual(330, page["jobs"][0]["duration_sec"])

    def test_history_includes_in_memory_running_job(self) -> None:
        mine = "cmsqxcxcesgairws0"
        job = make_job("dddddddddddd")
        job["client_id"] = mine
        job["created_at"] = "2026-08-15 13:00:00"
        job["service_id"] = "memory-service"
        with server._jobs_lock:
            server._jobs[job["id"]] = job
        page = server.list_build_history(client_id=mine, page=1, page_size=10)
        self.assertEqual(1, page["total"])
        self.assertEqual("dddddddddddd", page["jobs"][0]["id"])
        self.assertEqual("running", page["jobs"][0]["status"])
        self.assertIsNone(page["jobs"][0]["duration_sec"])

    def test_reap_orphaned_running_jobs_marks_disk_jobs_failed(self) -> None:
        running = {
            "id": "eeeeeeeeeeee",
            "client_id": "cmsqxcxcesgairws0",
            "status": "running",
            "stage": "building",
            "error": None,
        }
        finished = {
            "id": "ffffffffffff",
            "client_id": "cmsqxcxcesgairws0",
            "status": "ok",
            "stage": "done",
        }
        (self.log_dir / "job-eeeeeeeeeeee.json").write_text(
            json.dumps(running), encoding="utf-8"
        )
        (self.log_dir / "job-ffffffffffff.json").write_text(
            json.dumps(finished), encoding="utf-8"
        )
        (self.log_dir / "job-eeeeeeeeeeee.log").write_text("still going\n", encoding="utf-8")
        self.assertEqual(1, server.reap_orphaned_running_jobs())
        dead = json.loads((self.log_dir / "job-eeeeeeeeeeee.json").read_text(encoding="utf-8"))
        kept = json.loads((self.log_dir / "job-ffffffffffff.json").read_text(encoding="utf-8"))
        self.assertEqual("failed", dead["status"])
        self.assertEqual("interrupted", dead["stage"])
        self.assertEqual("building", dead["stage_before_stop"])
        self.assertEqual(server.INTERRUPTED_JOB_ERROR, dead["error"])
        self.assertEqual("ok", kept["status"])
        log_text = (self.log_dir / "job-eeeeeeeeeeee.log").read_text(encoding="utf-8")
        self.assertIn(server.INTERRUPTED_JOB_ERROR, log_text)

    def test_prune_build_history_trims_to_250_when_over_500(self) -> None:
        mine = "cmsqxcxcesgairws0"
        for index in range(501):
            job_id = f"{index:012d}"
            payload = {
                "id": job_id,
                "client_id": mine,
                "created_at": f"2026-08-01T{index:05d}",
                "service_id": "temporal",
                "status": "ok",
            }
            (self.log_dir / f"job-{job_id}.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )
            (self.log_dir / f"job-{job_id}.log").write_text("log\n", encoding="utf-8")
        removed = server.prune_build_history(mine)
        self.assertEqual(251, removed)
        remaining = sorted(path.stem.replace("job-", "") for path in self.log_dir.glob("job-*.json"))
        self.assertEqual(250, len(remaining))
        self.assertNotIn("000000000000", remaining)
        self.assertIn("000000000251", remaining)
        self.assertIn("000000000500", remaining)

    def test_record_build_artifact_keeps_full_history(self) -> None:
        path = server.artifacts_log_path()
        for index in range(101):
            server.record_build_artifact(
                {
                    "created_at": f"2026-08-01 {index:02d}:00:00",
                    "service_id": f"svc-{index}",
                    "archive": f"/tmp/{index}.tar",
                    "operator": "l30042018",
                }
            )
        entries = server._parse_artifact_entries(path.read_text(encoding="utf-8"))
        self.assertEqual(101, len(entries))
        self.assertEqual("svc-0", entries[0]["service_id"])
        self.assertEqual("svc-100", entries[-1]["service_id"])
        self.assertEqual("l30042018", entries[-1]["operator"])


class BusinessImageAndTmpTests(unittest.TestCase):
    def test_protected_base_images_are_not_removed(self) -> None:
        self.assertTrue(server.is_protected_base_image("local/ai-go-toolchain:1.26.4"))
        self.assertTrue(server.is_protected_base_image("local/ai-jdk-build:21.0.12"))
        self.assertTrue(server.is_protected_base_image("local/ai-jdk-runtime:21.0.12"))
        self.assertTrue(server.is_protected_base_image("local/ai-ubuntu-build:22.04"))
        self.assertTrue(server.is_protected_base_image("local/ai-python-build:3.11.15"))
        self.assertTrue(server.is_protected_base_image("local/ai-python-runtime:3.11.15"))
        self.assertTrue(server.is_protected_base_image("local/ai-node-build:20.19.2"))
        self.assertTrue(server.is_protected_base_image("local/ai-node-runtime:20.19.2"))
        self.assertTrue(server.is_protected_base_image("multica-cloud-opencode:demo"))
        self.assertTrue(server.is_protected_base_image("multica-cloud-hermes:demo"))
        self.assertTrue(server.is_protected_base_image("archive-only"))
        self.assertFalse(server.is_protected_base_image("local/temporal-server:202608151143_eecea40"))
        self.assertFalse(
            server.is_protected_base_image(
                "swr.cn-southwest-2.myhuaweicloud.com/public_ai/temporal-server:tag"
            )
        )

    @patch.object(server, "docker_cmd", return_value=(0, ""))
    @patch.object(server, "append_job_log")
    def test_remove_business_images_skips_toolchain(self, _log, docker_cmd) -> None:
        server.remove_business_images(
            "job-1",
            "local/temporal-server:tag",
            "local/ai-go-toolchain:1.26.4",
            "local/temporal-server:tag",
        )
        docker_cmd.assert_called_once_with("rmi", "-f", "local/temporal-server:tag", timeout=60)

    @patch.object(server, "ensure_protected_image_holds")
    @patch.object(server, "append_job_log")
    @patch.object(server, "docker_cmd", return_value=(0, ""))
    def test_reclaim_pins_keep_refs_before_image_prune(self, docker_cmd, _log, _holds) -> None:
        local_ref = "local/config-service:202608201544_2d45dd1"
        server.reclaim_unused_docker_images("job-1", keep_refs=(local_ref,))
        calls = [c.args[0:4] for c in docker_cmd.call_args_list]
        create_at = next(i for i, args in enumerate(calls) if args and args[0] == "create")
        prune_at = next(i for i, args in enumerate(calls) if args[:2] == ("image", "prune"))
        self.assertLess(create_at, prune_at)
        self.assertIn(local_ref, docker_cmd.call_args_list[create_at].args)

    @patch.object(server, "run_stream", return_value=0)
    def test_build_from_source_points_scratch_at_ci_tmp(self, run_stream) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = server.Path(temporary) / "ci-tmp"
            svc = {"id": "temporal", "image": "temporal-server", "repo": "rollingfruit/aiwelink-temporal"}
            with patch.dict(server.CFG, {"ci_tmp_root": str(root)}):
                ok, detail = server.build_from_source("no-job", svc, "eecea40")
            self.assertTrue(ok, detail)
            env = run_stream.call_args.kwargs["env"]
            self.assertEqual(env["TMPDIR"], str(root))
            self.assertEqual(env["GOTMPDIR"], str(root))
            self.assertEqual(env["DOCKER_TMPDIR"], str(root))
            self.assertTrue(root.is_dir())


class DockerReadyForPushTests(unittest.TestCase):
    def setUp(self) -> None:
        self.saved_cache = server._docker_cache
        server._docker_cache = None
        with server._jobs_lock:
            self.saved_jobs = dict(server._jobs)
            server._jobs.clear()

    def tearDown(self) -> None:
        server._docker_cache = self.saved_cache
        with server._jobs_lock:
            server._jobs.clear()
            server._jobs.update(self.saved_jobs)

    def test_running_job_ignores_failed_docker_cache(self) -> None:
        server._docker_cache = (time.time(), {"ok": False, "detail": "timeout"})
        self.assertIsNone(server.register_job_if_idle(make_job("job-a")))
        with patch.object(server, "check_docker") as probe:
            ready = server.docker_ready_for_push()
            probe.assert_not_called()
        self.assertTrue(ready["ok"])

    def test_missing_docker_binary_is_still_rejected(self) -> None:
        server._docker_cache = (time.time(), {"ok": False, "detail": "docker not installed"})
        ready = server.docker_ready_for_push()
        self.assertFalse(ready["ok"])

    def test_failed_cache_does_not_block_when_idle(self) -> None:
        server._docker_cache = (time.time(), {"ok": False, "detail": "busy"})
        with patch.object(server, "schedule_docker_probe") as sched:
            ready = server.docker_ready_for_push()
            sched.assert_called()
        self.assertTrue(ready["ok"])


class PublicServiceReuseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.saved_ready = server._public_service_ready_at
        server._public_service_ready_at = 0.0
        with server._jobs_lock:
            self.saved_jobs = dict(server._jobs)
            server._jobs.clear()

    def tearDown(self) -> None:
        server._public_service_ready_at = self.saved_ready
        with server._jobs_lock:
            server._jobs.clear()
            server._jobs.update(self.saved_jobs)

    def _tree(self, tmp: str) -> server.Path:
        dest = server.Path(tmp) / "public-service"
        (dest / ".git").mkdir(parents=True)
        rrd = dest / "windows-deploy" / "lib" / "source-rrd.sh"
        rrd.parent.mkdir(parents=True)
        rrd.write_text("ok", encoding="utf-8")
        return dest

    def test_reuses_existing_tree_when_fetch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = self._tree(tmp)
            with patch.dict(server.CFG, {"workspace_root": tmp}):
                with patch.object(server, "run_stream", return_value=1):
                    with patch.object(server, "run_cmd", return_value=(0, "abc1234")):
                        with patch.object(server, "append_job_log"):
                            ok, detail = server.ensure_public_service("ps-job")
            self.assertTrue(ok)
            self.assertEqual(str(dest), detail)

    def test_skips_fetch_when_recently_ready(self) -> None:
        server._public_service_ready_at = time.time()
        with tempfile.TemporaryDirectory() as tmp:
            self._tree(tmp)
            with patch.dict(server.CFG, {"workspace_root": tmp}):
                with patch.object(server, "run_stream") as stream:
                    with patch.object(server, "run_cmd", return_value=(0, "abc1234")):
                        with patch.object(server, "append_job_log"):
                            ok, _detail = server.ensure_public_service("ps-job")
            self.assertTrue(ok)
            stream.assert_not_called()


class BaseImageTargetTests(unittest.TestCase):
    def test_normalize_keeps_known_order(self) -> None:
        self.assertEqual(
            ["ubuntu", "openresty", "observability", "python"],
            server.normalize_base_image_targets(
                ["python", "observability", "openresty", "ubuntu", "openresty", "bogus", "python"]
            ),
        )

    def test_service_defaults_and_skips(self) -> None:
        self.assertEqual(["ubuntu"], server.base_image_targets_for_service({"id": "agentlink"}))
        self.assertEqual(
            ["python"],
            server.base_image_targets_for_service({"id": "agentlink", "base_image_targets": ["python"]}),
        )
        self.assertEqual(
            ["openresty"],
            server.base_image_targets_for_service({"id": "service-router", "base_image_targets": ["openresty"]}),
        )
        self.assertEqual([], server.base_image_targets_for_service({"id": "llm-gateway", "skip_public_service": True}))

    def test_union_for_batch(self) -> None:
        targets = server.base_image_targets_for_services(
            [
                {"id": "semantic-schedule", "base_image_targets": ["python"]},
                {"id": "service-router", "base_image_targets": ["openresty"]},
                {"id": "es-service", "base_image_targets": ["observability"]},
                {"id": "llm-gateway", "skip_public_service": True},
            ]
        )
        self.assertEqual(["openresty", "observability", "python"], targets)

    def test_ensure_versioned_base_images_passes_selected_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = server.Path(tmp)
            script = root / "public-service" / "scripts" / "build-versioned-base-images.sh"
            script.parent.mkdir(parents=True)
            script.write_text("#!/bin/bash\n", encoding="utf-8")
            captured: list[str] = []

            def fake_run_stream(job_id, cmd, **kwargs):
                del job_id, kwargs
                captured.append(" ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd))
                return 0

            with patch.dict(server.CFG, {"workspace_root": tmp}):
                with patch.object(server, "run_stream", side_effect=fake_run_stream):
                    with patch.object(server, "append_job_log"):
                        with patch.object(server, "bash_lc", side_effect=lambda s: ["bash", "-lc", s]):
                            with patch.object(server, "host_path", side_effect=lambda p: str(p)):
                                ok, detail = server.ensure_versioned_base_images("job-base", ["ubuntu", "openresty"])
            self.assertTrue(ok)
            self.assertEqual("", detail)
            self.assertEqual(1, len(captured))
            self.assertIn("build-versioned-base-images.sh", captured[0])
            self.assertIn(" ubuntu openresty", captured[0])
            self.assertNotIn(" all", captured[0])


class ConcurrentPrepareTests(unittest.TestCase):
    def setUp(self) -> None:
        with server._jobs_lock:
            self.saved_jobs = dict(server._jobs)
            server._jobs.clear()

    def tearDown(self) -> None:
        with server._jobs_lock:
            server._jobs.clear()
            server._jobs.update(self.saved_jobs)

    def test_append_job_log_writes_file_outside_jobs_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = server.Path(tmp) / "job-aa.log"
            job = make_job("logjob00aaaa")
            job["log_file"] = str(log_path)
            job["step_logs"] = {}
            with server._jobs_lock:
                server._jobs[job["id"]] = job
            server.append_job_log(job["id"], "hello from build")
            self.assertIn("hello from build", log_path.read_text(encoding="utf-8"))

    def test_running_job_meta_omits_step_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = server.Path(tmp)
            with patch.object(server, "LOG_DIR", log_dir):
                job = make_job("persistjobaaa")
                job["step_logs"] = {"prepare": ["line"]}
                with server._jobs_lock:
                    server._jobs[job["id"]] = job
                server.persist_job_meta(job["id"])
                meta = json.loads((log_dir / f"job-{job['id']}.json").read_text(encoding="utf-8"))
                self.assertNotIn("step_logs", meta)
                job["status"] = "ok"
                server.persist_job_meta(job["id"])
                meta = json.loads((log_dir / f"job-{job['id']}.json").read_text(encoding="utf-8"))
                self.assertEqual({"prepare": ["line"]}, meta.get("step_logs"))

    def test_ensure_swr_login_continues_when_probe_fails_but_local_auth_exists(self) -> None:
        job = make_job("swr-job")
        with server._jobs_lock:
            server._jobs[job["id"]] = job
        with patch.object(server, "check_login_detail", return_value=(False, "timeout")):
            with patch.object(server, "docker_config_has_swr_auth", return_value=True):
                with patch.object(server, "append_job_log"):
                    with patch.object(server, "set_job") as set_job:
                        self.assertTrue(server.ensure_swr_login("swr-job"))
                        set_job.assert_not_called()


if __name__ == "__main__":
    unittest.main()
