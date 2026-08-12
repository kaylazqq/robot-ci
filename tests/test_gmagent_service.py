import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server


ROOT = Path(__file__).resolve().parents[1]


class GmAgentServiceTests(unittest.TestCase):
    def test_catalog_uses_cloud_orchestration_branch_and_image(self) -> None:
        services = json.loads((ROOT / "services.json").read_text(encoding="utf-8"))
        gmagent = next(service for service in services if service["id"] == "gmagent")
        self.assertEqual("adshhzy/gmagent", gmagent["repo"])
        self.assertEqual("codex/cloud-im-orchestration", gmagent["default_branch"])
        self.assertEqual("gmagent", gmagent["image"])
        self.assertEqual("gmagent_", gmagent["tar_prefix"])
        self.assertTrue(gmagent["prefer_token_https"])

    def test_gmagent_prefers_token_https_over_shared_ssh_key(self) -> None:
        with patch.dict(
            server.CFG,
            {"github_use_ssh": True, "github_ssh_key": "/tmp/key"},
            clear=False,
        ):
            server._token_cache = "test-token"
            clone_url = server.clone_url_for(
                "https://github.com/adshhzy/gmagent.git",
                prefer_token_https=True,
            )
        self.assertTrue(clone_url.startswith("https://x-access-token:"))
        self.assertTrue(clone_url.endswith("@github.com/adshhzy/gmagent.git"))

    def test_test_plan_is_registered(self) -> None:
        plans = json.loads((ROOT / "test-plans.json").read_text(encoding="utf-8"))
        self.assertEqual("gmagent-pytest", plans["services"]["gmagent"])
        command = plans["profiles"]["gmagent-pytest"]["commands"][0]["command"]
        self.assertIn("test_orchestration_intent_api.py", command)
        self.assertNotIn("DEEPSEEK_API_KEY", command)

    def test_runtime_secrets_are_removed_from_build_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service_dir = Path(temp_dir) / "gmagent"
            service_dir.mkdir()
            with patch.dict(server.CFG, {"workspace_root": temp_dir}, clear=False), patch.dict(
                os.environ,
                {
                    "GM_AGENT_DATABASE_URL": "postgres://secret",
                    "GM_IM_RELAY_BEARER_TOKEN": "relay-secret",
                    "DEEPSEEK_API_KEY": "secret-key",
                    "SWR_BUILD_MARKER": "kept",
                },
                clear=False,
            ), patch.object(server, "run_stream", return_value=0) as run_stream:
                ok, detail = server.build_from_source(
                    "job",
                    {"id": "gmagent", "image": "gmagent"},
                    "4e3ace0",
                )

        self.assertTrue(ok, detail)
        env = run_stream.call_args.kwargs["env"]
        self.assertNotIn("GM_AGENT_DATABASE_URL", env)
        self.assertNotIn("GM_IM_RELAY_BEARER_TOKEN", env)
        self.assertNotIn("DEEPSEEK_API_KEY", env)
        self.assertEqual("kept", env["SWR_BUILD_MARKER"])


if __name__ == "__main__":
    unittest.main()
