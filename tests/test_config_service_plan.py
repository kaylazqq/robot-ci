import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ConfigServicePlanTests(unittest.TestCase):
    def test_config_service_is_registered_for_ut_and_dt(self) -> None:
        plans = json.loads((ROOT / "test-plans.json").read_text(encoding="utf-8"))
        catalog = json.loads((ROOT / "test-suites" / "catalog.json").read_text(encoding="utf-8"))
        services = json.loads((ROOT / "services.json").read_text(encoding="utf-8"))

        self.assertTrue(any(item["id"] == "config-service" for item in services))
        self.assertEqual("config-service-go", plans["services"]["config-service"])
        commands = plans["profiles"]["config-service-go"]["commands"]
        joined = " ".join(item["command"] for item in commands)
        self.assertIn("scripts/ci/ut.sh", joined)
        self.assertIn("scripts/ci/dt.sh", joined)
        self.assertIn("config-service", catalog["services"])
        roots = catalog["services"]["config-service"]["roots"]
        self.assertIn("tests/dt", roots)
        self.assertIn("internal/httpapi", roots)


if __name__ == "__main__":
    unittest.main()
