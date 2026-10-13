#!/usr/bin/env python3
"""List and run fixed Robot CI pipelines using the current user's API token."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def config() -> tuple[str, str]:
    base_url = os.environ.get("ROBOT_CI_URL", "").strip().rstrip("/")
    token = os.environ.get("ROBOT_CI_TOKEN", "").strip()
    if not base_url or not token:
        raise RuntimeError("ROBOT_CI_URL and ROBOT_CI_TOKEN are required")
    return base_url, token


def request(method: str, path: str, payload: dict | None = None, headers: dict | None = None) -> dict:
    base_url, token = config()
    req = Request(base_url + path, data=json.dumps(payload).encode() if payload is not None else None,
                  method=method, headers={"Authorization": "Bearer " + token,
                  "Content-Type": "application/json", **(headers or {})})
    try:
        with urlopen(req, timeout=30) as response:
            return json.load(response)
    except HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode())
        except (ValueError, UnicodeDecodeError):
            detail = {"error": str(exc)}
        raise RuntimeError(f"Robot CI HTTP {exc.code}: {detail.get('error') or detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Robot CI unavailable: {exc.reason}") from exc


def run_pipeline(args: argparse.Namespace) -> dict:
    started = request("POST", "/api/ai/pipeline-runs",
                      {"pipeline_id": args.pipeline_id, "branch": args.branch,
                       "commit_sha": args.commit_sha.lower(), "reason": args.reason},
                      {"Idempotency-Key": args.idempotency_key or str(uuid.uuid4())})
    run_id = started.get("run_id")
    if not run_id:
        raise RuntimeError("Robot CI did not return run_id")
    deadline = time.monotonic() + args.timeout
    while True:
        status = request("GET", "/api/ai/pipeline-runs/" + str(run_id))
        if status.get("status") in {"success", "failed", "stopped"}:
            return status
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Timed out waiting for Robot CI run {run_id}")
        time.sleep(args.poll_interval)


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    run = commands.add_parser("run")
    run.add_argument("--pipeline-id", required=True)
    run.add_argument("--branch", required=True)
    run.add_argument("--commit-sha", required=True)
    run.add_argument("--reason", default="Requested by developer through AI")
    run.add_argument("--idempotency-key", default="")
    run.add_argument("--timeout", type=int, default=7200)
    run.add_argument("--poll-interval", type=float, default=3.0)
    args = parser.parse_args()
    try:
        result = request("GET", "/api/ai/pipelines") if args.command == "list" else run_pipeline(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if args.command == "list" or result.get("status") == "success" else 1
    except RuntimeError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
