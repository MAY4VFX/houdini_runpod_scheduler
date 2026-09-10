"""RunPodFarm host-cook process -- Submit As Job (mode 3, Ruling R71).

Runs as the pod's MAIN process (entrypoint.sh execs this instead of
worker.py when RPFARM_ROLE=host) -- not a worker.py task, not something an
external scheduler submits to. The pod boots, cooks the whole TOP graph
itself via $HHP/pdgjob/topcook.py (Houdini's own mechanism for cooking a
TOP network as a single job -- the pod's own Houdini install, from the
network volume, already has it; nothing is shipped for that part), and
terminates itself when done. Nothing outside this pod drives it and
nothing outside it is watching it -- the artist's Houdini does not need
to be running.

stdlib only, like worker.py: this runs under the pod's plain system
python3, no third-party packages.

The one thing this pod has that no other pod ever has: RUNPOD_API_KEY
(Ruling R71, hardening item 2 -- "the key goes only to the pod running
the cook"). It is read from the environment once, at start, and never
appears in a log line, a print, an exception message, or anything
written under /workspace. self_terminate()'s own logging is deliberately
just the pod id and an HTTP status, nothing from the request itself.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

RUNPOD_API_BASE = "https://rest.runpod.io/v1"
USER_AGENT = "rpfarm-host-cook/1"

#: Hard ceiling on how long this pod is allowed to run, regardless of what
#: the cook itself is doing. A cook that hangs, or a topcook.py that never
#: returns, must not leave a machine billing forever -- the same hole
#: fixed this morning for orphaned render pods (Ruling R69's neighbour,
#: _sweepOrphanPods), now for the pod running the cook itself. Overridable
#: for a deliberately long cook; the default errs long, not short, since a
#: false-positive kill loses real render time.
DEFAULT_MAX_MINUTES = 240


def log(msg):
    print("[host-cook] {}".format(msg), flush=True)


def _runpod_call(method, path, api_key, body=None):
    url = RUNPOD_API_BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, method=method, data=data,
        headers={
            "Authorization": "Bearer {}".format(api_key),
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, r.read()


def terminate_pod(pod_id, api_key):
    """DELETE /pods/<id> -- the same call rpfarm.runpod_api.RunPodAPI.
    terminate_pod makes, reimplemented stdlib-only here since the pod has
    no rpfarm package to import for it. Never raises: called from a
    watchdog and from cleanup paths where the pod is already on its way
    out either way, and a failed self-terminate must not crash something
    that would otherwise still have terminated the pod correctly.
    """
    try:
        status, _ = _runpod_call("DELETE", "/pods/{}".format(pod_id), api_key)
        log("terminate {} -> HTTP {}".format(pod_id, status))
    except (urllib.error.URLError, OSError) as e:
        # Never the exception's own str() beyond the type -- a network
        # error message can echo back request details in some libraries,
        # and this pod's own id is the only thing worth knowing here.
        log("terminate {} failed ({})".format(pod_id, type(e).__name__))


def list_cook_pods(user, cook_id, api_key):
    """Every pod named rpfarm-<user>-*-<cook_id>-* -- this cook's own
    render pods, if any are still around. Read-only. Mirrors rpfarm.pods'
    own name format (rpfarm-<user>-<project>-<cook_id>-<n>) without
    importing rpfarm."""
    try:
        status, raw = _runpod_call("GET", "/pods", api_key)
    except (urllib.error.URLError, OSError) as e:
        log("could not list pods for the render-pod sweep ({})".format(type(e).__name__))
        return []
    if status >= 300:
        log("could not list pods for the render-pod sweep (HTTP {})".format(status))
        return []
    try:
        pods = json.loads(raw)
    except (ValueError, TypeError):
        return []
    prefix = "rpfarm-{}-".format(user)
    return [p for p in pods
           if str(p.get("name", "")).startswith(prefix)
           and cook_id in str(p.get("name", "")).split("-")]


def sweep_render_pods(pod_id, user, cook_id, api_key):
    """Defense in depth, not the primary mechanism: topcook.py's own
    nested cook already terminates its render pods normally through the
    scheduler's existing onStopCook (the exact code every other cook
    already uses). This only matters if that cook crashed or was killed
    before onStopCook got to run -- the same class of gap
    _sweepOrphanPods closes for a locally-driven cook, applied here for
    one driven from inside a pod instead."""
    for pod in list_cook_pods(user, cook_id, api_key):
        candidate = pod.get("id")
        if not candidate or candidate == pod_id:
            continue
        log("sweeping orphaned render pod {} ({})".format(candidate, pod.get("name")))
        terminate_pod(candidate, api_key)


class Watchdog(threading.Thread):
    """Runs independently of the main cook -- a separate thread, so a
    hung main thread (blocked in subprocess.wait(), say) does not stop
    this from firing. Sleeps in short increments rather than one long
    sleep so stop() is responsive, not so it can react to anything about
    the cook's own progress: this is a hard ceiling on wall-clock time,
    not an idle detector, because a cook driven headlessly has no
    externally-observable "idle" state distinct from "running" the way a
    render pod waiting for a task does.
    """

    def __init__(self, max_seconds, on_expire, poll_s=15):
        super().__init__(name="rpfarm-host-cook-watchdog", daemon=True)
        self._deadline = time.monotonic() + max_seconds
        self._on_expire = on_expire
        self._poll_s = poll_s
        self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            if time.monotonic() >= self._deadline:
                log("watchdog: max runtime exceeded -- terminating regardless of cook state")
                self._on_expire()
                return
            self._stop.wait(self._poll_s)

    def cancel(self):
        self._stop.set()


def find_topcook(hfs):
    """``$HFS/houdini/python<major>.<minor>libs/pdgjob/topcook.py`` -- the
    exact directory name is version-specific and undocumented as a fixed
    path (it is ``$HHP``, which ``houdini_setup_bash`` computes), so this
    globs for it rather than hardcoding a Python version. Returns ``None``
    if not found rather than raising -- callers turn that into a clear
    log line instead of a bare glob-index crash.
    """
    matches = sorted(glob.glob(os.path.join(hfs, "houdini", "python*libs", "pdgjob", "topcook.py")))
    return matches[0] if matches else None


def build_topcook_command(hython, topcook, hip_path, top_path, verbosity=2):
    return [
        hython, "--pdg", topcook,
        "--report", "none",
        "--hip", hip_path,
        "--verbosity", str(verbosity),
        "--logs",
        "--toppath", top_path,
    ]


def main():
    api_key = os.environ.get("RUNPOD_API_KEY", "")
    pod_id = os.environ.get("RUNPOD_POD_ID", "")
    user = os.environ.get("RPFARM_USER", "")
    cook_id = os.environ.get("RPFARM_COOK", "")
    hip_path = os.environ.get("RPFARM_HOST_HIP", "")
    top_path = os.environ.get("RPFARM_HOST_TOPPATH", "")
    pkg_dir = os.environ.get("RPFARM_HOST_PKGDIR", "")
    ssh_key_pem = os.environ.get("RPFARM_HOST_SSH_KEY", "")
    hfs = os.environ.get("HFS", "")
    max_minutes = int(os.environ.get("RPFARM_HOST_MAX_MINUTES") or DEFAULT_MAX_MINUTES)

    if not (api_key and pod_id and hip_path and top_path and pkg_dir and ssh_key_pem):
        log("missing required env (RUNPOD_API_KEY/RUNPOD_POD_ID/RPFARM_HOST_HIP/"
            "RPFARM_HOST_TOPPATH/RPFARM_HOST_PKGDIR/RPFARM_HOST_SSH_KEY) -- "
            "cannot run, terminating self")
        if pod_id and api_key:
            terminate_pod(pod_id, api_key)
        return 1

    watchdog = Watchdog(max_minutes * 60, lambda: (
        sweep_render_pods(pod_id, user, cook_id, api_key),
        terminate_pod(pod_id, api_key),
    ))
    watchdog.start()

    hda_dir = os.path.join(pkg_dir, "hda")
    rpfarm_root = pkg_dir  # <pkg_dir>/rpfarm/__init__.py -- same layout submitAsJob shipped
    env = dict(os.environ)
    existing_scan = env.get("HOUDINI_OTLSCAN_PATH", "")
    # Trailing "&" tells Houdini's own scan-path syntax to still include
    # whatever it would have scanned by default -- HOUDINI_OTLSCAN_PATH
    # replaces the default path unless told to append to it.
    env["HOUDINI_OTLSCAN_PATH"] = hda_dir + (":" + existing_scan if existing_scan else ":&")
    env["RPFARM_ROOT"] = rpfarm_root
    # rpcfg.load() (rpfarm/config.py) reads $RPFARM_HOME/config.toml --
    # defaults to ~/.rpfarm, i.e. /root/.rpfarm on this pod, which does
    # not exist. Confirmed live (2026-09-10): the nested scheduler's
    # onStartCook raised ConfigError("no config at /root/.rpfarm/
    # config.toml") and PDG showed only "Failed to start scheduler" --
    # the instrumentation added for exactly this (Ruling R71) is what
    # surfaced it in one rental instead of a fourth guess. The shipped
    # config.toml lives at <pkg_dir>/config.toml (same place submitAsJob
    # wrote it), so $RPFARM_HOME is that same directory -- not a second
    # copy anywhere else, which would only be one more thing to drift.
    env["RPFARM_HOME"] = pkg_dir

    # The nested scheduler (running inside topcook.py's own cook, same
    # rpfarm code every other cook uses) needs to reach the sync pod over
    # SFTP the exact same way the artist's own machine does -- but the
    # artist's config.toml points at Mac-only local paths for the ssh key
    # and rclone binary, which mean nothing on this Linux pod, and never
    # carries api_key onto the shared volume at all (Ruling R71's own
    # hard constraint). rpfarm.config.load()'s _LOAD_OVERRIDE_ENV reads
    # these three from this pod's OWN environment instead -- RUNPOD_API_KEY
    # already is one (inherited from this process's own env, set only on
    # this pod); the other two are set here, from what actually exists on
    # THIS machine, not copied from the shipped config.toml.
    key_path = "/tmp/.rpfarm_host_key"
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(ssh_key_pem if ssh_key_pem.endswith("\n") else ssh_key_pem + "\n")
    env["RPFARM_SSH_KEY_PATH"] = key_path
    env["RPFARM_RCLONE_PATH"] = shutil.which("rclone") or "/usr/bin/rclone"

    # "hython" bare, not an absolute path: entrypoint.sh already sourced
    # houdini_setup_bash before execing this process, so hython is on
    # PATH the same way it already is for hserver/houdini_setup_bash's
    # other exports -- no need to also know Houdini's own bin/ layout.
    hython = "hython"
    topcook = find_topcook(hfs) if hfs else None
    rc = 1
    if not topcook:
        log("could not find pdgjob/topcook.py under $HFS/houdini/python*libs "
            "(HFS={!r}) -- terminating self".format(hfs))
        watchdog.cancel()
        terminate_pod(pod_id, api_key)
        return 1

    command = build_topcook_command(hython, topcook, hip_path, top_path)
    try:
        log("cook starting: {} --toppath {}".format(os.path.basename(hip_path), top_path))
        t0 = time.monotonic()
        proc = subprocess.run(
            command, env=env, timeout=max_minutes * 60 - 30,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        rc = proc.returncode
        log("cook finished in {:.0f}s, exit {}".format(time.monotonic() - t0, rc))
        sys.stdout.write(proc.stdout[-20000:])
        sys.stdout.flush()
    except subprocess.TimeoutExpired as e:
        log("cook did not finish within {} minutes -- killed".format(max_minutes))
        if e.stdout:
            sys.stdout.write(e.stdout[-20000:])
    except Exception as e:  # noqa: BLE001 - this process must still terminate the pod
        log("cook raised an unexpected error ({}) -- terminating anyway".format(type(e).__name__))
    finally:
        watchdog.cancel()
        sweep_render_pods(pod_id, user, cook_id, api_key)
        terminate_pod(pod_id, api_key)
    return rc


if __name__ == "__main__":
    sys.exit(main())
