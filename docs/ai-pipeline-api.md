# AI Pipeline API

The API reuses Robot CI users and permissions. Create and revoke personal tokens through an authenticated browser session; use the returned token as `Authorization: Bearer <token>`. A new token value is returned only once.

## Endpoints

- `POST /api/ai/tokens`: create a personal token with `name` and `expires_in_days` (1-365).
- `GET /api/ai/tokens`: list the current user's token metadata; secrets are never returned.
- `POST /api/ai/tokens/{id}/revoke`: disable one personal token.
- `GET /api/ai/pipelines`: list fixed pipelines enabled for the current user.
- `POST /api/ai/pipeline-runs`: run an enabled pipeline. Requires an `Idempotency-Key` header and `pipeline_id`, `branch`, full `commit_sha`, and optional `reason`.
- `GET /api/ai/pipeline-runs/{run_id}`: read the caller's AI-triggered run state.
- `POST /api/ai/skill-packages`: from a browser session, create a user Token and download a ZIP for Codex, Claude Code, and/or Cursor. The ZIP stores the service URL and Token in `config/robot-ci.json` and includes PowerShell and shell installers.

The run API verifies the current remote branch tip equals `commit_sha`, pins checkout to that SHA, and does not accept registry, environment, deployment, or approval overrides. Pipelines enabled for AI are approval-exempt by server policy; production targets still require the caller's production-release permission.

