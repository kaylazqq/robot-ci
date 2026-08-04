#!/usr/bin/env python3
"""Run trusted service UT/DT plans and write a compact machine-readable summary."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


def empty_summary(service_id: str, plan_name: str, plan_version: int) -> dict[str, Any]:
    return {
        "service_id": service_id,
        "plan": plan_name,
        "plan_version": plan_version,
        "status": "passed",
        "total": 0,
        "passed": 0,
        "failed": 0,
        "errors": 0,
        "duration_ms": 0,
        "failures": [],
        "commands": [],
    }


def failure(name: str, detail: str) -> dict[str, str]:
    return {"name": name, "detail": (detail or "test command failed").strip()[:1000]}


def parse_go_json(path: Path, command_name: str, output: str) -> tuple[int, int, list[dict[str, str]]]:
    states: dict[tuple[str, str], str] = {}
    details: dict[tuple[str, str], str] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            test = event.get("Test")
            package = event.get("Package") or "package"
            action = event.get("Action")
            if test and action in {"pass", "fail"}:
                key = (package, test)
                states[key] = action
                if event.get("Output"):
                    details[key] = str(event["Output"])
    total = len(states)
    failed = [failure(f"{package}.{test}", details.get((package, test), output)) for (package, test), state in states.items() if state == "fail"]
    if total == 0 and output:
        return 1, 0, [failure(command_name, output)]
    return total, total - len(failed), failed


def parse_junit(path: Path, command_name: str, output: str) -> tuple[int, int, list[dict[str, str]]]:
    if not path.is_file():
        return 1, 0, [failure(command_name, output or "JUnit report was not produced")]
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        return 1, 0, [failure(command_name, f"invalid JUnit XML: {exc}")]
    cases = root.findall(".//testcase")
    failed: list[dict[str, str]] = []
    for case in cases:
        problem = case.find("failure")
        if problem is None:
            problem = case.find("error")
        if problem is not None:
            label = ".".join(part for part in (case.get("classname"), case.get("name")) if part)
            failed.append(failure(label or command_name, (problem.text or problem.get("message") or "test failed")))
    return len(cases), len(cases) - len(failed), failed


def parse_unittest(command_name: str, output: str) -> tuple[int, int, list[dict[str, str]]]:
    match = re.search(r"Ran\s+(\d+)\s+tests?", output)
    total = int(match.group(1)) if match else 1
    failures = []
    for match in re.finditer(r"^(?:FAIL|ERROR):\s+(.+)$", output, flags=re.MULTILINE):
        failures.append(failure(match.group(1), "unittest failure; see job log"))
    if not failures and "FAILED" in output:
        failures.append(failure(command_name, output))
    return total, max(0, total - len(failures)), failures


def parse_shell(command_name: str, code: int, output: str) -> tuple[int, int, list[dict[str, str]]]:
    return (1, 1, []) if code == 0 else (1, 0, [failure(command_name, output)])


def run_command(command: dict[str, Any], repo_dir: Path, report_dir: Path) -> tuple[int, str, int]:
    env = os.environ.copy()
    env.update({"REPO_DIR": str(repo_dir), "REPORT_DIR": str(report_dir)})
    started = time.monotonic()
    print(f"$ {command['command']}", flush=True)
    try:
        proc = subprocess.run(
            command["command"],
            cwd=repo_dir,
            env=env,
            shell=True,
            executable="/bin/bash",
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
            timeout=int(command.get("timeout_sec") or 1200),
        )
        output = proc.stdout or ""
        if output:
            print(output, end="" if output.endswith("\n") else "\n", flush=True)
        return proc.returncode, output, int((time.monotonic() - started) * 1000)
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        print(output, end="" if output.endswith("\n") else "\n", flush=True)
        print("ERROR: test command timed out", flush=True)
        return 124, output + "\nERROR: timeout", int((time.monotonic() - started) * 1000)
    except OSError as exc:
        print(f"ERROR: {exc}", flush=True)
        return 127, str(exc), int((time.monotonic() - started) * 1000)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plans", required=True, type=Path)
    parser.add_argument("--service", required=True)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--report-dir", required=True, type=Path)
    args = parser.parse_args()

    plans_data = json.loads(args.plans.read_text(encoding="utf-8"))
    catalog_path = args.plans.parent / "test-suites" / "catalog.json"
    catalog_services: dict[str, Any] = {}
    if catalog_path.is_file():
        catalog_data = json.loads(catalog_path.read_text(encoding="utf-8"))
        catalog_services = catalog_data.get("services") or {}
    service_map = plans_data.get("services") or {}
    profile_name = service_map.get(args.service)
    if not profile_name:
        summary = empty_summary(args.service, "not_configured", int(plans_data.get("version") or 1))
        summary["status"] = "not_configured"
        args.report_dir.mkdir(parents=True, exist_ok=True)
        (args.report_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print("TEST not configured", flush=True)
        return 0
    profile = (plans_data.get("profiles") or {}).get(profile_name)
    if not profile:
        raise SystemExit(f"missing test profile: {profile_name}")

    args.report_dir.mkdir(parents=True, exist_ok=True)
    summary = empty_summary(args.service, profile_name, int(plans_data.get("version") or 1))
    suite = catalog_services.get(args.service)
    if suite:
        # Keep the report self-describing even after the source checkout is
        # cleaned up. This is metadata only; commands remain trusted plans.
        summary["suite"] = {
            key: suite.get(key)
            for key in ("repository", "scope", "roots", "convention", "excluded")
            if key in suite
        }
    started = time.monotonic()
    for command in profile.get("commands") or []:
        name = command.get("name") or command.get("command") or "test command"
        code, output, elapsed = run_command(command, args.repo, args.report_dir)
        parser_name = command.get("parser") or "shell"
        if parser_name == "go-json":
            total, passed, failures = parse_go_json(args.report_dir / command["report"], name, output)
        elif parser_name == "junit":
            total, passed, failures = parse_junit(args.report_dir / command["report"], name, output)
        elif parser_name == "unittest":
            total, passed, failures = parse_unittest(name, output)
        else:
            total, passed, failures = parse_shell(name, code, output)
        if code != 0 and not failures:
            failures = [failure(name, output or f"exit={code}")]
            total = max(total, 1)
            passed = max(0, total - 1)
        # A well-formed report is authoritative too: some wrappers collect
        # failures and still return zero.  Do not present those as passed.
        if failures and summary["status"] == "passed":
            summary["status"] = "failed"
        if code == 124:
            summary["status"] = "timeout"
            summary["errors"] += 1
        elif code == 127:
            summary["status"] = "error"
            summary["errors"] += 1
        elif code != 0 and summary["status"] != "timeout":
            summary["status"] = "failed"
        summary["total"] += total
        summary["passed"] += passed
        summary["failed"] += len(failures)
        summary["commands"].append({"name": name, "exit_code": code, "duration_ms": elapsed})
        summary["failures"].extend(failures)
    summary["duration_ms"] = int((time.monotonic() - started) * 1000)
    (args.report_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("@@TEST_SUMMARY@@ " + json.dumps(summary, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    main()
