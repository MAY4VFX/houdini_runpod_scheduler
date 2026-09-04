"""Cook the artist demo scene on the real farm, headlessly, with no GUI.

This is the verification counterpart of ``scripts/build_demo_scene.py``: it
loads ``~/Desktop/rpfarm_demo/rpfarm_demo.hip`` untouched and cooks it exactly
the way the scene's own ``SUBMIT_TO_FARM`` button does -- one non-blocking
``cookWorkItems`` on the downstream-most node, ``/obj/topnet1/download``, and
nothing else. Anything less faithful would verify a different code path than
the one the artist clicks.

Two processes, for the same reason ``rpfarm smoke`` uses two: the cook runs in
``hython``, but pod cleanup lives in this plain-python parent, so a hung or
crashed ``hython`` still cannot leave a GPU pod billing. The child gets its own
process group and the parent kills the group, not just the process -- otherwise
the out-of-process package runners survive their parent.

Run it as ordinary ``python3`` (it re-execs Houdini for the cook itself)::

    python3 scripts/cook_demo_headless.py [--scene PATH] [--timeout 1800]

Exit status is 0 only when every work item cooked, every expected frame is on
disk *and newer than this run*, the ledger has one record per frame, and
``GET /pods`` shows none of this user's pods left.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
import threading
import time


def _bootstrap_rpfarm():
    root = os.environ.get("RPFARM_ROOT") or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)
    return root


REPO = _bootstrap_rpfarm()

from rpfarm import config as rpcfg  # noqa: E402
from rpfarm import houdini_local  # noqa: E402
from rpfarm import smoke as rpsmoke  # noqa: E402

DEFAULT_SCENE = os.path.expanduser("~/Desktop/rpfarm_demo/rpfarm_demo.hip")
TOPNET = "/obj/topnet1"
COOK_NODE = TOPNET + "/render"
NODE_NAMES = ("upload", "render")
RENDER_SUBDIR = "render"

# Margin between the parent's kill-the-process-group deadline and the child's
# own cancelCook guard, so the cook gets a chance to shut the farm down
# politely (and write its ledger summary) before the parent stops being polite.
HYTHON_MARGIN = 240


# ---------------------------------------------------------------------------
# hython half
# ---------------------------------------------------------------------------


def hython_main(payload_path):
    import hou

    log = rpsmoke.make_log("demo-cook")
    with open(payload_path) as f:
        payload = json.load(f)

    # Evidence, from inside Houdini, of the environment the cook is actually
    # running in. Without this the only claim available is "I set the env in
    # the parent", which is exactly the kind of claim that let the interpreter
    # defect pass three verification runs.
    import shutil as _shutil
    log("environment inside Houdini: PATH={}".format(os.environ.get("PATH")))
    log("environment inside Houdini: which(python3)={}".format(_shutil.which("python3")))
    log("environment inside Houdini: sys.executable={} ({}.{}.{})".format(
        sys.executable, *sys.version_info[:3]))

    result = {"ok": False}
    try:
        started = time.time()
        hou.hipFile.load(payload["hip"], suppress_save_prompt=True)
        log("loaded {} in {:.1f}s".format(payload["hip"], time.time() - started))

        # The scene carries its own $JOB; nothing here overrides it, because
        # what the artist gets is exactly what has to be verified.
        job = hou.text.expandString("$JOB")
        hip_dir = hou.text.expandString("$HIP")
        log("$JOB = {}".format(job))
        log("$HIP = {}".format(hip_dir))
        if os.path.realpath(job) != os.path.realpath(os.path.dirname(payload["hip"])):
            log("WARNING: $JOB is not the scene's own folder -- uploads and "
                "downloads will not land where the sticky notes say")

        topnet = hou.node(TOPNET)
        nodes = {n: topnet.node(n) for n in NODE_NAMES}
        submit = topnet.node("SUBMIT_TO_FARM")
        if submit is None or submit.parm("rpdemo_submit") is None:
            raise RuntimeError("the scene has no SUBMIT_TO_FARM button to press")
        if not callable(getattr(hou.session, "rpfarm_demo_submit", None)):
            raise RuntimeError("the scene's Submit callback is missing from hou.session")

        # Press the artist's own button rather than an equivalent of it: this
        # runs the save, the already-cooking guard and the single
        # cookWorkItems(block=False) call that the .hip actually ships.
        elapsed, aborted = _cook_via_button(hou, submit, topnet, payload["timeout"], log,
                                            rpsmoke.state_tracker(nodes, log, heartbeat_every=30.0))
        rpsmoke.node_messages(list(nodes.values()) + [topnet, topnet.node("rpfarm")], log)

        failed = 0
        for node in nodes.values():
            succeeded, total, _items = rpsmoke.report_items(node, log)
            failed += total - succeeded
            # Zero items is not zero failures: a node whose generate raised
            # reports no items at all, which is how a crashed `download`
            # first passed for a clean cook.
            if total == 0:
                log("NODE {} produced no work items at all".format(node.name()))
                failed += 1
        sched = topnet.node("rpfarm")
        status = sched.parm("rpfarm_status_text")
        if status is not None:
            log("scheduler status:\n" + status.evalAsString())

        result = {
            "ok": (not aborted) and failed == 0,
            "elapsed": elapsed,
            "failed_items": failed,
            "cook_id": _cook_id(sched),
        }
    except Exception as e:  # noqa: BLE001 - the parent still has to clean up
        log("hython stage blew up: {!r}".format(e))
        import traceback
        traceback.print_exc()
    finally:
        with open(payload["result"], "w") as f:
            json.dump(result, f, indent=2)
    log("RESULT: {}".format("PASS" if result.get("ok") else "FAIL"))
    return 0 if result.get("ok") else 1


def _cook_via_button(hou, submit, topnet, timeout, log, on_poll, poll_interval=0.5):
    """Click Submit, then poll from this same thread under a wall-clock guard.

    Polling from the calling thread is not decoration: if Houdini's main thread
    were stuck inside a blocking cook, this loop could not run at all, so its
    output is itself the evidence that dispatch is out of process.
    """
    ctx = topnet.getPDGGraphContext()
    expired = threading.Event()

    def guard():
        expired.set()
        log("TIMEOUT after {}s -- cancelling the cook".format(timeout))
        try:
            ctx.cancelCook()
        except Exception as e:  # noqa: BLE001 - best effort from a timer thread
            log("cancelCook failed: {}".format(e))

    watchdog = threading.Timer(timeout, guard)
    watchdog.daemon = True
    watchdog.start()

    started = time.time()
    log("pressing SUBMIT_TO_FARM (guard {}s, non-blocking poll)...".format(timeout))
    failed = False
    try:
        hou.session.rpfarm_demo_submit({"node": submit})
        while ctx.cooking:
            on_poll(time.time() - started)
            if expired.is_set():
                break
            time.sleep(poll_interval)
        on_poll(time.time() - started)
    except hou.Error as e:
        failed = True
        log("COOK FAILED: {}".format(e))
    finally:
        watchdog.cancel()

    status = submit.parm("rpdemo_status")
    if status is not None:
        log("button status field:\n" + status.evalAsString())
    elapsed = time.time() - started
    log("cook returned after {:.0f}s".format(elapsed))
    return elapsed, expired.is_set() or failed


def _cook_id(sched):
    """The cook id the scheduler printed into its own status field."""
    parm = sched.parm("rpfarm_status_text")
    text = parm.evalAsString() if parm is not None else ""
    for line in text.splitlines():
        if line.startswith("cook "):
            return line.split()[1]
    return None


# ---------------------------------------------------------------------------
# parent half
# ---------------------------------------------------------------------------


def parse_args(argv):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scene", default=DEFAULT_SCENE)
    ap.add_argument("--timeout", type=int, default=1800,
                    help="wall-clock guard for the whole run, seconds")
    ap.add_argument("--frames", type=int, default=8, help="frames the scene renders")
    ap.add_argument("--keep-pods", action="store_true",
                    help="leave the farm as the cook left it (production behaviour)")
    ap.add_argument("--dock-env", action="store_true",
                    help="run the hython half with a Dock-like minimal environment "
                         "(PATH=/usr/bin:/bin, cwd=/) instead of this shell's. This is "
                         "how the artist actually launches Houdini, and it is the only "
                         "way to catch anything that silently depends on the launching "
                         "shell -- a bare 'python3' in a work item's command resolved to "
                         "Xcode's 3.9 there and killed every upload item.")
    return ap.parse_args(argv)


def main(argv):
    args = parse_args(argv)
    log = rpsmoke.make_log("demo")
    started = time.time()

    scene = os.path.abspath(os.path.expanduser(args.scene))
    if not os.path.isfile(scene):
        print("error: scene not found at {} -- build it with "
              "`hython scripts/build_demo_scene.py`".format(scene), file=sys.stderr)
        return 1
    scene_dir = os.path.dirname(scene)
    render_dir = os.path.join(scene_dir, RENDER_SUBDIR)

    cfg = rpcfg.load()
    installs = houdini_local.find_houdini_installations()
    if not installs:
        print("error: no local Houdini found", file=sys.stderr)
        return 1
    inst = installs[0]
    log("Houdini {} ({})".format(inst.version, inst.hython))
    log("scene {}".format(scene))

    payload = {
        "hip": scene,
        "timeout": max(60, args.timeout - HYTHON_MARGIN),
        "result": os.path.join(scene_dir, ".demo_cook_result.json"),
    }
    payload_path = os.path.join(scene_dir, ".demo_cook_payload.json")
    with open(payload_path, "w") as f:
        json.dump(payload, f, indent=2)

    watcher = rpsmoke.PodWatcher(cfg)
    watcher.start()

    rc = 1
    result = {}
    frames = []
    tasks = []
    cost = None
    remaining = []
    try:
        rc = _run_hython(inst, payload_path, args.timeout, log, dock_env=args.dock_env)
        try:
            with open(payload["result"]) as f:
                result = json.load(f)
        except (OSError, ValueError) as e:
            log("no cook result file ({}) -- the hython stage did not finish".format(e))
        frames = _fresh_frames(render_dir, started, log)
        tasks, cost = _ledger(result.get("cook_id"), started, log)
    finally:
        watcher.stop()
        if args.keep_pods:
            rpsmoke.list_farm_pods(log, cfg)
        else:
            remaining = rpsmoke.terminate_pods(log, cfg)

    print()
    rates = ", ".join("{} ${:.3f}/h".format(e["name"], e["rate"])
                      for e in watcher.pods.values())
    print("wall clock {:.0f}s".format(time.time() - started))
    print("cook id    {}".format(result.get("cook_id") or "?"))
    print("cost       {}   ({})".format(
        "${:.4f}".format(cost) if cost is not None else "?", rates or "no pods seen"))
    print("frames     {}/{} in {}".format(len(frames), args.frames, render_dir))
    print("ledger     {} task record(s)".format(len(tasks)))

    reasons = []
    if rc != 0:
        reasons.append("hython stage exit {}".format(rc))
    if not result.get("ok"):
        reasons.append("cook reported failure")
    if len(frames) != args.frames:
        reasons.append("{} of {} frames on disk and fresh".format(len(frames), args.frames))
    if len(tasks) != args.frames:
        reasons.append("{} ledger task record(s), expected {}".format(len(tasks), args.frames))
    if remaining:
        reasons.append("{} pod(s) still running".format(len(remaining)))
    if reasons:
        print("\nFAIL: " + "; ".join(reasons))
        return 1
    print("\nOK: {} frame(s) rendered on the farm and downloaded to {}".format(
        len(frames), render_dir))
    return 0


def _fresh_frames(render_dir, since, log):
    """Frames written *by this run* -- existence alone would let a previous
    run's leftovers pass for success, since the output paths never change."""
    out = []
    for path in sorted(glob.glob(os.path.join(render_dir, "*.exr"))):
        fresh = os.path.getmtime(path) >= since - 1
        log("  {}  {:.1f}KB  {}".format(
            os.path.basename(path), os.path.getsize(path) / 1024.0,
            "written by this run" if fresh else "STALE -- from an earlier run"))
        if fresh:
            out.append(path)
    if not out:
        log("no fresh frames under {}".format(render_dir))
    return out


def _ledger(cook_id, since, log):
    records, _paths = rpsmoke.report_ledger(since, log)
    tasks = [r for r in records if r.get("record") != "cook_summary"
             and (cook_id is None or r.get("cook_id") == cook_id)]
    cost = None
    for r in records:
        if r.get("record") == "cook_summary" and (cook_id is None or r.get("cook_id") == cook_id):
            cost = r.get("cost_est")
    return tasks, cost


def _run_hython(inst, payload_path, timeout, log, dock_env=False):
    cmd = [str(inst.hython), os.path.abspath(__file__), "--hython", payload_path]
    if dock_env:
        # Deliberately NOT inheriting this shell: PATH is exactly the thing that
        # differed between every green headless run and the artist's first real
        # one. RPFARM_ROOT is left out too -- a Dock-launched Houdini gets it
        # from houdini.env, which `rpfarm setup` writes, so letting that supply
        # it is the faithful path rather than a convenience.
        env = {"PATH": "/usr/bin:/bin", "HOME": os.path.expanduser("~"),
               "PYTHONUNBUFFERED": "1"}
        cwd = "/"
        log("--dock-env: PATH={} cwd={}".format(env["PATH"], cwd))
    else:
        env = dict(os.environ)
        env["RPFARM_ROOT"] = REPO
        env.setdefault("PYTHONUNBUFFERED", "1")
        cwd = REPO
    log("$ " + " ".join(cmd))
    proc = subprocess.Popen(
        cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, start_new_session=True)
    killer = threading.Timer(timeout, rpsmoke._kill_process, args=(proc, log))
    killer.daemon = True
    killer.start()
    try:
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
        proc.wait()
    finally:
        killer.cancel()
        if proc.poll() is None:
            rpsmoke._kill_process(proc, log)
    return proc.returncode


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--hython":
        sys.exit(hython_main(sys.argv[2]))
    sys.exit(main(sys.argv[1:]))
