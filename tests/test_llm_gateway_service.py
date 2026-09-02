import json
import unittest
from pathlib import Path
from unittest.mock import patch

import server


ROOT = Path(__file__).resolve().parents[1]


class LlmGatewayServiceTests(unittest.TestCase):
    def test_catalog_points_at_rollingfruit_llm_gateway(self) -> None:
        services = json.loads((ROOT / "services.json").read_text(encoding="utf-8"))
        item = next(service for service in services if service["id"] == "llm-gateway")
        self.assertEqual("rollingfruit/llm-gateway", item["repo"])
        self.assertEqual("https://github.com/rollingfruit/llm-gateway.git", item["github"])
        self.assertEqual("main", item["default_branch"])
        self.assertEqual("llm-gateway", item["image"])
        self.assertEqual("llm-gateway_", item["tar_prefix"])
        self.assertTrue(item["dockerfile_build"])
        self.assertTrue(item["skip_public_service"])

    def test_test_plan_runs_repo_pytest(self) -> None:
        plans = json.loads((ROOT / "test-plans.json").read_text(encoding="utf-8"))
        self.assertEqual("llm-gateway-pytest", plans["services"]["llm-gateway"])
        command = plans["profiles"]["llm-gateway-pytest"]["commands"][0]["command"]
        self.assertIn("pytest -q tests", command)
        self.assertIn("compileall", command)

    def test_dockerfile_build_tags_local_image(self) -> None:
        svc = {
            "id": "llm-gateway",
            "image": "llm-gateway",
            "dockerfile_build": True,
            "build_env": {"PIP_INDEX_URL": "https://example.invalid/simple/"},
        }
        with patch.object(server, "repo_dir", return_value=Path("/tmp/llm-gateway")), patch.object(
            server, "host_path", side_effect=lambda path: str(path)
        ), patch.object(server, "public_service_dir", return_value=Path("/tmp/public-service")), patch.object(
            server, "append_job_log"
        ), patch.object(server, "run_stream", return_value=0) as run_stream, patch.object(
            server.time, "strftime", return_value="202609021800"
        ):
            ok, detail = server.build_from_source("job", svc, "abc1234")
        self.assertTrue(ok, detail)
        command = run_stream.call_args.args[1][-1]
        self.assertIn("docker build", command)
        self.assertIn("local/llm-gateway:202609021800_abc1234", command)
        self.assertIn("PIP_INDEX_URL", command)


if __name__ == "__main__":
    unittest.main()
