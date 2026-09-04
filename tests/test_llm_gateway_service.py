import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server
from cid_config import load_cid_config


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
        self.assertNotIn("dockerfile_build", item)
        self.assertTrue(item["skip_public_service"])

    def test_test_plan_runs_repo_pytest(self) -> None:
        # Fallback plan remains for old branches without .cid; CID UT is preferred.
        plans = json.loads((ROOT / "test-plans.json").read_text(encoding="utf-8"))
        self.assertEqual("llm-gateway-pytest", plans["services"]["llm-gateway"])
        command = plans["profiles"]["llm-gateway-pytest"]["commands"][0]["command"]
        self.assertIn("pytest -q tests", command)
        self.assertIn("compileall", command)

    def test_cid_build_uses_package_script(self) -> None:
        svc = {
            "id": "llm-gateway",
            "image": "llm-gateway",
            "skip_public_service": True,
        }
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".cid").mkdir()
            (repo / ".cid" / "build.yaml").write_text(
                """
version: 1
service:
  id: llm-gateway
  name: llm-gateway
  language: python
  image: llm-gateway
scripts:
  - id: ut
    name: UT
    type: test
    test_type: ut
    enabled: true
    command: bash scripts/ci/ut.sh
    timeout_sec: 1800
    report:
      format: junit
      path: .cid/output/reports/ut/pytest.xml
  - id: build
    name: Build image
    type: build
    enabled: true
    command: bash build/package/build.sh
    timeout_sec: 3600
artifacts:
  image:
    enabled: true
    delivery: swr
    archive: true
  package:
    enabled: false
""".strip()
                + "\n",
                encoding="utf-8",
            )
            (repo / "build" / "package").mkdir(parents=True)
            (repo / "build" / "package" / "build.sh").write_text("#!/bin/bash\n", encoding="utf-8")
            cid = load_cid_config(repo, "llm-gateway")
            self.assertIsNotNone(cid)
            with patch.object(server, "repo_dir", return_value=repo), patch.object(
                server, "host_path", side_effect=lambda path: str(path)
            ), patch.object(server, "public_service_dir", return_value=Path("/tmp/public-service")), patch.object(
                server, "append_job_log"
            ), patch.object(server, "run_stream", return_value=0) as run_stream, patch.object(
                server.time, "strftime", return_value="202609021800"
            ):
                ok, detail = server.build_from_source("job", svc, "abc1234")
            self.assertTrue(ok, detail)
            command = run_stream.call_args.args[1][-1]
            self.assertIn("bash build/package/build.sh", command)
            self.assertIn("LLM_GATEWAY_IMAGE=", command)
            self.assertIn("local/llm-gateway:202609021800_abc1234", command)


if __name__ == "__main__":
    unittest.main()
