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

    def tearDown(self) -> None:
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
        self.assertTrue(server.request_job_stop("stop-job-aa")["ok"])
        missing = server.request_job_stop("missing-job")
        self.assertFalse(missing["ok"])
        self.assertEqual(404, missing["http_status"])

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

    def test_concurrent_jobs_are_capped_not_serialized_per_service(self) -> None:
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
        limit = server.max_concurrent_jobs()
        self.assertEqual(limit, len(accepted))
        self.assertEqual(limit, len(server._jobs))
        self.assertIn(server.active_job_summary()["id"], accepted)

    def test_same_service_can_run_two_jobs_under_the_cap(self) -> None:
        first = make_job("job-a")
        second = make_job("job-b")
        self.assertIsNone(server.register_job_if_idle(first))
        self.assertIsNone(server.register_job_if_idle(second))
        ids = {item["id"] for item in server.list_running_job_summaries()}
        self.assertEqual({"job-a", "job-b"}, ids)

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
        self.assertEqual("pending", statuses["build"])
        self.assertTrue(payload["pipeline"]["prepare"])
        self.assertTrue(all("subtasks" in step for step in payload["pipeline"]["steps"]))

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
        with server._branch_cache_lock:
            server._branch_cache.clear()

    def tearDown(self) -> None:
        with server._branch_cache_lock:
            server._branch_cache.clear()

    @patch.object(server, "gh_token", return_value="")
    @patch.object(server, "run_cmd", return_value=(0, ""))
    def test_empty_ssh_result_is_an_error_not_a_fake_main_branch(self, _run_cmd, _token) -> None:
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
        marker_dir = workspace / "deploy" / "runtime-images"
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

            restored = workspace / "deploy" / "runtime-images"
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

                (workspace / "deploy" / "runtime-images" / ".build-image-ids").write_text(
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
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.httpd.server_port}"
        self.opener = build_opener(ProxyHandler({}))

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        with server._job_procs_lock:
            server._job_procs.clear()
        with server._jobs_lock:
            server._jobs.clear()
            server._jobs.update(self.saved_jobs)

    def test_stop_endpoint_stops_running_job(self) -> None:
        job = make_job("active-stop")
        self.assertIsNone(server.register_job_if_idle(job))
        request = Request(
            self.base_url + "/api/jobs/active-stop/stop",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.opener.open(request, timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertTrue(payload["ok"])
        self.assertEqual("stopping", payload["status"])
        with server._jobs_lock:
            self.assertTrue(server._jobs["active-stop"]["cancel_requested"])

    def test_stop_endpoint_rejects_idle_and_missing_jobs(self) -> None:
        job = make_job("done-stop", status="failed")
        self.assertIsNone(server.register_job_if_idle(job))
        finished = Request(
            self.base_url + "/api/jobs/done-stop/stop",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as raised:
            self.opener.open(finished, timeout=2)
        self.assertEqual(409, raised.exception.code)
        missing = Request(
            self.base_url + "/api/jobs/deadbeefdead/stop",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as missing_err:
            self.opener.open(missing, timeout=2)
        self.assertEqual(404, missing_err.exception.code)

    def test_running_endpoint_is_lightweight_and_push_hits_concurrency_limit(self) -> None:
        job = make_job("active-job")
        job["log"] = ["large raw log"]
        self.assertIsNone(server.register_job_if_idle(job))

        with self.opener.open(self.base_url + "/api/running-job", timeout=2) as response:
            active = json.loads(response.read().decode("utf-8"))
        self.assertEqual("active-job", active["id"])
        self.assertNotIn("log", active)
        self.assertNotIn("test_runs", active)

        for index in range(1, server.max_concurrent_jobs()):
            extra = make_job(f"active-job-{index}")
            self.assertIsNone(server.register_job_if_idle(extra))

        request = Request(
            self.base_url + "/api/push",
            data=json.dumps({"items": [{"service_id": "memory-service", "branch": "main"}]}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with patch.object(server, "check_docker", return_value={"ok": True, "detail": ""}):
            with self.assertRaises(HTTPError) as raised:
                self.opener.open(request, timeout=2)
        self.assertEqual(409, raised.exception.code)
        payload = json.loads(raised.exception.read().decode("utf-8"))
        self.assertEqual("concurrency_limit", payload["error_code"])
        self.assertEqual(server.max_concurrent_jobs(), len(payload["active_jobs"]))

    def test_push_hits_mattermost_service_busy(self) -> None:
        job = make_job("mm-active", service_id="mattermost")
        self.assertIsNone(server.register_job_if_idle(job))
        request = Request(
            self.base_url + "/api/push",
            data=json.dumps({"items": [{"service_id": "mattermost", "branch": "main"}]}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with patch.object(server, "check_docker", return_value={"ok": True, "detail": ""}):
            with self.assertRaises(HTTPError) as raised:
                self.opener.open(request, timeout=2)
        self.assertEqual(409, raised.exception.code)
        payload = json.loads(raised.exception.read().decode("utf-8"))
        self.assertEqual("service_busy", payload["error_code"])
        self.assertEqual("mattermost", payload["service"])

    def test_multica_server_version_is_validated_before_build(self) -> None:
        request = Request(
            self.base_url + "/api/push",
            data=json.dumps(
                {"items": [{"service_id": "multica-server", "branch": "main", "version": "1.2.3"}]}
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with patch.object(server, "save_last_daemon_version") as save_version:
            with self.assertRaises(HTTPError) as raised:
                self.opener.open(request, timeout=2)
            save_version.assert_not_called()
        self.assertEqual(400, raised.exception.code)
        payload = json.loads(raised.exception.read().decode("utf-8"))
        self.assertIn("vMAJOR.MINOR.PATCH", payload["error"])

    def test_services_expose_version_and_archive_capabilities(self) -> None:
        with self.opener.open(self.base_url + "/api/services", timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
        services = {item["id"]: item for item in payload["services"]}
        self.assertTrue(services["multica-server"]["requires_version"])
        self.assertEqual("v1.2.3", services["multica-server"]["version_example"])
        self.assertIn("last_version", services["multica-server"])
        self.assertTrue(services["multica-fleet"]["archive_only"])
        self.assertEqual("censong574-spec/multica-fleet", services["multica-fleet"]["repo"])

    @patch.object(server, "list_branches_api", return_value=(False, "SSH timeout"))
    def test_branch_lookup_failure_is_explicit_service_error(self, lookup) -> None:
        with self.assertRaises(HTTPError) as raised:
            self.opener.open(self.base_url + "/api/services/memory-service/branches", timeout=2)
        self.assertEqual(503, raised.exception.code)
        payload = json.loads(raised.exception.read().decode("utf-8"))
        self.assertEqual("branch lookup failed", payload["error"])
        self.assertEqual("SSH timeout", payload["detail"])
        lookup.assert_called_once_with("rollingfruit/CellMem", force=False)

    @patch.object(server, "list_branches_api", return_value=(True, ["main", "feature/latest"]))
    def test_branch_retry_forces_backend_refresh(self, lookup) -> None:
        with self.opener.open(
            self.base_url + "/api/services/memory-service/branches?refresh=1", timeout=2
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(["main", "feature/latest"], payload["branches"])
        lookup.assert_called_once_with("rollingfruit/CellMem", force=True)


class DiskPruneAndArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = server.Path(self.tmp.name)
        self.archive_root = root / "images"
        self.archive_root.mkdir()
        self.log_dir = root / "logs"
        self.log_dir.mkdir()
        self.cfg = patch.dict(server.CFG, {"archive_root": str(self.archive_root)})
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

    def _disk(self, used: int, total: int = 1000):
        return type("usage", (), {"total": total, "used": used, "free": total - used})()

    def _stamp_dir(self, name: str, filename: str = "pkg.tar") -> server.Path:
        folder = self.archive_root / name
        folder.mkdir()
        payload = folder / filename
        payload.write_bytes(b"tar")
        return payload

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
    def test_reclaim_skips_builder_prune_below_80_percent(self, docker_cmd) -> None:
        with patch.object(server.shutil, "disk_usage", return_value=self._disk(700)):
            with patch.object(server, "reclaim_build_swap", return_value=0):
                server.reclaim_ci_disk("job-1")
        docker_cmd.assert_not_called()

    @patch.object(server, "ensure_protected_image_holds")
    @patch.object(server, "docker_cmd", return_value=(0, "Total: 32GB"))
    def test_reclaim_prunes_images_then_builder_when_still_over_80(
        self, docker_cmd, ensure_holds
    ) -> None:
        self._stamp_dir("20260804000000")
        with patch.object(server.shutil, "disk_usage", return_value=self._disk(960)):
            with patch.object(server, "reclaim_build_swap", return_value=0):
                server.reclaim_ci_disk("job-1")
        ensure_holds.assert_called()
        prune_args = [call.args for call in docker_cmd.call_args_list if len(call.args) >= 3]
        self.assertIn(("image", "prune", "-af"), prune_args)
        self.assertIn(("builder", "prune", "-af"), prune_args)
        image_index = prune_args.index(("image", "prune", "-af"))
        builder_index = prune_args.index(("builder", "prune", "-af"))
        self.assertLess(image_index, builder_index)

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
        self.assertIn(("image", "prune", "-af"), prune_args)
        self.assertNotIn(("builder", "prune", "-af"), prune_args)


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

    def _write_meta(self, job_id: str, client_id: str, created_at: str, service_id: str = "temporal") -> None:
        payload = {
            "id": job_id,
            "client_id": client_id,
            "created_at": created_at,
            "service_id": service_id,
            "service_ids": [service_id],
            "branch": "main",
            "status": "ok",
        }
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
        self.assertEqual(0, empty["total"])
        self.assertEqual([], empty["jobs"])
        page = server.list_build_history(client_id=mine, page=1, page_size=1)
        self.assertEqual(2, page["total"])
        self.assertEqual(2, page["page_count"])
        self.assertEqual("bbbbbbbbbbbb", page["jobs"][0]["id"])
        self.assertEqual("agentlink", page["jobs"][0]["service_id"])
        page2 = server.list_build_history(client_id=mine, page=2, page_size=1)
        self.assertEqual("aaaaaaaaaaaa", page2["jobs"][0]["id"])

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
        self.assertEqual(server.INTERRUPTED_JOB_ERROR, dead["error"])
        self.assertEqual("ok", kept["status"])
        log_text = (self.log_dir / "job-eeeeeeeeeeee.log").read_text(encoding="utf-8")
        self.assertIn(server.INTERRUPTED_JOB_ERROR, log_text)

    def test_prune_build_history_trims_to_50_when_over_100(self) -> None:
        mine = "cmsqxcxcesgairws0"
        for index in range(101):
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
        self.assertEqual(51, removed)
        remaining = sorted(path.stem.replace("job-", "") for path in self.log_dir.glob("job-*.json"))
        self.assertEqual(50, len(remaining))
        self.assertNotIn("000000000000", remaining)
        self.assertIn("000000000100", remaining)

    def test_record_build_artifact_trims_to_50_when_over_100(self) -> None:
        path = server.artifacts_log_path()
        for index in range(101):
            server.record_build_artifact(
                {
                    "created_at": f"2026-08-01 {index:02d}:00:00",
                    "service_id": f"svc-{index}",
                    "archive": f"/tmp/{index}.tar",
                }
            )
        entries = server._parse_artifact_entries(path.read_text(encoding="utf-8"))
        self.assertEqual(50, len(entries))
        self.assertEqual("svc-51", entries[0]["service_id"])
        self.assertEqual("svc-100", entries[-1]["service_id"])


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


if __name__ == "__main__":
    unittest.main()
