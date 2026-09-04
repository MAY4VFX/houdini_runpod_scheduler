"""End-to-end smoke test for the ``runpodfarm_upload`` TOP node, with no GUI.

Builds a throwaway ``runpodfarmupload`` node under ``/obj/topnet1`` in both
its modes and cooks each to completion against the real sync pod:

- ``custom``: a temp ``$JOB`` with two directories of small files plus a
  standalone "fake .hip" file, uploaded via three Custom Paths entries, plus
  a Post-command whose marker file proves it ran (once, after every
  package -- see Ruling R3 in the node's Help).
- ``deps``: a tiny real ``.hip`` (saved with ``hou.hipFile.save()``) with one
  SOP file reference, uploaded via ``rpfarm.deps.collect_refs``.

Both runs share one sync pod (``rpfarm.pods.ensure_sync_pod`` is idempotent
per user). After cooking, each remote path is verified with
``WorkerClient.exec("ls -la ...")`` against the sync pod.

Run it with Houdini's ``hython``, not a system python::

    RPFARM_ROOT=$PWD \\
    /Applications/Houdini/Houdini22.0.368/Frameworks/Houdini.framework/\\
Versions/Current/Resources/bin/hython scripts/smoke_upload_headless.py

Exit status is 0 only when every work item in both cooks succeeded and
every remote `ls` check found what it expected.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
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

DEFAULT_TIMEOUT = 900

log = rpsmoke.make_log("smoke-upload")


def parse_args(argv):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", choices=["custom", "deps", "both"], default="both")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="wall-clock guard per cook, seconds")
    ap.add_argument("--keep", action="store_true", help="don't delete the local temp dirs afterward")
    return ap.parse_args(argv)


def build_node(topnet, name):
    return topnet.createNode("runpodfarmupload", name)


def cook(node, timeout, prove_nonblocking=False):
    """Cook one runpodfarmupload node to completion, guarded by a wall clock.

    When ``prove_nonblocking`` is set, cooks with ``block=False`` and polls
    from this same thread instead -- if Houdini's main thread were actually
    stuck inside a blocking call (the old in-process default), this poll
    loop could not run at all. Each poll also timestamps every work item's
    current state, so overlapping start/finish times across items are
    direct evidence of out-of-process *parallel* dispatch, not just
    non-blocking.
    """
    return rpsmoke.cook_node(
        node, timeout, log,
        non_blocking=prove_nonblocking,
        on_poll=rpsmoke.state_tracker(node, log) if prove_nonblocking else None,
        extra_nodes=[node.node("pythonprocessor1")],
    )


def report_items(node):
    """Print every work item's final state + rpfarm attributes. Returns (succeeded, total)."""
    succeeded, total, _items = rpsmoke.report_items(node, log, string_attribs=("rpfarm_role",))
    return succeeded, total


def remote_ls(cfg, path):
    return rpsmoke.sync_exec(cfg, "ls -la {}".format(path), timeout_s=30)


def make_custom_job(job_dir):
    """Two dirs of small files + a standalone 'fake .hip', per the task brief."""
    dir_a = os.path.join(job_dir, "dirA")
    dir_b = os.path.join(job_dir, "dirB")
    os.makedirs(dir_a)
    os.makedirs(dir_b)
    for i in range(4):
        with open(os.path.join(dir_a, "a{}.bin".format(i)), "wb") as f:
            f.write(os.urandom(64) * (i + 1))
    for i in range(4):
        with open(os.path.join(dir_b, "b{}.bin".format(i)), "wb") as f:
            f.write(os.urandom(64) * (i + 1))
    fake_hip = os.path.join(job_dir, "fake_scene.hip")
    with open(fake_hip, "wb") as f:
        f.write(b"not a real hip, just a placeholder file for the smoke test\n")
    return dir_a, dir_b, fake_hip


def run_custom(topnet, cfg, timeout, keep):
    job_dir = tempfile.mkdtemp(prefix="rpfarm-smoke-custom-")
    dir_a, dir_b, fake_hip = make_custom_job(job_dir)
    project = "smoke-upload-custom-{}".format(int(time.time()))
    remote_project = "/workspace/projects/{}/{}".format(cfg.user, project)
    marker = "/workspace/.rpfarm/smoke_post_marker_{}".format(int(time.time()))

    hou.putenv("JOB", job_dir)
    log("[custom] $JOB = {}".format(job_dir))
    log("[custom] remote project = {}".format(remote_project))

    node = build_node(topnet, "upload_custom")
    node.parm("rpfarm_mode").set("custom")
    node.parm("rpfarm_project").set(project)
    node.parm("rpfarm_compress").set("off")
    node.parm("rpfarm_postcmd").set("mkdir -p /workspace/.rpfarm && echo POST_OK > {}".format(marker))
    node.parm("rpfarm_custom").set(3)
    node.parm("rpfarm_local1").set(dir_a)
    node.parm("rpfarm_remote1").set(remote_project + "/dirA")
    node.parm("rpfarm_local2").set(dir_b)
    node.parm("rpfarm_remote2").set(remote_project + "/dirB")
    node.parm("rpfarm_local3").set(fake_hip)
    node.parm("rpfarm_remote3").set(remote_project + "/fake_scene.hip")

    elapsed, aborted = cook(node, timeout, prove_nonblocking=True)
    succeeded, total = report_items(node)

    checks_ok = True
    for remote_dir, expect_n in ((remote_project + "/dirA", 4), (remote_project + "/dirB", 4)):
        result = remote_ls(cfg, remote_dir)
        log("[custom] ls {} (exit={}):\n{}".format(remote_dir, result.get("exit_code"), result.get("stdout", "")))
        if result.get("exit_code") != 0 or result.get("stdout", "").count(".bin") < expect_n:
            checks_ok = False

    hip_ls = remote_ls(cfg, remote_project + "/fake_scene.hip")
    log("[custom] ls {} (exit={}):\n{}".format(remote_project + "/fake_scene.hip", hip_ls.get("exit_code"), hip_ls.get("stdout", "")))
    checks_ok = checks_ok and hip_ls.get("exit_code") == 0

    marker_result = remote_ls(cfg, marker)
    log("[custom] post-command marker {} (exit={}): {!r}".format(marker, marker_result.get("exit_code"), marker_result.get("stdout", "")))
    checks_ok = checks_ok and marker_result.get("exit_code") == 0

    if not keep:
        shutil.rmtree(job_dir, ignore_errors=True)

    ok = not aborted and total > 0 and succeeded == total and checks_ok
    log("[custom] RESULT: {} -- {}/{} items, remote checks {}, {:.0f}s".format(
        "PASS" if ok else "FAIL", succeeded, total, "OK" if checks_ok else "FAILED", elapsed))
    return ok


def make_deps_job(job_dir):
    """A tiny real .hip (hou.hipFile.save()) with one SOP file reference."""
    tex_dir = os.path.join(job_dir, "tex")
    os.makedirs(tex_dir)
    tex_path = os.path.join(tex_dir, "tex.exr")
    with open(tex_path, "wb") as f:
        f.write(b"not a real exr, just a referenced file for collect_refs()\n")

    geo = hou.node("/obj").createNode("geo", "smoke_geo")
    file_sop = geo.createNode("file", "file1")
    file_sop.parm("file").set(tex_path)

    hip_path = os.path.join(job_dir, "smoke_scene.hip")
    hou.hipFile.save(hip_path)
    return hip_path, tex_path


def run_deps(topnet, cfg, timeout, keep):
    job_dir = tempfile.mkdtemp(prefix="rpfarm-smoke-deps-")
    hip_path, tex_path = make_deps_job(job_dir)
    project = "smoke-upload-deps-{}".format(int(time.time()))
    remote_project = "/workspace/projects/{}/{}".format(cfg.user, project)

    hou.putenv("JOB", job_dir)
    log("[deps] $JOB = {}".format(job_dir))
    log("[deps] hip = {}".format(hip_path))
    log("[deps] remote project = {}".format(remote_project))

    node = build_node(topnet, "upload_deps")
    node.parm("rpfarm_mode").set("deps")
    node.parm("rpfarm_project").set(project)
    node.parm("rpfarm_compress").set("off")

    elapsed, aborted = cook(node, timeout)
    succeeded, total = report_items(node)

    hip_result = remote_ls(cfg, remote_project + "/smoke_scene.hip")
    log("[deps] ls {} (exit={}):\n{}".format(remote_project + "/smoke_scene.hip", hip_result.get("exit_code"), hip_result.get("stdout", "")))
    tex_result = remote_ls(cfg, remote_project + "/tex/tex.exr")
    log("[deps] ls {} (exit={}):\n{}".format(remote_project + "/tex/tex.exr", tex_result.get("exit_code"), tex_result.get("stdout", "")))
    checks_ok = hip_result.get("exit_code") == 0 and tex_result.get("exit_code") == 0

    pathmap_file = os.path.join(job_dir, ".rpfarm_pathmap.json")
    pathmap_ok = os.path.exists(pathmap_file)
    if pathmap_ok:
        with open(pathmap_file) as f:
            log("[deps] .rpfarm_pathmap.json: {}".format(json.dumps(json.load(f))))
    else:
        log("[deps] .rpfarm_pathmap.json MISSING at {}".format(pathmap_file))

    if not keep:
        shutil.rmtree(job_dir, ignore_errors=True)

    ok = not aborted and total > 0 and succeeded == total and checks_ok and pathmap_ok
    log("[deps] RESULT: {} -- {}/{} items, remote checks {}, pathmap {}, {:.0f}s".format(
        "PASS" if ok else "FAIL", succeeded, total, "OK" if checks_ok else "FAILED",
        "OK" if pathmap_ok else "MISSING", elapsed))
    return ok


def report_pods():
    return rpsmoke.list_farm_pods(log)


def main(argv):
    args = parse_args(argv)

    from rpfarm import config as rpcfg

    cfg = rpcfg.load()

    topnet = hou.node("/obj").createNode("topnet", "topnet1")

    results = {}
    try:
        if args.mode in ("custom", "both"):
            results["custom"] = run_custom(topnet, cfg, args.timeout, args.keep)
        if args.mode in ("deps", "both"):
            results["deps"] = run_deps(topnet, cfg, args.timeout, args.keep)
    finally:
        report_pods()

    log("SUMMARY: {}".format(results))
    return 0 if results and all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
