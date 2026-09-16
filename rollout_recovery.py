"""Historical release recovery, scoped to a recorded workload family."""
from __future__ import annotations

import base64
import hashlib
import json
import shlex
import threading
import time
from contextlib import contextmanager
from copy import deepcopy

_locks = {}
_locks_guard = threading.Lock()


@contextmanager
def environment_lock(environment_id):
    with _locks_guard:
        lock = _locks.setdefault(environment_id, threading.RLock())
    with lock:
        yield


def pending_operation(records):
    for record in records:
        operation = json.loads(record.get("rollback_operation_json") or "{}")
        if operation.get("phase") in {"restoring", "cleaning", "failed"}:
            return record, operation
    return None


def manage(s, rollout_id, action, *args):
    record = s.get_parallel_rollout(rollout_id)
    if not record:
        return None, "平滑发布记录不存在"
    with environment_lock(record["environment_id"]):
        record = s.get_parallel_rollout(rollout_id)
        if record["status"] != "active":
            return None, "本次发布已结束，不能再下线或调整实例数"
        if pending_operation(s.list_parallel_rollouts(record["environment_id"])):
            return None, "环境回滚尚未完成，请先重试回滚"
        s.ensure_action_job(record["job_id"])
        result = action(rollout_id, *args)
        s.invalidate_rollout_live_cache()
        return result


def _family(records, record):
    names = {record["source_workload"], record["candidate_workload"]}
    related = []
    while True:
        matches = [r for r in records if r["service_id"] == record["service_id"]
                   and names.intersection({r["source_workload"], r["candidate_workload"]})]
        expanded = names | {r[key] for r in matches for key in ("source_workload", "candidate_workload")}
        related = matches
        if expanded == names:
            return related, names
        names = expanded


def _manifest(payload, namespace):
    payload = deepcopy(payload)
    payload.pop("status", None)
    metadata = payload["metadata"]
    for key in ("resourceVersion", "uid", "creationTimestamp", "generation", "managedFields", "deletionTimestamp", "deletionGracePeriodSeconds"):
        metadata.pop(key, None)
    metadata.get("annotations", {}).pop("kubectl.kubernetes.io/last-applied-configuration", None)
    if metadata.get("namespace", namespace) != namespace:
        raise ValueError("恢复快照的命名空间与发布环境不一致")
    metadata["namespace"] = namespace
    pod_spec = ((payload.get("spec") or {}).get("template") or {}).get("spec") or {}
    pod_spec.pop("nodeName", None)
    # A zero-scaled historical workload must become ready before traffic is removed.
    payload["spec"]["replicas"] = max(1, int(payload["spec"].get("replicas", 1)))
    return payload


def _identity(payload):
    return {"uid": payload.get("metadata", {}).get("uid"), "spec": payload.get("spec")}


def _plan(s, record):
    if str(record.get("status") or "") == "rolled_back":
        raise ValueError("本次发布已经回滚，不能重复执行一键回滚")
    records = s.list_parallel_rollouts(record["environment_id"])
    pending = pending_operation(records)
    if pending:
        owner, operation = pending
        if owner["id"] != record["id"]:
            raise ValueError("环境存在其他未完成回滚，请先重试该回滚")
        return operation
    env, remote, namespace = s._parallel_rollout_remote(record)
    related, names = _family(records, record)
    current = env.get("active_workload_name") or env.get("workload_name")
    if current and current not in names:
        raise ValueError("环境当前负载不属于这条发布版本链，不能自动清理")
    live = {}
    for name in sorted(names):
        payload = s.cce_rollout.get_deployment_payload(remote, namespace=namespace, deploy=name)
        if payload is not None:
            live[name] = payload
    target = record["source_workload"]
    snapshot = json.loads(record.get("old_manifest_json") or "null") or live.get(target)
    if not snapshot:
        raise ValueError("历史版本缺少恢复快照，且负载已不存在，无法回滚")
    manifest = _manifest(snapshot, namespace)
    if manifest["metadata"]["name"] != target:
        raise ValueError("恢复快照的负载名称与发布记录不一致")
    # Refuse to overwrite a different version that happens to reuse the name.
    if target in live:
        actual = _manifest(live[target], namespace)
        if actual["spec"].get("template") != manifest["spec"].get("template"):
            raise ValueError("目标负载已被修改，不能覆盖同名的不同版本")
    plan = {
        "target": target, "manifest": manifest, "namespace": namespace,
        "delete": sorted(set(live) - {target}),
        "observed": {name: _identity(payload) for name, payload in live.items()},
        "affected": [r["id"] for r in related if r["status"] == "active" and r["id"] != record["id"]],
        "record_ids": sorted(r["id"] for r in related),
        "snapshots": {r["id"]: live[r["source_workload"]] for r in related
                      if not r.get("old_manifest_json") and r["source_workload"] in live},
    }
    fingerprint = {k: v for k, v in plan.items() if k != "snapshots"}
    plan["token"] = hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest()
    return plan


def preview(s, rollout_id):
    record = s.get_parallel_rollout(rollout_id)
    if not record:
        raise ValueError("平滑发布记录不存在")
    with environment_lock(record["environment_id"]):
        plan = _plan(s, record)
        return {key: plan[key] for key in ("target", "delete", "affected", "token")}


def _save(s, rollout_id, plan):
    with s._db_lock:
        conn = s._connect_db()
        try:
            conn.execute("UPDATE parallel_rollouts SET rollback_operation_json=? WHERE id=?",
                         (json.dumps(plan, ensure_ascii=False), rollout_id))
            conn.commit()
        finally:
            conn.close()


def finish_superseded(s, affected):
    job_id = affected["job_id"]
    job = s.ensure_action_job(job_id)
    interrupted_wait = job.get("error") == s.INTERRUPTED_JOB_ERROR and job.get("stage_before_stop") == "release"
    if not job.get("production_released") or job.get("cancel_requested") or not (job.get("stage") == "release" or interrupted_wait):
        return
    own = [r for r in s.list_parallel_rollouts(affected["environment_id"]) if r["job_id"] == job_id]
    if own and all(r["status"] in {"superseded", "old_deleted", "rolled_back"} for r in own):
        origin = s.get_parallel_rollout(affected["superseded_by"]) or {}
        message = f"已由历史回滚结束：流水线 {origin.get('job_id', '')} 恢复 {origin.get('source_workload', '')}，本次发布负载已清理"
        s.append_job_step_log(job_id, "release", "offline", message)
        s.set_job(job_id, status="ok", stage="done", current="", error=None)


def reconcile_completed(s):
    """Close the crash window between the DB commit and job metadata update."""
    with s._db_lock:
        conn = s._connect_db()
        try:
            records = [dict(row) for row in conn.execute("SELECT * FROM parallel_rollouts WHERE status='superseded'")]
        finally:
            conn.close()
    for record in records:
        finish_superseded(s, record)


def execute(s, rollout_id, plan_token=""):
    record = s.get_parallel_rollout(rollout_id)
    if not record:
        return None, "平滑发布记录不存在"
    with environment_lock(record["environment_id"]):
        plan = None
        started = False
        try:
            record = s.get_parallel_rollout(rollout_id)
            plan = _plan(s, record)
            if plan_token and plan_token != plan["token"]:
                raise ValueError("环境版本已变化，请重新预览并确认回滚")
            _, remote, namespace = s._parallel_rollout_remote(record)
            existing = s.cce_rollout.get_deployment_payload(remote, namespace=namespace, deploy=plan["target"])
            if existing and _manifest(existing, namespace)["spec"].get("template") != plan["manifest"]["spec"].get("template"):
                raise ValueError("目标负载已被修改，不能覆盖同名的不同版本")
            # Persist legacy snapshots before deleting anything, including V2
            # from a still-running V2 -> V3 release.
            with s._db_lock:
                conn = s._connect_db()
                try:
                    for rid, payload in plan["snapshots"].items():
                        conn.execute("UPDATE parallel_rollouts SET old_manifest_json=? WHERE id=? AND old_manifest_json=''",
                                     (json.dumps(payload, ensure_ascii=False), rid))
                    conn.commit()
                finally:
                    conn.close()
            plan["phase"] = "restoring"
            _save(s, rollout_id, plan)
            started = True
            encoded = base64.b64encode(json.dumps(plan["manifest"]).encode()).decode()
            code, out, err = remote(f"printf %s {shlex.quote(encoded)} | base64 -d | kubectl -n {shlex.quote(namespace)} apply -f -", 120)
            if code:
                raise ValueError(err or out or "恢复版本失败")
            timeout = str(s.CFG.get("cce_rollout_timeout") or "180s")
            code, out, err = remote(f"kubectl -n {shlex.quote(namespace)} rollout status deploy/{shlex.quote(plan['target'])} --timeout={shlex.quote(timeout)}", s.cce_rollout.rollout_timeout_seconds(timeout) + 60)
            if code:
                raise ValueError(err or out or "恢复版本尚未就绪，未清理其他版本")
            target = s.cce_rollout.get_deployment_payload(remote, namespace=namespace, deploy=plan["target"])
            if not target or int(target.get("status", {}).get("readyReplicas") or 0) < 1:
                raise ValueError("恢复版本尚未就绪，未清理其他版本")
            plan["phase"] = "cleaning"
            _save(s, rollout_id, plan)
            for name in plan["delete"]:
                payload = s.cce_rollout.get_deployment_payload(remote, namespace=namespace, deploy=name)
                if payload is None:
                    continue
                if _identity(payload) != plan["observed"][name]:
                    raise ValueError(f"负载 {name} 已变化，停止清理")
                s.cce_rollout.delete_deployment(remote, namespace=namespace, deploy=name)
            now = time.strftime("%Y-%m-%d %H:%M:%S")
            plan["phase"] = "completed"
            plan.pop("error", None)
            with s._db_lock:
                conn = s._connect_db()
                try:
                    conn.execute("UPDATE environments SET active_workload_name=?, updated_at=? WHERE id=?", (plan["target"], now, record["environment_id"]))
                    conn.execute("UPDATE parallel_rollouts SET status='rolled_back', rolled_back_at=?, rollback_operation_json=? WHERE id=?", (now, json.dumps(plan, ensure_ascii=False), rollout_id))
                    for rid in plan["affected"]:
                        conn.execute("UPDATE parallel_rollouts SET status='superseded', superseded_by=? WHERE id=?", (rollout_id, rid))
                    conn.commit()
                finally:
                    conn.close()
            for rid in plan["affected"]:
                finish_superseded(s, s.get_parallel_rollout(rid))
            s.invalidate_rollout_live_cache()
            # Completion must not depend on a subsequent remote read succeeding.
            return s.get_parallel_rollout(rollout_id), ""
        except Exception as exc:
            if started and plan is not None and plan.get("phase") != "completed":
                plan.update(phase="failed", error=str(exc))
                _save(s, rollout_id, plan)
            s.invalidate_rollout_live_cache()
            return None, str(exc)
