"""Durable job identity, result retrieval and cooperative cancellation.

Records and result manifests are public job metadata. Account credentials live
in config/private transfer contexts, never in these records or checkpoints.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import time

from . import config, pods, sync
from .worker_client import WorkerClient

FIELDS = frozenset(('id', 'mode', 'state', 'user', 'project', 'volume_id', 'hip', 'target',
    'package_dir', 'host_pod', 'pid', 'log_path', 'submitted_at', 'updated_at',
    'max_minutes', 'error', 'taskgraph_sha256', 'downloaded_files', 'outputs_count', 'local_root', 'logical_hip',
    'items_total', 'items_done', 'items_failed', 'phase'))


def download_node(target):
    """Find or add the visible delivery stage for a selected farm target."""
    candidates = [n for n in target.outputs() if n.type().name() == 'runpodfarmdownload']
    if len(candidates) > 1:
        raise ValueError('The target has multiple Download nodes; select the intended branch first')
    if candidates:
        return candidates[0]
    node = target.parent().createNode('runpodfarmdownload', 'download')
    node.setInput(0, target)
    node.moveToGoodPosition()
    return node


def pdg_output_node(top):
    seen = set()
    while top is not None and top.path() not in seen:
        seen.add(top.path())
        node = top.getPDGNode()
        if node is not None:
            return node
        top = top.outputNode()
    raise RuntimeError('The selected TOP has no PDG output node')


def validate_id(job_id):
    if not re.fullmatch('[a-f0-9]{8,32}', str(job_id)):
        raise ValueError('Invalid job id')
    return job_id


def directory():
    path = config.home() / 'jobs'
    path.mkdir(parents=True, exist_ok=True)
    return path


def write(path, record):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    clean = {k: v for k, v in record.items() if k in FIELDS}
    validate_id(clean['id'])
    clean['updated_at'] = time.time()
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(clean, ensure_ascii=False))
    os.replace(tmp, path)
    return clean


def save(record):
    return write(directory() / (validate_id(record['id']) + '.json'), record)


def load(job_id):
    return json.loads((directory() / (validate_id(job_id) + '.json')).read_text())


def list_jobs(project=None):
    out = []
    for path in directory().glob('*.json'):
        try:
            record = json.loads(path.read_text())
            validate_id(record['id'])
            if project is None or record.get('project') == project:
                out.append(record)
        except (ValueError, KeyError, OSError):
            continue
    return sorted(out, key=lambda r: r.get('submitted_at', 0), reverse=True)


def menu(node=None, farm_only=False):
    values = ['', 'Current graph']
    for job in list_jobs():
        if farm_only and job.get('mode') != 'farm':
            continue
        values.extend((job['id'], '{} · {} · {} · {}'.format(job['id'],
            job.get('project', ''), job.get('mode', ''), job.get('state', 'unknown'))))
    return values


def describe(job):
    state = job.get('state', 'unknown')
    downloaded = [p for p in job.get('downloaded_files', []) if os.path.isfile(p)]
    if downloaded:
        state += ' · {} local output(s)'.format(len(downloaded))
    text = '{} · {} · {}\nTarget: {}\nLog: {}'.format(
        job['id'], job.get('mode', ''), state, job.get('target', ''), job.get('log_path', ''))
    if job.get('items_total'):
        text += '\nItems: {} / {} done · {} failed'.format(
            job.get('items_done', 0), job['items_total'], job.get('items_failed', 0))
    if job.get('phase') and job.get('state') not in ('complete', 'failed', 'canceled'):
        text += '\n' + job['phase']
    return text + ('\n' + job['error'] if job.get('error') else '')


def selected_description(job_id):
    if not job_id:
        return 'Current graph: render upstream, then deliver its outputs'
    try:
        return describe(load(job_id))
    except (OSError, ValueError) as exc:
        return 'Job metadata unavailable: {}'.format(exc)


def _client(job, cfg, api, create=False):
    if job.get('user') != cfg.user or job.get('volume_id') != cfg.volume_id:
        raise ValueError('Selected job belongs to another user/volume. Select its farm context first.')
    if create:
        pubkey = Path(cfg.ssh_key_path + '.pub').read_text()
        pod = pods.ensure_sync_pod(api, cfg, config.session_token(), pubkey)
    else:
        pod = pods.find_running_sync_pod(api, cfg)
    if not pod:
        raise RuntimeError('No sync pod is running; job status on the farm is unavailable')
    return pod, WorkerClient(pod['id'], config.session_token())


def refresh(job, cfg=None, api=None):
    if job.get('mode') == 'background':
        return load(job['id'])
    if job.get('state') in ('complete', 'failed', 'canceled'):
        return job
    _pod, client = _client(job, cfg, api)
    raw = client.read_file(job['package_dir'] + '/job.json')
    if not raw:
        return dict(job, error='Remote status unavailable')
    remote = json.loads(raw)
    if remote.get('id') != job['id']:
        raise ValueError('Job identity mismatch')
    merged = dict(job)
    merged.pop('error', None)
    merged.update({k: v for k, v in remote.items() if k in FIELDS and k != 'downloaded_files'})
    return save(merged)


def cancel(job, cfg=None, api=None):
    if job.get('state') in ('complete', 'failed', 'canceled'):
        return job
    if job.get('mode') == 'background':
        (directory() / (validate_id(job['id']) + '.cancel')).touch()
    else:
        _pod, client = _client(job, cfg, api)
        path = job['package_dir'] + '/cancel'
        result = client.exec('touch ' + shlex.quote(path), timeout_s=15)
        if result.get('exit_code'):
            raise RuntimeError('Could not request cancellation')
    return save(dict(job, state='cancel_requested'))


def downloaded(job_id, paths):
    if not job_id:
        return
    lock = directory() / (validate_id(job_id) + '.lock')
    with pods._file_lock(lock):
        job = load(job_id)
        job['downloaded_files'] = sorted(set(job.get('downloaded_files', [])) | set(paths))
        save(job)


def fetch_results(job_id, cfg, api, cancel=lambda: False, log=print):
    """Download the native checkpoint + output manifest for this exact job."""
    job = load(job_id)
    if job.get('mode') != 'farm':
        raise ValueError('This job ran locally; its outputs are already on this machine.')
    pod, client = _client(job, cfg, api, create=True)
    deadline = job.get('submitted_at', time.time()) + job.get('max_minutes', 240) * 60 + 60
    announced = False
    while True:
        if cancel():
            raise InterruptedError('Result retrieval cancelled')
        raw = client.read_file(job['package_dir'] + '/job.json')
        if raw:
            remote = json.loads(raw)
            if remote.get('id') != job_id:
                raise ValueError('Job identity mismatch')
            job = save(dict(job, **{k: v for k, v in remote.items() if k in FIELDS and k != 'downloaded_files'}))
        if job.get('state') in ('complete', 'failed', 'canceled'):
            break
        if time.time() >= deadline:
            raise TimeoutError('Job has not published results within its runtime limit')
        if not announced:
            log('Waiting for job {} ({})'.format(job_id, job.get('state')))
            announced = True
        time.sleep(2)
    if not job.get('taskgraph_sha256'):
        raise RuntimeError('Job {} ended before publishing usable results. Open its log.'.format(job_id))
    from .context import _snapshot_root
    local = _snapshot_root() / 'job-results' / validate_id(job_id)
    local.mkdir(parents=True, exist_ok=True)
    ip, port = pods.pod_public_endpoint(pod, 22)
    target = sync.SftpTarget(ip, port, cfg.ssh_key_path)
    names = ['taskgraph_out.bin', 'outputs.json']
    entries = [sync.FileEntry(str(local / n), job['package_dir'] + '/' + n, 0) for n in names]
    sync.rclone_copy(entries, target, 'down', cfg.rclone_path, str(local), job['package_dir'])
    state = local / names[0]
    expected = job.get('taskgraph_sha256')
    if expected and hashlib.sha256(state.read_bytes()).hexdigest() != expected:
        raise ValueError('Downloaded task graph is incomplete or has changed')
    manifest = json.loads((local / names[1]).read_text())
    if manifest.get('id') != job_id:
        raise ValueError('Result manifest belongs to another job')
    return job, str(state), manifest
