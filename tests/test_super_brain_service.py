import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server
from cid_config import load_cid_config


ROOT = Path(__file__).resolve().parents[1]


class SuperBrainServiceTests(unittest.TestCase):
    def test_super_brain_is_registered_for_image_and_pytest(self) -> None:
        plans = json.loads((ROOT / "test-plans.json").read_text(encoding="utf-8"))
        catalog = json.loads((ROOT / "test-suites" / "catalog.json").read_text(encoding="utf-8"))
        services = json.loads((ROOT / "services.json").read_text(encoding="utf-8"))

        item = next(service for service in services if service["id"] == "super-brain")
        self.assertEqual("censong574-spec/super-brain", item["repo"])
        self.assertEqual("https://github.com/censong574-spec/super-brain.git", item["github"])
        self.assertEqual("main", item["default_branch"])
        self.assertEqual("super-brain", item["image"])
        self.assertEqual("super-brain_", item["tar_prefix"])
        self.assertEqual(["python"], item["base_image_targets"])
        self.assertFalse(any(service["id"] == "log-agent" for service in services))

        self.assertEqual("super-brain-pytest", plans["services"]["super-brain"])
        self.assertNotIn("log-agent", plans["services"])
        command = plans["profiles"]["super-brain-pytest"]["commands"][0]["command"]
        self.assertIn("pytest -q tests", command)
        self.assertIn("super-brain", catalog["services"])
        self.assertNotIn("log-agent", catalog["services"])
        roots = catalog["services"]["super-brain"]["roots"]
        self.assertIn("tests", roots)

    def test_cid_build_uses_package_script(self) -> None:
        svc = {"id": "super-brain", "image": "super-brain"}
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".cid").mkdir()
            (repo / ".cid" / "build.yaml").write_text(
                """
version: 1
service:
  id: super-brain
  name: super-brain
  language: python
  image: super-brain
scripts:
  - id: ut
    name: super-brain pytest UT
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
            cid = load_cid_config(repo, "super-brain")
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
            self.assertIn("SUPER_BRAIN_IMAGE=", command)
            self.assertNotIn("LOG_AGENT_IMAGE=", command)
            self.assertIn("local/super-brain:202609101800_abc1234", command)


if __name__ == "__main__":
    unittest.main()
