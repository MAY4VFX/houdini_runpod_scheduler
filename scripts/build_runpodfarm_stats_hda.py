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
import hashlib
import os
import pathlib
import sys

import hou

OUT_HDA = sys.argv[1] if len(sys.argv) > 1 else "/tmp/runpodfarm_stats.hda"

# -- the family look (Task 17) ------------------------------------------------
#
# All four RunPodFarm nodes share one colour, one node shape and one icon
# family, so a farm node is recognisable at a glance among stock TOP nodes.
# Violet because it is RunPod's own colour and is essentially absent from
# stock Houdini.
#
# The icon travels INSIDE the asset as an ``IconSVG`` section referenced by
# ``opdef:.?IconSVG``, so it needs no installation. The node shape cannot:
# Houdini resolves a shape by name out of ``config/NodeShapes`` on
# HOUDINI_PATH, so ``rpfarm setup`` copies hda/nodeshapes/rpfarm.json into
# the user pref dir (rpfarm.houdini_local.install_node_shape) and ``rpfarm
# doctor`` checks it is there. Without it the nodes simply draw as plain
# rectangles -- they still work.
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
NODE_COLOR = (0.549, 0.361, 0.882)
NODE_SHAPE = "rpfarm"
ICON_SVG = (REPO_ROOT / "hda" / "icons" / "runpodfarm_stats.svg").read_text()

# Colour is applied in OnCreated because a definition carries none: a node
# coloured before createDigitalAsset comes back grey on the next instance
# (verified in Houdini 22.0.368). The shape does survive -- setUserData
# below bakes an ``opuserdata`` line into the generated CreateScript -- but
# it is re-asserted here too, so one mechanism failing is not the whole
# look failing.
FAMILY_ONCREATED = (
    'node.setColor(hou.Color((0.549, 0.361, 0.882)))\n'
    'node.setUserData("nodeshape", "rpfarm")\n'
)

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

import ast
import datetime
import json
import os
import pathlib
import sys
import time

_RPFARM_ROOT = pathlib.Path(os.environ.get("RPFARM_ROOT", pathlib.Path.home() / ".rpfarm" / "src"))
if str(_RPFARM_ROOT) not in sys.path:
    sys.path.insert(0, str(_RPFARM_ROOT))

# -- stale-module guard ------------------------------------------------------
#
# The asset and the package ship together and are updated together, but Python
# caches modules in sys.modules for the life of the process. A Houdini that was
# already open when the checkout updated runs the NEW asset against the OLD
# package, and the artist sees either an ImportError naming a symbol they have
# never heard of, or -- worse, and this is what happened on 2026-09-05 -- a
# cook whose work items simply fail.
#
# THE CHECK IS A FACT, NOT A NUMBER. It compares the package's own
# FINGERPRINT (size + content digest of every module file, taken when this
# process imported it) against those files as they are now. If anything
# differs, the code in memory is not the code on disk, and no version needs
# to have been bumped for us to know it.
#
# That matters because the version check that used to be the whole guard
# failed exactly where it was needed: rpfarm.VERSION sat at 2.2.0 through
# seven commits that changed deps.py, preflight.py and usddeps.py, so
# "loaded >= minimum" was true while the loaded code was a week behind. A
# guard that depends on someone remembering to bump a number is a guard that
# is off whenever they forget. The version is still read -- but only to make
# the message concrete.
_MIN_RPFARM_VERSION = "2.3.0"


def _version_tuple(text):
    """("2.1.0") -> (2, 1, 0). Unparseable parts sort as 0, never raises."""
    parts = []
    for chunk in str(text or "").split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def _ondisk_rpfarm_version(root):
    """rpfarm's VERSION as it is ON DISK, read without importing it.

    Importing is precisely what cannot answer this question: the import is
    what hands back the cached module. Parsed with ast, so a half-written or
    unexpected __init__ cannot execute anything or raise here.
    """
    try:
        source = (pathlib.Path(root) / "rpfarm" / "__init__.py").read_text(encoding="utf-8")
        for node in ast.parse(source).body:
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "VERSION" for t in node.targets):
                return ast.literal_eval(node.value)
    except Exception:  # noqa: BLE001 - a diagnostic must not become the failure
        return None
    return None


def _ondisk_fingerprint(package_dir):
    """The same measurement rpfarm takes of itself, computed here.

    Only used by the bake script and the tests -- the runtime check never
    looks at the disk (see _asset_mismatch for why).
    """
    out = {}
    try:
        names = sorted(os.listdir(package_dir))
    except Exception:
        return out
    for name in names:
        if not name.endswith(".py"):
            continue
        try:
            with open(os.path.join(package_dir, name), "rb") as handle:
                data = handle.read()
        except Exception:
            continue
        out[name] = (len(data), hashlib.sha256(data).hexdigest()[:16])
    return out


def _asset_mismatch(package, baked):
    """Modules whose loaded content is not what this asset was built against.

    Both sides live INSIDE this Houdini: ``package.FINGERPRINT`` is what the
    modules were when this process imported them, ``baked`` is what they were
    when this asset was built. The disk is deliberately not consulted.

    That is the whole correction. Comparing against the disk answered the
    wrong question -- "has anyone touched the checkout?" -- so every push
    while an artist had Houdini open blocked their next cook, while a
    session that was genuinely broken (an asset reinstalled under a running
    Houdini, which reloads definitions without reopening the scene) could
    still look fine. Comparing the asset with the package it was built
    against answers the only question that matters to the artist: is my tool
    consistent with itself?

    An asset with no baked fingerprint predates this and gets a warning, not
    a stop -- it may well be fine, and refusing to cook on "I cannot tell"
    is how a guard gets switched off.
    """
    loaded = getattr(package, "FINGERPRINT", None)
    if not isinstance(loaded, dict):
        return ["<no fingerprint: this rpfarm predates the check>"]
    if not baked:
        return []
    return sorted(name for name in baked if loaded.get(name) != baked[name])


def _stale_module_message(minimum, loaded, on_disk, root, changed=(), baked=True):
    """The sentence to show the artist, or None when nothing is wrong.

    ``changed`` is the answer from :func:`_asset_mismatch`; the version
    arguments only make the message concrete. An asset that carries no baked
    fingerprint at all cannot be judged, so it says so quietly instead of
    refusing to work.
    """
    changed = list(changed)
    if not changed:
        return None
    if not baked or any(name.startswith("<") for name in changed):
        return (
            "ВНИМАНИЕ: не могу проверить, сходится ли нода с кодом фермы.\\n"
            "\\n"
            "Это старая нода или старый пакет rpfarm. Кук пойдёт, но если он\\n"
            "упадёт странно — перезапустите Houdini, а потом обновите ноды:\\n"
            "    python3 -m rpfarm setup"
        )
    shown = ", ".join(changed[:4])
    more = " и ещё {}".format(len(changed) - 4) if len(changed) > 4 else ""
    return (
        "Нода собрана против другого кода фермы, чем сейчас в памяти Houdini.\\n"
        "\\n"
        "ПЕРЕЗАПУСТИТЕ HOUDINI. Больше ничего делать не нужно.\\n"
        "\\n"
        "Разошлись: {shown}{more}.\\n"
        "В памяти rpfarm {seen}, нода собрана против {disk}.".format(
            shown=shown, more=more, seen=loaded or "неизвестной версии",
            disk=on_disk or "неизвестной версии")
    )

# BEGIN baked by scripts/bake_asset_fingerprint.py -- do not edit
_ASSET_BUILT_AGAINST_VERSION = '2.3.0'
_ASSET_FINGERPRINT = {
    '__init__.py': (2490, 'c1250306daf5961f'),
    '__main__.py': (52, '13a1a5b340cdcfc1'),
    'cli.py': (61628, '98763091948d537a'),
    'compression.py': (23234, 'bef2f19daebbc929'),
    'config.py': (12718, 'c81813f3ccec7011'),
    'deps.py': (38546, 'a2cc13cc1eecb505'),
    'dispatch.py': (22191, '1121a6505c88adb3'),
    'gpus.py': (8311, '7a28d5c2692b776e'),
    'houdini_local.py': (25206, 'a5ec6729405ccb1a'),
    'ledger.py': (17327, '70425e75fb216f01'),
    'package_runner.py': (8752, '96770e3879a6cb65'),
    'packages.py': (56224, '327c8debf6b236fc'),
    'pods.py': (27040, '64cabd27b99479f6'),
    'preflight.py': (28602, 'bfad37b5ef089fc5'),
    'runpod_api.py': (14539, 'b90960f9860c97fb'),
    'smoke.py': (41503, 'd25dfbac9eddb12b'),
    'sync.py': (16104, '853b8d48734c88f9'),
    'tls.py': (3642, 'f3e50ea6ebd0308f'),
    'tools.py': (4290, 'c5d3b026f125578f'),
    'usddeps.py': (9631, '3c7192d3bd94d07f'),
    'worker_client.py': (9791, 'cf6b40b1e879c658'),
}
# END baked

import rpfarm as _rpfarm_pkg

_stale = _stale_module_message(
    _MIN_RPFARM_VERSION,
    getattr(_rpfarm_pkg, "VERSION", None),
    _ASSET_BUILT_AGAINST_VERSION,
    _RPFARM_ROOT,
    _asset_mismatch(_rpfarm_pkg, _ASSET_FINGERPRINT),
    bool(_ASSET_FINGERPRINT),
)
if _stale:
    # Raised, not printed: this is the same place the ImportError used
    # to come from, so Houdini surfaces it in the same dialog on scene
    # open -- with the instruction as the first line the artist reads.
    raise ImportError(_stale)

from rpfarm import config as rpcfg
from rpfarm import ledger as rpledger
from rpfarm import packages as rppkg
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


def _storage_snapshot(cfg, node):
    """Real per-(user, project) bytes and the volume's total bytes, via the
    sync pod's housekeeping ``ls`` (Task 12). Returns ``(sizes, vol_bytes)``
    -- ``sizes`` keyed ``"<user>/<project>"`` -- or ``({}, None)`` on any
    failure. Never raises: a stats readout must never block on the farm
    being reachable. A missing config (no ``rpfarm setup`` yet) is quiet
    (the common local-only view works with none at all, same as billing);
    any other failure (RunPod unreachable, sync pod wouldn't come up, a
    stale pod image with no housekeeping ``ls``) leaves a node warning.

    Ruling R27: ``ls``'s ``total`` is only meaningful with
    ``--volume-size-gb`` (the pod's own ``shutil.disk_usage`` reports the
    backing storage pool, not the volume's real size -- confirmed live, a
    50GB volume read back as ~2.14 PiB). The real size comes from
    ``RunPodAPI.get_volume`` via :func:`rpfarm.packages.get_volume_size_gb`
    (cached on disk, same file `maybe_grow_volume` uses) -- without it,
    ``vol_bytes`` stays ``None`` just like an unreachable pod would leave it.
    """
    quiet = cfg is None
    try:
        if cfg is None:
            cfg = rpcfg.load()
        token = rpcfg.session_token()
        pubkey = _read_pubkey(cfg)
        api = RunPodAPI(cfg.api_key)
        volume_size_gb = rppkg.get_volume_size_gb(api, cfg)
        pod = rppods.ensure_sync_pod(api, cfg, token, pubkey)
        client = WorkerClient(pod["id"], token)
        command = "python3 /opt/rpfarm/housekeeping.py ls"
        if volume_size_gb:
            command += " --volume-size-gb {}".format(volume_size_gb)
        result = client.exec(command, timeout_s=60)
        if result.get("exit_code") != 0:
            node.addWarning("storage sizes unavailable: {}".format(
                result.get("stderr") or "housekeeping ls failed"))
            return {}, None
        info = json.loads(result.get("stdout") or "{}")
        sizes = {
            "{}/{}".format(p.get("user"), p.get("project")): p.get("bytes", 0)
            for p in info.get("projects", [])
        }
        vol_bytes = (info.get("volume") or {}).get("total")
        return sizes, vol_bytes
    except rpcfg.ConfigError:
        return {}, None
    except (RunPodError, OSError, TimeoutError, ValueError) as e:
        if not quiet:
            node.addWarning("storage sizes unavailable: {}".format(e))
        return {}, None


def compute(node):
    """Load, filter (project/user/since/until) and -- if Use Billing is on
    -- billing-merge the local ledger for *node*'s current parameters.

    Returns ``(filtered, by_project, by_user, cleanup, vol_total, usebilling)``.
    ``vol_total`` is the sum of ``GET /billing/networkvolumes`` over the
    period (``None`` when billing is off or unreachable). ``cleanup`` is
    computed from the FULL local ledger (not the project/user filter --
    the point is to surface stale projects the current filter might be
    hiding), each entry a dict with ``user, project, last_cook, age_days,
    bytes, monthly_cost``. ``bytes`` comes from Task 12's housekeeping
    ``ls`` on the sync pod, and is only ever fetched when Use Billing is
    on (review finding: a plain per-cook/Refresh/Export network call and
    sync-pod touch regardless of the toggle is not what "Use Billing"
    promises, and before ``ls``'s Ruling R26 speedup this made routine,
    billing-off stats viewing slow on top of it) -- ``None`` with Use
    Billing off or the snapshot unreachable. ``monthly_cost`` is that
    project's share of ``vol_total``, prorated by size and by the
    period's length to a 30-day estimate -- ``None`` unless both Use
    Billing and the storage snapshot are available (there is no invented
    $/GB/month constant here: RunPod's own billing is the only price this
    codebase trusts, per spec 4.4).
    """
    # rpcfg.home() needs no config.toml (it is just $RPFARM_HOME or
    # ~/.rpfarm) -- config (and the sync-pod-touching storage snapshot
    # below) is only ever loaded when Use Billing is on, so the common
    # local-only view works with no `rpfarm setup` at all, and costs
    # nothing extra even when it is set up.
    local_dir = rpcfg.home() / "ledger"
    records = rpledger.load_all(local_dir)

    project_filter = (node.evalParm("rpfarm_project") or "").strip()
    user_filter = (node.evalParm("rpfarm_user") or "").strip()
    since_epoch = _parse_date(node.evalParm("rpfarm_since"))
    until_epoch = _parse_date(node.evalParm("rpfarm_until"), end_of_day=True)
    usebilling = bool(node.evalParm("rpfarm_usebilling"))

    vol_total = None
    merged = records
    cfg = None
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
    # Keyed by "<user>/<project>", not project alone: two users can share a
    # project name (the volume itself namespaces by user --
    # /workspace/projects/<user>/<project>), and the storage snapshot below
    # is keyed the same way.
    last_by_project = {}
    for rec in records:
        if rec.get("record") == "cook_summary":
            continue
        project = rec.get("project")
        user = rec.get("user")
        ts = rec.get("ended") or rec.get("started")
        if not project or not user or ts is None:
            continue
        key = "{}/{}".format(user, project)
        if key not in last_by_project or ts > last_by_project[key][0]:
            last_by_project[key] = (ts, user, project)

    # Gated on usebilling (review finding): _storage_snapshot touches the
    # sync pod over the network, which "Use Billing" off must never do,
    # config present or not -- this used to run unconditionally.
    if usebilling:
        sizes_by_key, vol_bytes_total = _storage_snapshot(cfg, node)
    else:
        sizes_by_key, vol_bytes_total = {}, None

    period_start = since_epoch if since_epoch is not None else (now - 90 * 86400.0)
    period_end = until_epoch if until_epoch is not None else now
    period_days = max(1.0, (period_end - period_start) / 86400.0)

    cleanup = []
    for key, (ts, user, project) in sorted(last_by_project.items()):
        age_days = (now - ts) / 86400.0
        if age_days <= _STALE_DAYS:
            continue
        entry_bytes = sizes_by_key.get(key)
        monthly_cost = None
        if usebilling and vol_total is not None and vol_bytes_total and entry_bytes is not None:
            monthly_cost = (vol_total / period_days * 30.0) * (entry_bytes / vol_bytes_total)
        cleanup.append({
            "user": user,
            "project": project,
            "last_cook": time.strftime("%Y-%m-%d", time.gmtime(ts)),
            "age_days": age_days,
            "bytes": entry_bytes,
            "monthly_cost": monthly_cost,
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
            size_str = "{:.2f} GB".format(c["bytes"] / 2**30) if c["bytes"] is not None else "size n/a"
            cost_str = "{} /mo".format(_fmt_money(c["monthly_cost"])) if c["monthly_cost"] is not None else "$/mo n/a"
            lines.append("  {:<24s} last cook {}  ({:.0f}d ago)  {:>10s}  {}".format(
                "{}/{}".format(c["user"], c["project"]), c["last_cook"], c["age_days"], size_str, cost_str))
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


def _unique_csv_path(out_dir, stamp):
    """rpfarm_stats_<stamp>.csv, or the same with a "-2", "-3", ... suffix
    if that name is already taken -- the timestamp alone is only
    second-granularity, so two exports (or export + a leftover from a
    prior run) in the same second would otherwise silently overwrite each
    other's CSV via to_csv's plain open(path, "w")."""
    path = out_dir / "rpfarm_stats_{}.csv".format(stamp)
    n = 2
    while path.exists():
        path = out_dir / "rpfarm_stats_{}-{}.csv".format(stamp, n)
        n += 1
    return path


def onExportCsv(kwargs):
    node = kwargs["node"]
    try:
        filtered, by_project, by_user, cleanup, vol_total, usebilling = compute(node)
        out_dir = rpcfg.home() / "exports"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        path = _unique_csv_path(out_dir, stamp)
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
    cleanup candidates -- projects with no cook in over 30 days. With Use
    Billing on, each candidate also gets its real on-disk size (Task 12's
    housekeeping `ls`, via the sync pod) and an estimated $/month: that
    project's byte share of the period's actual
    `GET /billing/networkvolumes` charge, prorated to 30 days -- never a
    guessed $/GB rate. With Use Billing off, size/$/month show as "n/a"
    and nothing touches the sync pod or the network at all.

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

    # Baked into the generated CreateScript as an "opuserdata" line, so
    # every instance is the right shape from the moment it is created.
    sn.setUserData("nodeshape", NODE_SHAPE)

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
    # The icon rides inside the asset rather than as a file on disk:
    # nothing to install, nothing to lose, and it follows the .hda
    # wherever it is copied.
    definition.addSection("IconSVG", ICON_SVG)
    definition.setIcon("opdef:.?IconSVG")
    definition.addSection("PythonModule", PYTHON_MODULE)
    definition.setParmTemplateGroup(ptg)

    definition.addSection(
        "OnCreated",
        'node = kwargs["node"]\n'
        + FAMILY_ONCREATED +
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
