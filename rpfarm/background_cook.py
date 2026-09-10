"""Mode 2 of three cook modes (Ruling R68): a detached local background cook.

The owner's own words: he wants it opt-in and switchable off --
"я не хочу, чтобы это было невозможно отключить... если человеку надо
автономно, чтобы этот процесс шел, то он галку поставил". Three modes
exist in the scheduler HDA: inside Houdini (today's default, unchanged),
this one (a background process on the owner's own machine), and
Submit As Job (mode 3, cooks on a rented farm pod instead) -- which stays
broken and out of scope: nothing on the farm can shut its own pods down
without the RunPod API key, which the owner refuses to put there. This
mode dodges that problem entirely: the orchestrating process runs on his
own machine, so the key never leaves it and the pods still get shut down
normally when the cook ends -- it is the SAME cook as mode 1, just driven
by a headless, detached ``hython`` instead of the interactive session.

``$HHP/pdgjob/topcook.py`` ships with every Houdini install and is the
native mechanism for cooking a TOP network as a single job -- the exact
script this farm's OWN (broken) mode 3 already transfers to a pod and
runs there (see the scheduler HDA's ``submitAsJob``, which this module's
command shape deliberately mirrors: ``do it the way Houdini does it, not
our way``). This module only changes WHERE that same script runs.

Deliberately not covered here: the ``hou``-dependent parts (saving the
scene, resolving the topnet's own path, showing the artist the one-time
"where to watch" message) -- those need a live Houdini session and live
in the scheduler HDA's ``onBackgroundCook``. Everything here is plain
Python, runs and is tested outside Houdini, the same split the rest of
this package uses.
"""

from __future__ import annotations

import datetime
import os
import subprocess
from pathlib import Path


class BackgroundCookError(Exception):
    pass


def find_topcook(hhp: str | Path) -> Path:
    """``$HHP/pdgjob/topcook.py`` -- raises :class:`BackgroundCookError`
    with a plain-language message if it is not there (a Houdini install
    too old, or ``$HHP`` pointing somewhere unexpected), rather than
    letting the artist find out from a launch that silently does nothing.
    """
    path = Path(hhp) / "pdgjob" / "topcook.py"
    if not path.is_file():
        raise BackgroundCookError(
            "{} does not exist -- this Houdini install may be too old, or "
            "$HHP points somewhere unexpected".format(path))
    return path


def build_command(hython: str | Path, topcook: str | Path, hip_path: str,
                  top_path: str, verbosity: int = 2) -> list[str]:
    """The command line for the detached process.

    Mirrors the scheduler's own ``submitAsJob`` almost exactly (``--pdg``,
    ``--report none``, ``--verbosity 2``) -- the one difference on purpose
    is ``--logs``: mode 3's log lives on the pod and is fetched through
    this farm's own tooling, but mode 2 has no UI at all, so failed work
    item logs are folded into the one log file the artist is told to
    watch, rather than left to find some other way.
    """
    return [
        str(hython), "--pdg", str(topcook),
        "--report", "none",
        "--hip", hip_path,
        "--verbosity", str(verbosity),
        "--logs",
        "--toppath", top_path,
    ]


def default_log_path(ledger_dir: Path, top_path: str, clock=None) -> Path:
    """Where this run's stdout/stderr go -- named so several background
    cooks (this project today, another tomorrow) do not overwrite each
    other's log. Told to the artist right after launch; there is no other
    way to watch progress."""
    now = (clock or datetime.datetime.now)()
    label = top_path.strip("/").replace("/", "_") or "topnet"
    stamp = now.strftime("%Y%m%d-%H%M%S")
    return ledger_dir / "bgcook" / "{}-{}.log".format(stamp, label)


def launch(command: list[str], log_file: Path, cwd: str | None = None,
          env: dict | None = None, popen=subprocess.Popen) -> int:
    """Start ``command``, genuinely detached: closing Houdini (or this
    Python process) must not kill it. stdout+stderr both go to
    ``log_file`` -- opened here, not left to the child, so the artist has
    something to watch even if the child dies before writing a line.

    Returns the child's pid. Never blocks -- Popen returns as soon as the
    process is started, it does not wait for the cook to finish.
    """
    log_file.parent.mkdir(parents=True, exist_ok=True)
    kwargs = {}
    if os.name == "nt":
        # DETACHED_PROCESS: no console inherited from this one, so closing
        # Houdini's own console (if any) cannot signal the child. CREATE_
        # NEW_PROCESS_GROUP: Ctrl+C in a parent's console does not reach it.
        # Both are Windows-only subprocess attributes -- the literal
        # fallback (their real, documented Win32 values) keeps this branch
        # testable on macOS/Linux by simulating os.name, without needing
        # the attributes to actually exist on this platform's subprocess.
        detached_process = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        create_new_process_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        kwargs["creationflags"] = detached_process | create_new_process_group
    else:
        # A new session: SIGHUP from a closing terminal/parent process
        # group does not reach the child (the POSIX equivalent of setsid).
        kwargs["start_new_session"] = True
    with open(log_file, "ab") as f:
        proc = popen(command, stdout=f, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL, cwd=cwd, env=env, **kwargs)
    return proc.pid


def launch_tracked(node):
    """Launch the explicit target and its local delivery stage as one durable job."""
    import os
    import time
    import uuid
    import hou
    from . import context, jobs
    from .houdini_local import HoudiniInstall

    reference = context.value(node, 'submitjobnode', '')
    target = node.node(reference) if reference else None
    if target is None or target.type().name() in ('topnet', 'topnetmgr', 'runpodfarmdownload'):
        raise BackgroundCookError('Set Farm Target to the render/compute node first.')
    graph = target.getPDGGraphContext()
    if graph and graph.cooking:
        raise BackgroundCookError('Finish or cancel the current cook first.')
    download = jobs.download_node(target)
    if node.parm('rpfarm_downloadoutputs'):
        node.parm('rpfarm_downloadoutputs').set(0)
    download.parm('rpfarm_job').set('')
    hou.hipFile.save()
    farm = context.resolve(node)
    job_id = uuid.uuid4().hex[:8]
    artifact_dir = jobs.directory() / 'artifacts' / job_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    log_file = artifact_dir / 'background.log'
    record = jobs.save({'id': job_id, 'mode': 'background', 'state': 'submitted',
        'user': farm.cfg.user, 'project': farm.project, 'volume_id': farm.cfg.volume_id,
        'hip': hou.hipFile.path(), 'logical_hip': hou.hipFile.path(),
        'local_root': farm.local_root, 'target': download.path(),
        'log_path': str(log_file), 'submitted_at': time.time(), 'max_minutes': 240})
    package_root = Path(__file__).resolve().parent.parent
    install = HoudiniInstall(Path(hou.expandString('$HFS')))
    env = dict(os.environ)
    env.update(RPFARM_ROOT=str(package_root), RPFARM_COOK=job_id,
               RPFARM_CONTEXT_PATH=context.snapshot(farm.cfg))
    env['PYTHONPATH'] = str(package_root) + os.pathsep + env.get('PYTHONPATH', '')
    command = [str(install.hython), str(package_root / 'rpfarm' / 'host_render.py'),
               '--hip', record['hip'], '--toppath', record['target'],
               '--taskgraphout', str(artifact_dir / 'taskgraph_out.bin'),
               '--jobfile', str(jobs.directory() / (job_id + '.json'))]
    try:
        pid = launch(command, log_file, env=env)
    except Exception:
        jobs.save(dict(record, state='failed', error='Could not start local controller'))
        raise
    record = jobs.save(dict(jobs.load(job_id), pid=pid))
    if node.parm('rpfarm_job'):
        node.parm('rpfarm_job').set(job_id)
    return record
