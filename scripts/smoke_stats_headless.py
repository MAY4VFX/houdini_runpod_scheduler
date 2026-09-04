"""Headless smoke test for runpodfarm_stats, with no GUI and no network.

Populates a throwaway ``$RPFARM_HOME/ledger`` with synthetic records (never
the artist's real ``~/.rpfarm/ledger`` -- see Task 11's addendum), cooks a
``runpodfarmstats`` node over it with Use Billing off, and asserts: the
work-item count and a couple of representative attributes, the summary
text's per-project/per-user totals and its cleanup-candidates section, and
the CSV export button.

Run with Houdini's ``hython``, not a system python::

    RPFARM_ROOT=$PWD \\
    /Applications/Houdini/Houdini22.0.368/Frameworks/Houdini.framework/\\
Versions/Current/Resources/bin/hython scripts/smoke_stats_headless.py

Exit status is 0 only when every assertion passes.
"""

from __future__ import annotations

import glob
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

log = rpsmoke.make_log("smoke-stats")


def write_ledger(ledger_dir):
    """Two cooks worth of synthetic records: a fresh one for project "shot"
    (user may) with 2 tasks + a cook_summary, and a stale one (40 days old)
    for project "old_proj" (user bob) with 1 task -- exercises both the
    per-project/user totals and the cleanup-candidates section."""
    now = time.time()
    fresh = now - 3600  # an hour ago
    stale = now - 40 * 86400  # 40 days ago

    from rpfarm import ledger

    p1 = ledger_dir / "aaaa1111.jsonl"
    ledger.append(
        p1, cook_id="aaaa1111", user="may", project="shot", work_item=1,
        work_item_name="gen_1", pod="pod1", gpu="RTX 4090",
        started=fresh, ended=fresh + 5, duration_s=5, exit_code=0, cost_est=0.001,
    )
    ledger.append(
        p1, cook_id="aaaa1111", user="may", project="shot", work_item=2,
        work_item_name="gen_2", pod="pod1", gpu="RTX 4090",
        started=fresh + 5, ended=fresh + 12, duration_s=7, exit_code=0, cost_est=0.0014,
    )
    ledger.append_cook_summary(
        p1, cook_id="aaaa1111", user="may", project="shot",
        started=fresh - 10, ended=fresh + 12, canceled=False, items_failed=0, cost_est=0.003,
    )

    p2 = ledger_dir / "bbbb2222.jsonl"
    ledger.append(
        p2, cook_id="bbbb2222", user="bob", project="old_proj", work_item=1,
        work_item_name="gen_1", pod="pod2", gpu="RTX A4500",
        started=stale, ended=stale + 20, duration_s=20, exit_code=0, cost_est=0.004,
    )
    ledger.append_cook_summary(
        p2, cook_id="bbbb2222", user="bob", project="old_proj",
        started=stale - 5, ended=stale + 20, canceled=False, items_failed=0, cost_est=0.004,
    )

    return {"fresh_records": 2, "stale_records": 1, "cook_summaries": 2}


def build_node(rpfarm_root):
    hda_path = os.path.join(rpfarm_root, "hda", "runpodfarm_stats.hda")
    hou.hda.installFile(hda_path)
    topnet = hou.node("/obj").createNode("topnet", "topnet_stats")
    stats = topnet.createNode("runpodfarmstats", "stats")
    topnet.parm("topscheduler").set(stats.path())
    stats.setDisplayFlag(True)
    return topnet, stats


def report_items(node):
    pdg_node = node.getPDGNode()
    items = list(pdg_node.workItems) if pdg_node else []
    log("work items ({}):".format(len(items)))
    for item in items:
        attrs = {}
        for name in ("cook_id", "user", "project", "pod", "kind", "duration_s", "cost_est"):
            try:
                attrs[name] = item.attribValue(name)
            except Exception:
                pass
        log("  {:<10} {}".format(item.name, attrs))
    return items


def main():
    rpfarm_root = os.getcwd()
    rpfarm_home = tempfile.mkdtemp(prefix="rpfarm-stats-smoke-home-")
    os.environ["RPFARM_HOME"] = rpfarm_home
    os.environ.setdefault("RPFARM_ROOT", rpfarm_root)
    if rpfarm_root not in sys.path:
        sys.path.insert(0, rpfarm_root)
    log("$RPFARM_HOME = {} (throwaway -- never the real ~/.rpfarm)".format(rpfarm_home))

    from pathlib import Path
    ledger_dir = Path(rpfarm_home) / "ledger"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    counts = write_ledger(ledger_dir)

    topnet, stats = build_node(rpfarm_root)

    ok = True

    # -- 1. cook with no filters, billing off ---------------------------------
    stats.parm("rpfarm_usebilling").set(0)
    stats.cookWorkItems(block=True, save_prompt=False)
    items = report_items(stats)
    expected_items = counts["fresh_records"] + counts["stale_records"]
    if len(items) != expected_items:
        log("FAIL: expected {} work items (cook_summary rows excluded), got {}".format(
            expected_items, len(items)))
        ok = False
    else:
        log("OK: {} work items, cook_summary rows excluded".format(len(items)))

    names_by_project = {}
    for item in items:
        try:
            names_by_project.setdefault(item.attribValue("project"), []).append(item.name)
        except Exception:
            pass
    if sorted(names_by_project.get("shot", [])) and len(names_by_project.get("shot", [])) == 2 \
            and len(names_by_project.get("old_proj", [])) == 1:
        log("OK: 2 items for project=shot, 1 for project=old_proj")
    else:
        log("FAIL: unexpected project grouping: {}".format(
            {k: len(v) for k, v in names_by_project.items()}))
        ok = False

    summary = stats.parm("rpfarm_summary").evalAsString()
    log("summary text:\n" + summary)

    for needle in ("shot", "old_proj", "may", "bob", "Cleanup candidates", "cost_est"):
        if needle not in summary:
            log("FAIL: summary missing expected text {!r}".format(needle))
            ok = False
    if "old_proj" not in summary.split("Cleanup candidates")[-1]:
        log("FAIL: old_proj (40 days stale) not listed as a cleanup candidate")
        ok = False
    else:
        log("OK: old_proj listed under Cleanup candidates")
    if "shot" in summary.split("Cleanup candidates")[-1]:
        log("FAIL: shot (fresh, 1h old) wrongly listed as a cleanup candidate")
        ok = False

    # Task 12: no rpfarm config in this throwaway $RPFARM_HOME, so the
    # storage snapshot must degrade quietly to "n/a" -- never raise, never
    # fabricate a size or a $/month guess.
    cleanup_section = summary.split("Cleanup candidates")[-1]
    if "size n/a" in cleanup_section and "$/mo n/a" in cleanup_section:
        log("OK: cleanup candidate shows size/cost as n/a with no rpfarm config")
    else:
        log("FAIL: expected 'size n/a' and '$/mo n/a' in cleanup section:\n" + cleanup_section)
        ok = False

    # -- 1b. Task 12: real sizes plumb through compute(), $/month formula ------
    # Exercises _storage_snapshot's caller-facing contract and the
    # size-share-of-vol_total proration without needing a real farm: fakes
    # everything compute() would otherwise reach over the network for.
    hm = stats.hm()

    class _FakeCfg:
        api_key = "k"
        user = "bob"
        volume_id = "vol1"
        ssh_key_path = "/tmp/does-not-matter"

    class _FakeApi:
        def billing_pods(self, since_iso, until_iso):
            return []

        def billing_volumes(self, since_iso, until_iso):
            return [{"amount": 30.0}]  # $30 over the (Since/Until empty -> 90-day fallback) period

        def get_volume(self, vid):
            return {"size": 100}  # Ruling R27: real size, from RunPod's own API

    gb = 2**30

    ls_commands = []

    class _FakeWorkerClient:
        def __init__(self, pod_id, token):
            pass

        def exec(self, command, timeout_s=600):
            ls_commands.append(command)
            payload = {"projects": [{"user": "bob", "project": "old_proj", "bytes": 10 * gb}],
                       "volume": {"total": 100 * gb, "used": 40 * gb}}
            return {"exit_code": 0, "stdout": json.dumps(payload), "stderr": ""}

    # `hm.compute`'s bare-name imports (`RunPodAPI`, `WorkerClient`,
    # `_read_pubkey`) are globals of *that function's own module*, which
    # `stats.hm()` does not reliably hand back as the same mutable object
    # on every access -- patch `compute.__globals__` directly so this is
    # unaffected by however `hm()` is implemented. `rpcfg`/`rppods` are
    # real shared module objects (`sys.modules['rpfarm...']`), so patching
    # their attributes is safe however they were reached.
    g = hm.compute.__globals__
    orig = {k: g[k] for k in ("RunPodAPI", "WorkerClient", "_read_pubkey")}
    orig_load = hm.rpcfg.load
    orig_ensure_sync_pod = hm.rppods.ensure_sync_pod

    g["RunPodAPI"] = lambda api_key: _FakeApi()
    g["WorkerClient"] = _FakeWorkerClient
    g["_read_pubkey"] = lambda cfg: "ssh-ed25519 AAAA"
    hm.rpcfg.load = lambda: _FakeCfg()
    hm.rppods.ensure_sync_pod = lambda api, cfg, token, pubkey: {"id": "pod1"}

    try:
        stats.parm("rpfarm_usebilling").set(1)
        _f, _bp, _bu, cleanup2, vol_total2, usebilling2 = hm.compute(stats)
    finally:
        g.update(orig)
        hm.rpcfg.load = orig_load
        hm.rppods.ensure_sync_pod = orig_ensure_sync_pod
        stats.parm("rpfarm_usebilling").set(0)

    entry = next((c for c in cleanup2 if c["project"] == "old_proj"), None)
    if entry is None:
        log("FAIL: old_proj missing from compute() cleanup with fakes wired up")
        ok = False
    elif entry["bytes"] != 10 * gb:
        log("FAIL: old_proj bytes = {}, expected {} (10 GB)".format(entry["bytes"], 10 * gb))
        ok = False
    else:
        log("OK: old_proj bytes from housekeeping ls = {:.1f} GB".format(entry["bytes"] / gb))
        # $30 billed over the ~90-day fallback period, prorated to 30 days,
        # times this project's 10/100 GB share of the volume: $1.00.
        expected_cost = 30.0 / 90.0 * 30.0 * (10.0 / 100.0)
        if entry["monthly_cost"] is not None and abs(entry["monthly_cost"] - expected_cost) < 0.05:
            log("OK: old_proj $/month = {:.2f} (share of real volume billing, not a guessed rate)".format(
                entry["monthly_cost"]))
        else:
            log("FAIL: old_proj monthly_cost = {}, expected ~{:.2f}".format(entry["monthly_cost"], expected_cost))
            ok = False

    if any("--volume-size-gb 100" in c for c in ls_commands):
        log("OK: _storage_snapshot passed the real volume size (100 GB, from RunPod's own "
            "get_volume) to housekeeping ls -- never shutil.disk_usage (Ruling R27)")
    else:
        log("FAIL: expected an ls call with --volume-size-gb 100, got: {}".format(ls_commands))
        ok = False

    # -- 1c. Review fix: config present but Use Billing OFF must never touch
    # the sync pod. Before this fix, _storage_snapshot ran unconditionally
    # (only rpcfg.load()'s own ConfigError -- absent here, a fake config is
    # wired up -- ever stopped it), so a configured user got a network call
    # and a sync-pod touch on every cook/Refresh/Export CSV even with
    # billing off. Reuses 1b's fakes; the only thing that changes is the
    # Use Billing toggle.
    ensure_sync_pod_calls = []

    def _spy_ensure_sync_pod(api, cfg, token, pubkey):
        ensure_sync_pod_calls.append(1)
        return {"id": "pod1"}

    g3 = hm.compute.__globals__
    orig3 = {k: g3[k] for k in ("RunPodAPI", "WorkerClient", "_read_pubkey")}
    orig_load3 = hm.rpcfg.load
    orig_ensure_sync_pod3 = hm.rppods.ensure_sync_pod

    g3["RunPodAPI"] = lambda api_key: _FakeApi()
    g3["WorkerClient"] = _FakeWorkerClient
    g3["_read_pubkey"] = lambda cfg: "ssh-ed25519 AAAA"
    hm.rpcfg.load = lambda: _FakeCfg()
    hm.rppods.ensure_sync_pod = _spy_ensure_sync_pod

    try:
        stats.parm("rpfarm_usebilling").set(0)  # the point of this case
        _f3, _bp3, _bu3, cleanup3, vol_total3, usebilling3 = hm.compute(stats)
    finally:
        g3.update(orig3)
        hm.rpcfg.load = orig_load3
        hm.rppods.ensure_sync_pod = orig_ensure_sync_pod3

    if ensure_sync_pod_calls:
        log("FAIL: ensure_sync_pod was called {} time(s) with Use Billing off (config present)".format(
            len(ensure_sync_pod_calls)))
        ok = False
    else:
        log("OK: Use Billing off + config present never touches the sync pod")

    entry3 = next((c for c in cleanup3 if c["project"] == "old_proj"), None)
    if entry3 is not None and entry3["bytes"] is None and entry3["monthly_cost"] is None:
        log("OK: cleanup candidate has no size/cost when the snapshot was correctly skipped")
    else:
        log("FAIL: expected old_proj bytes/monthly_cost both None (snapshot skipped), got {}".format(entry3))
        ok = False

    # -- 2. project filter ------------------------------------------------------
    stats.parm("rpfarm_project").set("shot")
    # A plain parm change on this node doesn't by itself dirty its own
    # already-cooked work items (PDG doesn't track evalParm() calls made
    # inside a Python generate() script as automatic cook dependencies) --
    # force regeneration explicitly, same as toggling the filter and
    # pressing Refresh would need to in the real node.
    stats.dirtyWorkItems(False)
    stats.cookWorkItems(block=True, save_prompt=False)
    filtered_items = list(stats.getPDGNode().workItems)
    if len(filtered_items) == counts["fresh_records"]:
        log("OK: Project=shot filter narrows to {} item(s)".format(len(filtered_items)))
    else:
        log("FAIL: Project=shot filter gave {} item(s), expected {}".format(
            len(filtered_items), counts["fresh_records"]))
        ok = False
    stats.parm("rpfarm_project").set("")

    # -- 3. CSV export ------------------------------------------------------------
    stats.cookWorkItems(block=True, save_prompt=False)
    before = set(glob.glob(os.path.join(rpfarm_home, "exports", "*.csv")))
    stats.hm().onExportCsv({"node": stats})
    after = set(glob.glob(os.path.join(rpfarm_home, "exports", "*.csv")))
    new_csvs = after - before
    if len(new_csvs) == 1:
        csv_path = new_csvs.pop()
        with open(csv_path) as f:
            rows = f.read().strip().splitlines()
        log("OK: CSV exported to {} ({} line(s) incl. header)".format(csv_path, len(rows)))
        if len(rows) - 1 != expected_items:
            log("FAIL: CSV has {} data row(s), expected {}".format(len(rows) - 1, expected_items))
            ok = False
    else:
        log("FAIL: Export CSV did not produce exactly one new file: {}".format(new_csvs))
        ok = False

    # -- 3b. two exports in immediate succession must not collide -----------------
    # Review finding: the export path used to be second-granularity with a
    # plain open(path, "w") -- two exports inside the same second silently
    # overwrote each other. _unique_csv_path fixes that; prove it here by
    # firing the button twice back to back (well inside one second).
    before2 = set(glob.glob(os.path.join(rpfarm_home, "exports", "*.csv")))
    stats.hm().onExportCsv({"node": stats})
    stats.hm().onExportCsv({"node": stats})
    after2 = set(glob.glob(os.path.join(rpfarm_home, "exports", "*.csv")))
    new_csvs2 = after2 - before2
    if len(new_csvs2) == 2:
        log("OK: two back-to-back exports produced 2 distinct files (no overwrite): {}".format(
            sorted(os.path.basename(p) for p in new_csvs2)))
    else:
        log("FAIL: two back-to-back exports produced {} file(s), expected 2: {}".format(
            len(new_csvs2), new_csvs2))
        ok = False

    shutil.rmtree(rpfarm_home, ignore_errors=True)

    log("RESULT: {}".format("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
