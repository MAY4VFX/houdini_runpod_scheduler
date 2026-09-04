"""End-to-end smoke test for the ``runpodfarm_download`` TOP node, with no GUI.

Two runs against the real sync pod (``rpfarm.pods.ensure_sync_pod`` is
idempotent per user, so both share one):

- ``custom``: writes a few files directly on the farm volume via the sync
  pod's ``/exec`` (no need for the upload node), then downloads them with
  three separate ``runpodfarmdownload`` node instances pointed at the same
  remote/local pair, proving:
    1. a normal multi-file custom download (``overwrite=always``);
    2. ``overwrite=never`` leaves an already-downloaded, now-stale local
       file alone even though the remote copy has since changed;
    3. ``overwrite=newer`` picks up that same remote change once told to.
  Local file contents AND mtimes are checked after each stage.
- ``outputs``: cooks a tiny real graph on ``runpodfarm_scheduler`` (reusing
  ``scripts/smoke_scheduler_headless.py``'s ``build_graph``/``cook`` with
  ``--output-item``, so one work item declares a farm-side output via
  ``pdgcmd.addOutputFile``), with the scheduler's own auto-download
  (``rpfarm_downloadoutputs``) turned OFF so only the download node itself
  proves the outputs -> local path -- via ``rpfarm_pathmap``/
  ``localize_via_pathmap`` -- rather than reusing a file the scheduler
  already pulled down.

Both runs cook with ``block=False`` and poll from this thread, timestamping
every work item's state -- overlapping states across items is direct
evidence of genuine out-of-process, non-blocking dispatch (same method
``scripts/smoke_upload_headless.py`` uses for the upload node).

Run it with Houdini's ``hython``, not a system python::

    RPFARM_ROOT=$PWD \\
    /Applications/Houdini/Houdini22.0.368/Frameworks/Houdini.framework/\\
Versions/Current/Resources/bin/hython scripts/smoke_download_headless.py

Every pod this script touches (sync pod included) is terminated before
exit, on every code path -- this script does not leave the farm's normal
"sync pod idles" behaviour in place; see ``cleanup_all_pods``.

Exit status is 0 only when every check in every requested mode passed and
the account has zero pods left afterward.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sys
import tempfile
import time

import hou

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import smoke_scheduler_headless as sched_smoke  # noqa: E402  (reuse build_graph/cook/report_items)


def _bootstrap_rpfarm():
    """Put ``rpfarm`` on ``sys.path`` -- ``$RPFARM_ROOT`` if set (how these
    scripts are meant to be run), else this checkout, found from this file."""
    root = os.environ.get("RPFARM_ROOT") or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)


_bootstrap_rpfarm()

from rpfarm import smoke as rpsmoke  # noqa: E402  (needs the path above first)

DEFAULT_TIMEOUT = 900

log = rpsmoke.make_log("smoke-download")


def parse_args(argv):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", choices=["custom", "outputs", "both"], default="both")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="wall-clock guard per cook, seconds")
    ap.add_argument("--keep", action="store_true", help="don't delete local temp dirs afterward")
    return ap.parse_args(argv)


def build_node(topnet, name, input_node=None):
    node = topnet.createNode("runpodfarmdownload", name)
    if input_node is not None:
        node.setInput(0, input_node)
    return node


def cook(node, timeout, prove_nonblocking=True):
    """Cook one runpodfarmdownload node to completion, guarded by a wall clock.

    Mirrors ``scripts/smoke_upload_headless.py``'s ``cook``: with
    ``prove_nonblocking``, cooks with ``block=False`` and polls from this
    same thread, timestamping every work item's state transition -- direct
    evidence of out-of-process, non-blocking dispatch (Ruling R22).
    """
    return rpsmoke.cook_node(
        node, timeout, log,
        non_blocking=prove_nonblocking,
        on_poll=rpsmoke.state_tracker(node, log) if prove_nonblocking else None,
        extra_nodes=[node.node("pythonprocessor1")],
    )


def report_items(node):
    """Print every work item's final state + rpfarm attributes. Returns (succeeded, total, items)."""
    return rpsmoke.report_items(node, log)


def sync_client_and_sftp(cfg, timeout=180):
    pod, client = rpsmoke.sync_pod_client(cfg, timeout=timeout)
    return client, pod


def sync_exec(cfg, command, timeout_s=60):
    return rpsmoke.sync_exec(cfg, command, timeout_s=timeout_s)


# -- custom mode ----------------------------------------------------------------


def run_custom(topnet, cfg, timeout, keep):
    remote_dir = "/workspace/.rpfarm/smoke_download_custom_{}".format(int(time.time()))
    local_dir = tempfile.mkdtemp(prefix="rpfarm-smoke-download-custom-")

    log("[custom] remote dir = {}".format(remote_dir))
    log("[custom] local dir = {}".format(local_dir))

    setup = sync_exec(
        cfg,
        "mkdir -p {d} && printf 'v1-a' > {d}/a.txt && printf 'v1-b' > {d}/b.txt".format(d=shlex.quote(remote_dir)),
    )
    log("[custom] setup exit={} stderr={!r}".format(setup.get("exit_code"), setup.get("stderr", "")))
    if setup.get("exit_code") != 0:
        log("[custom] RESULT: FAIL -- could not seed remote files")
        return False

    checks_ok = True

    # -- stage 1: overwrite=always, baseline multi-file download -------------
    dl1 = build_node(topnet, "dl_custom_always")
    dl1.parm("rpfarm_mode").set("custom")
    dl1.parm("rpfarm_overwrite").set("always")
    dl1.parm("rpfarm_custom").set(1)
    dl1.parm("rpfarm_remote1").set(remote_dir)
    dl1.parm("rpfarm_local1").set(local_dir)

    elapsed1, aborted1 = cook(dl1, timeout)
    succeeded1, total1, items1 = report_items(dl1)

    a_path = os.path.join(local_dir, "a.txt")
    b_path = os.path.join(local_dir, "b.txt")
    stage1_ok = (
        not aborted1 and total1 > 0 and succeeded1 == total1
        and os.path.exists(a_path) and os.path.exists(b_path)
        and open(a_path).read() == "v1-a" and open(b_path).read() == "v1-b"
    )
    a_mtime_1 = os.path.getmtime(a_path) if os.path.exists(a_path) else None
    log("[custom] stage1 (always, baseline): {} -- a.txt={!r} mtime={}, {:.0f}s".format(
        "PASS" if stage1_ok else "FAIL",
        open(a_path).read() if os.path.exists(a_path) else None, a_mtime_1, elapsed1))
    checks_ok = checks_ok and stage1_ok

    # -- update the remote file, with an explicit far-future mtime so "newer" -
    #    is unambiguous regardless of clock skew between this machine and the pod.
    future_epoch = int(time.time()) + 3600
    update = sync_exec(
        cfg,
        "printf 'v2-a' > {f} && touch -d @{ts} {f}".format(f=shlex.quote(os.path.join(remote_dir, "a.txt")), ts=future_epoch),
    )
    log("[custom] remote update exit={} stderr={!r}".format(update.get("exit_code"), update.get("stderr", "")))
    checks_ok = checks_ok and update.get("exit_code") == 0

    # -- stage 2: overwrite=never must leave the stale local a.txt alone ------
    dl2 = build_node(topnet, "dl_custom_never")
    dl2.parm("rpfarm_mode").set("custom")
    dl2.parm("rpfarm_overwrite").set("never")
    dl2.parm("rpfarm_custom").set(1)
    dl2.parm("rpfarm_remote1").set(remote_dir)
    dl2.parm("rpfarm_local1").set(local_dir)

    elapsed2, aborted2 = cook(dl2, timeout)
    succeeded2, total2, items2 = report_items(dl2)

    a_content_2 = open(a_path).read() if os.path.exists(a_path) else None
    a_mtime_2 = os.path.getmtime(a_path) if os.path.exists(a_path) else None
    stage2_ok = not aborted2 and total2 > 0 and succeeded2 == total2 and a_content_2 == "v1-a" and a_mtime_2 == a_mtime_1
    log("[custom] stage2 (never, stale remote change must be skipped): {} -- a.txt={!r} mtime={} (unchanged={}), {:.0f}s".format(
        "PASS" if stage2_ok else "FAIL", a_content_2, a_mtime_2, a_mtime_2 == a_mtime_1, elapsed2))
    checks_ok = checks_ok and stage2_ok

    # -- stage 3: overwrite=newer must now pick up the remote change ---------
    dl3 = build_node(topnet, "dl_custom_newer")
    dl3.parm("rpfarm_mode").set("custom")
    dl3.parm("rpfarm_overwrite").set("newer")
    dl3.parm("rpfarm_custom").set(1)
    dl3.parm("rpfarm_remote1").set(remote_dir)
    dl3.parm("rpfarm_local1").set(local_dir)

    elapsed3, aborted3 = cook(dl3, timeout)
    succeeded3, total3, items3 = report_items(dl3)

    a_content_3 = open(a_path).read() if os.path.exists(a_path) else None
    a_mtime_3 = os.path.getmtime(a_path) if os.path.exists(a_path) else None
    stage3_ok = (
        not aborted3 and total3 > 0 and succeeded3 == total3
        and a_content_3 == "v2-a" and a_mtime_3 is not None and a_mtime_3 > a_mtime_2
    )
    log("[custom] stage3 (newer, remote change must now apply): {} -- a.txt={!r} mtime={} (advanced={}), {:.0f}s".format(
        "PASS" if stage3_ok else "FAIL", a_content_3, a_mtime_3,
        a_mtime_3 is not None and a_mtime_3 > a_mtime_2, elapsed3))
    checks_ok = checks_ok and stage3_ok

    sync_exec(cfg, "rm -rf {}".format(shlex.quote(remote_dir)))
    if not keep:
        shutil.rmtree(local_dir, ignore_errors=True)

    log("[custom] RESULT: {}".format("PASS" if checks_ok else "FAIL"))
    return checks_ok


# -- outputs mode -----------------------------------------------------------------


OUTPUTS_MULTI_DIR = "smoke_out_multi"
OUTPUTS_MULTI_ITEMS = 3


def build_multi_output_graph(job, n_items, sleep, gpus, maxcost, minpods, maxpods, slots, idletimeout):
    """A genericgenerator where EVERY item declares its own farm-side output
    file via pdgcmd.addOutputFile -- unlike scripts/smoke_scheduler_headless.py's
    own --output-item (exactly one index), this is what proves the download
    node handles a real multi-item farm cook: >=3 upstream items, each with
    its own resultData, downloaded by ONE runpodfarmdownload cook with no
    item dropped, no duplicate, and no work-item name collision.
    """
    hou.putenv("JOB", job)
    log("$JOB = {}".format(job))

    topnet = hou.node("/obj").createNode("topnet", "topnet_multi")
    sched = topnet.createNode("runpodfarmscheduler", "rpfarm")
    sched.parm("rpfarm_gpulist").set(gpus)
    sched.parm("rpfarm_maxcost").set(maxcost)
    sched.parm("rpfarm_minpods").set(minpods)
    sched.parm("rpfarm_maxpods").set(maxpods)
    sched.parm("rpfarm_slots").set(slots)
    sched.parm("rpfarm_idletimeout").set(idletimeout)
    sched.parm("rpfarm_verbose").set(1)
    sched.parm("rpfarm_project").set("smoke")
    # Only the download node should prove this path -- not the scheduler's
    # own auto-download (which would otherwise write the exact same local
    # files via localizePath before the download node ever ran).
    sched.parm("rpfarm_downloadoutputs").set(0)

    gen = topnet.createNode("genericgenerator", "gen")
    gen.parm("itemcount").set(n_items)
    out = '\\$PDG_DIR/{}/\\$PDG_ITEM_NAME.txt'.format(OUTPUTS_MULTI_DIR)
    report = (
        '__PDG_PYTHON__ -c \'import os, sys; '
        'sys.path.insert(0, os.environ["PDG_SCRIPTDIR"]); '
        'import pdgcmd; pdgcmd.addOutputFile(sys.argv[1])\' "{}"'.format(out))
    cmd = (
        'mkdir -p "\\$PDG_DIR/{}" && echo "written by \\$PDG_ITEM_NAME" > "{}" && {}; sleep {}'
        .format(OUTPUTS_MULTI_DIR, out, report, sleep)
    )
    gen.parm("pdg_command").set(cmd)
    gen.parm("shellcommand").set(1)
    topnet.parm("topscheduler").set(sched.path())
    gen.setDisplayFlag(True)
    log("graph: {} -> {} ({} item(s), each declaring its own output)".format(gen.path(), sched.path(), n_items))
    return topnet, gen, sched


def run_outputs(cfg, timeout, keep):
    job = tempfile.mkdtemp(prefix="rpfarm-smoke-download-outputs-")
    topnet, gen, sched = build_multi_output_graph(
        job, OUTPUTS_MULTI_ITEMS, sleep=3, gpus=sched_smoke.DEFAULT_GPUS,
        maxcost=1.0, minpods=1, maxpods=1, slots=OUTPUTS_MULTI_ITEMS, idletimeout=90,
    )
    log("[outputs] $JOB = {}".format(job))

    # IMPORTANT: wire dl_outputs downstream of gen and cook dl_outputs
    # ONCE, in a single cookWorkItems() call -- gen has not cooked yet at
    # this point. A separate gen.cookWorkItems() first, then a second
    # dl.cookWorkItems() afterward, each starts its own independent PDG
    # cook of the whole topnet: the second call does not treat the first
    # call's already-succeeded items as up to date, so it recooks gen from
    # scratch too -- a second real GPU pod, discovered live while building
    # this test (see the Task 10 report). One cook of the downstream-most
    # node is the correct, and the intended production, pattern: this is
    # exactly why the download/upload nodes override their OWN internal
    # scheduler to localscheduler while sitting in a topnet whose top-level
    # scheduler is runpodfarmscheduler -- one graph cook, gen's item on the
    # farm, the download item locally, one GPU pod total.
    dl = build_node(topnet, "dl_outputs", input_node=gen)
    dl.parm("rpfarm_mode").set("outputs")
    dl.parm("rpfarm_overwrite").set("always")

    dl_elapsed, dl_aborted = cook(dl, timeout)

    gen_succeeded, gen_total = sched_smoke.report_items(gen)
    log("[outputs] scheduler item(s) (cooked as a dependency of dl_outputs): {}/{}".format(gen_succeeded, gen_total))
    if sched.parm("rpfarm_status_text"):
        log("[outputs] status text:\n" + sched.parm("rpfarm_status_text").evalAsString())

    dl_succeeded, dl_total, dl_items = report_items(dl)

    # Work-item name collision check (the Task 10 fix under review): with
    # >=3 upstream items, a stale/restarting per-call counter would have
    # produced duplicate "download_000"-style names across separate
    # onGenerate invocations. Names are now derived from the parent
    # upstream item, so they must all be distinct.
    dl_names = [item.name for item in dl_items]
    unique_names_ok = len(dl_names) == len(set(dl_names))
    log("[outputs] download work item names: {} (unique={})".format(dl_names, unique_names_ok))

    gen_pdg_node = gen.getPDGNode()
    gen_items = list(gen_pdg_node.workItems) if gen_pdg_node else []
    expected_upstream_names = {item.name for item in gen_items}

    all_remotes = []
    content_ok = True
    for item in dl_items:
        try:
            it = json.loads(item.stringAttribValue("rpfarm_item"))
        except Exception as e:
            log("[outputs] could not parse rpfarm_item for {}: {}".format(item.name, e))
            content_ok = False
            continue
        for local, remote, _size in it.get("files", []):
            all_remotes.append(remote)
            log("[outputs] item {} file: local={} remote={}".format(item.name, local, remote))
            if os.path.exists(local):
                with open(local) as f:
                    content = f.read().strip()
                log("[outputs]   local file exists, content={!r}".format(content))
                if not content.startswith("written by"):
                    content_ok = False
            else:
                log("[outputs]   MISSING local file {}".format(local))
                content_ok = False

    # Every upstream item's output downloaded exactly once: as many
    # distinct remote files as upstream items, and no remote seen twice
    # (which would mean two download work items both claimed the same
    # output -- a dropped/duplicated planning bug, not just a naming one).
    no_duplicates_ok = len(all_remotes) == len(set(all_remotes))
    complete_ok = (
        len(set(all_remotes)) == len(expected_upstream_names) == OUTPUTS_MULTI_ITEMS
    )
    log(
        "[outputs] {} remote file(s) downloaded for {} upstream item(s) (expected {}); "
        "no_duplicates={}, complete={}".format(
            len(all_remotes), len(expected_upstream_names), OUTPUTS_MULTI_ITEMS,
            no_duplicates_ok, complete_ok
        )
    )

    checks_ok = (
        not dl_aborted and dl_total > 0 and dl_succeeded == dl_total
        and gen_total > 0 and gen_succeeded == gen_total
        and unique_names_ok and no_duplicates_ok and complete_ok and content_ok
    )
    log("[outputs] download node: {} -- {}/{} work items, {:.0f}s".format(
        "PASS" if checks_ok else "FAIL", dl_succeeded, dl_total, dl_elapsed))

    if not keep:
        shutil.rmtree(job, ignore_errors=True)

    log("[outputs] RESULT: {}".format("PASS" if checks_ok else "FAIL"))
    return checks_ok


# -- pod bookkeeping ----------------------------------------------------------------


def cleanup_all_pods():
    """Terminate every rpfarm pod on the account, sync pod included.

    Unlike production use (where the sync pod is left idling for reuse),
    this smoke test must leave the account exactly as it found it: zero
    pods. Runs on every exit path (see main()'s finally).
    """
    return rpsmoke.terminate_all_pods(log)


def main(argv):
    args = parse_args(argv)

    from rpfarm import config as rpcfg

    cfg = rpcfg.load()

    topnet = hou.node("/obj").createNode("topnet", "topnet_download_smoke")

    results = {}
    try:
        if args.mode in ("custom", "both"):
            results["custom"] = run_custom(topnet, cfg, args.timeout, args.keep)
        if args.mode in ("outputs", "both"):
            results["outputs"] = run_outputs(cfg, args.timeout, args.keep)
    finally:
        remaining = cleanup_all_pods()

    log("SUMMARY: {}".format(results))
    ok = bool(results) and all(results.values()) and not remaining
    log("OVERALL: {} (pods remaining: {})".format("PASS" if ok else "FAIL", len(remaining)))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
