# dev-gamma P0 queue integration

Status: implementation branch, not production acceptance. The Hub control plane
and Worker must support `/internal/gamma` before deploying this bridge.

## Execution contract

- dev-gamma deployment and browser tests enter the existing Hub queue, including
  deployment-only requests. Other environments retain their existing behavior.
- The bridge releases the Robot CI build slot before waiting for Gamma. A failed
  or unavailable Gamma driver cannot fall back to a direct cluster deployment.
- The handoff freezes the pushed SWR digest and archive image ID. The owned
  build-inbox hard link protects an archive while its Gamma task is queued.
- The response links to the same-site `/pipeline/runs/{id}`. Completion requires
  a successful test/deployment result, successful cleanup and a non-stale version.
- Stopping the Robot CI wait does not cancel an already submitted Hub task.
  No second task is submitted automatically after a restart.

## Source freezing

Set `freeze_build_inputs: true` in the existing private Robot CI configuration
after validating the deployment. The switch is off for compatibility until then.
New submissions resolve the selected branches and shared public-service branch
to full SHAs. Fetch or SHA verification failures stop the build; they do not fall
back to the default branch. Checkouts are separated by build ID under
`workspace_root/frozen-inputs/` and frozen inputs are persisted in job metadata.

Existing running builds must finish before enabling this switch. Versioned source
directories and pinned handoffs require ownership-aware retention; do not apply
generic shared-workspace deletion to pending Gamma tasks.

## Rollout gates

1. Back up application configuration and both queue/account databases.
2. Validate Hub leases, recovery and cleanup with disposable fault tests.
3. Reconcile active builds and the main/preview Robot CI instances. Preserve
   preview-only Gamma functions; do not replace its complete server.py blindly.
4. Deploy compatible Hub, Worker, then both Robot CI queue integrations.
5. Validate one diagnostic round and cleanup before starting a frozen 30-round job.

The current dev-gamma test identity has `system_user` permissions and
`EnableUserDeactivation=false`. Account cleanup needs an explicitly configured
administrator or approval to enable self-deactivation in this test environment.
This branch alone does not resolve that environment configuration or establish
30/30 stability. No PR is automatically merged.
