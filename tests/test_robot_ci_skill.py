import argparse
import importlib.util
import unittest
import json
import tempfile
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "skills/robot-ci-run-pipeline/scripts/robot_ci_pipeline.py"
SPEC = importlib.util.spec_from_file_location("robot_ci_pipeline_skill", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class RobotCiSkillTests(unittest.TestCase):
    def test_run_waits_until_success_and_reuses_returned_run_id(self):
        args = argparse.Namespace(
            pipeline_id="pipeline-1", branch="feature/api", commit_sha="A" * 40,
            reason="test", idempotency_key="request-123", timeout=10, poll_interval=0,
        )
        responses = [
            {"run_id": "abc123abc123", "status": "queued"},
            {"run_id": "abc123abc123", "status": "running"},
            {"run_id": "abc123abc123", "status": "success"},
        ]
        with patch.object(MODULE, "request", side_effect=responses) as request:
            result = MODULE.run_pipeline(args)
        self.assertEqual("success", result["status"])
        self.assertEqual("a" * 40, request.call_args_list[0].args[2]["commit_sha"])
        self.assertEqual("request-123", request.call_args_list[0].args[3]["Idempotency-Key"])

    def test_config_requires_url_and_token(self):
        with patch.dict(MODULE.os.environ, {}, clear=True), self.assertRaises(RuntimeError):
            MODULE.config()

    def test_config_reads_user_file_without_environment_variables(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / ".robot-ci" / "config.json"
            target.parent.mkdir()
            target.write_text(json.dumps({"base_url": "http://ci.example", "token": "rci_test"}), encoding="utf-8")
            with patch.dict(MODULE.os.environ, {}, clear=True), patch.object(MODULE.Path, "home", return_value=root):
                self.assertEqual(("http://ci.example", "rci_test"), MODULE.config())

    def test_request_preserves_remote_commit_sha_from_conflict(self):
        body = BytesIO(json.dumps({
            "error": "remote branch commit does not match",
            "remote_commit_sha": "b" * 40,
        }).encode())
        conflict = HTTPError("http://ci.example/api/ai/pipeline-runs", 409, "Conflict", {}, body)
        with patch.object(MODULE, "config", return_value=("http://ci.example", "rci_test")), \
                patch.object(MODULE, "urlopen", side_effect=conflict):
            with self.assertRaisesRegex(RuntimeError, r"remote_commit_sha=" + "b" * 40):
                MODULE.request("POST", "/api/ai/pipeline-runs", {})


if __name__ == "__main__":
    unittest.main()
