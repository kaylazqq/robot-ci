import json
import tempfile
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

    def test_resolve_credentials_prefers_env(self) -> None:
        creds = cce_rollout.resolve_credentials(
            {"cce_ssh_password": "from-cfg"},
            {"CCE_JUMP_PASSWORD": "from-env"},
            load_default_files=False,
        )
        self.assertEqual("from-env", creds["jump_password"])
        self.assertEqual("from-cfg", creds["node_password"])

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
        self.assertTrue(any("container-1=" in cmd for cmd in commands))
        self.assertFalse(any("filebeat=" in cmd for cmd in commands))

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
            "gamma_deploy": True,
            "gamma_test": False,
            "environment_id": "env1",
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
            self.assertEqual("gamma", server._jobs["gamma-run"]["stage"])

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
                                with patch.object(server, "ensure_public_service", return_value=(True, "")):
                                    with patch.object(
                                        server, "ensure_versioned_base_images", return_value=(True, "")
                                    ):
                                        with patch.object(server, "base_image_targets_for_services", return_value=[]):
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
