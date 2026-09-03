"""End-to-end smoke test for the RunPodFarm v2 scheduler, with no GUI.

Builds a throwaway TOP graph (one ``genericgenerator`` with a few trivial
shell work items), points it at a ``runpodfarmscheduler``, cooks it to
completion under a wall-clock guard, then reports every work item's final
state and the ledger records the cook wrote.

Run it with Houdini's ``hython``, not a system python::

    RPFARM_ROOT=$PWD \\
    /Applications/Houdini/Houdini22.0.368/Frameworks/Houdini.framework/\\
Versions/Current/Resources/bin/hython scripts/smoke_scheduler_headless.py

``--scheduler localscheduler`` runs the same graph on PDG's built-in local
scheduler instead, which costs nothing and is the quick way to tell a broken
harness apart from a broken scheduler before spending money on pods.

Exit status is 0 only when every work item cooked successfully.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import tempfile
import threading
import time

import hou

DEFAULT_GPUS = "NVIDIA RTX A4500, NVIDIA GeForce RTX 4090, NVIDIA RTX PRO 4000 Blackwell"


def log(message):
    print("[smoke] {}".format(message), flush=True)


def parse_args(argv):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scheduler", default="runpodfarmscheduler",
                    help="scheduler node type (use localscheduler for a free dry run)")
    ap.add_argument("--items", type=int, default=3, help="work items to generate")
    ap.add_argument("--sleep", type=int, default=5, help="seconds each work item sleeps")
    ap.add_argument("--timeout", type=int, default=1500,
                    help="wall-clock guard in seconds; the cook is cancelled past it")
    ap.add_argument("--gpus", default=DEFAULT_GPUS, help="comma-separated GPU priority list")
    ap.add_argument("--maxcost", type=float, default=1.0, help="budget in dollars for the cook")
    ap.add_argument("--minpods", type=int, default=1)
    ap.add_argument("--maxpods", type=int, default=1)
    ap.add_argument("--slots", type=int, default=1)
    ap.add_argument("--idletimeout", type=int, default=120)
    ap.add_argument("--job", default=None,
                    help="value for $JOB; defaults to a fresh temp directory")
    ap.add_argument("--quiet", action="store_true", help="turn the scheduler's verbose log off")
    return ap.parse_args(argv)


def build_graph(args):
    """Create /obj/topnet1 with a generator and the scheduler under test."""
    job = args.job or tempfile.mkdtemp(prefix="rpfarm-smoke-")
    os.makedirs(job, exist_ok=True)
    hou.putenv("JOB", job)
    log("$JOB = {}".format(job))

    topnet = hou.node("/obj").createNode("topnet", "topnet1")
    sched = topnet.createNode(args.scheduler, "rpfarm")

    if args.scheduler == "runpodfarmscheduler":
        sched.parm("rpfarm_gpulist").set(args.gpus)
        sched.parm("rpfarm_maxcost").set(args.maxcost)
        sched.parm("rpfarm_minpods").set(args.minpods)
        sched.parm("rpfarm_maxpods").set(args.maxpods)
        sched.parm("rpfarm_slots").set(args.slots)
        sched.parm("rpfarm_idletimeout").set(args.idletimeout)
        sched.parm("rpfarm_verbose").set(0 if args.quiet else 1)
        sched.parm("rpfarm_project").set("smoke")

    gen = topnet.createNode("genericgenerator", "gen")
    gen.parm("itemcount").set(args.items)
    # The backslash keeps Houdini's own parm expansion off $PDG_ITEM_NAME, so
    # the literal string reaches bash on the pod, which expands it from the
    # task environment the scheduler sends.
    gen.parm("pdg_command").set(
        "echo hello from \\$PDG_ITEM_NAME; sleep {}".format(args.sleep))
    # Without this the command is exec'd argv-style, so ';' and 'sleep' are
    # passed to echo as literal words. RunPodFarm always runs commands through
    # `bash -c` on the pod, but the local dry run has to be told.
    gen.parm("shellcommand").set(1)

    topnet.parm("topscheduler").set(sched.path())
    gen.setDisplayFlag(True)
    log("graph: {} -> {}".format(gen.path(), sched.path()))
    return topnet, gen, sched, job


def cook(topnet, gen, sched, timeout):
    """Cook to completion, cancelling if the wall-clock guard expires."""
    ctx = topnet.getPDGGraphContext()
    expired = threading.Event()

    def guard():
        expired.set()
        log("TIMEOUT after {}s -- cancelling the cook".format(timeout))
        try:
            ctx.cancelCook()
        except Exception as e:  # cancelCook from a timer thread is best-effort
            log("cancelCook failed: {}".format(e))

    watchdog = threading.Timer(timeout, guard)
    watchdog.daemon = True
    watchdog.start()

    started = time.time()
    log("cooking (guard {}s)...".format(timeout))
    failed = False
    try:
        gen.cookWorkItems(block=True, save_prompt=False)
    except hou.OperationFailed as e:
        # PDG reports a scheduler that refused to start as a bare "Failed to
        # start scheduler", with the actual reason only on the node. Print it,
        # then carry on so the caller still gets its item/ledger/pod report.
        failed = True
        log("COOK FAILED: {}".format(e))
        for node in (sched, gen.parent(), gen):
            for err in node.errors():
                log("  {} error: {}".format(node.path(), err))
    finally:
        watchdog.cancel()
    elapsed = time.time() - started
    log("cook returned after {:.0f}s".format(elapsed))
    return elapsed, expired.is_set() or failed


def report_items(gen):
    """Print each work item's final state. Returns (succeeded, total)."""
    pdg_node = gen.getPDGNode()
    items = list(pdg_node.workItems) if pdg_node else []
    succeeded = 0
    log("work items ({}):".format(len(items)))
    for item in items:
        state = str(item.state).rsplit(".", 1)[-1]
        if state.lower() in ("cookedsuccess", "success"):
            succeeded += 1
        log("  {:<24} {:<16} {:.1f}s".format(item.name, state, item.cookDuration))
    if items:
        log("item command: {!r}".format(items[0].command))
        # The mapping runpodfarm_download (Task 10) reads off its upstream
        # items; empty here means the scheduler never stamped it.
        try:
            log("item rpfarm_pathmap: {}".format(
                items[0].stringAttribValue("rpfarm_pathmap")))
        except Exception as e:
            log("item rpfarm_pathmap: MISSING ({})".format(e))
    return succeeded, len(items)


def report_ledger(since):
    """Print the ledger lines this run wrote. Returns the record count."""
    home = os.environ.get("RPFARM_HOME") or os.path.join(os.path.expanduser("~"), ".rpfarm")
    paths = [p for p in glob.glob(os.path.join(home, "ledger", "*.jsonl"))
             if os.path.getmtime(p) >= since - 1]
    if not paths:
        log("ledger: no file written under {}".format(os.path.join(home, "ledger")))
        return 0
    count = 0
    for path in sorted(paths):
        log("ledger {}:".format(path))
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                count += 1
                try:
                    log("  " + json.dumps(json.loads(line), sort_keys=True))
                except ValueError:
                    log("  " + line)
    return count


def report_pods():
    """List the pods still alive on the account, so nothing is left billing."""
    try:
        from rpfarm import config as rpcfg
        from rpfarm import pods as rppods
        from rpfarm.runpod_api import RunPodAPI

        cfg = rpcfg.load()
        api = RunPodAPI(cfg.api_key)
        alive = api.list_pods("rpfarm-")
        log("pods still on the account ({}):".format(len(alive)))
        for pod in alive:
            log("  {:<16} {:<28} {}".format(
                pod.get("id", "?"), pod.get("name", "?"), pod.get("desiredStatus", "?")))
        orphans = rppods.find_orphans(api, cfg.user)
        if orphans:
            log("ORPHAN GPU PODS STILL RUNNING: {}".format(
                ", ".join(p.get("name", p["id"]) for p in orphans)))
        return orphans
    except Exception as e:
        log("pod listing failed: {}".format(e))
        return []


def main(argv):
    args = parse_args(argv)
    started = time.time()
    topnet, gen, sched, job = build_graph(args)

    elapsed, aborted = cook(topnet, gen, sched, args.timeout)
    succeeded, total = report_items(gen)
    records = report_ledger(started)

    if args.scheduler == "runpodfarmscheduler":
        if sched.parm("rpfarm_status_text"):
            log("status text:\n" + sched.parm("rpfarm_status_text").evalAsString())
        report_pods()

    ok = not aborted and total > 0 and succeeded == total
    log("RESULT: {} -- {}/{} items succeeded, {} ledger record(s), {:.0f}s".format(
        "PASS" if ok else "FAIL", succeeded, total, records, elapsed))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
