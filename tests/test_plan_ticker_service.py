import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server
from cid_config import load_cid_config


ROOT = Path(__file__).resolve().parents[1]


class PlanTickerServiceTests(unittest.TestCase):
    def test_plan_ticker_is_registered_for_image_and_go_ut(self) -> None:
        plans = json.loads((ROOT / "test-plans.json").read_text(encoding="utf-8"))
        catalog = json.loads((ROOT / "test-suites" / "catalog.json").read_text(encoding="utf-8"))
        services = json.loads((ROOT / "services.json").read_text(encoding="utf-8"))

        item = next(service for service in services if service["id"] == "plan-ticker")
        self.assertEqual("censong574-spec/plan-ticker", item["repo"])
        self.assertEqual("https://github.com/censong574-spec/plan-ticker.git", item["github"])
        self.assertEqual("main", item["default_branch"])
        self.assertEqual("plan-ticker", item["image"])
        self.assertEqual("plan-ticker_", item["tar_prefix"])
        self.assertEqual(["ubuntu"], item["base_image_targets"])

        # Fallback plan remains for old branches without .cid; CID UT is preferred.
        self.assertEqual("plan-ticker-go", plans["services"]["plan-ticker"])
        command = plans["profiles"]["plan-ticker-go"]["commands"][0]["command"]
        self.assertIn("go test -count=1 -json ./...", command)
        self.assertIn("plan-ticker", catalog["services"])
        roots = catalog["services"]["plan-ticker"]["roots"]
        self.assertIn("internal/httpapi", roots)
        self.assertIn("internal/ticker", roots)

    def test_cid_build_uses_package_script(self) -> None:
        svc = {"id": "plan-ticker", "image": "plan-ticker"}
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".cid").mkdir()
            (repo / ".cid" / "build.yaml").write_text(
                """
version: 1
service:
  id: plan-ticker
  name: plan-ticker
  language: go
  image: plan-ticker
scripts:
  - id: ut
    name: plan-ticker Go UT
    type: test
    test_type: ut
    enabled: true
    command: bash scripts/ci/ut.sh
    timeout_sec: 900
    report:
      format: go-json
      path: .cid/output/reports/ut/go-test.json
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
            cid = load_cid_config(repo, "plan-ticker")
            self.assertIsNotNone(cid)
            with patch.object(server, "repo_dir", return_value=repo), patch.object(
                server, "host_path", side_effect=lambda path: str(path)
            ), patch.object(server, "public_service_dir", return_value=Path("/tmp/public-service")), patch.object(
                server, "append_job_log"
            ), patch.object(
                server, "run_stream", return_value=0
            ) as run_stream, patch.object(
                server.time, "strftime", return_value="202609091800"
            ):
                ok, detail = server.build_from_source("job", svc, "abc1234")
            self.assertTrue(ok, detail)
            command = run_stream.call_args.args[1][-1]
            self.assertIn("bash build/package/build.sh", command)
            self.assertIn("PLAN_TICKER_IMAGE=", command)
            self.assertIn("local/plan-ticker:202609091800_abc1234", command)


if __name__ == "__main__":
    unittest.main()
