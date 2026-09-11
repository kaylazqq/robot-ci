import tempfile
import unittest
from pathlib import Path

from cid_config import CidConfigError, build_test_plan, enabled_build_step, load_cid_config


VALID = """\
version: 1
service:
  id: demo
  name: Demo
  language: python
  image: demo
scripts:
  - id: ut
    name: Demo UT
    type: test
    test_type: ut
    enabled: true
    command: bash scripts/ci/ut.sh
    timeout_sec: 60
    report:
      format: junit
      path: .cid/output/reports/ut/pytest.xml
  - id: dt
    name: Demo DT
    type: test
    test_type: dt
    enabled: false
    reason: No DT yet.
  - id: build
    name: Build image
    type: build
    enabled: true
    command: bash build/package/build.sh
    timeout_sec: 600
artifacts:
  image:
    enabled: true
    delivery: swr
"""


class CidConfigTests(unittest.TestCase):
    def write(self, text: str) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / ".cid").mkdir()
        (root / ".cid" / "build.yaml").write_text(text, encoding="utf-8")
        return root

    def test_loads_build_and_enabled_test_stages(self) -> None:
        config = load_cid_config(self.write(VALID), "demo")
        self.assertIsNotNone(config)
        self.assertEqual("bash build/package/build.sh", enabled_build_step(config)["command"])
        plan = build_test_plan(config)
        command = plan["profiles"]["cid-demo"]["commands"][0]
        self.assertEqual("ut/pytest.xml", command["report"])
        self.assertEqual("bash scripts/ci/ut.sh", command["command"])
        self.assertEqual("ut", command["test_type"])
        self.assertEqual(1, len(plan["profiles"]["cid-demo"]["commands"]))

    def test_rejects_framework_owned_keys_anywhere(self) -> None:
        for forbidden in ("dependencies: {}", "machine: {}", "continue_on_error: true"):
            with self.subTest(forbidden=forbidden):
                with self.assertRaises(CidConfigError):
                    load_cid_config(self.write(VALID + "\n" + forbidden), "demo")

    def test_enabled_dt_without_report_uses_command_result_table(self) -> None:
        enabled_dt = VALID.replace(
            """    enabled: false
    reason: No DT yet.
""",
            """    enabled: true
    command: bash scripts/ci/dt.sh
""",
        )
        config = load_cid_config(self.write(enabled_dt), "demo")
        plan = build_test_plan(config)
        commands = plan["profiles"]["cid-demo"]["commands"]
        dt = next(command for command in commands if command["test_type"] == "dt")
        self.assertEqual("bash scripts/ci/dt.sh", dt["command"])
        self.assertNotIn("parser", dt)
        self.assertNotIn("report", dt)

    def test_rejects_service_id_mismatch(self) -> None:
        with self.assertRaisesRegex(CidConfigError, "service.id mismatch"):
            load_cid_config(self.write(VALID), "another-service")

    def test_rejects_missing_artifact_contract(self) -> None:
        without_artifacts = VALID.split("artifacts:", 1)[0]
        with self.assertRaisesRegex(CidConfigError, "artifacts must be a mapping"):
            load_cid_config(self.write(without_artifacts), "demo")

    def test_accepts_archive_only_package_pattern(self) -> None:
        package_contract = VALID.replace(
            """  image:
    enabled: true
    delivery: swr
""",
            """  image:
    enabled: false
    delivery: archive-only
    archive: false
  package:
    enabled: true
    pattern: .cid/output/demo_*.tar.gz
    delivery: archive-only
""",
        )
        config = load_cid_config(self.write(package_contract), "demo")
        self.assertTrue(config["artifacts"]["package"]["enabled"])

    def test_rejects_package_pattern_outside_workspace(self) -> None:
        unsafe = VALID.replace(
            """  image:
    enabled: true
    delivery: swr
""",
            """  image:
    enabled: false
    delivery: archive-only
  package:
    enabled: true
    pattern: ../demo_*.tar.gz
""",
        )
        with self.assertRaisesRegex(CidConfigError, "safe relative glob"):
            load_cid_config(self.write(unsafe), "demo")


if __name__ == "__main__":
    unittest.main()
