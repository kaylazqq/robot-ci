import base64
import json
import re
import tempfile
import threading
import unittest
from copy import deepcopy
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

import server
import rollout_recovery


def deployment(name):
    return {"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": name, "namespace": "default", "uid": name + "-uid"},
            "spec": {"replicas": 1, "selector": {"matchLabels": {"app": name}},
                     "template": {"metadata": {"labels": {"app": name}},
                                  "spec": {"containers": [{"name": "app", "image": "registry/" + name}]}}},
            "status": {"readyReplicas": 1}}


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        for key, value in (("DB_PATH", root / "db"), ("LOG_DIR", root), ("USERS_PATH", root / "users.json"), ("_jobs", {})):
            p = patch.object(server, key, value)
            p.start()
            self.addCleanup(p.stop)
        server.invalidate_rollout_live_cache()
        catalog = patch.object(server, "load_services", return_value=[{"id": "semantic-schedule", "title": "test"}])
        catalog.start()
        self.addCleanup(catalog.stop)
        known = patch.object(server, "_known_service_ids", return_value={"semantic-schedule"})
        known.start()
        self.addCleanup(known.stop)
        self.env, err = server.create_environment({
            "name": "test", "service_id": "semantic-schedule", "region": "cn-southwest-2",
            "cluster_name": "test", "workload_name": "v1", "jump_host": "test",
            "nodes": ["node"], "jump_password": "test", "node_password": "test", "environment_type": "production"})
        self.assertFalse(err)
        self.first = self.record("aaaaaaaa", "v1", "v2")
        self.second = self.record("bbbbbbbb", "v2", "v3")
        server.mark_parallel_rollout_old_deleted(self.first["id"])
        server.set_environment_active_workload(self.env["id"], "v2")
        self.live = {name: deployment(name) for name in ("v2", "v3")}
        self.events = []
        self.ready_failure = False
        self.delete_failure = ""
        for obj, key, value in (
            (server, "_parallel_rollout_remote", self.remote_config),
            (server.cce_rollout, "get_deployment_payload", self.get),
            (server.cce_rollout, "delete_deployment", self.delete),
        ):
            p = patch.object(obj, key, value)
            p.start()
            self.addCleanup(p.stop)
        for record, status in ((self.first, "ok"), (self.second, "running")):
            job = {"id": record["job_id"], "service_id": "semantic-schedule", "service_ids": ["semantic-schedule"],
                   "status": status, "stage": "release" if status == "running" else "done",
                   "production_released": True, "gamma_rollouts": [record],
                   "optional_steps": {"production_release": True, "release_environment_id": self.env["id"]},
                   "log": [], "step_logs": {}}
            server._jobs[job["id"]] = job
            server.persist_job_meta(job["id"])

    def record(self, job, source, candidate):
        return server.create_parallel_rollout_record(job_id=job, environment_id=self.env["id"],
            service_id="semantic-schedule", source_workload=source, candidate_workload=candidate,
            image="registry/" + candidate, source_manifest=deployment(source))

    def remote_config(self, record):
        return server.get_environment(record["environment_id"]), self.remote, "default"

    def get(self, remote, *, namespace, deploy):
        return deepcopy(self.live.get(deploy))

    def remote(self, command, timeout):
        if "base64 -d" in command:
            payload = json.loads(base64.b64decode(re.search(r"printf %s (\S+)", command)[1]))
            name = payload["metadata"]["name"]
            payload["metadata"]["uid"] = name + "-restored"
            payload["status"] = {"readyReplicas": 1}
            self.live[name] = payload
            self.events.append("restore:" + name)
        elif "rollout status" in command:
            self.events.append("ready")
            if self.ready_failure:
                return 1, "", "readiness failed"
        return 0, "ok", ""

    def delete(self, remote, *, namespace, deploy):
        self.events.append("delete:" + deploy)
        if deploy == self.delete_failure:
            raise server.cce_rollout.CceRolloutError("delete failed")
        self.live.pop(deploy, None)

    def rollback(self, record):
        plan = rollout_recovery.preview(server, record["id"])
        return server.execute_parallel_rollout_rollback(record["id"], plan["token"])

    def test_restore_manifest_drops_fixed_node_and_keeps_node_pool_affinity(self):
        payload = deployment("v1")
        pod_spec = payload["spec"]["template"]["spec"]
        pod_spec["nodeName"] = "172.31.8.33"
        pod_spec["affinity"] = {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {
            "nodeSelectorTerms": [{"matchExpressions": [{
                "key": "cce.cloud.com/cce-nodepool", "operator": "In", "values": ["business-pool"],
            }]}],
        }}}
        restored = rollout_recovery._manifest(payload, "default")
        restored_spec = restored["spec"]["template"]["spec"]
        self.assertNotIn("nodeName", restored_spec)
        self.assertEqual(pod_spec["affinity"], restored_spec["affinity"])

    def test_waiting_release_completed_and_reversible_history(self):
        # Legacy V2 record has not captured its source yet.
        with server._connect_db() as conn:
            conn.execute("UPDATE parallel_rollouts SET old_manifest_json='' WHERE id=?", (self.second["id"],))
        item, error = self.rollback(self.first)
        self.assertFalse(error)
        self.assertEqual({"v1"}, set(self.live))
        self.assertEqual(["restore:v1", "ready", "delete:v2", "delete:v3"], self.events)
        self.assertEqual("superseded", server.get_parallel_rollout(self.second["id"])["status"])
        self.assertEqual("ok", server._job_copy("bbbbbbbb")["status"])
        self.assertIn("已由历史回滚结束", str(server._job_copy("bbbbbbbb")["step_logs"]))
        self.assertTrue(server.offline_parallel_rollout_old(self.second["id"])[1])
        self.assertTrue(server.scale_parallel_rollout(self.second["id"], "old", 2)[1])
        self.assertFalse(self.rollback(self.second)[1])
        self.assertEqual({"v2"}, set(self.live))
        with self.assertRaisesRegex(ValueError, "已经回滚"):
            self.rollback(self.first)
        self.assertEqual({"v2"}, set(self.live))

    def test_completed_rollback_is_rejected_by_execute_without_preview(self):
        self.assertFalse(self.rollback(self.first)[1])
        item, error = server.execute_parallel_rollout_rollback(self.first["id"])
        self.assertIsNone(item)
        self.assertIn("已经回滚", error)

    def test_both_releases_completed_restore_v1_then_v2(self):
        server.mark_parallel_rollout_old_deleted(self.second["id"])
        server.set_environment_active_workload(self.env["id"], "v3")
        self.live.pop("v2")
        self.assertFalse(self.rollback(self.first)[1])
        self.assertEqual({"v1"}, set(self.live))
        self.assertFalse(self.rollback(self.second)[1])
        self.assertEqual({"v2"}, set(self.live))

    def test_readiness_failure_keeps_current_versions_and_waiting_job(self):
        self.ready_failure = True
        self.assertTrue(self.rollback(self.first)[1])
        self.assertTrue({"v2", "v3"}.issubset(self.live))
        self.assertEqual("running", server._job_copy("bbbbbbbb")["status"])
        self.assertFalse(any(e.startswith("delete:") for e in self.events))
        self.ready_failure = False
        self.assertFalse(self.rollback(self.first)[1])
        self.assertEqual({"v1"}, set(self.live))

    def test_cleanup_failure_retry_survives_reload_and_blocks_stale_offline(self):
        self.delete_failure = "v3"
        self.assertTrue(self.rollback(self.first)[1])
        self.assertEqual("running", server._job_copy("bbbbbbbb")["status"])
        self.assertTrue(server.offline_parallel_rollout_old(self.second["id"])[1])
        server._jobs.clear()
        server.reap_orphaned_running_jobs()
        self.delete_failure = ""
        self.assertFalse(self.rollback(self.first)[1])
        self.assertEqual({"v1"}, set(self.live))
        self.assertEqual("ok", server.load_job_from_disk("bbbbbbbb")["status"])

    def test_restart_reconciles_cleanup_committed_before_job_completion(self):
        with patch.object(rollout_recovery, "finish_superseded"):
            self.assertFalse(self.rollback(self.first)[1])
        server._jobs.clear()
        server.reap_orphaned_running_jobs()
        rollout_recovery.reconcile_completed(server)
        self.assertEqual("ok", server.load_job_from_disk("bbbbbbbb")["status"])

    def test_plan_changes_require_confirmation_without_remote_mutation(self):
        plan = rollout_recovery.preview(server, self.first["id"])
        self.live["v3"]["spec"]["replicas"] = 2
        item, error = server.rollback_parallel_rollout(self.first["id"], plan["token"])
        self.assertIsNone(item)
        self.assertIn("重新预览", error)
        self.assertEqual([], self.events)

    def test_unrelated_workload_untouched(self):
        self.live["other-service"] = deployment("other-service")
        self.assertFalse(self.rollback(self.first)[1])
        self.assertEqual({"v1", "other-service"}, set(self.live))

    def test_missing_legacy_snapshot_refuses_guessing(self):
        with server._connect_db() as conn:
            conn.execute("UPDATE parallel_rollouts SET old_manifest_json='' WHERE id=?", (self.first["id"],))
        with self.assertRaisesRegex(ValueError, "缺少恢复快照"):
            rollout_recovery.preview(server, self.first["id"])

    def test_inflight_offline_serialized_with_rollback(self):
        waiting, proceed, offline_done = threading.Event(), threading.Event(), threading.Event()
        original = self.remote
        def remote(command, timeout):
            if "rollout status" in command:
                waiting.set()
                if not proceed.wait(5):
                    raise TimeoutError("test timed out")
            return original(command, timeout)
        self.remote = remote
        results = []
        rollback = threading.Thread(target=lambda: results.append(self.rollback(self.first)))
        def offline():
            results.append(server.offline_parallel_rollout_old(self.second["id"]))
            offline_done.set()
        stale_click = threading.Thread(target=offline)
        rollback.start()
        try:
            self.assertTrue(waiting.wait(5))
            stale_click.start()
            self.assertFalse(offline_done.wait(0.1))
        finally:
            proceed.set()
            rollback.join(5)
            if stale_click.ident:
                stale_click.join(5)
        self.assertFalse(rollback.is_alive())
        self.assertEqual({"v1"}, set(self.live))
        self.assertEqual(1, sum(bool(error) for _, error in results))

    def test_waiting_worker_exits_after_historical_rollback(self):
        deployed = threading.Event()
        outcome = []
        def deploy(**kwargs):
            kwargs["on_parallel_created"]({"service_id": "semantic-schedule", "source_workload": "v2", "name": "v3", "image": "registry/v3"})
            deployed.set()
            return True, ""
        with patch.object(server, "wait_for_production_approval"), patch.object(server.cce_rollout, "deploy_job_results", side_effect=deploy), patch.object(server, "create_parallel_rollout_record", return_value=self.second):
            worker = threading.Thread(target=lambda: outcome.append(server.maybe_run_gamma_after_build("bbbbbbbb", [])))
            worker.start()
            try:
                self.assertTrue(deployed.wait(5))
                self.assertFalse(self.rollback(self.first)[1])
                worker.join(5)
                self.assertFalse(worker.is_alive())
                self.assertEqual([(True, "")], outcome)
            finally:
                if worker.is_alive():
                    server.set_job("bbbbbbbb", cancel_requested=True)
                    worker.join(5)

    def test_disk_only_job_http_approval_persisted_and_executed(self):
        server._jobs.pop("aaaaaaaa")
        with patch.object(server.Handler, "_require_api_user", return_value="tester"), patch.object(server, "_has_production_permission", return_value=True):
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                opener = build_opener(ProxyHandler({}))
                def post(path, data):
                    req = Request(f"http://127.0.0.1:{httpd.server_port}" + path,
                                  data=json.dumps(data).encode(), headers={"Content-Type": "application/json"})
                    with opener.open(req, timeout=10) as response:
                        return json.load(response)
                base = "/api/parallel-rollouts/" + self.first["id"]
                plan = post(base + "/rollback-plan", {})["plan"]
                self.assertTrue(post(base + "/rollback", {"plan_token": plan["token"]})["approval_required"])
                self.assertEqual("waiting", server.load_job_from_disk("aaaaaaaa")["approval"]["status"])
                server._jobs.pop("aaaaaaaa")
                self.assertTrue(post("/api/jobs/aaaaaaaa/approval", {"action": "continue"})["ok"])
                self.assertEqual({"v1"}, set(self.live))
                self.assertTrue(server.load_job_from_disk("aaaaaaaa")["rollback_completed"])
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join()

    def test_release_actions_do_not_require_permission_before_production_rollback_approval(self):
        with patch.object(server.Handler, "_require_api_user", return_value="unprivileged"), patch.object(server, "_has_production_permission", return_value=False):
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                opener = build_opener(ProxyHandler({}))
                def post(path, data):
                    req = Request(f"http://127.0.0.1:{httpd.server_port}" + path,
                                  data=json.dumps(data).encode(), headers={"Content-Type": "application/json"})
                    with opener.open(req, timeout=10) as response:
                        return json.load(response)

                # Old-version offline is a post-approval release action and
                # does not require production permission.
                second_base = "/api/parallel-rollouts/" + self.second["id"]
                self.assertTrue(post(second_base + "/offline-old", {})["ok"])
                self.assertEqual("old_deleted", server.get_parallel_rollout(self.second["id"])["status"])

                # Production rollback may be initiated by anyone, but still
                # creates a permission-protected approval gate.
                first_base = "/api/parallel-rollouts/" + self.first["id"]
                plan = post(first_base + "/rollback-plan", {})["plan"]
                self.assertTrue(post(first_base + "/rollback", {"plan_token": plan["token"]})["approval_required"])
                with self.assertRaises(HTTPError) as denied:
                    post("/api/jobs/aaaaaaaa/approval", {"action": "continue"})
                self.assertEqual(403, denied.exception.code)
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join()

    def test_nonproduction_rollback_executes_without_permission_or_approval(self):
        with server._connect_db() as conn:
            conn.execute("UPDATE environments SET environment_type='dev' WHERE id=?", (self.env["id"],))
        with patch.object(server.Handler, "_require_api_user", return_value="unprivileged"), patch.object(server, "_has_production_permission", return_value=False):
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                opener = build_opener(ProxyHandler({}))
                def post(path, data):
                    req = Request(f"http://127.0.0.1:{httpd.server_port}" + path,
                                  data=json.dumps(data).encode(), headers={"Content-Type": "application/json"})
                    with opener.open(req, timeout=10) as response:
                        return json.load(response)
                base = "/api/parallel-rollouts/" + self.first["id"]
                plan = post(base + "/rollback-plan", {})["plan"]
                result = post(base + "/rollback", {"plan_token": plan["token"]})
                self.assertTrue(result["ok"])
                self.assertNotIn("approval_required", result)
                self.assertEqual({"v1"}, set(self.live))
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join()
