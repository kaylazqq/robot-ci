import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server
from cid_config import load_cid_config


ROOT = Path(__file__).resolve().parents[1]


class LogAgentServiceTests(unittest.TestCase):
    def test_log_agent_is_registered_for_image_and_pytest(self) -> None:
        plans = json.loads((ROOT / "test-plans.json").read_text(encoding="utf-8"))
        catalog = json.loads((ROOT / "test-suites" / "catalog.json").read_text(encoding="utf-8"))
        services = json.loads((ROOT / "services.json").read_text(encoding="utf-8"))

        item = next(service for service in services if service["id"] == "log-agent")
        self.assertEqual("l-98761/log-agent", item["repo"])
        self.assertEqual("https://github.com/l-98761/log-agent.git", item["github"])
        self.assertEqual("main", item["default_branch"])
        self.assertEqual("log-agent", item["image"])
        self.assertEqual("log-agent_", item["tar_prefix"])
        self.assertEqual(["python"], item["base_image_targets"])

        self.assertEqual("log-agent-pytest", plans["services"]["log-agent"])
        command = plans["profiles"]["log-agent-pytest"]["commands"][0]["command"]
        self.assertIn("pytest -q tests", command)
        self.assertIn("log-agent", catalog["services"])
        roots = catalog["services"]["log-agent"]["roots"]
        self.assertIn("tests", roots)

    def test_cid_build_uses_package_script(self) -> None:
        svc = {"id": "log-agent", "image": "log-agent"}
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".cid").mkdir()
            (repo / ".cid" / "build.yaml").write_text(
                """
version: 1
service:
  id: log-agent
  name: log-agent
  language: python
  image: log-agent
scripts:
  - id: ut
    name: log-agent pytest UT
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
            cid = load_cid_config(repo, "log-agent")
            self.assertIsNotNone(cid)
            with patch.object(server, "repo_dir", return_value=repo), patch.object(
                server, "host_path", side_effect=lambda path: str(path)
            ), patch.object(server, "public_service_dir", return_value=Path("/tmp/public-service")), patch.object(
                server, "append_job_log"
            ), patch.object(
                server, "run_stream", return_value=0
            ) as run_stream, patch.object(
                server.time, "strftime", return_value="202609101800"
            ):
                ok, detail = server.build_from_source("job", svc, "abc1234")
            self.assertTrue(ok, detail)
            command = run_stream.call_args.args[1][-1]
            self.assertIn("bash build/package/build.sh", command)
            self.assertIn("LOG_AGENT_IMAGE=", command)
            self.assertIn("local/log-agent:202609101800_abc1234", command)


if __name__ == "__main__":
    unittest.main()
