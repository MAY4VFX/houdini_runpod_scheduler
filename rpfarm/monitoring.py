"""Background I/O, main-thread presentation for the Houdini farm/job snapshot."""
from concurrent.futures import Future
import datetime
import json
import os
import time
import threading

from . import config, context, jobs, pods, status, sync
from .runpod_api import RunPodAPI, pod_public_endpoint
from .worker_client import WorkerClient

_slots = threading.Semaphore(2)
_pending = {}
_started = False
_last_auto = 0.0
_last_housekeeping = 0.0


def _submit(*args):
    future = Future()
    def run():
        with _slots:
            try:
                future.set_result(snapshot(*args))
            except Exception as exc:
                future.set_exception(exc)
    threading.Thread(target=run, daemon=True, name='rpfarm-status').start()
    return future


def snapshot(cfg, job_id='', include_ledger=False, housekeeping=False):
    api = RunPodAPI(cfg.api_key)
    all_pods = api.list_pods()
    mine = [p for p in all_pods if pods.pod_owner(p) == cfg.user]
    live_cooks = [p for p in mine if p.get('desiredStatus') == 'RUNNING'
                  and p.get('name') != pods.sync_pod_name(cfg.user)]
    selected_sync = next((p for p in mine if p.get('name') == pods.sync_pod_name(cfg.user)
                          and pods._sync_volume_matches(p, cfg)), None)
    client = WorkerClient(selected_sync['id'], config.session_token()) if selected_sync else None
    if job_id:
        try:
            if jobs.load(job_id).get('state') not in ('complete', 'failed', 'canceled'):
                housekeeping = False  # cache the terminal job state before parking its sync pod
        except (OSError, ValueError):
            pass
    if housekeeping and selected_sync and not live_cooks:
        idle = stopped = None
        if selected_sync.get('desiredStatus') == 'RUNNING':
            health = client.health()
            if pods.classify_for_kill(selected_sync, cfg.user, health)[0] == 'safe':
                stamp = client.read_file('/workspace/.rpfarm/sync_last_used')
                try:
                    idle = time.time() - float(stamp or '')
                except ValueError:
                    pass
        else:
            try:
                then = datetime.datetime.fromisoformat(selected_sync['lastStatusChange'].replace('Z', '+00:00'))
                stopped = time.time() - then.timestamp()
            except (ValueError, KeyError):
                pass
        action = pods.sync_pod_action(selected_sync, idle, stopped,
                                     cfg.sync_idle_min * 60, cfg.sync_delete_min * 60)
        if action == pods.SYNC_STOP:
            api.stop_pod(selected_sync['id'])
            selected_sync = dict(selected_sync, desiredStatus='EXITED')
        elif action == pods.SYNC_DELETE:
            api.terminate_pod(selected_sync['id'])
            mine = [p for p in mine if p['id'] != selected_sync['id']]
            selected_sync = None
    lines = []
    for p in mine:
        state = selected_sync.get('desiredStatus') if selected_sync and p['id'] == selected_sync['id'] else p.get('desiredStatus')
        charge = '{:.3f} USD/h'.format(float(p.get('costPerHr') or 0)) if state == 'RUNNING' else 'disk still billed'
        lines.append('{} · {} · {}'.format(p.get('name'), state, charge))
    if not lines:
        lines.append('No pods owned by this user')
    if selected_sync:
        lines.append('Sync idle cleanup runs while Houdini is open')
    result = {'farm': '\n'.join(lines)}
    if job_id:
        try:
            record = jobs.load(job_id)
            record = jobs.refresh(record, cfg, api)
            result['job'] = jobs.describe(record)
        except Exception as exc:
            result['job'] = jobs.selected_description(job_id) + '\nStatus check: ' + str(exc)
    if selected_sync and selected_sync.get('desiredStatus') == 'RUNNING':
        try:
            volume = api.get_volume(cfg.volume_id)
            result['volume'] = '{} · {} GB allocated'.format(cfg.volume_id, volume.get('size', '?'))
            if include_ledger:
                ip, port = pod_public_endpoint(selected_sync, 22)
                target = sync.SftpTarget(ip, port, cfg.ssh_key_path)
                sync.rclone_copy_dir(str(config.home() / 'ledger'), target, 'down',
                                     cfg.rclone_path, '/workspace/ledger')
        except Exception as exc:
            result['volume'] = 'Volume/ledger refresh: ' + str(exc)
    return result


def request(node, include_ledger=False, housekeeping=False):
    key = node.path()
    if key in _pending:
        return
    cfg = context.resolve(node).cfg
    job_id = context.value(node, 'rpfarm_job', '')
    _pending[key] = _submit(cfg, job_id, include_ledger, housekeeping)
    if node.parm('rpfarm_status_text'):
        node.parm('rpfarm_status_text').set(status.update(key, farm='Refreshing…'))
    start()


def start():
    global _started
    import hou
    if _started or not hou.isUIAvailable():
        return
    def tick():
        global _last_auto, _last_housekeeping
        for key, future in list(_pending.items()):
            if not future.done():
                continue
            del _pending[key]
            node = hou.node(key)
            if node is None:
                continue
            try:
                data = future.result()
            except Exception as exc:
                data = {'farm': 'Status unavailable: ' + str(exc)}
            text = status.update(key, **{k: v for k, v in data.items() if k in ('farm', 'job')})
            if node.parm('rpfarm_status_text'):
                node.parm('rpfarm_status_text').set(text)
            if data.get('volume') and node.parm('rpfarm_volume_text'):
                node.parm('rpfarm_volume_text').set(data['volume'])
        now = time.monotonic()
        if now - _last_auto > 5:
            _last_auto = now
            cleanup = now - _last_housekeeping > 60
            if cleanup:
                _last_housekeeping = now
            node_type = hou.nodeType(hou.topNodeTypeCategory(), 'runpodfarmscheduler')
            for node in node_type.instances() if node_type else []:
                try:
                    request(node, housekeeping=cleanup)
                except Exception:
                    pass
    hou.ui.addEventLoopCallback(tick)
    _started = True
