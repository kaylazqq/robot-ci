---
name: robot-ci-run-pipeline
description: Run an existing Robot CI pipeline that administrators explicitly enabled for AI API access, pin the run to an exact remote branch commit, wait for completion, and report the result. Use when a developer asks Codex to deploy or test a pushed branch through one of the fixed internal Robot CI pipelines; do not use it to create, edit, approve, or administer pipelines.
---

# Run Robot CI Pipeline

Use `scripts/robot_ci_pipeline.py` for every API call. Never print the configured token or place it in a command argument, log, or response.

## Prerequisites

Use the downloaded `config/robot-ci.json` by default. Allow `ROBOT_CI_URL` and `ROBOT_CI_TOKEN` environment variables to override it. Before starting a run, push the requested branch and obtain its full 40-character commit SHA from the remote repository. Do not submit a local-only SHA.

## Workflow

1. Run `python scripts/robot_ci_pipeline.py list` to list authorized pipelines.
2. Match the requested service and environment to exactly one returned `pipeline_id`. Stop if no authorized pipeline matches or the request is ambiguous.
3. Run `python scripts/robot_ci_pipeline.py run --pipeline-id <id> --branch <branch> --commit-sha <sha> --reason <reason>`.
4. Report pipeline id, branch, commit SHA, run id, final status, and a concise failure reason. Do not claim success unless the returned status is `success`.

The server owns build, registry, environment, deployment mode, and approval policy. Never attempt to override those settings.

