import json
import unittest
from pathlib import Path

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

        self.assertEqual("plan-ticker-go", plans["services"]["plan-ticker"])
        command = plans["profiles"]["plan-ticker-go"]["commands"][0]["command"]
        self.assertIn("go test -count=1 -json ./...", command)
        self.assertIn("plan-ticker", catalog["services"])
        roots = catalog["services"]["plan-ticker"]["roots"]
        self.assertIn("internal/httpapi", roots)
        self.assertIn("internal/ticker", roots)


if __name__ == "__main__":
    unittest.main()
