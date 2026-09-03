"""One-shot builder for hda/runpodfarm_stats.hda.

Task 11 -- read this file's own docstring, then compare with
``scripts/build_runpodfarm_download_hda.py`` (structurally the closest
sibling: a ``pythonprocessor`` + forced ``localscheduler`` inside a subnet,
same override-in-``OnCreated`` belt-and-suspenders). The design differs in
one deliberate way (addendum for Task 11, see
``.superpowers/sdd/2026-09-02-rpfarm-v2/task-11-addendum.md``): this node's
data volume is small (the local ``~/.rpfarm/ledger`` journal, plus one
``GET /billing/pods`` call) so, unlike upload/download, it never dispatches
out of process through ``rpfarm.package_runner`` -- every work item is
created with ``inProcess=True`` and cooks inline in Houdini's own process
via this node's own ``cooktask`` (empty: all the real work -- loading the
ledger, merging billing, building the summary text -- already ran once in
``generate``, not once per item).

The heavy lifting (loading+filtering the ledger, merging billing, grouping
into project/user totals, finding cleanup candidates) lives in this asset's
own ``PythonModule`` section (``compute()``/``build_summary_text()``) so
both the pythonprocessor's ``generate`` callback and the ``Refresh``/
``Export CSV`` buttons share one implementation -- the same pattern the
scheduler HDA already uses for its own buttons (``onSyncLedger``,
``onKillAll``, ...; see that node's ``PythonModule``).

Regenerate the checked-in asset with, e.g.::

    HYTHON=/Applications/Houdini/Houdini22.0.368/Frameworks/Houdini.framework/Versions/Current/Resources/bin/hython
    OUT=/tmp/runpodfarm_stats.hda
    "$HYTHON" scripts/build_runpodfarm_stats_hda.py "$OUT"
    rm -rf hda/runpodfarm_stats.hda
    hotl -t hda/runpodfarm_stats.hda "$OUT"          # git-tracked expanded form
    hotl -l hda/runpodfarm_stats.hda \\
        ~/Library/Preferences/houdini/22.0/otls/runpodfarm_stats.hda  # install

Only ``hotl -l`` (the counterpart of ``-t``) round-trips correctly for this
directory layout -- see the download node's own docstring for how this was
verified.
"""
import os
import sys

import hou

OUT_HDA = sys.argv[1] if len(sys.argv) > 1 else "/tmp/runpodfarm_stats.hda"

PYTHON_MODULE = '''\
"""Shared logic for runpodfarm_stats: load+filter the local ledger, merge
in RunPod's actual billing, and build the multiline summary text.

Called from three places, all after the same ``RPFARM_ROOT`` bootstrap
(this asset's ``generate``/``cooktask`` callbacks, and this module's own
``onRefresh``/``onExportCsv`` button handlers): the pythonprocessor's
``generate`` builds one work item per (filtered, billing-merged) ledger
record via ``compute()`` + ``_set_attrib``, and writes ``rpfarm_summary``
via ``build_summary_text()``; ``Refresh`` pulls new ledger files off the
volume first, then re-cooks so ``generate`` runs again; ``Export CSV``
calls ``compute()`` a second time (its own filters may have changed since
the last cook) and writes the result with ``rpfarm.ledger.to_csv``.
"""

import datetime
import os
import pathlib
import sys
import time

_RPFARM_ROOT = pathlib.Path(os.environ.get("RPFARM_ROOT", pathlib.Path.home() / ".rpfarm" / "src"))
if str(_RPFARM_ROOT) not in sys.path:
    sys.path.insert(0, str(_RPFARM_ROOT))

from rpfarm import config as rpcfg
from rpfarm import ledger as rpledger
from rpfarm import pods as rppods
from rpfarm.runpod_api import RunPodAPI, RunPodError
from rpfarm.worker_client import WorkerClient

# Projects with no cook in longer than this show up as cleanup candidates.
# No config knob for it yet -- the same fixed-constant style dispatch.py
# uses for its own thresholds (BUDGET_WARN etc).
_STALE_DAYS = 30


def _read_pubkey(cfg):
    with open(cfg.ssh_key_path + ".pub", "r") as f:
        return f.read().strip()


def _parse_date(s, end_of_day=False):
    """"YYYY-MM-DD" -> epoch seconds (UTC), or None for an empty string."""
    s = (s or "").strip()
    if not s:
        return None
    dt = datetime.datetime.strptime(s, "%Y-%m-%d")
    if end_of_day:
        dt = dt + datetime.timedelta(days=1) - datetime.timedelta(seconds=1)
    return dt.replace(tzinfo=datetime.timezone.utc).timestamp()


def _iso(epoch, fallback_days_ago=None):
    if epoch is None:
        if fallback_days_ago is not None:
            dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=fallback_days_ago)
        else:
            dt = datetime.datetime.now(datetime.timezone.utc)
    else:
        dt = datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _set_attrib(wi, key, value):
    if value is None:
        return
    if isinstance(value, bool):
        wi.setIntAttrib(key, int(value))
    elif isinstance(value, int):
        wi.setIntAttrib(key, value)
    elif isinstance(value, float):
        wi.setFloatAttrib(key, value)
    else:
        wi.setStringAttrib(key, str(value))


def compute(node):
    """Load, filter (project/user/since/until) and -- if Use Billing is on
    -- billing-merge the local ledger for *node*'s current parameters.

    Returns ``(filtered, by_project, by_user, cleanup, vol_total, usebilling)``.
    ``vol_total`` is the sum of ``GET /billing/networkvolumes`` over the
    period (``None`` when billing is off or unreachable). ``cleanup`` is
    computed from the FULL local ledger (not the project/user filter --
    the point is to surface stale projects the current filter might be
    hiding), each entry a dict with ``project, last_cook, age_days``; the
    per-project on-disk size and $/month are left as a TODO for Task 12's
    housekeeping ``ls`` (not landed -- only ``du`` exists today, see
    ``pod/housekeeping.py`` -- inventing that API here would just be a
    second, divergent guess at its shape).
    """
    # rpcfg.home() needs no config.toml (it is just $RPFARM_HOME or
    # ~/.rpfarm) -- config is only actually loaded below, and only when
    # Use Billing is on, so the common local-only view works with no
    # `rpfarm setup` at all.
    local_dir = rpcfg.home() / "ledger"
    records = rpledger.load_all(local_dir)

    project_filter = (node.evalParm("rpfarm_project") or "").strip()
    user_filter = (node.evalParm("rpfarm_user") or "").strip()
    since_epoch = _parse_date(node.evalParm("rpfarm_since"))
    until_epoch = _parse_date(node.evalParm("rpfarm_until"), end_of_day=True)
    usebilling = bool(node.evalParm("rpfarm_usebilling"))

    vol_total = None
    merged = records
    if usebilling:
        try:
            cfg = rpcfg.load()
            api = RunPodAPI(cfg.api_key)
            since_iso = _iso(since_epoch, fallback_days_ago=90)
            until_iso = _iso(until_epoch)
            billing_pods = api.billing_pods(since_iso, until_iso)
            billing_volumes = api.billing_volumes(since_iso, until_iso)
            vol_total = sum(float(v.get("amount") or 0.0) for v in billing_volumes)
            merged = rpledger.merge_billing(records, billing_pods)
        except (rpcfg.ConfigError, RunPodError) as e:
            node.addWarning("billing unavailable, showing cost_est instead: {}".format(e))
            usebilling = False
            merged = records

    def _keep(rec):
        if rec.get("record") == "cook_summary":
            return False
        if project_filter and rec.get("project") != project_filter:
            return False
        if user_filter and rec.get("user") != user_filter:
            return False
        ts = rec.get("started", rec.get("ended"))
        if ts is not None:
            if since_epoch is not None and ts < since_epoch:
                return False
            if until_epoch is not None and ts > until_epoch:
                return False
        return True

    filtered = [r for r in merged if _keep(r)]
    by_project = rpledger.summarize(filtered, by="project")
    by_user = rpledger.summarize(filtered, by="user")

    now = time.time()
    last_by_project = {}
    for rec in records:
        if rec.get("record") == "cook_summary":
            continue
        project = rec.get("project")
        ts = rec.get("ended") or rec.get("started")
        if not project or ts is None:
            continue
        if project not in last_by_project or ts > last_by_project[project]:
            last_by_project[project] = ts
    cleanup = []
    for project, ts in sorted(last_by_project.items()):
        age_days = (now - ts) / 86400.0
        if age_days > _STALE_DAYS:
            cleanup.append({
                "project": project,
                "last_cook": time.strftime("%Y-%m-%d", time.gmtime(ts)),
                "age_days": age_days,
            })

    return filtered, by_project, by_user, cleanup, vol_total, usebilling


def _fmt_money(x):
    return "${:.4f}".format(x or 0.0)


def _set_summary(node, text):
    """Set rpfarm_summary, escaping "$" so Houdini's own parm expansion
    (bare "$" reads as a channel/variable reference, same as $HIP etc)
    doesn't eat the money signs the summary text is full of -- verified
    live: an un-escaped "$0.0064" round-trips through evalAsString() as
    just ".0064" (Houdini silently expands the undefined "$0" to nothing).
    "\\\\$" is the same escape this codebase's own shell-command building
    already relies on for $PDG_ITEM_NAME (see smoke_scheduler_headless.py).
    """
    node.parm("rpfarm_summary").set(text.replace("$", "\\\\$"))


def build_summary_text(by_project, by_user, cleanup, vol_total, usebilling, n_records):
    lines = []
    total_cost = sum(g["cost"] for g in by_project)
    total_gpu_hours = sum(g["gpu_hours"] for g in by_project)
    total_tasks = sum(g["tasks"] for g in by_project)
    lines.append("{} record(s) -- {} total, {:.2f} GPU-h, {} task(s), {} /task".format(
        n_records, _fmt_money(total_cost), total_gpu_hours, total_tasks,
        _fmt_money(total_cost / total_tasks) if total_tasks else "$0.0000",
    ))
    if not usebilling:
        lines.append("(cost_est -- Use Billing is off; turn it on for RunPod's actual charge)")
    lines.append("")
    lines.append("By project:")
    for g in by_project:
        lines.append("  {:<24s} {:>10s}  {:>8.2f} GPU-h  {:>4d} task(s)  {} /task".format(
            g["key"], _fmt_money(g["cost"]), g["gpu_hours"], g["tasks"], _fmt_money(g["cost_per_task"])))
    if not by_project:
        lines.append("  (no records match the current filters)")
    lines.append("")
    lines.append("By user:")
    for g in by_user:
        lines.append("  {:<24s} {:>10s}  {:>8.2f} GPU-h  {:>4d} task(s)  {} /task".format(
            g["key"], _fmt_money(g["cost"]), g["gpu_hours"], g["tasks"], _fmt_money(g["cost_per_task"])))
    if not by_user:
        lines.append("  (no records match the current filters)")
    if usebilling and vol_total is not None:
        lines.append("")
        lines.append("Volume storage (period): {}".format(_fmt_money(vol_total)))
    lines.append("")
    lines.append("Cleanup candidates (no cook in > {} days):".format(_STALE_DAYS))
    if cleanup:
        for c in cleanup:
            lines.append("  {:<24s} last cook {}  ({:.0f}d ago)  size: TODO (Task 12 housekeeping ls)".format(
                c["project"], c["last_cook"], c["age_days"]))
    else:
        lines.append("  (none)")
    return "\\n".join(lines)


def onRefresh(kwargs):
    """Pull new ledger files off the volume, then re-cook so `generate`
    (which does the billing pull) runs again with fresh data."""
    node = kwargs["node"]
    try:
        cfg = rpcfg.load()
        token = rpcfg.session_token()
        pubkey = _read_pubkey(cfg)
        api = RunPodAPI(cfg.api_key)
        pod = rppods.ensure_sync_pod(api, cfg, token, pubkey)
        client = WorkerClient(pod["id"], token)
        local_dir = rpcfg.home() / "ledger"
        n = rpledger.sync_from_volume(client, local_dir, cfg.user)
        node.cookWorkItems(block=True, save_prompt=False)
        # evalAsString() un-escapes "\\$" back to a real "$" -- re-escape
        # before writing it back, or the next round-trip mangles it again.
        current = node.parm("rpfarm_summary").evalAsString()
        _set_summary(node, "Synced {} new ledger file(s).\\n\\n{}".format(n, current))
    except (rpcfg.ConfigError, RunPodError, OSError) as e:
        node.addWarning("refresh failed: {}".format(e))


def onExportCsv(kwargs):
    node = kwargs["node"]
    try:
        filtered, by_project, by_user, cleanup, vol_total, usebilling = compute(node)
        out_dir = rpcfg.home() / "exports"
        path = out_dir / "rpfarm_stats_{}.csv".format(time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
        rpledger.to_csv(filtered, path)
        current = node.parm("rpfarm_summary").evalAsString()
        _set_summary(node, "Exported {} record(s) -> {}\\n\\n{}".format(len(filtered), path, current))
    except (rpcfg.ConfigError, OSError) as e:
        node.addWarning("CSV export failed: {}".format(e))
'''

GENERATE_CODE = '''\
# Called when this node should generate new work items. No upstream input
# (this node takes none -- see this node's Help) so generation is static:
# fires once per cook, not re-invoked per upstream item like the download
# node's "outputs" mode.
#
# self             -   A reference to the current pdg.Node instance
# item_holder      -   A pdg.WorkItemHolder for constructing and adding work items
# upstream_items   -   Always empty here (no input)
# generation_type  -   The type of generation

import os
import pathlib
import sys

_RPFARM_ROOT = pathlib.Path(os.environ.get("RPFARM_ROOT", pathlib.Path.home() / ".rpfarm" / "src"))
if str(_RPFARM_ROOT) not in sys.path:
    sys.path.insert(0, str(_RPFARM_ROOT))

node = self.topNode().parent()
hm = node.hm()

filtered, by_project, by_user, cleanup, vol_total, usebilling = hm.compute(node)

# One work item per (filtered, billing-merged) ledger record -- a task, a
# pod's synthetic idle remainder, or an unattributed billed pod (see
# rpfarm.ledger.merge_billing). Every item cooks inProcess: this node's
# whole point is fast local aggregation (Task 11 addendum -- "данных мало,
# сеть только на биллинг"), and inProcess=True is what lets this node's own
# (empty) cooktask actually run -- an item created without it silently
# no-ops in cooktask instead (see this node's Help).
for i, rec in enumerate(filtered):
    wi = item_holder.addWorkItem(name="rec_{:04d}".format(i), inProcess=True)
    for k, v in rec.items():
        hm._set_attrib(wi, k, v)

hm._set_summary(
    node, hm.build_summary_text(by_project, by_user, cleanup, vol_total, usebilling, len(filtered))
)
'''

COOKTASK_CODE = '''\
# Called for every inProcess work item (generate() above only creates
# inProcess ones -- see this node's Help for why that matters). All of the
# real work -- loading the ledger, merging billing, computing the
# per-record cost -- already ran ONCE in generate(), not once per item;
# this item's attributes were set there. Nothing left to cook.
#
# self              -   A reference to the current pdg.Node instance
# work_item         -   The work item being cooked by this callback
pass
'''

HELP_TEXT = '''\
= RunPodFarm Stats =

#type: node
#context: top
#internal: runpodfarmstats
#icon: TOP/pythonprocessor

"""Journal and cost analytics for RunPodFarm: turns the local ~/.rpfarm/ledger
into work items (one per task/idle/unattributed record) plus a summary of
totals by project and by user."""

Work item = one ledger record (a task, a pod's synthetic idle remainder, or
a billed pod with no matching local record -- see
[Node:python/rpfarm.ledger.merge_billing]) after the Project/User/Since/
Until filters. Every item cooks *in process* -- unlike
[Node:top/runpodfarm_upload] and [Node:top/runpodfarm_download], this node
never dispatches through `rpfarm.package_runner`: the data volume here is
small (the local journal, plus one `GET /billing/pods` call), so there is
nothing worth parallelizing out of process (Task 11 addendum). This node
still overrides `Scheduler` to its own internal `localscheduler` (same
absolute-path Python expression trick as the other two nodes, reasserted in
`OnCreated`) purely so cooking it never accidentally dispatches onto
`runpodfarm_scheduler` and spins up a real GPU pod for what is a read-only
report.

All the real logic (loading the ledger, filtering, merging billing,
building the summary text) lives in this asset's own `PythonModule` --
shared between the `generate` callback and the `Refresh`/`Export CSV`
buttons, the same pattern [Node:top/runpodfarm_scheduler] uses for its own
buttons.

@parameters

Since / Until:
    #id: rpfarm_since

    `YYYY-MM-DD`, either empty. Filters ledger records by their `started`
    timestamp. Billing calls (when Use Billing is on) default to 90 days
    back when Since is empty -- RunPod's billing endpoints need a bounded
    range, unlike the ledger filter itself.

Project / User:
    #id: rpfarm_project

    Exact-match filters on the ledger record's own `project`/`user`
    fields. Empty means no filter.

Use Billing:
    #id: rpfarm_usebilling

    Off: costs come from the scheduler's own live `cost_est` (no network
    call). On: `generate` calls `GET /billing/pods` and
    `GET /billing/networkvolumes` and merges RunPod's actual charge in via
    `rpfarm.ledger.merge_billing`, prorated per pod by task duration, with
    the remainder folded into a synthetic idle record.

Refresh:
    #id: rpfarm_refresh

    Pulls any ledger files off the volume this session doesn't have locally
    yet (`rpfarm.ledger.sync_from_volume`, via the sync pod's worker HTTP
    API -- not rclone/sftp, there is nothing to gain from a second
    transport just to fetch a handful of small text files), then re-cooks
    this node so `generate` picks up the new data (and re-pulls billing, if
    Use Billing is on).

Export CSV:
    #id: rpfarm_export_csv

    Writes the current filtered+merged record set to
    `~/.rpfarm/exports/rpfarm_stats_<timestamp>.csv`
    (`rpfarm.ledger.to_csv`) and prepends the path to the summary text.

Summary:
    #id: rpfarm_summary

    Read-only: totals by project and by user ($ / GPU-hours / tasks / $ per
    task), volume storage cost for the period (Use Billing only), and
    cleanup candidates -- projects with no cook in over 30 days. Per-project
    on-disk size and $/month are a TODO for Task 12's housekeeping `ls`
    (not landed yet -- only `du` exists today).

@related

- [Node:top/runpodfarm_scheduler]
- [Node:top/runpodfarm_upload]
- [Node:top/runpodfarm_download]
- [Node:top/pythonprocessor]
- [Node:top/localscheduler]
'''


def main():
    obj = hou.node("/obj")
    build_net = obj.createNode("topnet", "rpfarm_stats_build")
    sn = build_net.createNode("subnet", "runpodfarmstats_build")

    pp = sn.createNode("pythonprocessor", "pythonprocessor1")
    localsched = sn.createNode("localscheduler", "localscheduler")

    # No input (min_num_inputs=0 below) -- nothing to wire pp's input 0 to.
    out0 = sn.node("output0")
    out0.setInput(0, pp, 0)

    pp.parm("topscheduler").setExpression(
        'hou.pwd().parent().path() + "/localscheduler"', language=hou.exprLanguage.Python
    )
    pp.parm("generate").set(GENERATE_CODE)
    pp.parm("cooktask").set(COOKTASK_CODE)

    pp.moveToGoodPosition()
    localsched.moveToGoodPosition()
    out0.moveToGoodPosition()

    ptg = hou.ParmTemplateGroup()

    since_pt = hou.StringParmTemplate("rpfarm_since", "Since (YYYY-MM-DD)", 1, default_value=("",))
    until_pt = hou.StringParmTemplate("rpfarm_until", "Until (YYYY-MM-DD)", 1, default_value=("",))
    project_pt = hou.StringParmTemplate("rpfarm_project", "Project", 1, default_value=("",))
    user_pt = hou.StringParmTemplate("rpfarm_user", "User", 1, default_value=("",))
    usebilling_pt = hou.ToggleParmTemplate("rpfarm_usebilling", "Use Billing", default_value=False)
    usebilling_pt.setHelp(
        "Off: the scheduler's own live cost_est, no network call. "
        "On: merge in RunPod's actual GET /billing/pods charge (Task 11)."
    )

    refresh_pt = hou.ButtonParmTemplate("rpfarm_refresh", "Refresh")
    refresh_pt.setTags({"script_callback": "hou.phm().onRefresh(kwargs)", "script_callback_language": "python"})

    export_pt = hou.ButtonParmTemplate("rpfarm_export_csv", "Export CSV")
    export_pt.setTags({"script_callback": "hou.phm().onExportCsv(kwargs)", "script_callback_language": "python"})

    summary_pt = hou.StringParmTemplate("rpfarm_summary", "Summary", 1, default_value=("",))
    # Read-only multiline display -- same trick the scheduler HDA's own
    # rpfarm_status_text/rpfarm_volume_text use: disablewhen on a toggle
    # parm's own always-true range (>= 0 for a 0/1 toggle) rather than a
    # dedicated readonly parm type, which Houdini's string parm has none of.
    summary_pt.setTags({"editor": "1", "editorlines": "8-30"})
    summary_pt.setConditional(hou.parmCondType.DisableWhen, "{ rpfarm_usebilling >= 0 }")

    for pt in (since_pt, until_pt, project_pt, user_pt, usebilling_pt, refresh_pt, export_pt, summary_pt):
        ptg.append(pt)

    sn.setParmTemplateGroup(ptg)

    if os.path.exists(OUT_HDA):
        os.remove(OUT_HDA)

    new_type = sn.createDigitalAsset(
        name="runpodfarmstats",
        hda_file_name=OUT_HDA,
        description="RunPodFarm Stats",
        min_num_inputs=0,
        max_num_inputs=0,
        ignore_external_references=True,
    )

    definition = new_type.type().definition()
    definition.addSection("Help", HELP_TEXT)
    definition.addSection("PythonModule", PYTHON_MODULE)
    definition.setParmTemplateGroup(ptg)

    definition.addSection(
        "OnCreated",
        'node = kwargs["node"]\n'
        'pp = node.node("pythonprocessor1")\n'
        'if pp is not None:\n'
        '    try:\n'
        '        node.allowEditingOfContents()\n'
        '        pp.parm("topscheduler").setExpression(\n'
        '            \'hou.pwd().parent().path() + "/localscheduler"\', language=hou.exprLanguage.Python)\n'
        '    except hou.PermissionError:\n'
        '        pass  # the baked-in default (set at build time) already has it right\n',
    )
    definition.setExtraFileOption("OnCreated/IsPython", True)
    definition.setExtraFileOption("OnCreated/IsScript", True)

    definition.save(OUT_HDA, template_node=new_type)

    print("OK built", OUT_HDA, "type", new_type.type().name())

    build_net.destroy()


if __name__ == "__main__":
    main()
