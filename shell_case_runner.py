#!/usr/bin/env python3
"""Run each matched shell test file separately and emit case-level JSON."""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pattern", action="append", required=True)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    root = Path.cwd()
    files = sorted({path for pattern in args.pattern for path in root.glob(pattern) if path.is_file()})
    cases = []
    for path in files:
        started = time.monotonic()
        proc = subprocess.run(["bash", str(path)], cwd=root, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding="utf-8", errors="replace")
        detail = (proc.stdout or "").strip()[-1000:]
        item = {
            "name": path.relative_to(root).as_posix(),
            "status": "passed" if proc.returncode == 0 else "failed",
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
        if proc.returncode != 0:
            item["detail"] = detail or f"exit={proc.returncode}"
        cases.append(item)
        if proc.stdout:
            print(proc.stdout, end="" if proc.stdout.endswith("\n") else "\n", flush=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps({"cases": cases}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if cases and all(item["status"] == "passed" for item in cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
