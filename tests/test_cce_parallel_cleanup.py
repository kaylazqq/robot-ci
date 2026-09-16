import base64
import copy
import json
import re
import unittest
from unittest.mock import patch

import cce_rollout as cce


class ParallelCleanupTests(unittest.TestCase):
    def setUp(self):
        self.source = {
            "apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": "governance-old", "namespace": "test", "uid": "old-uid", "labels": {"app": "governance"}},
            "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "governance"}},
                     "template": {"metadata": {"labels": {"app": "governance"}},
                                  "spec": {"containers": [{"name": "app", "image": "registry/gw:old"}]}}},
            "status": {"readyReplicas": 1},
        }
        self.name = "governance-v-202609160937"
        self.live = {"governance-old": copy.deepcopy(self.source)}
        self.calls = []
        self.logs = []
        self.timeout = False
        self.create_denied = False
        self.create_reply_lost = False
        self.delete_denied = False
        self.delete_stuck = False
        self.replace_before_delete = False
        self.diagnostic_failure = False
        self.no_ready = False

    def remote(self, command, timeout):
        self.calls.append(command)
        if "get deploy -o json" in command:
            return 0, json.dumps({"items": list(self.live.values())}), ""
        if "get deploy " in command:
            name = re.search(r"get deploy ([^ ]+)", command)[1]
            return (0, json.dumps(self.live[name]), "") if name in self.live else (1, "", "NotFound")
        if "create -f" in command:
            if self.name in self.live:
                return 1, "", "AlreadyExists"
            if self.create_denied:
                return 1, "", "Forbidden"
            payload = json.loads(base64.b64decode(re.search(r"printf %s (\S+)", command)[1]))
            payload["metadata"]["uid"] = "new-uid"
            payload["status"] = {"readyReplicas": 0 if self.timeout or self.no_ready else 1}
            self.live[self.name] = payload
            if self.create_reply_lost:
                raise cce.CceRolloutError("SSH disconnected after create")
            return 0, "created", ""
        if "rollout status" in command:
            return (1, "", "timed out waiting for the condition") if self.timeout else (0, "ready", "")
        if "get rs,pods" in command:
            if self.diagnostic_failure:
                raise cce.CceRolloutError("diagnostic read denied")
            return 0, json.dumps({"items": [
                {"kind": "ReplicaSet", "metadata": {"name": "new-rs", "uid": "new-rs-uid", "ownerReferences": [{"uid": "new-uid"}]}},
                {"kind": "Pod", "metadata": {"name": "new-pod", "uid": "pod-uid", "ownerReferences": [{"uid": "new-rs-uid"}]},
                 "status": {"phase": "Pending", "containerStatuses": [{"state": {"waiting": {"reason": "ContainerCreating"}}}]}}
            ]}), ""
        if "get events" in command:
            return 0, json.dumps({"items": [{"reason": "FailedAssignENI", "message": "insufficient NICs on node"}]}), ""
        if "delete --raw" in command:
            options = json.loads(base64.b64decode(re.search(r"printf %s (\S+)", command)[1]))
            self.assertEqual("Foreground", options["propagationPolicy"])
            self.assertIn("/namespaces/test/deployments/" + self.name, command)
            self.assertEqual("new-uid", options["preconditions"]["uid"])
            if self.replace_before_delete:
                self.live[self.name]["metadata"]["uid"] = "replacement-uid"
                return 1, "", "Conflict: UID precondition failed"
            if self.delete_denied:
                return 1, "", "Forbidden: cannot delete"
            self.live[self.name]["metadata"]["deletionTimestamp"] = "2026-09-16T02:00:00Z"
            return 0, "deleting", ""
        if "wait --for=delete" in command:
            if self.delete_stuck:
                return 1, "", "cleanup timed out"
            self.live.pop(self.name, None)
            return 0, "deleted", ""
        raise AssertionError("Unexpected command " + command)

    def create(self):
        return cce.create_parallel_deployment(self.remote, namespace="test", source="governance-old",
            base_name="governance", release_id="job-202609160937", image="registry/gw:new", container="app",
            replicas=1, timeout="180s", source_payload=self.source, log=self.logs.append)

    def fail(self, text):
        with self.assertRaisesRegex(cce.CceRolloutError, text) as ctx:
            self.create()
        self.assertEqual(self.source, self.live["governance-old"])
        return str(ctx.exception)

    def test_success_keeps_both_versions_without_cleanup(self):
        self.assertEqual(self.name, self.create()["name"])
        self.assertEqual(2, len(self.live))
        self.assertFalse(any("delete" in c for c in self.calls))

    def test_timeout_captures_network_cause_then_waits_for_cleanup(self):
        self.timeout = True
        error = self.fail("insufficient NICs on node")
        self.assertIn("已清理本次失败的新负载", error)
        self.assertEqual({"governance-old"}, set(self.live))
        diagnostic = next(i for i,c in enumerate(self.calls) if "get events" in c)
        deleted = next(i for i,c in enumerate(self.calls) if "delete --raw" in c)
        waited = next(i for i,c in enumerate(self.calls) if "wait --for=delete" in c)
        self.assertLess(diagnostic, deleted)
        self.assertLess(deleted, waited)
        self.assertIn("get deploy", self.calls[-1])

    def test_transport_error_after_creation_cleans_owned_candidate(self):
        self.create_reply_lost = True
        self.fail("SSH disconnected after create")
        self.assertEqual({"governance-old"}, set(self.live))

    def test_create_rejected_before_creation_has_no_delete(self):
        self.create_denied = True
        self.fail("无残留")
        self.assertFalse(any("delete" in c for c in self.calls))

    def test_same_name_preexisting_release_is_not_modified_or_deleted(self):
        existing = copy.deepcopy(self.source)
        existing["metadata"]["name"] = self.name
        # Even another attempt of the same job must not be deleted.
        existing["metadata"]["labels"][cce.PARALLEL_RELEASE_LABEL] = "job-202609160937"
        existing["metadata"]["annotations"] = {cce.PARALLEL_ATTEMPT_ANNOTATION: "earlier-attempt"}
        self.live[self.name] = existing
        self.fail("拒绝删除")
        self.assertEqual(existing, self.live[self.name])
        self.assertFalse(any("delete --raw" in c for c in self.calls))

    def test_replacement_is_protected_by_uid_precondition(self):
        self.timeout = self.replace_before_delete = True
        error = self.fail("UID precondition failed")
        self.assertIn("可能残留", error)
        self.assertEqual("replacement-uid", self.live[self.name]["metadata"]["uid"])

    def test_cleanup_permission_failure_reports_residual(self):
        self.timeout = self.delete_denied = True
        error = self.fail("清理失败")
        self.assertIn("timed out", error)
        self.assertIn("Forbidden: cannot delete", error)
        self.assertIn(self.name, self.live)
        self.assertNotIn("已清理本次失败的新负载", error)

    def test_cleanup_timeout_does_not_claim_deleted(self):
        self.timeout = self.delete_stuck = True
        error = self.fail("cleanup timed out")
        self.assertIn(self.name, self.live)
        self.assertNotIn("已清理本次失败的新负载", error)

    def test_diagnostic_failure_still_cleans_up(self):
        self.timeout = self.diagnostic_failure = True
        error = self.fail("diagnostic read denied")
        self.assertIn("已清理", error)
        self.assertEqual({"governance-old"}, set(self.live))

    def test_false_success_without_ready_pods_also_cleans_up(self):
        self.no_ready = True
        self.fail("尚未就绪")
        self.assertEqual({"governance-old"}, set(self.live))

    def test_next_source_discovery_has_no_failed_candidate(self):
        self.source["metadata"]["labels"][cce.PARALLEL_SOURCE_LABEL] = "governance"
        self.live["governance-old"] = copy.deepcopy(self.source)
        self.timeout = True
        self.fail("已清理")
        name, _ = cce.resolve_source_deployment(self.remote, namespace="test", active="governance", baseline="governance")
        self.assertEqual("governance-old", name)

    def test_same_source_name_refused_without_remote_write(self):
        with patch.object(cce, "parallel_deployment_name", return_value="governance-old"):
            self.fail("拒绝覆盖旧版本")
        self.assertEqual([], self.calls)

    def test_pipeline_records_no_release_on_failed_creation(self):
        self.timeout = True
        completed = []
        with patch.object(cce, "exec_via_nodes", side_effect=lambda command, **kw: self.remote(command, kw["timeout"])):
            ok, error = cce.deploy_job_results(
                environment={"workload_name": "governance", "active_workload_name": "governance-old", "jump_host": "jump", "nodes": ["node"]},
                results=[{"service_id": "agent-governance-gw", "ok": True, "remote": "registry/gw:new"}],
                creds={"jump_password": "test", "node_password": "test"}, namespace="test", mode="parallel",
                release_id="job-202609160937", on_parallel_created=completed.append, log=self.logs.append)
        self.assertFalse(ok)
        self.assertIn("已清理", error)
        self.assertEqual([], completed)
        self.assertEqual({"governance-old"}, set(self.live))
