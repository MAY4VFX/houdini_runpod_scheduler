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
import threading
import time

import hou

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import smoke_scheduler_headless as sched_smoke  # noqa: E402  (reuse build_graph/cook/report_items)

DEFAULT_TIMEOUT = 900


def log(message):
    print("[smoke-download] {}".format(message), flush=True)


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
    ctx = node.parent().getPDGGraphContext()
    expired = threading.Event()

    def guard():
        expired.set()
        log("TIMEOUT after {}s -- cancelling the cook".format(timeout))
        try:
            ctx.cancelCook()
        except Exception as e:
            log("cancelCook failed: {}".format(e))

    watchdog = threading.Timer(timeout, guard)
    watchdog.daemon = True
    watchdog.start()

    started = time.time()
    log("cooking {} (guard {}s, {})...".format(
        node.path(), timeout, "non-blocking poll" if prove_nonblocking else "blocking"))
    failed = False
    try:
        if prove_nonblocking:
            node.cookWorkItems(block=False, save_prompt=False)
            last_states = {}
            heartbeats = 0
            while ctx.cooking:
                heartbeats += 1
                log("  main thread alive, still polling (t={:.1f}s, heartbeat #{})".format(
                    time.time() - started, heartbeats))
                pdg_node = node.getPDGNode()
                if pdg_node:
                    for item in pdg_node.workItems:
                        state = str(item.state).rsplit(".", 1)[-1]
                        if last_states.get(item.name) != state:
                            log("    t={:.1f}s  {:<24} -> {}".format(time.time() - started, item.name, state))
                            last_states[item.name] = state
                if expired.is_set():
                    break
                time.sleep(0.5)
        else:
            node.cookWorkItems(block=True, save_prompt=False)
    except hou.OperationFailed as e:
        failed = True
        log("COOK FAILED: {}".format(e))
        for n in (node, node.parent()):
            for err in n.errors():
                log("  {} error: {}".format(n.path(), err))
    finally:
        watchdog.cancel()
    elapsed = time.time() - started
    log("cook returned after {:.0f}s".format(elapsed))
    watch_nodes = [node, node.parent()]
    pp = node.node("pythonprocessor1")
    if pp is not None:
        watch_nodes.append(pp)
    for n in watch_nodes:
        for err in n.errors():
            log("  {} error: {}".format(n.path(), err))
        for warn in n.warnings():
            log("  {} warning: {}".format(n.path(), warn))
    return elapsed, expired.is_set() or failed


def report_items(node):
    """Print every work item's final state + rpfarm attributes. Returns (succeeded, total, items)."""
    pdg_node = node.getPDGNode()
    items = list(pdg_node.workItems) if pdg_node else []
    succeeded = 0
    log("work items ({}):".format(len(items)))
    for item in items:
        state = str(item.state).rsplit(".", 1)[-1]
        ok = state.lower() in ("cookedsuccess", "success")
        succeeded += 1 if ok else 0
        bytes_ = files_ = seconds_ = mbps_ = 0
        try:
            bytes_ = item.intAttribValue("bytes") or 0
            files_ = item.intAttribValue("files") or 0
            seconds_ = item.floatAttribValue("seconds") or 0.0
            mbps_ = item.floatAttribValue("mbps") or 0.0
        except Exception as e:
            log("    (attrib read failed for {}: {})".format(item.name, e))
        log(
            "  {:<16} {:<14} bytes={:<8} files={:<3} seconds={:<7.2f} mbps={:<7.3f}".format(
                item.name, state, bytes_, files_, seconds_, mbps_
            )
        )
        try:
            for line in str(item.logMessages).splitlines():
                if not ok or "pid=" in line:
                    log("    log: {}".format(line))
        except Exception as e:
            log("    (logMessages read failed: {})".format(e))
        try:
            uri = item.logURI
            path = uri[len("file://"):] if uri.startswith("file://") else uri
            if path and os.path.exists(path):
                with open(path) as f:
                    for line in f.read().splitlines():
                        if not ok or "pid=" in line:
                            log("    logfile: {}".format(line))
        except Exception as e:
            log("    (logURI read failed: {})".format(e))
    return succeeded, len(items), items


def sync_client_and_sftp(cfg, timeout=180):
    from rpfarm import config as rpcfg
    from rpfarm import pods as rppods
    from rpfarm import sync as rpsync
    from rpfarm.runpod_api import RunPodAPI
    from rpfarm.worker_client import WorkerClient

    api = RunPodAPI(cfg.api_key)
    token = rpcfg.session_token()
    pod = rppods.ensure_sync_pod(api, cfg, token, open(cfg.ssh_key_path + ".pub").read(), timeout=timeout)
    client = WorkerClient(pod["id"], token)
    return client, pod


def sync_exec(cfg, command, timeout_s=60):
    client, _pod = sync_client_and_sftp(cfg)
    result = client.exec(command, timeout_s=timeout_s)
    return result


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


def run_outputs(cfg, timeout, keep):
    sched_args = sched_smoke.parse_args([
        "--items", "1",
        "--output-item", "0",
        "--sleep", "3",
        "--maxcost", "1.0",
        "--minpods", "1",
        "--maxpods", "1",
        "--slots", "1",
        "--idletimeout", "90",
        "--timeout", str(timeout),
    ])
    topnet, gen, sched, job = sched_smoke.build_graph(sched_args)
    log("[outputs] $JOB = {}".format(job))

    # Only the download node should prove this path -- not the scheduler's
    # own auto-download (which would otherwise write the exact same local
    # file via localizePath before the download node ever ran).
    sched.parm("rpfarm_downloadoutputs").set(0)

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

    checks_ok = (
        not dl_aborted and dl_total > 0 and dl_succeeded == dl_total
        and gen_total > 0 and gen_succeeded == gen_total
    )
    found_content = None
    found_path = None
    for item in dl_items:
        try:
            it = json.loads(item.stringAttribValue("rpfarm_item"))
        except Exception as e:
            log("[outputs] could not parse rpfarm_item for {}: {}".format(item.name, e))
            continue
        for local, remote, _size in it.get("files", []):
            log("[outputs] item {} file: local={} remote={}".format(item.name, local, remote))
            if os.path.exists(local):
                with open(local) as f:
                    content = f.read().strip()
                if found_content is None:
                    found_content = content
                    found_path = local
                log("[outputs]   local file exists, content={!r}".format(content))
            else:
                log("[outputs]   MISSING local file {}".format(local))
                checks_ok = False

    checks_ok = checks_ok and found_content is not None and found_content.startswith("written by")
    log("[outputs] download node: {} -- {}/{} items, path={}, content={!r}, {:.0f}s".format(
        "PASS" if checks_ok else "FAIL", dl_succeeded, dl_total, found_path, found_content, dl_elapsed))

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
    from rpfarm import config as rpcfg
    from rpfarm.runpod_api import RunPodAPI

    cfg = rpcfg.load()
    api = RunPodAPI(cfg.api_key)
    alive = api.list_pods("rpfarm-")
    log("terminating {} pod(s) before exit: {}".format(
        len(alive), [(p.get("id"), p.get("name")) for p in alive]))
    for pod in alive:
        try:
            api.terminate_pod(pod["id"])
            log("  terminated {} ({})".format(pod.get("id"), pod.get("name")))
        except Exception as e:
            log("  FAILED to terminate {} ({}): {}".format(pod.get("id"), pod.get("name"), e))
    if alive:
        time.sleep(5)
    remaining = api.list_pods("rpfarm-")
    log("pods remaining after cleanup: {}".format(remaining))
    return remaining


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
