"""Bridge existing Robot CI builds to the installed real dev-gamma E2E driver."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tarfile
import sqlite3
import time
import os
from contextlib import closing

ROOT = Path('/var/lib/pr-e2e/build-inbox')
DRIVER = Path('/opt/pr-pipeline-ci/gamma_acceptance.py')
ENVIRONMENT = 'a5932430eb2f'
ROBOT_ROOT = Path(__file__).resolve().parent


def environment_config(environment):
    with closing(sqlite3.connect((ROBOT_ROOT / 'data/robot-ci.db').as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        selected = db.execute('SELECT * FROM environments WHERE id=?', (environment,)).fetchone()
        anchor = db.execute('SELECT * FROM environments WHERE id=?', (ENVIRONMENT,)).fetchone()
    if not selected or not anchor:
        raise ValueError('Robot CI environment was not found')
    # IDs are per module; authorize the existing cluster connection, not its display name.
    if selected['jump_host'] != anchor['jump_host'] or sorted(json.loads(selected['nodes_json'])) != sorted(json.loads(anchor['nodes_json'])):
        raise ValueError('Selected environment is not the configured dev-gamma cluster')
    if not selected['workload_name'] or not selected['service_id']:
        raise ValueError('Environment has no service/workload mapping')
    return dict(selected)


def owns_environment(environment):
    try:
        environment_config(environment)
        return True
    except (ValueError, OSError, sqlite3.Error, TypeError):
        return False


def available(environment):
    return owns_environment(environment) and DRIVER.is_file()


def validate_selection(environment, service_ids):
    env = environment_config(environment)
    if service_ids != [env['service_id']]:
        raise ValueError('Select the environment belonging to the built module')
    return env


def prepare(job_id, results, opts):
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', job_id) or len(results) != 1:
        raise ValueError('One completed service build required')
    row = results[0]
    validate_selection(opts['environment_id'], [row.get('service_id')])
    catalog = json.loads((ROBOT_ROOT / 'services.json').read_text())
    service = next((s for s in catalog if s['id'] == row.get('service_id')), None)
    if not row.get('ok') or not service:
        raise ValueError('A successful build from the Robot CI service catalog is required')
    archive = Path(row.get('archive', '')).resolve()
    if not archive.is_relative_to(Path('/usr/share/nginx/html/images')) or not archive.is_file():
        raise ValueError('Owned build archive required')
    with tarfile.open(archive) as bundle:
        entries = json.load(bundle.extractfile('manifest.json'))
        if len(entries) != 1:
            raise ValueError('Expected one image in archive')
        image_id = 'sha256:' + hashlib.file_digest(bundle.extractfile(entries[0]['Config']), 'sha256').hexdigest()
    source_job = row.get('job_id') or job_id
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', source_job):
        raise ValueError('Invalid source build ID')
    remote = row['remote']
    if not re.fullmatch(r'swr\.cn-southwest-2\.myhuaweicloud\.com/public_ai/' + re.escape(service['image']) + r':[A-Za-z0-9_.-]+', remote):
        raise ValueError('Expected the configured service SWR image')
    tag = remote.rsplit(':', 1)[-1]
    logs = (Path(__file__).resolve().parent / 'logs' / ('job-' + source_job + '.log')).read_text()
    digests = re.findall(re.escape(tag) + r': digest: (sha256:[0-9a-f]{64})', logs)
    if not digests or len(set(digests)) != 1:
        raise ValueError('Unique successful SWR push digest required')
    pinned = remote.rsplit(':', 1)[0] + '@' + digests[0]
    result = subprocess.run(['docker', 'manifest', 'inspect', pinned], capture_output=True, text=True, timeout=90)
    if result.returncode or json.loads(result.stdout).get('config', {}).get('digest') != image_id:
        raise ValueError('SWR manifest does not match the archived build image')
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    pinned_archive = ROOT / (job_id + '.tar')
    if not pinned_archive.exists():
        os.link(archive, pinned_archive)
    manifest = ROOT / (job_id + '.json')
    body = {'build_id': job_id, 'environment_id': opts['environment_id'], 'result': row,
            'pinned_image': pinned, 'image_id': image_id, 'deploy': bool(opts.get('gamma_deploy'))}
    from gamma_e2e import options
    body['suite_ids'] = options(opts)['gamma_suites'] if opts.get('gamma_test') else []
    if manifest.exists() and json.loads(manifest.read_text()) != body:
        raise ValueError('Immutable build handoff already exists with different inputs')
    manifest.write_text(json.dumps(body))
    manifest.chmod(0o600)
    return manifest


def run(job_id, results, opts, log, progress, cancelled):
    if not available(opts.get('environment_id')):
        return False, 'Real Gamma environment is not configured'
    manifest = prepare(job_id, results, opts)
    from gamma_e2e import request
    frozen = json.loads(manifest.read_text())
    submitted = request('/internal/gamma', {'submission_id': 'robot-' + job_id,
        'environment_id': opts['environment_id'], 'mode': 'test' if opts.get('gamma_test') else 'deploy',
        'suite_ids': frozen['suite_ids'], 'build_manifest': str(manifest),
        'build_manifest_sha256': hashlib.sha256(manifest.read_bytes()).hexdigest(), 'requested_by': 'robot-ci:' + job_id})
    run_id = submitted['id']
    record = {'id': run_id, 'url': 'http://119.8.233.58/pipeline/runs/' + run_id, 'state': 'queued'}
    log('Gamma Pipeline: ' + record['url'])
    progress(record)
    stop_requested = False
    last = None
    while True:
        if cancelled() and not stop_requested:
            stop_requested = True
            log('构建停止请求已记录；Gamma 环境任务保留，等待安全完成或恢复检查')
        result = request('/api/runs/' + run_id)
        state = (result.get('status'), result.get('summary'), result.get('queue_position'))
        if state != last:
            log('Gamma ' + json.dumps(state, ensure_ascii=False))
            progress({**record, 'state': state[0], 'summary': state[1], 'queue_position': state[2],
                      'stages': result.get('stages', [])})
            last = state
        if result.get('status') in ('completed', 'interrupted', 'blocked'):
            break
        time.sleep(5)
    ok = result.get('conclusion') == 'success' and result.get('cleanup', {}).get('status') == 'passed' and not result.get('stale') and not stop_requested
    progress({**record, 'state': 'completed', 'conclusion': result.get('conclusion'), 'summary': result.get('summary'),
              'stages': result.get('stages', []), 'rollback': result.get('rollback')})
    return ok, '' if ok else ('Cancelled after safe driver completion' if stop_requested else result.get('summary', 'Gamma failed')) + ': ' + record['url']
