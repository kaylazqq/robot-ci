import base64
import json
import re
import sys
import tempfile
import types
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import cce_rollout
import server


def _job(job_id: str, service_id: str = "memory-service") -> dict:
    return {
        "id": job_id,
        "service_id": service_id,
        "service_ids": [service_id],
        "branch": "main",
        "status": "running",
        "stage": "pushing",
        "error": None,
        "remote": None,
        "archive": None,
        "results": [],
        "log": [],
        "ui_log": [],
        "optional_steps": {"gamma_deploy": False, "gamma_test": False, "environment_id": ""},
    }


class CceRolloutHelperTests(unittest.TestCase):
    def test_jump_timeout_reports_cloud_network_action(self) -> None:
        class FakeClient:
            def set_missing_host_key_policy(self, _policy) -> None:
                pass

            def connect(self, *_args, **_kwargs) -> None:
                raise TimeoutError("timed out")

            def close(self) -> None:
                pass

        fake_paramiko = types.SimpleNamespace(
            SSHClient=lambda: FakeClient(),
            AutoAddPolicy=lambda: object(),
        )
        with patch.object(cce_rollout, "_import_paramiko", return_value=fake_paramiko):
            with self.assertRaises(cce_rollout.CceRolloutError) as failed:
                cce_rollout.jump_exec(
                    "kubectl get deploy",
                    jump_user="root",
                    jump_host="jump.example",
                    jump_port=2222,
                    jump_password="secret",
                )
        message = str(failed.exception)
        self.assertIn("jump.example:2222", message)
        self.assertIn("CI 打包机出口 IP", message)
        self.assertIn("云安全组/ACL", message)

    def test_parse_ssh_target(self) -> None:
        self.assertEqual(("root", "122.9.139.49", 22), cce_rollout.parse_ssh_target("root@122.9.139.49"))
        self.assertEqual(("root", "122.9.139.49", 22), cce_rollout.parse_ssh_target("122.9.139.49"))
        self.assertEqual(("ubuntu", "10.0.0.8", 2222), cce_rollout.parse_ssh_target("ubuntu@10.0.0.8:2222"))

    def test_validate_image_ref_rejects_latest(self) -> None:
        good = "swr.cn-southwest-2.myhuaweicloud.com/public_ai/semantic-schedule:202609101200_abc"
        self.assertEqual(good, cce_rollout.validate_image_ref(good))
        with self.assertRaises(cce_rollout.CceRolloutError):
            cce_rollout.validate_image_ref(good.rsplit(":", 1)[0] + ":latest")
        with self.assertRaises(cce_rollout.CceRolloutError):
            cce_rollout.validate_image_ref("swr.example.com/public_ai/semantic-schedule")

    def test_pick_container_skips_filebeat_and_prefers_image(self) -> None:
        rows = [
            ("filebeat", "elastic/filebeat:8"),
            ("container-1", "swr.example.com/public_ai/semantic-schedule:old"),
        ]
        picked = cce_rollout.pick_container(
            rows,
            image="swr.example.com/public_ai/semantic-schedule:202609101200_abc",
        )
        self.assertEqual("container-1", picked)
        self.assertEqual(
            "container1",
            cce_rollout.pick_container(
                [("container1", "old"), ("filebeat", "fb")],
                image="swr.example.com/public_ai/service-router:tag",
                preferred="container1",
            ),
        )

    def test_resolve_deploy_uses_env_workload_and_map(self) -> None:
        deploy, container = cce_rollout.resolve_deploy_target(
            "agent-governance-gw",
            "swr.example.com/public_ai/agent-governance-gw:tag1",
            "governance",
        )
        self.assertEqual("governance", deploy)
        self.assertEqual("container-1", container)
        deploy, container = cce_rollout.resolve_deploy_target(
            "semantic-schedule",
            "swr.example.com/public_ai/semantic-schedule:tag1",
            "semantic-schedule",
        )
        self.assertEqual("semantic-schedule", deploy)
        self.assertEqual("container-1", container)

    def test_multi_service_only_matches_workload(self) -> None:
        self.assertTrue(
            cce_rollout.should_rollout_result(
                "semantic-schedule",
                "swr.example.com/public_ai/semantic-schedule:tag1",
                "semantic-schedule",
                result_count=2,
            )
        )
        self.assertFalse(
            cce_rollout.should_rollout_result(
                "agentops",
                "swr.example.com/public_ai/agentops:tag1",
                "semantic-schedule",
                result_count=2,
            )
        )

    def test_overlay_environment_passwords_win(self) -> None:
        merged = cce_rollout.overlay_environment_passwords(
            {"jump_password": "cfg-jump", "node_password": "cfg-node", "node_user": "root"},
            {"jump_password": "env-jump", "node_password": "env-node"},
        )
        self.assertEqual("env-jump", merged["jump_password"])
        self.assertEqual("env-node", merged["node_password"])

    def test_parallel_manifest_keeps_service_labels_and_uses_unique_selector(self) -> None:
        source = {
            "apiVersion": "apps/v1",
            "metadata": {"name": "semantic-schedule", "namespace": "default", "labels": {"app": "schedule"}},
            "spec": {
                "replicas": 4,
                "selector": {"matchLabels": {"app": "schedule"}},
                "template": {"metadata": {"labels": {"app": "schedule"}}, "spec": {"containers": [
                    {"name": "container-1", "image": "registry/schedule:old"},
                    {"name": "filebeat", "image": "elastic/filebeat:8"},
                ]}},
            },
        }
        manifest = cce_rollout.deployment_manifest_for_parallel_release(
            source, source_name="semantic-schedule", release_id="ab12cd34ef56",
            image="registry/schedule:new", container="container-1", replicas=1,
        )
        self.assertRegex(manifest["metadata"]["name"], r"^semantic-schedule-v20\d{10}$")
        labels = manifest["spec"]["template"]["metadata"]["labels"]
        self.assertEqual("schedule", labels["app"])
        self.assertEqual("ab12cd34ef56", labels[cce_rollout.PARALLEL_RELEASE_LABEL])
        self.assertEqual("ab12cd34ef56", manifest["spec"]["selector"]["matchLabels"][cce_rollout.PARALLEL_RELEASE_LABEL])
        images = {row["name"]: row["image"] for row in manifest["spec"]["template"]["spec"]["containers"]}
        self.assertEqual("registry/schedule:new", images["container-1"])
        self.assertEqual("elastic/filebeat:8", images["filebeat"])

    def test_parallel_manifest_drops_fixed_node_but_keeps_scheduling_constraints(self) -> None:
        affinity = {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {
            "nodeSelectorTerms": [{"matchExpressions": [{
                "key": "cce.cloud.com/cce-nodepool", "operator": "In", "values": ["business-pool"],
            }]}],
        }}}
        source = {
            "metadata": {"name": "governance", "labels": {"app": "governance"}},
            "spec": {
                "selector": {"matchLabels": {"app": "governance"}},
                "template": {"metadata": {"labels": {"app": "governance"}}, "spec": {
                    "nodeName": "172.31.8.33",
                    "nodeSelector": {"environment": "production"},
                    "affinity": affinity,
                    "tolerations": [{"key": "dedicated", "operator": "Exists"}],
                    "containers": [{"name": "container-1", "image": "registry/governance:old"}],
                }},
            },
        }
        manifest = cce_rollout.deployment_manifest_for_parallel_release(
            source, source_name="governance", release_id="release-1",
            image="registry/governance:new", container="container-1",
        )
        pod_spec = manifest["spec"]["template"]["spec"]
        self.assertNotIn("nodeName", pod_spec)
        self.assertEqual({"environment": "production"}, pod_spec["nodeSelector"])
        self.assertEqual(affinity, pod_spec["affinity"])
        self.assertEqual(source["spec"]["template"]["spec"]["tolerations"], pod_spec["tolerations"])

    def test_parallel_manifest_names_from_configured_baseline_not_active_workload(self) -> None:
        source = {
            "apiVersion": "apps/v1",
            "metadata": {"name": "service-v-202609151000", "labels": {"app": "service"}},
            "spec": {
                "selector": {"matchLabels": {"app": "service"}},
                "template": {"metadata": {"labels": {"app": "service"}}, "spec": {
                    "containers": [{"name": "app", "image": "registry/service:old"}],
                }},
            },
        }
        manifest = cce_rollout.deployment_manifest_for_parallel_release(
            source,
            source_name="service-v-202609151000",
            base_name="service",
            release_id="job-202609151053",
            image="registry/service:new",
            container="app",
        )
        self.assertEqual("service-v202609151053", manifest["metadata"]["name"])
        self.assertEqual(
            "service-v-202609151000",
            manifest["metadata"]["labels"][cce_rollout.PARALLEL_SOURCE_LABEL],
        )

    def test_resolve_credentials_prefers_env(self) -> None:
        creds = cce_rollout.resolve_credentials(
            {"cce_ssh_password": "from-cfg"},
            {"CCE_JUMP_PASSWORD": "from-env"},
            load_default_files=False,
        )
        self.assertEqual("from-env", creds["jump_password"])
        self.assertEqual("from-cfg", creds["node_password"])

    def test_exec_via_nodes_runs_directly_on_jump_when_nodes_are_empty(self) -> None:
        calls: list[tuple[str, dict]] = []

        def direct(command, **kwargs):  # noqa: ANN001, ANN003
            calls.append((command, kwargs))
            return 0, "ok", ""

        result = cce_rollout.exec_via_nodes(
            "kubectl get deploy",
            jump_host="ci@jump.example:2222",
            nodes=[],
            creds={"jump_password": "jump-secret"},
            timeout=45,
            direct=direct,
            kubeconfig_path="/etc/robot-ci/kubeconfigs/prod.yaml",
        )

        self.assertEqual((0, "ok", ""), result)
        self.assertEqual(1, len(calls))
        command, kwargs = calls[0]
        self.assertTrue(command.startswith("export KUBECONFIG=/etc/robot-ci/kubeconfigs/prod.yaml;"))
        self.assertEqual("ci", kwargs["jump_user"])
        self.assertEqual("jump.example", kwargs["jump_host"])
        self.assertEqual(2222, kwargs["jump_port"])
        self.assertNotIn("node_host", kwargs)

    def test_list_jump_kubeconfigs_returns_only_safe_yaml_paths(self) -> None:
        calls: list[tuple[str, dict]] = []

        def direct(command, **kwargs):  # noqa: ANN001, ANN003
            calls.append((command, kwargs))
            return 0, (
                "/etc/robot-ci/kubeconfigs/prod.yaml\x00"
                "/etc/robot-ci/kubeconfigs/dev.yml\x00"
                "/etc/robot-ci/kubeconfigs/nested/ignored.yaml\x00"
                "/tmp/outside.yaml\x00"
            ), ""

        paths = cce_rollout.list_jump_kubeconfigs(
            jump_host="ops@jump.example:2222",
            creds={"jump_password": "secret"},
            direct=direct,
        )

        self.assertEqual(
            [
                "/etc/robot-ci/kubeconfigs/dev.yml",
                "/etc/robot-ci/kubeconfigs/prod.yaml",
            ],
            paths,
        )
        self.assertIn("-maxdepth 1", calls[0][0])
        self.assertIn("-readable", calls[0][0])
        self.assertEqual("ops", calls[0][1]["jump_user"])
        self.assertEqual(2222, calls[0][1]["jump_port"])

    def test_list_jump_kubeconfigs_reports_missing_directory(self) -> None:
        with self.assertRaises(cce_rollout.CceRolloutError) as failed:
            cce_rollout.list_jump_kubeconfigs(
                jump_host="root@jump",
                creds={"jump_password": "secret"},
                direct=lambda *_args, **_kwargs: (44, "", "KUBECONFIG_DIR_NOT_FOUND"),
            )
        self.assertIn("不存在 kubeconfig 目录", str(failed.exception))

    def test_deploy_job_results_sets_image(self) -> None:
        commands: list[str] = []

        def hop(command, **kwargs):  # noqa: ANN003
            commands.append(command)
            if "get deploy" in command and "-o json" in command:
                payload = {
                    "spec": {
                        "template": {
                            "spec": {
                                "containers": [
                                    {
                                        "name": "container-1",
                                        "image": "swr.example.com/public_ai/semantic-schedule:old",
                                    },
                                    {"name": "filebeat", "image": "elastic/filebeat:8"},
                                ]
                            }
                        }
                    }
                }
                return 0, json.dumps(payload), ""
            return 0, "deployment \"semantic-schedule\" successfully rolled out\n", ""

        logs: list[str] = []
        ok, err = cce_rollout.deploy_job_results(
            environment={
                "name": "联调",
                "cluster_name": "AgentPlatform",
                "workload_name": "semantic-schedule",
                "jump_host": "root@122.9.139.49",
                "nodes": ["172.31.8.33"],
            },
            results=[
                {
                    "service_id": "semantic-schedule",
                    "ok": True,
                    "remote": "swr.example.com/public_ai/semantic-schedule:202609101200_abc",
                }
            ],
            creds={"jump_password": "x", "node_password": "x", "node_user": "root"},
            hop=hop,
            log=logs.append,
        )
        self.assertTrue(ok, err)
        self.assertTrue(any("set image" in cmd for cmd in commands))
        self.assertTrue(any("rollout status" in cmd for cmd in commands))
        self.assertTrue(any("patch deployment" in cmd and '"type":"Recreate"' in cmd for cmd in commands))
        self.assertTrue(any("container-1=" in cmd for cmd in commands))
        self.assertFalse(any("filebeat=" in cmd for cmd in commands))

    def test_runtime_variables_replace_only_ci_managed_environment(self) -> None:
        template = {
            "metadata": {"annotations": {
                cce_rollout.RUNTIME_ENV_KEYS_ANNOTATION: "OLD_MANAGED,AGENT_MCP_PUBLIC_URL",
            }},
            "spec": {"containers": [{
                "name": "router",
                "image": "registry/router:old",
                "env": [
                    {"name": "KEEP_EXISTING", "valueFrom": {"configMapKeyRef": {"name": "base", "key": "keep"}}},
                    {"name": "OLD_MANAGED", "value": "remove-me"},
                    {"name": "AGENT_MCP_PUBLIC_URL", "value": "old"},
                ],
            }]},
        }

        cce_rollout.apply_runtime_variables_to_pod_template(
            template,
            container="router",
            variables={"AGENT_MCP_PUBLIC_URL": "http://example.test/api/mcp", "EMPTY_VALUE": ""},
            revision="revision-2",
        )

        env = template["spec"]["containers"][0]["env"]
        self.assertEqual(["KEEP_EXISTING", "AGENT_MCP_PUBLIC_URL", "EMPTY_VALUE"], [item["name"] for item in env])
        self.assertEqual("http://example.test/api/mcp", env[1]["value"])
        annotations = template["metadata"]["annotations"]
        self.assertEqual("AGENT_MCP_PUBLIC_URL,EMPTY_VALUE", annotations[cce_rollout.RUNTIME_ENV_KEYS_ANNOTATION])
        self.assertEqual("revision-2", annotations[cce_rollout.RUNTIME_CONFIG_REVISION_ANNOTATION])

    def test_runtime_variables_remove_explicit_deleted_names_before_first_ci_release(self) -> None:
        template = {"metadata": {"annotations": {}}, "spec": {"containers": [{
            "name": "router", "env": [
                {"name": "REMOVE_ME", "value": "old"},
                {"name": "KEEP_ME", "value": "same"},
            ]
        }]}}
        cce_rollout.apply_runtime_variables_to_pod_template(
            template, container="router", variables={}, revision="r2", deleted_names=["REMOVE_ME"]
        )
        self.assertEqual(["KEEP_ME"], [item["name"] for item in template["spec"]["containers"][0]["env"]])

    def test_read_runtime_environment_skips_secret_references_and_sensitive_literals(self) -> None:
        payload = {"spec": {"template": {"spec": {"containers": [{
            "name": "router", "image": "registry/router:old", "env": [
                {"name": "PUBLIC_URL", "value": "http://example.test"},
                {"name": "EMPTY_VALUE", "value": ""},
                {"name": "DB_PASSWORD", "value": "must-not-copy"},
                {"name": "FROM_SECRET", "valueFrom": {"secretKeyRef": {"name": "s", "key": "x"}}},
            ]
        }]}}}}

        def direct(command, **_kwargs):  # noqa: ANN001
            if "get deploy echoagent-router -o json" in command:
                return 0, json.dumps(payload), ""
            return 1, "", "not found"

        result = cce_rollout.read_workload_runtime_environment(
            environment={
                "service_id": "echoagent-router", "workload_name": "echoagent-router",
                "jump_host": "root@jump", "nodes": [], "namespace": "echo-prod",
                "kubeconfig_path": "/etc/robot-ci/kubeconfigs/dev2.yaml",
            },
            creds={"jump_password": "x"}, direct=direct,
        )
        self.assertEqual({"EMPTY_VALUE": "", "PUBLIC_URL": "http://example.test"}, result["variables"])
        self.assertEqual(["FROM_SECRET"], result["skipped_value_from_names"])
        self.assertEqual(["DB_PASSWORD"], result["skipped_sensitive_names"])

    def test_deploy_applies_runtime_variables_with_image_in_one_patch(self) -> None:
        commands: list[str] = []

        def direct(command, **_kwargs):  # noqa: ANN001
            commands.append(command)
            if "get deploy echoagent-router -o json" in command:
                return 0, json.dumps({
                    "spec": {"template": {
                        "metadata": {"annotations": {}},
                        "spec": {"containers": [{"name": "router", "image": "registry/router:old"}]},
                    }},
                }), ""
            return 0, "rolled out", ""

        logs: list[str] = []
        ok, err = cce_rollout.deploy_job_results(
            environment={
                "name": "dev2",
                "workload_name": "echoagent-router",
                "jump_host": "root@jump",
                "nodes": [],
                "kubeconfig_path": "/etc/robot-ci/kubeconfigs/dev2.yaml",
                "runtime_variables": {"AGENT_MCP_PUBLIC_URL": "http://example.test/api/mcp"},
                "runtime_config_revision": "revision-1",
            },
            results=[{"service_id": "echoagent-router", "ok": True, "remote": "registry/router:new"}],
            creds={"jump_password": "x"},
            direct=direct,
            log=logs.append,
            mode="inplace",
        )

        self.assertTrue(ok, err)
        release = next(command for command in commands if "--type=json" in command)
        self.assertIn("patch deployment", release)
        self.assertIn("patch deployment echoagent-router --type=json", release)
        self.assertNotIn("http://example.test/api/mcp", release)
        self.assertTrue(any("variables=1" in line for line in logs))
        self.assertFalse(any("http://example.test/api/mcp" in line for line in logs))

    def test_replacement_release_restarts_when_image_reference_is_unchanged(self) -> None:
        commands: list[str] = []
        image = "registry/echoagent-router:202610121637_52337da"

        def direct(command, **_kwargs):  # noqa: ANN001
            commands.append(command)
            if "get deploy echoagent-router -o json" in command:
                return 0, json.dumps({"spec": {"template": {"spec": {"containers": [
                    {"name": "router", "image": image}
                ]}}}}), ""
            return 0, "rolled out", ""

        ok, err = cce_rollout.deploy_job_results(
            environment={
                "workload_name": "echoagent-router",
                "jump_host": "root@jump",
                "nodes": [],
                "kubeconfig_path": "/etc/robot-ci/kubeconfigs/prod.yaml",
            },
            results=[{"service_id": "echoagent-router", "ok": True, "remote": image}],
            creds={"jump_password": "x"},
            direct=direct,
            mode="inplace",
        )

        self.assertTrue(ok, err)
        release = next(command for command in commands if "set image" in command)
        self.assertIn("patch deployment", release)
        self.assertIn("rollout restart deployment/echoagent-router", release)

    def test_deploy_job_results_supports_direct_kubectl_on_jump_host(self) -> None:
        commands: list[str] = []

        def direct(command, **_kwargs):  # noqa: ANN001
            commands.append(command)
            if "get deploy" in command and "-o json" in command:
                return 0, json.dumps({"spec": {"template": {"spec": {"containers": [
                    {"name": "frontend", "image": "registry/echoagent-frontend:old"}
                ]}}}}), ""
            return 0, "rolled out", ""

        logs: list[str] = []
        ok, err = cce_rollout.deploy_job_results(
            environment={
                "name": "prod",
                "workload_name": "echoagent-frontend",
                "jump_host": "root@jump",
                "nodes": [],
                "kubeconfig_path": "/etc/robot-ci/kubeconfigs/prod.yaml",
            },
            results=[{
                "service_id": "echoagent-frontend",
                "ok": True,
                "remote": "registry/echoagent-frontend:202610120930_abc1234",
            }],
            creds={"jump_password": "x"},
            direct=direct,
            log=logs.append,
        )

        self.assertTrue(ok, err)
        self.assertTrue(any("set image" in command for command in commands))
        self.assertTrue(any("kubectl执行位置=跳板机" in line for line in logs))
        self.assertTrue(all(command.startswith("export KUBECONFIG=/etc/robot-ci/kubeconfigs/prod.yaml;") for command in commands))

    def test_deploy_ignores_stale_nodes_without_explicit_node_auth(self) -> None:
        direct_commands: list[str] = []
        hop_calls: list[str] = []

        def direct(command, **_kwargs):  # noqa: ANN001
            direct_commands.append(command)
            if "get deploy echoagent-router -o json" in command:
                return 0, json.dumps({"spec": {"template": {"spec": {"containers": [
                    {"name": "router", "image": "registry/echoagent-router:old"}
                ]}}}}), ""
            return 0, "rolled out", ""

        def hop(command, **_kwargs):  # noqa: ANN001
            hop_calls.append(command)
            return 1, "", "unexpected hop"

        logs: list[str] = []
        ok, err = cce_rollout.deploy_job_results(
            environment={
                "workload_name": "echoagent-router",
                "jump_host": "root@jump",
                "nodes": ["10.20.0.214"],
                "kubeconfig_path": "/etc/robot-ci/kubeconfigs/prod.yaml",
            },
            results=[{
                "service_id": "echoagent-router",
                "ok": True,
                "remote": "registry/echoagent-router:202610121700_abc1234",
            }],
            creds={"jump_password": "x"},
            direct=direct,
            hop=hop,
            log=logs.append,
        )

        self.assertTrue(ok, err)
        self.assertTrue(direct_commands)
        self.assertFalse(hop_calls)
        self.assertTrue(any("忽略节点列表" in line for line in logs))

    def test_statefulset_inplace_release_uses_statefulset_commands(self) -> None:
        commands: list[str] = []
        statefulset = {
            "apiVersion": "apps/v1",
            "kind": "StatefulSet",
            "metadata": {"name": "echoagent-backend"},
            "spec": {"template": {"spec": {"containers": [
                {"name": "backend", "image": "registry/echoagent-backend:old"}
            ]}}},
        }

        def direct(command, **_kwargs):  # noqa: ANN001
            commands.append(command)
            if "get deploy echoagent-backend -o json" in command:
                return 1, "", 'deployments.apps "echoagent-backend" not found'
            if "get statefulset echoagent-backend -o json" in command:
                return 0, json.dumps(statefulset), ""
            return 0, "statefulset rolling update complete", ""

        ok, err = cce_rollout.deploy_job_results(
            environment={
                "name": "prod",
                "workload_name": "echoagent-backend",
                "jump_host": "root@jump",
                "nodes": [],
                "kubeconfig_path": "/etc/robot-ci/kubeconfigs/prod.yaml",
            },
            results=[{
                "service_id": "echoagent-backend",
                "ok": True,
                "remote": "registry/echoagent-backend:202610121700_abc1234",
            }],
            creds={"jump_password": "x"},
            direct=direct,
            mode="inplace",
        )

        self.assertTrue(ok, err)
        release = next(command for command in commands if "set image" in command)
        self.assertIn("set image statefulset/echoagent-backend", release)
        self.assertIn("rollout status statefulset/echoagent-backend", release)

    def test_statefulset_parallel_release_is_rejected_before_mutation(self) -> None:
        commands: list[str] = []
        statefulset = {
            "apiVersion": "apps/v1",
            "kind": "StatefulSet",
            "metadata": {"name": "echoagent-backend"},
            "spec": {"template": {"spec": {"containers": [
                {"name": "backend", "image": "registry/echoagent-backend:old"}
            ]}}},
        }

        def direct(command, **_kwargs):  # noqa: ANN001
            commands.append(command)
            if "get deploy echoagent-backend -o json" in command:
                return 1, "", "not found"
            if "get statefulset echoagent-backend -o json" in command:
                return 0, json.dumps(statefulset), ""
            return 0, "", ""

        ok, err = cce_rollout.deploy_job_results(
            environment={
                "workload_name": "echoagent-backend",
                "jump_host": "root@jump",
                "nodes": [],
                "kubeconfig_path": "/etc/robot-ci/kubeconfigs/prod.yaml",
            },
            results=[{
                "service_id": "echoagent-backend",
                "ok": True,
                "remote": "registry/echoagent-backend:202610121700_abc1234",
            }],
            creds={"jump_password": "x"},
            direct=direct,
            mode="parallel",
            release_id="job-202610121700",
        )

        self.assertFalse(ok)
        self.assertIn("StatefulSet echoagent-backend 不支持平滑升级，请选择替换升级", err)
        self.assertFalse(any("set image" in command or "create -f" in command for command in commands))

    def test_deploy_job_results_parallel_creates_a_second_deployment(self) -> None:
        commands: list[str] = []
        source = {
            "apiVersion": "apps/v1",
            "metadata": {"name": "semantic-schedule", "namespace": "default", "labels": {"app": "schedule"}},
            "spec": {"replicas": 2, "selector": {"matchLabels": {"app": "schedule"}}, "template": {
                "metadata": {"labels": {"app": "schedule"}},
                "spec": {"containers": [{"name": "container-1", "image": "registry/schedule:old"}]},
            }},
            "status": {"readyReplicas": 2, "availableReplicas": 2},
        }

        candidate = None

        def hop(command, **_kwargs):  # noqa: ANN001
            nonlocal candidate
            commands.append(command)
            if "create -f" in command:
                candidate = json.loads(base64.b64decode(re.search(r"printf %s (\S+)", command).group(1)))
                candidate["metadata"]["uid"] = "new-uid"
                candidate["status"] = {"readyReplicas": 1, "availableReplicas": 1}
                return 0, "created", ""
            if "get deploy" in command and "-o json" in command:
                if "semantic-schedule-v" in command:
                    if candidate is None:
                        return 1, "", "NotFound"
                    return 0, json.dumps(candidate), ""
                return 0, json.dumps(source), ""
            return 0, "created", ""

        created: list[dict] = []
        ok, err = cce_rollout.deploy_job_results(
            environment={"workload_name": "semantic-schedule", "jump_host": "root@jump", "nodes": ["node"]},
            results=[{"service_id": "semantic-schedule", "ok": True, "remote": "registry/schedule:new"}],
            creds={"jump_password": "x", "node_password": "x"}, hop=hop,
            mode="parallel", release_id="ab12cd34", on_parallel_created=created.append,
        )
        self.assertTrue(ok, err)
        self.assertRegex(created[0]["name"], r"^semantic-schedule-v20\d{10}$")
        self.assertEqual(0, created[0]["desired_replicas"])
        self.assertTrue(any("create -f" in command for command in commands))
        self.assertFalse(any("set image" in command for command in commands))

    def test_resolve_source_falls_back_to_baseline_when_active_missing(self) -> None:
        baseline = {
            "apiVersion": "apps/v1",
            "metadata": {"name": "semantic-schedule"},
            "spec": {"template": {"spec": {"containers": [{"name": "container-1", "image": "old"}]}}},
        }

        def run_remote(command: str, _timeout: int = 60) -> tuple[int, str, str]:
            if "get deploy semantic-schedule-v-gone" in command:
                return 1, "", 'Error from server (NotFound): deployments.apps "semantic-schedule-v-gone" not found'
            if "get deploy semantic-schedule " in command or command.rstrip().endswith("get deploy semantic-schedule -o json"):
                return 0, json.dumps(baseline), ""
            return 1, "", "unexpected"

        logs: list[str] = []
        name, payload = cce_rollout.resolve_source_deployment(
            run_remote,
            namespace="default",
            active="semantic-schedule-v-gone",
            baseline="semantic-schedule",
            log=logs.append,
        )
        self.assertEqual("semantic-schedule", name)
        self.assertEqual(baseline, payload)
        self.assertTrue(any("活动负载" in line and "基准负载" in line for line in logs))

    def test_resolve_source_uses_unique_approximate_match(self) -> None:
        only = {
            "metadata": {
                "name": "semantic-schedule-v-202609151200",
                "labels": {cce_rollout.PARALLEL_SOURCE_LABEL: "semantic-schedule"},
            },
            "spec": {"template": {"spec": {"containers": [{"name": "c", "image": "x"}]}}},
            "status": {"readyReplicas": 1},
        }

        def run_remote(command: str, _timeout: int = 60) -> tuple[int, str, str]:
            if "get deploy " in command and " -o json" in command and "get deploy -o json" not in command:
                return 1, "", "not found"
            if "get deploy -o json" in command:
                return 0, json.dumps({"items": [only]}), ""
            return 1, "", "unexpected"

        name, payload = cce_rollout.resolve_source_deployment(
            run_remote, namespace="default", active="semantic-schedule-v-gone", baseline="semantic-schedule"
        )
        self.assertEqual("semantic-schedule-v-202609151200", name)
        self.assertEqual(only, payload)

    def test_resolve_source_errors_when_approximate_matches_multiple(self) -> None:
        items = [
            {"metadata": {"name": "semantic-schedule-v-1"}},
            {"metadata": {"name": "semantic-schedule-v-2"}},
        ]

        def run_remote(command: str, _timeout: int = 60) -> tuple[int, str, str]:
            if "get deploy " in command and "get deploy -o json" not in command:
                return 1, "", "not found"
            if "get deploy -o json" in command:
                return 0, json.dumps({"items": items}), ""
            return 1, "", "unexpected"

        with self.assertRaises(cce_rollout.CceRolloutError) as ctx:
            cce_rollout.resolve_source_deployment(
                run_remote, namespace="default", active="gone", baseline="semantic-schedule"
            )
        self.assertIn("多个负载", str(ctx.exception))

    def test_deploy_job_results_requires_creds(self) -> None:
        with patch.object(cce_rollout, "default_key_candidates", return_value=[]):
            ok, err = cce_rollout.deploy_job_results(
                environment={
                    "name": "联调",
                    "workload_name": "semantic-schedule",
                    "jump_host": "root@122.9.139.49",
                    "nodes": ["172.31.8.33"],
                },
                results=[
                    {
                        "service_id": "semantic-schedule",
                        "ok": True,
                        "remote": "swr.example.com/public_ai/semantic-schedule:202609101200_abc",
                    }
                ],
                creds={},
                hop=lambda *args, **kwargs: (0, "", ""),
            )
        self.assertFalse(ok)
        self.assertIn("SSH", err)

    def test_deploy_job_results_uses_environment_kubeconfig(self) -> None:
        commands: list[str] = []

        def hop(command, **_kwargs):  # noqa: ANN001
            commands.append(command)
            if "get deploy" in command and "-o json" in command:
                return 0, json.dumps({"spec": {"template": {"spec": {"containers": [
                    {"name": "container-1", "image": "registry/semantic-schedule:old"}
                ]}}}}), ""
            return 0, "ok", ""

        ok, err = cce_rollout.deploy_job_results(
            environment={
                "workload_name": "semantic-schedule", "jump_host": "root@jump", "nodes": ["node"],
                "kubeconfig_path": "/etc/robot-ci/kubeconfigs/dev2.yaml",
            },
            results=[{"service_id": "semantic-schedule", "ok": True, "remote": "registry/semantic-schedule:new"}],
            creds={"jump_password": "x", "node_password": "x"}, hop=hop, namespace="echo-prod",
        )
        self.assertTrue(ok, err)
        self.assertTrue(commands)
        self.assertTrue(all(command.startswith("export KUBECONFIG=/etc/robot-ci/kubeconfigs/dev2.yaml;") for command in commands))
        self.assertTrue(any("-n echo-prod get deploy" in command for command in commands))

    def test_deploy_job_results_reports_unreadable_kubeconfig(self) -> None:
        ok, err = cce_rollout.deploy_job_results(
            environment={
                "workload_name": "semantic-schedule", "jump_host": "root@jump", "nodes": ["node"],
                "kubeconfig_path": "/etc/robot-ci/kubeconfigs/missing.yaml",
            },
            results=[{"service_id": "semantic-schedule", "ok": True, "remote": "registry/semantic-schedule:new"}],
            creds={"jump_password": "x", "node_password": "x"},
            hop=lambda *_args, **_kwargs: (42, "", "KUBECONFIG_NOT_READABLE"),
            namespace="echo-prod",
        )
        self.assertFalse(ok)
        self.assertIn("无法读取 kubeconfig", err)
        self.assertIn("missing.yaml", err)


class GammaAfterBuildTests(unittest.TestCase):
    def setUp(self) -> None:
        with server._jobs_lock:
            self.saved_jobs = dict(server._jobs)
            server._jobs.clear()

    def tearDown(self) -> None:
        with server._jobs_lock:
            server._jobs.clear()
            server._jobs.update(self.saved_jobs)

    def test_skips_when_not_selected(self) -> None:
        job = _job("gamma-skip")
        self.assertIsNone(server.register_job_if_idle(job))
        ok, err = server.maybe_run_gamma_after_build(
            "gamma-skip",
            [{"service_id": "memory-service", "ok": True, "remote": "swr.example/img:tag1"}],
        )
        self.assertTrue(ok)
        self.assertEqual("", err)

    def test_fails_without_environment(self) -> None:
        job = _job("gamma-noenv")
        job["optional_steps"] = {"gamma_deploy": True, "gamma_test": False, "environment_id": ""}
        self.assertIsNone(server.register_job_if_idle(job))
        ok, err = server.maybe_run_gamma_after_build("gamma-noenv", [])
        self.assertFalse(ok)
        self.assertIn("未选择环境", err)

    def test_deploys_selected_environment(self) -> None:
        job = _job("gamma-run", service_id="semantic-schedule")
        job["optional_steps"] = {
            "production_release": True,
            "gamma_deploy": False,
            "gamma_test": False,
            "environment_id": "env1",
            "deployment_mode": "inplace",
        }
        self.assertIsNone(server.register_job_if_idle(job))
        env = {
            "id": "env1",
            "name": "联调",
            "service_id": "semantic-schedule",
            "cluster_name": "AgentPlatform",
            "workload_name": "semantic-schedule",
            "jump_host": "root@122.9.139.49",
            "jump_password": "jump-from-env",
            "node_password": "node-from-env",
            "nodes": ["172.31.8.33"],
        }
        image = "swr.example.com/public_ai/semantic-schedule:202609101200_abc"
        commands: list[str] = []

        hop_kwargs: dict = {}

        def hop(command, **kwargs):  # noqa: ANN003
            commands.append(command)
            hop_kwargs.update(kwargs)
            if "-o json" in command:
                return (
                    0,
                    json.dumps(
                        {
                            "spec": {
                                "template": {
                                    "spec": {
                                        "containers": [{"name": "container-1", "image": "old"}]
                                    }
                                }
                            }
                        }
                    ),
                    "",
                )
            return 0, "rolled out\n", ""

        with patch.object(server, "get_environment", return_value=env):
            with patch.object(
                cce_rollout,
                "resolve_credentials",
                return_value={"jump_password": "cfg-pass", "node_password": "cfg-pass", "node_user": "root"},
            ):
                ok, err = server.maybe_run_gamma_after_build(
                    "gamma-run",
                    [{"service_id": "semantic-schedule", "ok": True, "remote": image}],
                    hop=hop,
                )
        self.assertTrue(ok, err)
        self.assertTrue(any("set image" in cmd for cmd in commands))
        self.assertEqual("jump-from-env", hop_kwargs.get("jump_password"))
        self.assertEqual("node-from-env", hop_kwargs.get("node_password"))
        with server._jobs_lock:
            self.assertEqual("release", server._jobs["gamma-run"]["stage"])

    def test_parallel_deploy_uses_cce_without_gamma_platform(self) -> None:
        job = _job("gamma-parallel", service_id="semantic-schedule")
        job["optional_steps"] = {
            "production_release": True,
            "gamma_deploy": False,
            "gamma_test": False,
            "environment_id": "env1",
            "deployment_mode": "parallel",
        }
        self.assertIsNone(server.register_job_if_idle(job))
        env = {"id": "env1", "name": "Gamma", "service_id": "semantic-schedule", "workload_name": "semantic-schedule"}
        with patch.object(server, "get_environment", return_value=env):
            with patch.object(cce_rollout, "resolve_credentials", return_value={}):
                with patch.object(cce_rollout, "deploy_job_results", return_value=(True, "")) as deploy:
                    ok, err = server.maybe_run_gamma_after_build(
                        "gamma-parallel",
                        [{"service_id": "semantic-schedule", "ok": True, "remote": "swr.example/img:tag1"}],
                    )
        self.assertTrue(ok, err)
        deploy.assert_called_once()

    def test_gamma_deploy_then_test_stays_in_gamma_driver(self) -> None:
        job = _job("gamma-parallel-test", service_id="semantic-schedule")
        job["optional_steps"] = {
            "gamma_deploy": True,
            "gamma_test": True,
            "environment_id": "env1",
            "deployment_mode": "parallel",
        }
        self.assertIsNone(server.register_job_if_idle(job))
        env = {"id": "env1", "name": "Gamma", "service_id": "semantic-schedule", "workload_name": "semantic-schedule"}
        calls: list[dict] = []
        fake_gamma = types.ModuleType("gamma_real")
        fake_gamma.owns_environment = lambda _env_id: True
        fake_gamma.available = lambda _env_id: True
        fake_gamma.run = lambda _job_id, _results, opts, *_args: (calls.append(opts), (True, ""))[1]
        with patch.dict(sys.modules, {"gamma_real": fake_gamma}):
            with patch.object(server, "get_environment", return_value=env):
                with patch.object(cce_rollout, "resolve_credentials", return_value={}):
                    with patch.object(cce_rollout, "deploy_job_results", return_value=(True, "")):
                        ok, err = server.maybe_run_gamma_after_build(
                            "gamma-parallel-test",
                            [{"service_id": "semantic-schedule", "ok": True, "remote": "swr.example/img:tag1"}],
                        )
        self.assertTrue(ok, err)
        self.assertEqual(1, len(calls))
        self.assertTrue(calls[0]["gamma_deploy"])
        self.assertFalse(calls[0]["production_release"])

    def test_prepare_slot_is_done_after_gamma_starts(self) -> None:
        job = _job("gamma-progressed")
        job.update({"stage": "gamma", "commit_sha": "b1c108f01142", "status": "running", "slot_held": False})
        rows = server._prepare_lane_for_job(job, ["memory-service"], {"memory-service": {"id": "memory-service"}})
        slot = next(row for row in rows if row["id"] == "slot")
        self.assertEqual("done", slot["status"])

    def test_stop_request_hides_running_gamma_stage(self) -> None:
        job = _job("gamma-stopping")
        job.update({
            "stage": "gamma",
            "status": "running",
            "cancel_requested": True,
            "results": [{"service_id": "memory-service", "ok": True}],
            "optional_steps": {"production_release": True, "gamma_deploy": False, "gamma_test": False, "environment_id": "env1"},
        })
        with patch.object(server, "load_services", return_value=[{"id": "memory-service", "title": "memory"}]):
            pipeline = server.build_job_pipeline(job)
        release = next(step for step in pipeline["steps"] if step["id"] == "release")
        self.assertEqual("skipped", release["status"])

    def test_restart_preserves_requested_stop_as_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_log_dir = server.LOG_DIR
            try:
                server.LOG_DIR = root
                (root / "job-abcd1234.json").write_text(json.dumps({
                    "id": "abcd1234", "status": "running", "stage": "gamma", "cancel_requested": True,
                }), encoding="utf-8")
                self.assertEqual(1, server.reap_orphaned_running_jobs())
                meta = json.loads((root / "job-abcd1234.json").read_text(encoding="utf-8"))
            finally:
                server.LOG_DIR = old_log_dir
        self.assertEqual("stopped", meta["status"])
        self.assertEqual("done", meta["stage"])

    def test_restart_converts_old_failed_stop_request_to_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_log_dir = server.LOG_DIR
            try:
                server.LOG_DIR = root
                (root / "job-abcd1234.json").write_text(json.dumps({
                    "id": "abcd1234", "status": "failed", "stage": "gamma", "cancel_requested": True,
                }), encoding="utf-8")
                self.assertEqual(1, server.reap_orphaned_running_jobs())
                meta = json.loads((root / "job-abcd1234.json").read_text(encoding="utf-8"))
            finally:
                server.LOG_DIR = old_log_dir
        self.assertEqual("stopped", meta["status"])

    def test_disk_failed_job_with_stop_request_is_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_log_dir = server.LOG_DIR
            try:
                server.LOG_DIR = root
                (root / "job-abcd1234.json").write_text(json.dumps({
                    "id": "abcd1234", "status": "failed", "stage": "gamma", "cancel_requested": True,
                }), encoding="utf-8")
                loaded = server.load_job_from_disk("abcd1234")
            finally:
                server.LOG_DIR = old_log_dir
        self.assertEqual("stopped", loaded["status"])
        self.assertEqual("done", loaded["stage"])

    def test_rejects_environment_owned_by_other_service(self) -> None:
        job = _job("gamma-wrong-env", service_id="memory-service")
        job["optional_steps"] = {
            "gamma_deploy": True,
            "gamma_test": False,
            "environment_id": "env1",
        }
        self.assertIsNone(server.register_job_if_idle(job))
        env = {
            "id": "env1",
            "name": "schedule-gamma",
            "service_id": "semantic-schedule",
            "workload_name": "semantic-schedule",
        }
        with patch.object(server, "get_environment", return_value=env):
            ok, err = server.maybe_run_gamma_after_build(
                "gamma-wrong-env",
                [{"service_id": "memory-service", "ok": True, "remote": "swr.example/img:tag1"}],
            )
        self.assertFalse(ok)
        self.assertIn("semantic-schedule", err)
        self.assertIn("不符", err)

    def test_run_push_job_marks_gamma_failure(self) -> None:
        job = _job("gamma-hook", service_id="memory-service")
        job["status"] = "running"
        job["stage"] = "starting"
        self.assertIsNone(server.register_job_if_idle(job))
        svc = {"id": "memory-service", "title": "memory", "default_branch": "main"}
        result = {
            "service_id": "memory-service",
            "ok": True,
            "remote": "swr.example.com/public_ai/memory-service:tag1",
            "archive": "",
        }
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(server, "load_services", return_value=[svc]):
                with patch.object(server, "wait_for_build_slot", return_value=True):
                    with patch.object(server, "register_live_job_marker"):
                        with patch.object(server, "unregister_live_job_marker"):
                            with patch.object(server, "ensure_swr_login", return_value=True):
                                with patch.object(
                                    server, "make_archive_dir", return_value=Path(tmp)
                                ):
                                    with patch.object(
                                        server,
                                        "host_service_compile_lock",
                                        return_value=nullcontext(),
                                    ):
                                        with patch.object(
                                            server, "push_one_service", return_value=result
                                        ):
                                            with patch.object(
                                                server,
                                                "maybe_run_gamma_after_build",
                                                return_value=(False, "kubectl failed"),
                                            ) as gamma:
                                                with patch.object(server, "reclaim_ci_disk"):
                                                    with patch.object(
                                                        server, "release_build_slot"
                                                    ):
                                                        with patch.object(server, "append_job_log"):
                                                            with patch.object(
                                                                server, "persist_job_meta"
                                                            ):
                                                                server.run_push_job(
                                                                    "gamma-hook",
                                                                    [
                                                                        {
                                                                            "service_id": "memory-service",
                                                                            "branch": "main",
                                                                        }
                                                                    ],
                                                                )
        gamma.assert_called_once()
        with server._jobs_lock:
            stored = server._jobs["gamma-hook"]
        self.assertEqual("failed", stored["status"])
        self.assertEqual("gamma", stored["stage"])
        self.assertEqual("kubectl failed", stored["error"])
