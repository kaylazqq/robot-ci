"""Robot CI's server-side bridge to the single Pipeline Hub execution queue."""
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import subprocess
import shutil
import tarfile
import time
from urllib.request import Request, urlopen, build_opener, ProxyHandler

TARGET = 'ci-e2e'
SUITES = ('E01', 'E02', 'E03', 'E04', 'E05', 'E06')


def options(raw):
    selected=raw.get('gamma_suites',['E01','E02','E03'])
    if not isinstance(selected,list) or not selected or len(set(selected))!=len(selected) or any(s not in SUITES for s in selected):
        raise ValueError('Gamma E2E requires a nonempty E01-E06 set')
    return {'gamma_suites':selected,'gamma_baseline':bool(raw.get('gamma_baseline',True))}


def request(route, payload=None):
    token=Path(os.environ.get('GAMMA_E2E_TOKEN_FILE','/etc/pr-e2e/secrets/worker-token')).read_text().strip()
    data=json.dumps(payload).encode() if payload is not None else None
    req=Request('http://127.0.0.1:8792'+route,data=data,
                headers={'X-Worker-Token':token,'Content-Type':'application/json'})
    with build_opener(ProxyHandler({})).open(req,timeout=30) as response:
        return json.load(response)


def prepare(job_id, results, opts, log):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,120}',job_id):raise ValueError('Invalid build ID')
    baseline=json.loads(Path(os.environ.get('GAMMA_E2E_BASELINE_FILE','/etc/pr-e2e/artifact-baseline.json')).read_text())
    root=Path('/var/lib/pr-e2e/artifact-inbox')
    directory=root/job_id
    account=pwd.getpwnam('pr-e2e')
    directory.mkdir(parents=True,exist_ok=True,mode=0o750)
    os.chown(directory,0,account.pw_gid)
    saved=directory/'manifest.json'
    if saved.exists():
        manifest=json.loads(saved.read_text())
        if manifest['suite_ids']!=opts['gamma_suites'] or manifest['baseline_enabled']!=opts['gamma_baseline']:
            raise ValueError('Existing build handoff options changed')
        return manifest
    candidates={}
    for result in results:
        sid=result['service_id']
        catalog=json.loads((Path(__file__).resolve().parent/'services.json').read_text())
        repository=next((s['repo'] for s in catalog if s['id']==sid),None)
        service=next((s for s,m in baseline.items() if m['repo']==repository),None)
        if not service or not result.get('ok') or not re.fullmatch(r'[0-9a-f]{40}',result.get('commit_sha','')):
            raise ValueError('Build result missing a supported service or full SHA: '+sid)
        reference=result.get('remote')
        if not reference or reference.startswith('-'):raise ValueError('Successful pushed image required')
        source=Path(result.get('archive','')).resolve()
        archive_root=Path(os.environ.get('GAMMA_E2E_ARCHIVE_ROOT','/usr/share/nginx/html/images')).resolve()
        if not source.is_relative_to(archive_root) or not source.is_file():raise ValueError('Owned build archive required')
        archive=directory/(service+'.tar')
        shutil.copyfile(source,archive)
        with tarfile.open(archive) as bundle:
            entries=json.load(bundle.extractfile('manifest.json'))
            if len(entries)!=1:raise ValueError('Expected one built image in archive')
            stream=bundle.extractfile(entries[0]['Config'])
            image_id='sha256:'+hashlib.file_digest(stream,'sha256').hexdigest()
        if image_id!=result.get('image_id'):raise ValueError('Archive does not match captured build image ID')
        log('Gamma image archive: '+sid+' '+image_id)
        archive.chmod(0o640);os.chown(archive,0,account.pw_gid)
        with archive.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
        candidates[service]={'repo':baseline[service]['repo'],'source_sha':result['commit_sha'],
            'branch':result.get('branch',''),'image_id':image_id,'registry_reference':reference,
            'archive_name':archive.name,'archive_sha256':digest}
    manifest={'schema_version':1,'build_id':job_id,'target':'ci-compose',
              'suite_ids':opts['gamma_suites'],'baseline_enabled':opts['gamma_baseline'],
              'baseline_images':baseline,'candidate_images':candidates}
    temporary=saved.with_suffix('.tmp')
    temporary.write_text(json.dumps(manifest,indent=2));temporary.chmod(0o640)
    os.chown(temporary,0,account.pw_gid);os.replace(temporary,saved)
    return manifest


def run(job_id, results, opts, log, progress, cancelled):
    manifest=prepare(job_id,results,opts,log)
    receipt=request('/internal/artifact-batches',manifest)
    run_id=receipt['id']
    url=os.environ.get('GAMMA_E2E_PUBLIC_BASE','http://119.8.233.58/pipeline').rstrip('/')+receipt['web_path']
    log('Gamma Pipeline: '+url)
    progress({'id':run_id,'url':url,'state':'queued','build_status':'success'})
    deadline=time.monotonic()+6*3600
    previous=None
    errors=0
    while time.monotonic()<deadline:
        if cancelled():
            progress({'id':run_id,'url':url,'state':'detached','error':'Build stopped; queued E2E evidence retained'})
            return False,'构建已停止；独立 E2E 记录保留：'+url
        try:
            value=request('/api/batches/'+run_id)
            errors=0
        except Exception:
            errors+=1
            if errors>=12:raise RuntimeError('Gamma status unavailable; retained record: '+url)
            time.sleep(10);continue
        state=value.get('status')
        active=next((s['name'] for s in value.get('stages',[]) if s['status']=='running'),'')
        marker=(state,active)
        if marker!=previous:
            progress({'id':run_id,'url':url,'state':state,'stage':active,'summary':value.get('summary','')})
            log('Gamma '+str(state)+' '+active);previous=marker
        if state in ('completed','blocked','interrupted'):
            ok=state=='completed' and value.get('conclusion')=='success' and not value.get('stale')
            progress({'id':run_id,'url':url,'state':state,'conclusion':value.get('conclusion'),
                      'summary':value.get('summary',''),'build_status':'success'})
            return ok,'' if ok else value.get('summary') or 'Gamma E2E 未通过：'+url
        time.sleep(5)
    return False,'Gamma 等待超时；执行记录保留：'+url
