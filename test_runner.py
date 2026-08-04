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
from datetime import datetime
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
        "skipped": 0,
        "duration_ms": 0,
        "failures": [],
        "commands": [],
        "test_cases": [],
    }


def failure(name: str, detail: str) -> dict[str, str]:
    return {"name": name, "detail": (detail or "test command failed").strip()[:1000]}


def test_case(
    name: str,
    status: str,
    duration_ms: float | int | None = None,
    detail: str = "",
    source_file: str = "",
) -> dict[str, Any]:
    item: dict[str, Any] = {"name": name, "status": status, "duration_ms": duration_ms}
    if detail:
        item["detail"] = detail.strip()[:1000]
    if source_file:
        item["file"] = source_file
    return item


def failure_cases(cases: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        failure(str(item.get("name") or "test case"), str(item.get("detail") or item.get("status") or "test failed"))
        for item in cases
        if item.get("status") in {"failed", "error"}
    ]


def _go_test_file_map(repo_dir: Path, packages: set[str]) -> dict[tuple[str, str], str]:
    modules: list[tuple[str, Path]] = []
    candidates = {repo_dir / "go.mod", *repo_dir.glob("*/go.mod"), *repo_dir.glob("*/*/go.mod")}
    for go_mod in candidates:
        if not go_mod.is_file():
            continue
        match = re.search(r"^module\s+(\S+)", go_mod.read_text(encoding="utf-8", errors="replace"), re.MULTILINE)
        if match:
            modules.append((match.group(1), go_mod.parent))

    mapping: dict[tuple[str, str], str] = {}
    for package in packages:
        matches = [(module, root) for module, root in modules if package == module or package.startswith(module + "/")]
        if not matches:
            continue
        module, module_root = max(matches, key=lambda item: len(item[0]))
        suffix = package[len(module) :].lstrip("/")
        package_dir = module_root / Path(suffix)
        for test_file in sorted(package_dir.glob("*_test.go")):
            content = test_file.read_text(encoding="utf-8", errors="replace")
            relative = test_file.relative_to(repo_dir).as_posix()
            for match in re.finditer(r"^\s*func\s+(Test[A-Za-z0-9_]+)\s*\(", content, re.MULTILINE):
                mapping[(package, match.group(1))] = relative
    return mapping


def _go_case_detail(raw: str, status: str) -> str:
    if status != "skipped":
        return raw
    reasons = []
    for line in raw.splitlines():
        clean = line.strip()
        if not clean or clean.startswith(("=== RUN", "--- SKIP")):
            continue
        clean = re.sub(r"^[^:]+_test\.go:\d+:\s*", "", clean)
        reasons.append(clean)
    return reasons[-1] if reasons else "test skipped by source test"


def parse_go_json(
    path: Path, command_name: str, output: str, repo_dir: Path
) -> tuple[int, int, int, list[dict[str, str]], list[dict[str, Any]]]:
    states: dict[tuple[str, str], str] = {}
    details: dict[tuple[str, str], str] = {}
    durations: dict[tuple[str, str], int | None] = {}
    started: dict[tuple[str, str], datetime] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            test = event.get("Test")
            package = event.get("Package") or "package"
            action = event.get("Action")
            key = (package, test) if test else None
            event_time = None
            if event.get("Time"):
                try:
                    event_time = datetime.fromisoformat(str(event["Time"]).replace("Z", "+00:00"))
                except ValueError:
                    pass
            if key and action == "run" and event_time:
                started[key] = event_time
            if key and event.get("Output"):
                details[key] = (details.get(key, "") + str(event["Output"]))[-2000:]
            if test and action in {"pass", "fail", "skip"}:
                key = (package, test)
                states[key] = {"pass": "passed", "fail": "failed", "skip": "skipped"}[action]
                elapsed = event.get("Elapsed")
                durations[key] = round(float(elapsed) * 1000, 3) if isinstance(elapsed, (int, float)) else None
                if event_time and started.get(key):
                    durations[key] = max(0, round((event_time - started[key]).total_seconds() * 1000, 3))
    parent_keys: set[tuple[str, str]] = set()
    for package, test in states:
        parts = test.split("/")
        for index in range(1, len(parts)):
            parent_keys.add((package, "/".join(parts[:index])))
    source_files = _go_test_file_map(repo_dir, {package for package, _test in states})
    cases = []
    for (package, test), state in states.items():
        if (package, test) in parent_keys:
            continue
        raw_detail = details.get((package, test), output if state == "failed" else "")
        case_detail = "" if state == "passed" else _go_case_detail(raw_detail, state)
        cases.append(
            test_case(
                test,
                state,
                durations.get((package, test)),
                case_detail,
                source_files.get((package, test.split("/", 1)[0]), ""),
            )
        )
    if not cases and output:
        cases = [test_case(command_name, "failed", None, output)]
    return len(cases), sum(item["status"] == "passed" for item in cases), sum(item["status"] == "failed" for item in cases), failure_cases(cases), cases


def _python_case_location(repo_dir: Path, classname: str) -> tuple[str, str]:
    parts = [part for part in classname.split(".") if part]
    for size in range(len(parts), 0, -1):
        candidate = repo_dir.joinpath(*parts[:size]).with_suffix(".py")
        if candidate.is_file():
            return candidate.relative_to(repo_dir).as_posix(), ".".join(parts[size:])
    return "", classname


def parse_junit(
    path: Path, command_name: str, output: str, repo_dir: Path
) -> tuple[int, int, int, list[dict[str, str]], list[dict[str, Any]]]:
    if not path.is_file():
        cases = [test_case(command_name, "error", None, output or "JUnit report was not produced")]
        return 1, 0, 0, failure_cases(cases), cases
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        cases = [test_case(command_name, "error", None, f"invalid JUnit XML: {exc}")]
        return 1, 0, 0, failure_cases(cases), cases
    cases = root.findall(".//testcase")
    parsed: list[dict[str, Any]] = []
    for case in cases:
        source_file, class_name = _python_case_location(repo_dir, case.get("classname") or "")
        label = ".".join(part for part in (class_name, case.get("name")) if part) or command_name
        try:
            duration_ms = round(float(case.get("time") or 0) * 1000, 3)
        except ValueError:
            duration_ms = None
        problem = case.find("failure")
        status = "failed" if problem is not None else "passed"
        if problem is None:
            problem = case.find("error")
            if problem is not None:
                status = "error"
        skipped = case.find("skipped")
        if problem is None and skipped is not None:
            status = "skipped"
        detail = (problem.text or problem.get("message") or "test failed") if problem is not None else ""
        if skipped is not None:
            detail = skipped.get("message") or skipped.text or "test skipped by source test"
        parsed.append(test_case(label, status, duration_ms, detail, source_file))
    return len(parsed), sum(item["status"] == "passed" for item in parsed), sum(item["status"] == "failed" for item in parsed), failure_cases(parsed), parsed


def parse_unittest(
    command_name: str, output: str
) -> tuple[int, int, int, list[dict[str, str]], list[dict[str, Any]]]:
    match = re.search(r"Ran\s+(\d+)\s+tests?", output)
    total = int(match.group(1)) if match else 1
    failures = []
    for match in re.finditer(r"^(?:FAIL|ERROR):\s+(.+)$", output, flags=re.MULTILINE):
        failures.append(failure(match.group(1), "unittest failure; see job log"))
    if not failures and "FAILED" in output:
        failures.append(failure(command_name, output))
    cases = [test_case(command_name, "failed" if failures else "passed", None, failures[0]["detail"] if failures else "")]
    return total, max(0, total - len(failures)), len(failures), failures, cases


def parse_shell(
    command_name: str, code: int, output: str, duration_ms: int
) -> tuple[int, int, int, list[dict[str, str]], list[dict[str, Any]]]:
    cases = [test_case(command_name, "passed" if code == 0 else "failed", duration_ms, "" if code == 0 else output)]
    return 1, int(code == 0), int(code != 0), failure_cases(cases), cases


def parse_case_json(
    path: Path, command_name: str, output: str
) -> tuple[int, int, int, list[dict[str, str]], list[dict[str, Any]]]:
    if not path.is_file():
        cases = [test_case(command_name, "error", None, output or "test case report was not produced")]
        return 1, 0, 0, failure_cases(cases), cases
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        cases = [item for item in data.get("cases", []) if isinstance(item, dict)]
    except (OSError, json.JSONDecodeError) as exc:
        cases = [test_case(command_name, "error", None, f"invalid test case report: {exc}")]
    if not cases:
        cases = [test_case(command_name, "error", None, "test case report has no cases")]
    for item in cases:
        item.setdefault("name", command_name)
        item.setdefault("status", "error")
        item.setdefault("duration_ms", None)
    return len(cases), sum(item["status"] == "passed" for item in cases), sum(item["status"] == "failed" for item in cases), failure_cases(cases), cases


def run_command(command: dict[str, Any], repo_dir: Path, report_dir: Path) -> tuple[int, str, int]:
    env = os.environ.copy()
    env.update(
        {
            "REPO_DIR": str(repo_dir),
            "REPORT_DIR": str(report_dir),
            "TEST_RUNNER_ROOT": str(Path(__file__).resolve().parent),
        }
    )
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
        print("@@TEST_STEP@@ " + name, flush=True)
        code, output, elapsed = run_command(command, args.repo, args.report_dir)
        parser_name = command.get("parser") or "shell"
        if parser_name == "go-json":
            total, passed, failed, failures, cases = parse_go_json(args.report_dir / command["report"], name, output, args.repo)
        elif parser_name == "junit":
            total, passed, failed, failures, cases = parse_junit(args.report_dir / command["report"], name, output, args.repo)
        elif parser_name == "case-json":
            total, passed, failed, failures, cases = parse_case_json(args.report_dir / command["report"], name, output)
        elif parser_name == "unittest":
            total, passed, failed, failures, cases = parse_unittest(name, output)
        else:
            total, passed, failed, failures, cases = parse_shell(name, code, output, elapsed)
        if code != 0 and not failures:
            cases = [test_case(name, "failed", elapsed, output or f"exit={code}")]
            failures = failure_cases(cases)
            total, passed, failed = 1, 0, 1
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
        summary["failed"] += failed
        summary["errors"] += sum(item.get("status") == "error" for item in cases)
        summary["skipped"] += sum(item.get("status") == "skipped" for item in cases)
        summary["commands"].append({"name": name, "exit_code": code, "duration_ms": elapsed, "total": total})
        summary["failures"].extend(failures)
        for case in cases:
            case["command"] = name
        summary["test_cases"].extend(cases)
    summary["duration_ms"] = int((time.monotonic() - started) * 1000)
    (args.report_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("@@TEST_SUMMARY@@ " + json.dumps(summary, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    main()
