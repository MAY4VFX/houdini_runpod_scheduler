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
import os
import sys
import tempfile
import time

import hou


def _bootstrap_rpfarm():
    """Put ``rpfarm`` on ``sys.path`` -- ``$RPFARM_ROOT`` if set (how these
    scripts are meant to be run), else this checkout, found from this file."""
    root = os.environ.get("RPFARM_ROOT") or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)


_bootstrap_rpfarm()

from rpfarm import smoke as rpsmoke  # noqa: E402  (needs the path above first)

DEFAULT_GPUS = "NVIDIA RTX A4500, NVIDIA GeForce RTX 4090, NVIDIA RTX PRO 4000 Blackwell"


log = rpsmoke.make_log("smoke")


def parse_args(argv):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scheduler", default="runpodfarmscheduler",
                    help="scheduler node type (use localscheduler for a free dry run)")
    ap.add_argument("--items", type=int, default=3, help="work items to generate")
    ap.add_argument("--fail-item", type=int, default=None, metavar="INDEX",
                    help="make the item at this index exit non-zero (proves the "
                         "failure path reports the item exactly once)")
    ap.add_argument("--output-item", type=int, default=None, metavar="INDEX",
                    help="make the item at this index write a file under $PDG_DIR "
                         "and declare it with pdgcmd.addOutputFile (proves the "
                         "output-download path)")
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
    gen.parm("pdg_command").set(item_command(args))
    # Without this the command is exec'd argv-style, so ';' and 'sleep' are
    # passed to echo as literal words. RunPodFarm always runs commands through
    # `bash -c` on the pod, but the local dry run has to be told.
    gen.parm("shellcommand").set(1)

    topnet.parm("topscheduler").set(sched.path())
    gen.setDisplayFlag(True)
    log("graph: {} -> {}".format(gen.path(), sched.path()))
    return topnet, gen, sched, job


OUTPUT_DIR = "smoke_out"


def item_command(args):
    """The shell command every work item runs.

    One string for all items, branching on $PDG_INDEX, because
    genericgenerator gives every item the same command. Backslashes keep
    Houdini's own parm expansion off the variables so the literal text reaches
    bash, which expands it from the task environment the scheduler sends.
    """
    parts = ["echo hello from \\$PDG_ITEM_NAME"]

    if args.output_item is not None:
        # $PDG_DIR is the working dir on whichever machine runs this -- the
        # farm's project dir on a pod, the local one under localscheduler --
        # so the path maps cleanly back through localizePath either way.
        out = '\\$PDG_DIR/{}/\\$PDG_ITEM_NAME.txt'.format(OUTPUT_DIR)
        report = (
            '__PDG_PYTHON__ -c \'import os, sys; '
            'sys.path.insert(0, os.environ["PDG_SCRIPTDIR"]); '
            'import pdgcmd; pdgcmd.addOutputFile(sys.argv[1])\' "{}"'.format(out))
        parts.append(
            'if [ "\\$PDG_INDEX" = "{}" ]; then mkdir -p "\\$PDG_DIR/{}" && '
            'echo "written by \\$PDG_ITEM_NAME" > "{}" && {}; fi'.format(
                args.output_item, OUTPUT_DIR, out, report))

    if args.fail_item is not None:
        parts.append(
            'if [ "\\$PDG_INDEX" = "{}" ]; then echo "deliberate smoke failure" '
            '>&2; exit 3; fi'.format(args.fail_item))

    parts.append("sleep {}".format(args.sleep))
    return "; ".join(parts)


def report_downloads(job, sched):
    """List the files the output-download path pulled back to this machine.

    The items write under $PDG_DIR, which localises to the scheduler's own
    local working directory -- $JOB for runpodfarmscheduler, but whatever
    pdg_workingdir says for the local scheduler -- so look in both.
    """
    roots = [job]
    parm = sched.parm("pdg_workingdir")
    if parm is not None:
        roots.append(hou.expandString(parm.evalAsString()))
    files = []
    for root in dict.fromkeys(r for r in roots if r):
        out_dir = os.path.join(root, OUTPUT_DIR)
        found = sorted(glob.glob(os.path.join(out_dir, "*")))
        log("outputs in {} ({}):".format(out_dir, len(found)))
        for path in found:
            with open(path) as f:
                log("  {}  {!r}".format(path, f.read().strip()))
        files.extend(found)
    return files


def cook(topnet, gen, sched, timeout):
    """Cook to completion, cancelling if the wall-clock guard expires."""
    return rpsmoke.cook_node(gen, timeout, log, extra_nodes=[sched])


def report_items(gen):
    """Print each work item's final state. Returns (succeeded, total)."""
    succeeded, total, items = rpsmoke.report_items(
        gen, log, attribs=(), string_attribs=())
    if items:
        log("item command: {!r}".format(items[0].command))
        # The mapping runpodfarm_download (Task 10) reads off its upstream
        # items; empty here means the scheduler never stamped it.
        try:
            log("item rpfarm_pathmap: {}".format(
                items[0].stringAttribValue("rpfarm_pathmap")))
        except Exception as e:
            log("item rpfarm_pathmap: MISSING ({})".format(e))
    return succeeded, total


def report_ledger(since):
    """Print the ledger lines this run wrote. Returns the record count."""
    records, _paths = rpsmoke.report_ledger(since, log)
    return len(records)


def report_pods():
    """List the pods still alive on the account, so nothing is left billing.

    Returns the orphan GPU pods (a non-empty list is a failure for the
    caller); the sync pod is deliberately not an orphan -- it is shared and
    outlives a cook.
    """
    try:
        from rpfarm import config as rpcfg
        from rpfarm import pods as rppods
        from rpfarm.runpod_api import RunPodAPI

        cfg = rpcfg.load()
        rpsmoke.list_farm_pods(log, cfg)
        orphans = rppods.find_orphans(RunPodAPI(cfg.api_key), cfg.user)
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

    expected_failures = 1 if args.fail_item is not None else 0
    downloads = report_downloads(job, sched) if args.output_item is not None else []
    want_downloads = 1 if args.output_item is not None else 0

    ok = (not aborted
          and total > 0
          and succeeded == total - expected_failures
          and len(downloads) >= want_downloads)
    log("RESULT: {} -- {}/{} items succeeded ({} expected to fail), "
        "{} ledger record(s), {} output(s) downloaded, {:.0f}s".format(
            "PASS" if ok else "FAIL", succeeded, total, expected_failures,
            records, len(downloads), elapsed))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
