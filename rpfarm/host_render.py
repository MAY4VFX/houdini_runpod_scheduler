"""Render-only cook of one TOP node, resuming a partly-cooked graph --
Submit As Job's own replacement for ``$HHP/pdgjob/topcook.py`` (Ruling
R71, second cut). Runs under ``hython``, spawned by ``host_cook.py``.

Why not topcook.py: its own CLI ``main()`` hardcodes the ``--taskgraphin``
value away before using it --

    # FIXME: Disabled pending verification of deserialize and
    # generation-after-deserialize
    # tasgraphin = args.taskgraphin
    taskgraphin = ''

-- a documented no-op in this Houdini build (confirmed by reading the
shipped source, `$HFS/houdini/python*libs/pdgjob/topcook.py`). Using it
as advertised would have silently ignored the state file: the cook would
look clean while quietly re-running upload's work on the farm. The
underlying API the CLI wraps is not gated the same way and is what this
script calls directly -- verified live, locally, at zero farm cost before
this was written (two-process round trip: cook a node, serialize, load
the saved .hip in a FRESH hython process, deserialize, cook a downstream
node -- the upstream node's side effect happened exactly once, not
twice). See Ruling R71 in docs/superpowers/specs/2026-09-02-rpfarm-v2-
design.md for the full account.

Sequence, mirroring topcook.py's own ``cookTopNode`` internals:
  1. ``node.cookWorkItems(tops_only=True)`` -- PDG topology only, for
     ``node`` and everything upstream of it. Never cooks anything.
  2. ``ctx.deserializeWorkItems(taskgraphin)`` -- if given, restores
     upload's already-cooked state (produced by the submitting session's
     own ``_submitAsJobInner``) into that topology.
  3. ``node.cookWorkItems(block=True)`` -- the real cook. PDG sees
     upload's state as already done and starts from there; only ``node``
     and its upstream are ever touched -- PDG cannot cook downstream of a
     cook target, so ``node`` being the render node (never the download
     node, never the network) is what keeps download off this pod
     structurally, not a special case in this script.
  4. ``ctx.serializeWorkItems(taskgraphout)`` -- if given, writes the
     now-cooked state (upload + render) back out, for a later download
     cook to pick up.

No upload/download of its own: ``taskgraphin``/``taskgraphout`` are paths
under the shipped ``host_pkg/`` directory, which already lives on the
same network volume every pod mounts -- a plain local file read/write,
same as ``config.toml``/``RPFARM_HOME`` already are for this pod.
"""

from __future__ import annotations

import argparse
import os
import sys


def parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--hip", required=True, help="Path to the .hip file to load")
    p.add_argument("--toppath", required=True, help="TOP node to cook (and its upstream only)")
    p.add_argument("--taskgraphin", default="", help="Serialized state to restore before cooking, if any")
    p.add_argument("--taskgraphout", default="", help="Where to write the cooked state after, if any")
    p.add_argument('--jobfile', default='', help='Local controller job record (background mode)')
    return p.parse_args(argv)


def log(msg):
    print("[host-render] {}".format(msg), flush=True)


def run(args, hou):
    import hashlib
    import json
    import time
    from pathlib import Path
    from rpfarm import jobs, submission

    package = Path(args.taskgraphout).parent
    package.mkdir(parents=True, exist_ok=True)
    job_path = Path(args.jobfile) if args.jobfile else package / 'job.json'
    job = json.loads(job_path.read_text())
    job = jobs.write(job_path, dict(job, state='running'))
    ctx = node = scheduler_type = None
    cancelled = False
    rc = 1
    cook_errors = []
    error_handler = None
    marker = (job_path.with_suffix('.cancel') if job.get('mode') == 'background' else package / 'cancel')
    try:
        hou.hipFile.load(args.hip, ignore_load_warnings=True)
        if job.get('logical_hip'):
            hou.hipFile.setName(job['logical_hip'])
        if job.get('local_root'):
            hou.putenv('JOB', job['local_root'])
        scheduler_type = hou.nodeType(hou.topNodeTypeCategory(), 'runpodfarmscheduler')
        if job.get('mode') == 'farm' and scheduler_type:
            for scheduler in scheduler_type.instances():
                scheduler.parm('rpfarm_downloadoutputs').set(0)
                scheduler.parm('rpfarm_autoclean').set('off')
        node = hou.node(args.toppath)
        if node is None:
            raise ValueError('Submitted target does not exist')
        node.cookWorkItems(block=True, tops_only=True)
        ctx = node.getPDGGraphContext()
        import pdg
        error_handler = ctx.addEventHandler(lambda event: cook_errors.append(True), pdg.EventType.CookError)
        if args.taskgraphin:
            ctx.deserializeWorkItems(args.taskgraphin)
        cancelled = marker.exists()
        deadline = time.monotonic() + job.get('max_minutes', 240) * 60
        if not cancelled:
            node.cookWorkItems(block=False)
            active_nodes = list(ctx.cookSet)
            last_report = 0.0
            while ctx.cooking:
                if (marker.exists() or time.monotonic() > deadline) and not cancelled:
                    cancelled = True
                    ctx.cancelCook()
                if time.monotonic() - last_report > 2:
                    last_report = time.monotonic()
                    work = [wi for n in ctx.cookSet for wi in n.workItems]
                    done_states = (pdg.workItemState.CookedSuccess, pdg.workItemState.CookedCache)
                    job.update(items_total=len(work),
                               items_done=sum(wi.state in done_states for wi in work),
                               items_failed=sum(wi.state == pdg.workItemState.CookedFail for wi in work),
                               phase='Cancel requested' if cancelled else 'Computing / transferring')
                    jobs.write(job_path, job)
                time.sleep(.2)
        ctx.waitAllEvents()
        active_nodes = list(ctx.cookSet) if cancelled else active_nodes
        failed = [wi for n in active_nodes for wi in n.workItems
                  if wi.state == pdg.workItemState.CookedFail]
        node_errors = [error for n in active_nodes if n.topNode() for error in n.topNode().errors()]
        final_work = [wi for n in active_nodes for wi in n.workItems]
        job.update(items_total=len(final_work),
                   items_done=sum(wi.state in (pdg.workItemState.CookedSuccess, pdg.workItemState.CookedCache)
                                  for wi in final_work),
                   items_failed=len(failed))
        empty = not jobs.pdg_output_node(node).workItems
        unfinished = any(wi.state not in (pdg.workItemState.CookedSuccess, pdg.workItemState.CookedCache)
                         for wi in jobs.pdg_output_node(node).workItems)
        rc = 1 if failed or cancelled or cook_errors or node_errors or empty or unfinished else 0
        if empty and not cancelled:
            job['error'] = 'The target produced no work items; see host/controller log.'
    except Exception as exc:
        log('Cook failed: {}'.format(type(exc).__name__))
        import traceback
        traceback.print_exc()
    finally:
        if ctx is not None and error_handler is not None:
            ctx.removeEventHandler(error_handler)
        if ctx is not None and node is not None:
            try:
                manifest = {'id': job['id'], 'target': job['target'], 'items': []}
                items = list(jobs.pdg_output_node(node).workItems)
                children = []
                for parent in list(items):
                    children.extend(getattr(parent, 'batchItems', ()) or ())
                items = children + items  # sub-frame identity wins over a batch aggregate
                seen = set()
                seen_outputs = set()
                for item in items:
                    if item.id in seen:
                        continue
                    seen.add(item.id)
                    outputs = [(out.path, out.tag) for out in item.outputFiles if out.path not in seen_outputs]
                    if not outputs:
                        continue
                    seen_outputs.update(path for path, _tag in outputs)
                    manifest['items'].append({
                        'id': item.id, 'frame': item.frame if item.hasFrame else None,
                        'outputs': outputs,
                        'pathmap': item.stringAttribValue('rpfarm_pathmap') or '{}',
                        'delivery_key': item.stringAttribValue('rpfarm_delivery_key') or
                                        '{}/{}'.format(job['id'], item.id),
                    })
                state = Path(args.taskgraphout)
                temp = state.with_name(state.name + '.partial')
                ctx.serializeWorkItems(str(temp), '')
                submission.redact_checkpoint(temp, [
                    os.environ.get('RUNPOD_API_KEY'), os.environ.get('RPFARM_SESSION_TOKEN'),
                    os.environ.get('RPFARM_HOST_SESSION_TOKEN'), os.environ.get('SESINETD_URL')])
                os.replace(temp, state)
                manifest_path = package / 'outputs.json'
                temp_manifest = manifest_path.with_suffix('.tmp')
                temp_manifest.write_text(json.dumps(manifest))
                os.replace(temp_manifest, manifest_path)
                job['taskgraph_sha256'] = hashlib.sha256(state.read_bytes()).hexdigest()
                job['outputs_count'] = len({p for item in manifest['items'] for p, _tag in item['outputs']})
                if job.get('mode') == 'background':
                    job['downloaded_files'] = sorted({p for item in manifest['items']
                        for p, _tag in item['outputs'] if os.path.isfile(p)})
            except Exception as exc:
                rc = 1
                log('Could not publish results: {}'.format(type(exc).__name__))
        job['state'] = 'canceled' if cancelled else 'complete' if rc == 0 else 'failed'
        jobs.write(job_path, job)
        if job.get('mode') == 'background' and rc != 0 and scheduler_type and scheduler_type.instances():
            try:
                from rpfarm import config
                from rpfarm.runpod_api import RunPodAPI
                cfg = config.load()
                api = RunPodAPI(cfg.api_key)
                for pod in api.list_pods():
                    env = pod.get('env') or {}
                    if env.get('RPFARM_USER') == job['user'] and env.get('RPFARM_COOK') == job['id']:
                        api.terminate_pod(pod['id'])
            except Exception as exc:
                log('Background cleanup could not finish: {}'.format(type(exc).__name__))
    return rc


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    import hou
    return run(args, hou)


if __name__ == "__main__":
    sys.exit(main())
