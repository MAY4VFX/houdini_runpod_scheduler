"""``rpfarm smoke`` -- one end-to-end proof that the whole farm works.

Two things live here, deliberately in one module:

1. **The shared headless-cook helpers** (:func:`make_log`, :func:`cook_node`,
   :func:`report_items`, :func:`report_ledger`, :func:`list_farm_pods`,
   :func:`terminate_pods`, :func:`sync_pod_client`). Before this module
   they were copy-pasted, with small drifts, across the four
   ``scripts/smoke_*_headless.py``; those scripts now import them from here,
   so there is one wall-clock guard, one work-item report and one pod-cleanup
   path for every live test in the repo.

2. **The ``smoke`` command itself** -- :func:`cmd_smoke` (plain ``python3``)
   and :func:`hython_main` (inside Houdini). It is the only test that runs the
   real production graph: upload the scene's dependencies, cook it on a GPU
   pod, get the frames back in the ROP's own local paths, write the ledger,
   and leave the account with no pods running.

Why two processes. Everything that has to touch ``hou`` -- loading the
fixture, cooking the TOP graph -- runs under Houdini's ``hython``. Everything
else -- picking the Houdini install, watching RunPod, checking the files that
came back, terminating pods -- runs in the plain ``python3`` parent, which is
also what guarantees the cleanup: a ``hython`` that hangs or dies still leaves
the parent alive to kill the pods (see :func:`cmd_smoke`'s ``finally``).

The stage table is measured, not narrated: :class:`StageTimer` polls every
work item's state from the cooking thread and records when each node's items
first start and last finish, so "farm ready" really is the wall-clock gap
between the last upload package landing and the first work item starting on a
GPU pod. The cook's cost comes from the scheduler's own ledger record, not
from a guess here.

Usage::

    python3 -m rpfarm smoke [--gpu "NVIDIA RTX A4500"] [--keep] [--timeout 1800]

Exit status is 0 only when every stage passed, every expected file came back
newly written, the ledger recorded every task with exit code 0, and none of
*this user's* pods are left. The account is shared by several artists, so the
cleanup only ever touches ``cfg.user``'s own pods unless ``--everyone`` says
otherwise.
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
from pathlib import Path

from . import config as rpcfg
from . import houdini_local
from . import pods as rppods
from .cli import _fmt_bytes, _fmt_duration, _fmt_table
from .runpod_api import RunPodAPI
from .worker_client import WorkerClient

# ---------------------------------------------------------------------------
# Shared helpers for every headless live test in this repo
# ---------------------------------------------------------------------------

# Work-item states that mean "finished, one way or the other".
_TERMINAL_STATES = ("cookedsuccess", "success", "cookedfail", "failed", "cookedcancel", "canceled", "cancelled")
_SUCCESS_STATES = ("cookedsuccess", "success")
# Only "Cooking". An item is "Waiting" from the moment the graph is planned and
# "Scheduled" as soon as its dependencies are met -- neither means a pod has it,
# and counting them made the farm-ready stage read as a negative duration in one
# live run and 0.0s in the next. "Cooking" is set when a pod accepted the task.
_RUNNING_STATES = ("cooking",)


def make_log(prefix):
    """A ``log(message)`` that prefixes and flushes -- flushing matters: this
    output is read live through a pipe by :func:`cmd_smoke`."""

    def log(message):
        print("[{}] {}".format(prefix, message), flush=True)

    return log


def item_state(item):
    """``pdg.workItemState.CookedSuccess`` -> ``"CookedSuccess"``."""
    return str(item.state).rsplit(".", 1)[-1]


def node_messages(nodes, log):
    """Print every error and warning on ``nodes`` (missing nodes skipped)."""
    for n in nodes:
        if n is None:
            continue
        for err in n.errors():
            log("  {} error: {}".format(n.path(), err))
        for warn in n.warnings():
            log("  {} warning: {}".format(n.path(), warn))


def cook_node(node, timeout, log, non_blocking=False, on_poll=None,
              poll_interval=0.5, extra_nodes=()):
    """Cook one TOP node to completion under a wall-clock guard.

    Returns ``(elapsed_seconds, aborted)``; ``aborted`` is true if the guard
    fired or PDG raised ``hou.OperationFailed`` (which is how a scheduler that
    refused to start surfaces -- as a bare "Failed to start scheduler", with
    the real reason only on the node, hence :func:`node_messages` afterwards).

    With ``non_blocking`` the cook is started with ``block=False`` and polled
    from this same thread. That is not a stylistic choice: if Houdini's main
    thread were stuck inside a blocking call, this poll loop could not run at
    all, so the poll output is itself the evidence that dispatch is
    out-of-process (Ruling R22). ``on_poll(elapsed)`` is called once per poll.
    """
    import hou

    ctx = node.parent().getPDGGraphContext()
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
    log("cooking {} (guard {}s, {})...".format(
        node.path(), timeout, "non-blocking poll" if non_blocking else "blocking"))
    failed = False
    try:
        if non_blocking:
            node.cookWorkItems(block=False, save_prompt=False)
            while ctx.cooking:
                if on_poll is not None:
                    on_poll(time.time() - started)
                if expired.is_set():
                    break
                time.sleep(poll_interval)
            if on_poll is not None:
                on_poll(time.time() - started)
        else:
            node.cookWorkItems(block=True, save_prompt=False)
    except hou.OperationFailed as e:
        failed = True
        log("COOK FAILED: {}".format(e))
    finally:
        watchdog.cancel()

    elapsed = time.time() - started
    log("cook returned after {:.0f}s".format(elapsed))
    node_messages([node, node.parent()] + list(extra_nodes), log)
    return elapsed, expired.is_set() or failed


def state_tracker(nodes, log, heartbeat_every=0.0):
    """An ``on_poll`` callback for :func:`cook_node` that narrates the cook.

    Logs every work-item state transition it sees, plus a "still polling"
    heartbeat -- on every poll when ``heartbeat_every`` is 0 (what the
    per-node smoke scripts want: the heartbeat *is* the evidence that the
    calling thread is not blocked), or throttled to one line per that many
    seconds for a long cook where a line twice a second would bury the
    transitions.

    ``nodes`` is one node or a ``{label: node}`` mapping.
    """
    if not isinstance(nodes, dict):
        nodes = {nodes.name(): nodes}
    last = {}
    # -inf, not 0: the very first poll must always beat, so a cook that dies
    # early still leaves a line saying the poll loop ever ran.
    counter = {"n": 0, "last_beat": float("-inf")}

    def on_poll(elapsed):
        counter["n"] += 1
        if elapsed - counter["last_beat"] >= heartbeat_every:
            counter["last_beat"] = elapsed
            log("  main thread alive, still polling (t={:.1f}s, heartbeat #{})".format(
                elapsed, counter["n"]))
        for label, node in nodes.items():
            pdg_node = node.getPDGNode()
            if pdg_node is None:
                continue
            for item in pdg_node.workItems:
                state = item_state(item)
                key = (label, item.name)
                if last.get(key) != state:
                    log("    t={:.1f}s  {:<10} {:<24} -> {}".format(
                        elapsed, label, item.name, state))
                    last[key] = state

    return on_poll


def report_items(node, log, attribs=("bytes", "files", "seconds", "mbps"),
                 string_attribs=(), dump_logs_on_failure=True):
    """Print every work item's final state (and attributes) on ``node``.

    Returns ``(succeeded, total, items)``. An attribute that a node does not
    set is simply omitted -- this is a report, not an assertion.
    """
    pdg_node = node.getPDGNode()
    items = list(pdg_node.workItems) if pdg_node else []
    succeeded = 0
    log("work items on {} ({}):".format(node.name(), len(items)))
    for item in items:
        state = item_state(item)
        ok = state.lower() in _SUCCESS_STATES
        succeeded += 1 if ok else 0
        try:
            duration = "{:.1f}s".format(item.cookDuration)
        except Exception:
            duration = "?"
        parts = ["  {:<28} {:<14} {:>8}".format(item.name, state, duration)]
        for name in string_attribs:
            try:
                parts.append("{}={}".format(name, item.stringAttribValue(name) or ""))
            except Exception:
                pass
        for name in attribs:
            try:
                value = item.attribValue(name)
            except Exception:
                continue
            if isinstance(value, float):
                parts.append("{}={:.2f}".format(name, value))
            else:
                parts.append("{}={}".format(name, value))
        log(" ".join(parts))
        if not ok and dump_logs_on_failure:
            _dump_item_log(item, log)
    return succeeded, len(items), items


def _dump_item_log(item, log):
    """Everything we can get about why one work item failed."""
    try:
        for line in str(item.logMessages).splitlines():
            log("    log: {}".format(line))
    except Exception as e:
        log("    (logMessages read failed: {})".format(e))
    try:
        uri = item.logURI or ""
        log("    logURI: {!r}".format(uri))
        path = uri[len("file://"):] if uri.startswith("file://") else uri
        if path and os.path.exists(path):
            with open(path) as f:
                for line in f.read().splitlines():
                    log("    logfile: {}".format(line))
    except Exception as e:
        log("    (logURI read failed: {})".format(e))


def ledger_dir():
    return rpcfg.home() / "ledger"


def report_ledger(since, log):
    """Print the ledger lines written since ``since``. Returns ``(records, paths)``."""
    paths = [p for p in glob.glob(str(ledger_dir() / "*.jsonl"))
             if os.path.getmtime(p) >= since - 1]
    if not paths:
        log("ledger: no file written under {}".format(ledger_dir()))
        return [], []
    records = []
    for path in sorted(paths):
        log("ledger {}:".format(path))
        for record in read_ledger_file(path):
            records.append(record)
            log("  " + json.dumps(record, sort_keys=True))
    return records, sorted(paths)


def read_ledger_file(path):
    """Parse one ``.jsonl`` ledger file, skipping blank/corrupt lines."""
    out = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return out


def _api(cfg=None):
    cfg = cfg or rpcfg.load()
    return cfg, RunPodAPI(cfg.api_key)


def list_farm_pods(log=None, cfg=None):
    """Every ``rpfarm-*`` pod on the account (all users). Never raises."""
    try:
        _cfg, api = _api(cfg)
        pods = api.list_pods("rpfarm-")
    except Exception as e:  # noqa: BLE001 - a listing failure must not mask the real result
        if log:
            log("pod listing failed: {}".format(e))
        return []
    if log:
        log("pods on the account ({}):".format(len(pods)))
        for pod in pods:
            log("  {:<16} {:<32} {}".format(
                pod.get("id", "?"), pod.get("name", "?"), pod.get("desiredStatus", "?")))
    return pods


def own_pods(pods, user):
    """The subset of ``pods`` belonging to ``user``.

    Two shapes, matching ``rpfarm.pods``' own naming: this user's cook pods
    are ``rpfarm-<user>-*``, and the sync pod is exactly
    ``rpfarm-sync-<user>`` -- matched by full name rather than by prefix,
    because ``rpfarm-sync-may`` is itself a prefix of another artist's
    ``rpfarm-sync-mayakovsky`` (the same trap ``pods.ensure_sync_pod``
    documents).
    """
    sync_name = rppods.sync_pod_name(user)
    cook_prefix = "rpfarm-{}-".format(user)
    return [
        p for p in pods
        if p.get("name") == sync_name or (p.get("name") or "").startswith(cook_prefix)
    ]


def terminate_pods(log, cfg=None, settle=5.0, everyone=False):
    """Terminate this user's ``rpfarm-*`` pods, sync pod included, and report
    what is left.

    Scoped to ``cfg.user`` by default and account-wide only with
    ``everyone=True`` -- the same vocabulary ``rpfarm farm kill`` uses, and
    for the same reason (final-review finding 4). The design is one RunPod
    account shared by several trusted artists, so an unconditional sweep of
    the ``rpfarm-`` prefix here means a smoke run kills a colleague's
    in-flight render.

    Live tests must leave the account exactly as *they* found it -- none of
    their own pods running -- unlike production, where the sync pod is
    deliberately left idling for the next cook. Returns the list of
    **in-scope** pods still there afterwards, so another artist's pod can
    never fail this run.
    """
    try:
        cfg, api = _api(cfg)
    except Exception as e:  # noqa: BLE001
        log("CLEANUP FAILED -- could not reach RunPod ({}); check `rpfarm farm status`".format(e))
        return []
    try:
        account = api.list_pods("rpfarm-")
    except Exception as e:  # noqa: BLE001
        log("CLEANUP FAILED -- could not list pods ({}); check `rpfarm farm status`".format(e))
        return []

    alive = account if everyone else own_pods(account, cfg.user)
    others = [p for p in account if p not in alive]
    if others:
        log("leaving {} pod(s) that are not {}'s alone: {}".format(
            len(others), cfg.user, [(p.get("id"), p.get("name")) for p in others]))

    log("terminating {} pod(s) before exit: {}".format(
        len(alive), [(p.get("id"), p.get("name")) for p in alive]))
    for pod in alive:
        try:
            api.terminate_pod(pod["id"])
            log("  terminated {} ({})".format(pod.get("id"), pod.get("name")))
        except Exception as e:  # noqa: BLE001
            log("  FAILED to terminate {} ({}): {} -- IT MAY STILL BE BILLING".format(
                pod.get("id"), pod.get("name"), e))
    if alive and settle:
        time.sleep(settle)
    try:
        account = api.list_pods("rpfarm-")
    except Exception as e:  # noqa: BLE001
        log("could not re-list pods after cleanup ({}) -- check `rpfarm farm status`".format(e))
        return []
    remaining = account if everyone else own_pods(account, cfg.user)
    log("pods remaining after cleanup: {}".format(
        [(p.get("id"), p.get("name")) for p in remaining]))
    return remaining


def sync_pod_client(cfg, timeout=180):
    """``(pod, WorkerClient)`` for this user's sync pod, creating it if needed."""
    _cfg, api = _api(cfg)
    token = rpcfg.session_token()
    with open(cfg.ssh_key_path + ".pub") as f:
        pubkey = f.read()
    pod = rppods.ensure_sync_pod(api, cfg, token, pubkey, timeout=timeout)
    return pod, WorkerClient(pod["id"], token)


def sync_exec(cfg, command, timeout_s=60):
    """Run one short command on the sync pod and return the ``/exec`` result.

    Short is not a suggestion: RunPod's proxy sits behind Cloudflare and cuts
    the response at roughly 100 seconds, so ``WorkerClient.exec`` clamps
    ``timeout_s`` to 90 (spec 3.4). Anything longer needs the detached path.
    """
    _pod, client = sync_pod_client(cfg)
    return client.exec(command, timeout_s=timeout_s)


# ---------------------------------------------------------------------------
# Stage timing
# ---------------------------------------------------------------------------


class StageTimer:
    """Turns polled work-item states into per-node start/finish timestamps.

    :meth:`poll` is cheap enough to call twice a second from the cooking
    thread; it only looks at ``item.state``. For each watched node it keeps
    the first time any item was seen running and the last time an item was
    seen reaching a terminal state, which is exactly what the stage table
    needs and does not depend on parsing anybody's log.
    """

    def __init__(self, nodes):
        self.nodes = dict(nodes)  # label -> hou node
        self.first_running = {}
        self.last_done = {}
        self._done_seen = {}

    def poll(self, _elapsed=None):
        now = time.time()
        for label, node in self.nodes.items():
            pdg_node = node.getPDGNode()
            if pdg_node is None:
                continue
            for item in pdg_node.workItems:
                state = item_state(item).lower()
                key = (label, item.name)
                if state in _RUNNING_STATES and label not in self.first_running:
                    self.first_running[label] = now
                if state in _TERMINAL_STATES and key not in self._done_seen:
                    self._done_seen[key] = now
                    self.last_done[label] = now
                    # An item can finish before any poll ever caught it running
                    # (a sub-poll-interval task); count it as started then too.
                    self.first_running.setdefault(label, now)

    def window(self, label):
        """``(first_running, last_done)`` for one node, either may be None."""
        return self.first_running.get(label), self.last_done.get(label)


# ---------------------------------------------------------------------------
# The smoke test
# ---------------------------------------------------------------------------

FIXTURE_REL = os.path.join("tests", "fixtures", "smoke", "smoke.hip")

# Must match scripts/build_smoke_fixture.py -- that script builds the fixture,
# this one asserts what the fixture produces.
EXPECTED_FRAMES = 3
RENDER_SUBDIR = "render"
PROBE_SUBDIR = "smoke_probe"
TOPNET_PATH = "/obj/topnet1"
NODE_NAMES = ("upload", "probe", "render", "download")

DEFAULT_TIMEOUT = 1800
# Room for hython startup, the scene load and the final report, so the inner
# cook guard always fires before the outer process kill and we get a report.
_HYTHON_MARGIN = 180

RESULT_FILENAME = "smoke_result.json"


def _repo_root():
    return houdini_local.repo_root()


def _pick_houdini(explicit_hfs=None):
    """The Houdini install to cook with: ``--houdini`` if given, else the
    newest one that actually has ``hython``."""
    if explicit_hfs:
        inst = houdini_local.HoudiniInstall(Path(explicit_hfs))
        if inst.hython is None:
            raise RuntimeError("no hython under {}".format(explicit_hfs))
        return inst
    for inst in houdini_local.find_houdini_installations():
        if inst.hython is not None:
            return inst
    raise RuntimeError(
        "no local Houdini installation with hython found -- install Houdini, "
        "then rerun `rpfarm setup`")


def _check_hdas(inst):
    """Missing HDA names for this install, so the failure is 'run setup',
    not 'Invalid node type name' three minutes into a paid cook."""
    otls = inst.user_pref_dir / "otls"
    return [n for n in houdini_local.HDA_NAMES if not (otls / "{}.hda".format(n)).exists()]


class PodWatcher(threading.Thread):
    """Polls ``GET /pods`` while the cook runs, from the *parent* process.

    Two things this gets that the cook itself cannot report: when each pod
    first appeared with a public IP (RunPod's own side of "the pod is up"),
    and the GPU type and hourly rate the scheduler actually got -- which is
    what makes the cost line in the summary checkable against the ledger.
    """

    daemon = True

    def __init__(self, cfg, interval=5.0):
        super().__init__(name="rpfarm-smoke-podwatch")
        self.cfg = cfg
        self.interval = interval
        self.pods = {}  # id -> {name, rate, gpu, first_seen, first_ip}
        self._stop = threading.Event()

    def run(self):
        api = RunPodAPI(self.cfg.api_key)
        while not self._stop.is_set():
            try:
                for p in api.list_pods("rpfarm-"):
                    self._record(p)
            except Exception:  # noqa: BLE001 - a watcher must never fail the run
                pass
            self._stop.wait(self.interval)

    def _record(self, p):
        now = time.time()
        entry = self.pods.setdefault(p["id"], {
            "name": p.get("name", "?"),
            "rate": float(p.get("costPerHr") or 0.0),
            "gpu": "",
            "first_seen": now,
            "first_ip": None,
        })
        entry["rate"] = float(p.get("costPerHr") or entry["rate"])
        # `or {}`, not a default: RunPod sends machine: null for a CPU pod.
        gpu = (p.get("machine") or {}).get("gpuTypeId") or p.get("gpuTypeId") or ""
        if gpu:
            entry["gpu"] = gpu
        if entry["first_ip"] is None and p.get("publicIp"):
            entry["first_ip"] = now

    def stop(self):
        self._stop.set()


def _stage_rows(result, watcher, started):
    """The stage table: label, detail, seconds.

    Two kinds of row. The named stages come from the cook's own work-item
    timeline (:class:`StageTimer`). The ``pod ...`` rows come from
    :class:`PodWatcher` and read differently: their detail is the hourly rate
    and GPU the scheduler actually got, and their time is how long after the
    pod first appeared in ``GET /pods`` it had a public IP -- RunPod's side of
    "the pod is up", which is not the same as the worker answering ``/health``
    (that gap is inside the "farm ready" stage above).
    """
    stages = result.get("stages") or {}
    counts = result.get("counts") or {}
    rows = []

    def add(label, detail, seconds):
        if seconds is None:
            text = "?"
        elif seconds < 10:
            text = "{:.1f}s".format(seconds)
        else:
            text = "{:.0f}s".format(seconds)
        rows.append((label, detail, text))

    add("scene load", os.path.basename(result.get("hip") or ""), stages.get("load"))
    add("upload", "{} item(s), {}".format(
        counts.get("upload", 0), _fmt_bytes(result.get("upload_bytes"))), stages.get("upload"))
    add("farm ready", "sync pod + MQ + GPU pod", stages.get("farm_ready"))
    add("probe", "{} item(s)".format(counts.get("probe", 0)), stages.get("probe"))
    add("render", "{} item(s)".format(counts.get("render", 0)), stages.get("render"))
    add("download", "{} item(s)".format(counts.get("download", 0)), stages.get("download"))

    for entry in sorted(watcher.pods.values(), key=lambda e: e["first_seen"]):
        boot = (entry["first_ip"] - entry["first_seen"]) if entry["first_ip"] else None
        add("pod " + entry["name"],
            "${:.3f}/h{}".format(entry["rate"], " " + entry["gpu"] if entry["gpu"] else ""),
            boot)

    add("total", "wall clock", time.time() - started)
    return rows


def _verify_outputs(run_dir, started, log):
    """The frames and the probe file, back in the paths the graph aimed at.

    ``started`` matters as much as existence: with a fixed project name on the
    volume, a previous run's frames could otherwise make a cook that rendered
    nothing look like a pass.
    """
    ok = True
    render_dir = os.path.join(run_dir, RENDER_SUBDIR)
    frames = sorted(glob.glob(os.path.join(render_dir, "*.exr")))
    log("downloaded frames in {} ({}):".format(render_dir, len(frames)))
    for path in frames:
        st = os.stat(path)
        fresh = st.st_mtime >= started - 1
        log("  {}  {}  {}".format(
            os.path.basename(path), _fmt_bytes(st.st_size),
            "written by this run" if fresh else "STALE (mtime before this run)"))
        if not fresh or st.st_size == 0:
            ok = False
    if len(frames) != EXPECTED_FRAMES:
        log("FAIL: expected {} frame(s), got {}".format(EXPECTED_FRAMES, len(frames)))
        ok = False

    probes = sorted(glob.glob(os.path.join(run_dir, PROBE_SUBDIR, "*.txt")))
    log("probe outputs in {} ({}):".format(os.path.join(run_dir, PROBE_SUBDIR), len(probes)))
    for path in probes:
        with open(path) as f:
            log("  {}  {!r}".format(os.path.basename(path), f.read().strip()))
    if not probes:
        log("FAIL: the probe item's declared output never came back")
        ok = False
    return ok, frames, probes


def _verify_ledger(cook_id, started, log):
    """The cook's own ledger file: one row per task, all exit code 0, plus the
    cook summary that carries the cost. Returns ``(ok, cost, task_rows)``."""
    path = ledger_dir() / "{}.jsonl".format(cook_id) if cook_id else None
    if path is None or not path.exists():
        # Fall back to whatever this run touched, so a missing cook_id (the
        # cook died before onStartCook) still reports something useful.
        candidates = [p for p in glob.glob(str(ledger_dir() / "*.jsonl"))
                      if os.path.getmtime(p) >= started - 1]
        if not candidates:
            log("FAIL: no ledger file written under {}".format(ledger_dir()))
            return False, None, []
        path = candidates[0]
    records = read_ledger_file(str(path))
    log("ledger {} ({} record(s)):".format(path, len(records)))
    for r in records:
        log("  " + json.dumps(r, sort_keys=True))

    tasks = [r for r in records if r.get("record") != "cook_summary"]
    summary = next((r for r in records if r.get("record") == "cook_summary"), None)
    ok = True
    if len(tasks) < EXPECTED_FRAMES:
        log("FAIL: expected at least {} task record(s), got {}".format(EXPECTED_FRAMES, len(tasks)))
        ok = False
    bad = [r for r in tasks if r.get("exit_code") != 0]
    if bad:
        log("FAIL: {} task record(s) with a non-zero exit code".format(len(bad)))
        ok = False
    if summary is None:
        log("FAIL: no cook_summary record -- onStopCook did not finish")
        ok = False
    cost = (summary or {}).get("cost_est")
    return ok, cost, tasks


def _hython_command(inst, repo, payload_path):
    """``hython -m rpfarm.smoke <payload>`` -- ``-m`` with ``PYTHONPATH``, the
    same way ``rpfarm.package_runner`` is launched, so the package is imported
    normally and its relative imports work."""
    return [str(inst.hython), "-m", "rpfarm.smoke", payload_path]


def _child_env(repo, run_dir):
    env = dict(os.environ)
    env["RPFARM_ROOT"] = str(repo)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(repo) + (os.pathsep + existing if existing else "")
    env["JOB"] = run_dir
    # Houdini's own "don't pop anything up / don't phone home" switches: this
    # is a batch process on someone's workstation, not an interactive session.
    env.setdefault("HOUDINI_DISABLE_CONSOLE", "1")
    return env


def cmd_smoke(args):
    """``rpfarm smoke`` -- the plain-python3 half. See the module docstring."""
    log = make_log("smoke")
    started = time.time()

    try:
        cfg = rpcfg.load()
    except rpcfg.ConfigError as e:
        print("error: {}".format(e), file=sys.stderr)
        return 1

    repo = _repo_root()
    fixture = os.path.join(str(repo), FIXTURE_REL)
    if not os.path.isfile(fixture):
        print("error: fixture scene missing at {} -- rebuild it with "
              "`hython scripts/build_smoke_fixture.py`".format(fixture), file=sys.stderr)
        return 1

    try:
        inst = _pick_houdini(args.houdini)
    except RuntimeError as e:
        print("error: {}".format(e), file=sys.stderr)
        return 1
    missing = _check_hdas(inst)
    if missing:
        print("error: Houdini {} is missing HDA(s) {} -- run `rpfarm setup`".format(
            inst.version, ", ".join(missing)), file=sys.stderr)
        return 1
    log("Houdini {} ({})".format(inst.version, inst.hython))

    run_dir = args.workdir or os.path.join(
        str(repo), "tmp", "smoke_{}".format(time.strftime("%Y%m%d-%H%M%S")))
    os.makedirs(run_dir, exist_ok=True)
    hip = os.path.join(run_dir, os.path.basename(fixture))
    shutil.copyfile(fixture, hip)
    log("run directory {}".format(run_dir))

    payload = {
        "hip": hip,
        "job": run_dir,
        "topnet": TOPNET_PATH,
        "nodes": list(NODE_NAMES),
        "gpu": args.gpu or "",
        "timeout": max(60, args.timeout - _HYTHON_MARGIN),
        "result": os.path.join(run_dir, RESULT_FILENAME),
    }
    payload_path = os.path.join(run_dir, "smoke_payload.json")
    with open(payload_path, "w") as f:
        json.dump(payload, f, indent=2)

    watcher = PodWatcher(cfg)
    watcher.start()

    # Set before the try so the report below is printable on every path,
    # including one where _run_hython itself blows up -- the pod cleanup in
    # the finally is the whole reason this function has a try at all, and it
    # must not be followed by a NameError instead of a summary.
    rc = 1
    result = {}
    outputs_ok = ledger_ok = False
    frames = probes = tasks = []
    cost = None
    try:
        rc = _run_hython(inst, repo, run_dir, payload_path, args.timeout, log)
        try:
            with open(payload["result"]) as f:
                result = json.load(f)
        except (OSError, ValueError) as e:
            log("no cook result file ({}) -- the hython stage did not finish".format(e))

        outputs_ok, frames, probes = _verify_outputs(run_dir, started, log)
        ledger_ok, cost, tasks = _verify_ledger(result.get("cook_id"), started, log)
    finally:
        watcher.stop()
        if args.keep:
            log("--keep: leaving the farm as the cook left it (production "
                "behaviour) -- `rpfarm farm kill --everyone` when you are done")
            list_farm_pods(log, cfg)
            remaining = []
        else:
            remaining = terminate_pods(log, cfg, everyone=args.everyone)

    print()
    print(_fmt_table(_stage_rows(result, watcher, started), ["stage", "detail", "time"]))
    print()
    if cost is not None:
        rates = ", ".join(
            "{} ${:.3f}/h".format(e["name"], e["rate"]) for e in watcher.pods.values())
        print("cook cost  ${:.3f}   ({})".format(cost, rates or "no pods seen"))
    print("cook id    {}".format(result.get("cook_id") or "?"))
    print("outputs    {} frame(s), {} probe file(s) in {}".format(
        len(frames), len(probes), run_dir))
    print("ledger     {} task record(s)".format(len(tasks)))

    ok = (rc == 0 and result.get("ok") and outputs_ok and ledger_ok and not remaining)
    if not ok:
        reasons = []
        if rc != 0:
            reasons.append("hython stage exit {}".format(rc))
        if not result.get("ok"):
            reasons.append("cook reported failure")
        if not outputs_ok:
            reasons.append("outputs missing or stale")
        if not ledger_ok:
            reasons.append("ledger incomplete")
        if remaining:
            reasons.append("{} pod(s) still running".format(len(remaining)))
        print("\nFAIL: " + "; ".join(reasons))
        return 1
    print("\nOK: {} frame(s) rendered on the farm and downloaded to the ROP's own "
          "paths in {}".format(len(frames), _fmt_duration(time.time() - started)))
    # Kept either way -- --keep is about the farm, not about this directory.
    print("Run directory kept for inspection: {}".format(run_dir))
    return 0


def _run_hython(inst, repo, run_dir, payload_path, timeout, log):
    """Run the hython half, streaming its output, and kill it past ``timeout``."""
    cmd = _hython_command(inst, repo, payload_path)
    log("$ " + " ".join(cmd))
    proc = subprocess.Popen(
        cmd, cwd=str(repo), env=_child_env(repo, run_dir),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        # Its own process group, so _kill_process can take out the whole tree
        # (hython plus every out-of-process package runner and pod-side poller
        # it started) without also killing this process.
        start_new_session=True,
    )
    deadline = time.time() + timeout
    killer = threading.Timer(timeout, _kill_process, args=(proc, log))
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
            _kill_process(proc, log)
    if time.time() > deadline:
        log("WALL CLOCK GUARD ({}s) expired".format(timeout))
    return proc.returncode


def _kill_process(proc, log):
    """Kill the hython child and everything it spawned.

    ``terminate()`` alone leaves the out-of-process package runners and any
    ``hython`` the local scheduler started -- the same grandchild problem that
    corrupted a Houdini install in Task 14 (spec 3.4).
    """
    log("killing the hython stage")
    try:
        os.killpg(os.getpgid(proc.pid), 15)
    except (OSError, AttributeError):
        try:
            proc.terminate()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# The hython half
# ---------------------------------------------------------------------------


def hython_main(argv):
    """Load the fixture, cook the graph once, write ``smoke_result.json``.

    Cooks the bottom-most node (``download``) and only that node, in a single
    ``cookWorkItems()`` call: cooking an upstream node first and the download
    node afterwards starts two independent PDG cooks and pays for a second GPU
    pod (found live in Task 10, and in both nodes' Help).
    """
    import hou

    log = make_log("smoke-cook")
    if len(argv) != 1:
        print("usage: hython -m rpfarm.smoke <payload.json>", file=sys.stderr)
        return 2
    with open(argv[0]) as f:
        payload = json.load(f)

    result = {"ok": False, "hip": payload["hip"], "stages": {}, "counts": {}}
    try:
        t0 = time.time()
        hou.hipFile.load(payload["hip"], suppress_save_prompt=True, ignore_load_warnings=True)
        result["stages"]["load"] = time.time() - t0
        # AFTER the load, never before: a .hip stores its own variable table
        # and $JOB from it overrides both the environment and an earlier
        # putenv. Setting it first looks like it works and silently leaves
        # $JOB pointing at whatever machine built the fixture -- which then
        # becomes the path map root, the PDG working directory and the place
        # every downloaded output lands (caught in the first live run).
        hou.putenv("JOB", payload["job"])
        log("$JOB = {}".format(hou.text.expandString("$JOB")))
        log("$HIP = {}".format(hou.text.expandString("$HIP")))
        log("loaded {} in {:.1f}s".format(payload["hip"], result["stages"]["load"]))

        topnet = hou.node(payload["topnet"])
        if topnet is None:
            raise RuntimeError("no {} in the fixture".format(payload["topnet"]))
        nodes = {name: topnet.node(name) for name in payload["nodes"]}
        missing = [n for n, node in nodes.items() if node is None]
        if missing:
            raise RuntimeError("fixture is missing node(s): {}".format(", ".join(missing)))

        sched = topnet.node("rpfarm")
        if payload.get("gpu"):
            sched.parm("rpfarm_gpulist").set(payload["gpu"])
            log("GPU priority overridden to {!r}".format(payload["gpu"]))

        timer = StageTimer(nodes)
        narrate = state_tracker(nodes, log, heartbeat_every=30.0)

        def on_poll(elapsed_s):
            timer.poll(elapsed_s)
            narrate(elapsed_s)

        cook_started = time.time()
        elapsed, aborted = cook_node(
            nodes["download"], payload["timeout"], log,
            non_blocking=True, on_poll=on_poll,
            extra_nodes=[nodes["upload"], nodes["render"], nodes["probe"], sched],
        )

        totals = {}
        upload_bytes = 0
        node_ok = True
        for name in payload["nodes"]:
            attribs = ("bytes", "files", "seconds", "mbps") if name in ("upload", "download") else ()
            succeeded, total, items = report_items(nodes[name], log, attribs=attribs)
            totals[name] = total
            node_ok = node_ok and total > 0 and succeeded == total
            if name == "upload":
                for item in items:
                    try:
                        upload_bytes += item.intAttribValue("bytes") or 0
                    except Exception:
                        pass

        result["counts"] = totals
        result["upload_bytes"] = upload_bytes
        result["cook_id"] = _cook_id_from(sched, log)
        result["stages"].update(_stage_seconds(timer, cook_started, elapsed))
        result["ok"] = bool(node_ok and not aborted)

        if sched.parm("rpfarm_status_text"):
            log("scheduler status:\n" + sched.parm("rpfarm_status_text").evalAsString())
    except Exception as e:  # noqa: BLE001 - the parent needs a result file whatever happens
        import traceback

        traceback.print_exc()
        result["error"] = "{}: {}".format(type(e).__name__, e)
    finally:
        try:
            with open(payload["result"], "w") as f:
                json.dump(result, f, indent=2)
        except OSError as e:
            log("could not write the result file: {}".format(e))

    log("RESULT: {}".format("PASS" if result.get("ok") else "FAIL"))
    return 0 if result.get("ok") else 1


def _cook_id_from(sched, log):
    """This cook's id, read off the scheduler's Status text.

    The ledger file is named after it, and the scheduler is the only thing
    that knows it (it is minted in ``onStartCook``); the Status tab is the one
    place it is published to the outside.
    """
    parm = sched.parm("rpfarm_status_text")
    if parm is None:
        return None
    for line in parm.evalAsString().splitlines():
        parts = line.split()
        # "cook <8 hex>  project <name>" -- _update_status_text's first line.
        if len(parts) >= 2 and parts[0] == "cook" and _is_cook_id(parts[1]):
            return parts[1]
    log("could not read the cook id off the scheduler status text")
    return None


def _is_cook_id(token):
    return len(token) == 8 and all(c in "0123456789abcdef" for c in token)


def _stage_seconds(timer, cook_started, elapsed):
    """Turn the polled windows into the stage durations the table shows."""
    up_start, up_done = timer.window("upload")
    probe_start, probe_done = timer.window("probe")
    render_start, render_done = timer.window("render")
    dl_start, dl_done = timer.window("download")

    def span(a, b):
        return (b - a) if (a is not None and b is not None) else None

    return {
        "upload": span(cook_started, up_done),
        "farm_ready": span(up_done or cook_started, probe_start),
        "probe": span(probe_start, probe_done),
        "render": span(render_start, render_done),
        "download": span(dl_start, dl_done),
        "cook": elapsed,
    }


def build_smoke_parser(sub):
    """Add the ``smoke`` subcommand to ``rpfarm``'s argument parser."""
    p = sub.add_parser(
        "smoke", help="end-to-end test: upload a tiny scene, render it on a GPU pod, "
                      "get the frames back, kill every pod")
    p.add_argument("--gpu", default=None,
                   help='GPU priority for this run, e.g. "NVIDIA RTX A4500" '
                        "(default: gpu_priority from config.toml)")
    p.add_argument("--keep", action="store_true",
                   help="leave the pods running afterwards (production behaviour); "
                        "by default this user's own rpfarm pods are terminated, "
                        "sync pod included")
    p.add_argument("--everyone", action="store_true",
                   help="DANGER: at the end, terminate EVERY rpfarm pod on the account, "
                        "including other artists' in-flight renders. Off by default -- "
                        "cleanup is scoped to your own pods (rpfarm-<user>-* and "
                        "rpfarm-sync-<user>)")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                   help="wall-clock guard for the whole run, seconds (default {})".format(DEFAULT_TIMEOUT))
    p.add_argument("--workdir", default=None,
                   help="run directory (default: tmp/smoke_<timestamp>/ in the checkout)")
    p.add_argument("--houdini", default=None,
                   help="HFS of the Houdini install to cook with (default: the newest found)")
    return p


def main(argv=None):
    """``hython -m rpfarm.smoke <payload.json>`` -- the in-Houdini stage.

    The plain-python3 entry point is ``rpfarm smoke``, i.e.
    :func:`cmd_smoke` via ``rpfarm.cli``.
    """
    argv = sys.argv[1:] if argv is None else argv
    return hython_main(argv)


if __name__ == "__main__":
    sys.exit(main())
