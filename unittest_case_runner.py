#!/usr/bin/env python3
"""Run unittest discovery and emit a case-level JSON report with durations."""
from __future__ import annotations

import argparse
import json
import time
import unittest
from pathlib import Path
from typing import Any


class TimingResult(unittest.TextTestResult):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.started: dict[int, float] = {}
        self.cases: list[dict[str, Any]] = []

    def startTest(self, test: unittest.TestCase) -> None:  # noqa: N802
        self.started[id(test)] = time.monotonic()
        super().startTest(test)

    def _record(self, test: unittest.TestCase, status: str, detail: str = "") -> None:
        started = self.started.pop(id(test), time.monotonic())
        item: dict[str, Any] = {
            "name": test.id(),
            "status": status,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
        if detail:
            item["detail"] = detail.strip()[:1000]
        self.cases.append(item)

    def addSuccess(self, test: unittest.TestCase) -> None:  # noqa: N802
        super().addSuccess(test)
        self._record(test, "passed")

    def addFailure(self, test: unittest.TestCase, err: Any) -> None:  # noqa: N802
        detail = self._exc_info_to_string(err, test)
        super().addFailure(test, err)
        self._record(test, "failed", detail)

    def addError(self, test: unittest.TestCase, err: Any) -> None:  # noqa: N802
        detail = self._exc_info_to_string(err, test)
        super().addError(test, err)
        self._record(test, "error", detail)

    def addSkip(self, test: unittest.TestCase, reason: str) -> None:  # noqa: N802
        super().addSkip(test, reason)
        self._record(test, "skipped", reason)

    def addExpectedFailure(self, test: unittest.TestCase, err: Any) -> None:  # noqa: N802
        super().addExpectedFailure(test, err)
        self._record(test, "skipped", "expected failure")

    def addUnexpectedSuccess(self, test: unittest.TestCase) -> None:  # noqa: N802
        super().addUnexpectedSuccess(test)
        self._record(test, "failed", "unexpected success")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-dir", default="tests")
    parser.add_argument("--pattern", default="test_*.py")
    parser.add_argument("--top-level-dir")
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.discover(args.start_dir, args.pattern, args.top_level_dir)
    result = unittest.TextTestRunner(verbosity=2, resultclass=TimingResult).run(suite)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps({"cases": result.cases}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
