import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_runner


class TestResultParsing(unittest.TestCase):
    def test_go_parser_keeps_leaf_cases_and_resolves_source_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            package_dir = repo / "server" / "public" / "model"
            package_dir.mkdir(parents=True)
            (repo / "server" / "go.mod").write_text(
                "module github.com/example/project/server\n", encoding="utf-8"
            )
            (package_dir / "version_test.go").write_text(
                "package model\nfunc TestVersion(t *testing.T) {}\n", encoding="utf-8"
            )
            package = "github.com/example/project/server/public/model"
            report = repo / "go-test.json"
            events = [
                {"Time": "2026-08-04T10:00:00.000000Z", "Action": "run", "Package": package, "Test": "TestVersion"},
                {"Time": "2026-08-04T10:00:00.000010Z", "Action": "run", "Package": package, "Test": "TestVersion/1.2.3-rc1"},
                {"Time": "2026-08-04T10:00:00.000020Z", "Action": "output", "Package": package, "Test": "TestVersion/1.2.3-rc1", "Output": "version_test.go:10: environment is unsupported\n"},
                {"Time": "2026-08-04T10:00:00.000030Z", "Action": "skip", "Package": package, "Test": "TestVersion/1.2.3-rc1", "Elapsed": 0},
                {"Time": "2026-08-04T10:00:00.000040Z", "Action": "pass", "Package": package, "Test": "TestVersion", "Elapsed": 0},
            ]
            report.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")

            total, passed, failed, _failures, cases = test_runner.parse_go_json(report, "Go tests", "", repo)

            self.assertEqual((total, passed, failed), (1, 0, 0))
            self.assertEqual(cases[0]["name"], "TestVersion/1.2.3-rc1")
            self.assertEqual(cases[0]["file"], "server/public/model/version_test.go")
            self.assertEqual(cases[0]["detail"], "environment is unsupported")
            self.assertGreater(cases[0]["duration_ms"], 0)

    def test_junit_parser_resolves_python_test_file_and_skip_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            test_dir = repo / "tests"
            test_dir.mkdir()
            (test_dir / "test_api.py").write_text("def test_health(): pass\n", encoding="utf-8")
            report = repo / "pytest.xml"
            report.write_text(
                '<testsuite><testcase classname="tests.test_api" name="test_health" time="0.001">'
                '<skipped message="requires PostgreSQL" /></testcase></testsuite>',
                encoding="utf-8",
            )

            total, passed, failed, _failures, cases = test_runner.parse_junit(report, "pytest", "", repo)

            self.assertEqual((total, passed, failed), (1, 0, 0))
            self.assertEqual(cases[0]["name"], "test_health")
            self.assertEqual(cases[0]["file"], "tests/test_api.py")
            self.assertEqual(cases[0]["detail"], "requires PostgreSQL")

    def test_junit_parser_decodes_unicode_parameter_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            report = repo / "pytest.xml"
            report.write_text(
                '<testsuite><testcase classname="tests.test_content_safety" '
                'name="test_static[\\u4e2d\\u6587]" time="0" /></testsuite>',
                encoding="utf-8",
            )

            _total, _passed, _failed, _failures, cases = test_runner.parse_junit(
                report, "pytest", "", repo
            )

            self.assertEqual(cases[0]["name"], "tests.test_content_safety.test_static[中文]")

    def test_build_test_environment_supplies_persistent_go_and_pip_caches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache_root = root / "shared-cache"
            with patch.dict(
                os.environ,
                {"PATH": os.environ.get("PATH", ""), "SWR_TEST_CACHE_ROOT": str(cache_root)},
                clear=True,
            ):
                env = test_runner.build_test_environment(root, root / "reports")

            self.assertTrue(env["HOME"])
            self.assertEqual(env["GOCACHE"], str(cache_root / "go-build"))
            self.assertEqual(env["GOMODCACHE"], str(cache_root / "go-mod"))
            self.assertEqual(env["GOPATH"], str(cache_root / "go-path"))
            self.assertEqual(env["PIP_CACHE_DIR"], str(cache_root / "pip"))
            for name in ("GOCACHE", "GOMODCACHE", "GOPATH", "PIP_CACHE_DIR"):
                self.assertTrue(Path(env[name]).is_dir())

    def test_build_test_environment_keeps_parent_tmpdir(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tmp_root = root / "ci-tmp"
            tmp_root.mkdir()
            with patch.dict(
                os.environ,
                {
                    "PATH": os.environ.get("PATH", ""),
                    "TMPDIR": str(tmp_root),
                    "SWR_TEST_CACHE_ROOT": str(root / "cache"),
                },
                clear=True,
            ):
                env = test_runner.build_test_environment(root, root / "reports")
            self.assertEqual(env["TMPDIR"], str(tmp_root))
            self.assertEqual(env["GOTMPDIR"], str(tmp_root))


if __name__ == "__main__":
    unittest.main()
