"""One-shot builder for hda/runpodfarm_download.hda.

Task 10 -- the mirror image of ``scripts/build_runpodfarm_upload_hda.py``
(read that file's own docstring first; this one only documents where
download differs). Builds a Top/subnet node containing a pythonprocessor1
(work item generation/cook) and a localscheduler (forces this node's items
to cook on PDG's local scheduler regardless of the parent topnet's own
scheduler -- same reasoning and the same belt-and-suspenders OnCreated
event as the upload node, see its Help), wires them up, sets the
``rpfarm_*`` parameter interface, converts the subnet to a digital asset,
and saves it as a single packed .hda file at the path given on the command
line. Items dispatch out of process through ``rpfarm.package_runner``
(Ruling R22, same runner module the upload node uses -- see Task 10's
addendum for why a shared runner was chosen over a second file) by
default.

Regenerate the checked-in asset with, e.g.::

    HYTHON=/Applications/Houdini/Houdini22.0.368/Frameworks/Houdini.framework/Versions/Current/Resources/bin/hython
    OUT=/tmp/runpodfarm_download.hda
    "$HYTHON" scripts/build_runpodfarm_download_hda.py "$OUT"
    rm -rf hda/runpodfarm_download.hda
    hotl -t hda/runpodfarm_download.hda "$OUT"          # git-tracked expanded form
    hotl -l hda/runpodfarm_download.hda \\
        ~/Library/Preferences/houdini/22.0/otls/runpodfarm_download.hda  # install

Only ``hotl -l`` (the counterpart of ``-t``) round-trips correctly for this
directory layout -- ``-c``/``-C`` are for the other (``-x``/``-X``) expanded
format and silently produce an interface-less asset here (verified on the
upload node; see its own docstring).
"""
import os
import pathlib
import sys

import hou
import pdg

OUT_HDA = sys.argv[1] if len(sys.argv) > 1 else "/tmp/runpodfarm_download.hda"

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
ICON_SVG = (REPO_ROOT / "hda" / "icons" / "runpodfarm_download.svg").read_text()

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

GENERATE_CODE = '''\
# Called when this node should generate new work items from upstream items.
#
# self             -   A reference to the current pdg.Node instance
# item_holder      -   A pdg.WorkItemHolder for constructing and adding work items
# upstream_items   -   The list of work items in the node above, or empty list if there are no inputs
# generation_type  -   The type of generation, e.g. pdg.generationType.Static, Dynamic, or Regenerate
#
# See this node's Help for the design: modes, and why items dispatch out of
# process by default through rpfarm.package_runner (Ruling R22).

import json
import os
import pathlib
import posixpath
import shlex
import shutil
import sys
import tempfile

import hou

# generate runs in this same hython process (not a spawned one, unlike
# cooktask below), but rpfarm still is not on Houdini's own sys.path --
# same bootstrap as cooktask, see its comment for why $RPFARM_ROOT/~/.rpfarm/src.
_RPFARM_ROOT = pathlib.Path(os.environ.get("RPFARM_ROOT", pathlib.Path.home() / ".rpfarm" / "src"))
if str(_RPFARM_ROOT) not in sys.path:
    sys.path.insert(0, str(_RPFARM_ROOT))

from rpfarm import config as rpcfg
from rpfarm import houdini_local as rphou
from rpfarm import packages as rppkg
from rpfarm import pods as rppods
from rpfarm.runpod_api import RunPodAPI, RunPodError, pod_public_endpoint
from rpfarm.worker_client import WorkerClient

node = self.topNode().parent()

mode = node.evalParm("rpfarm_mode")
package_gb = node.evalParm("rpfarm_packagegb")
overwrite = node.evalParm("rpfarm_overwrite")

cfg = rpcfg.load()
api = RunPodAPI(cfg.api_key)
token = rpcfg.session_token()
with open(cfg.ssh_key_path + ".pub") as f:
    pubkey = f.read()

# Both modes need the sync pod: "outputs" to stat remote file sizes for
# packaging, "custom" to list each remote directory's files in the first
# place (there is no local filesystem to walk -- the files only exist on
# the farm volume).
pod = rppods.ensure_sync_pod(api, cfg, token, pubkey)
sync_client = WorkerClient(pod["id"], token)

def _stat_sizes(remotes):
    """One exec() for every distinct remote file in this generate -- not one
    per file (addendum: "одним вызовом на пакет, не по файлу")."""
    sizes = {}
    if not remotes:
        return sizes
    cmd = "stat -c '%s %n' " + " ".join(shlex.quote(r) for r in remotes)
    result = sync_client.exec(cmd, timeout_s=rppkg._scaled_timeout(0))
    if result.get("exit_code") == 0:
        for line in result.get("stdout", "").splitlines():
            line = line.strip()
            if not line:
                continue
            size_str, _sep, path = line.partition(" ")
            try:
                sizes[path] = int(size_str)
            except ValueError:
                pass
    else:
        node.addWarning(
            "stat on {} remote output file(s) failed (sizes default to 0): {}".format(
                len(remotes), (result.get("stderr") or "").strip()
            )
        )
    return sizes


# (item dict, parent upstream pdg.WorkItem or None) -- "outputs" mode's
# items each need an explicit parent (see below); "custom" mode's don't
# (this node takes no upstream input in that mode).
planned = []

if mode == "outputs":
    # Each upstream item was tagged with this cook's {local prefix: farm
    # prefix} map by the scheduler's _tagPathMap (hda/runpodfarm_scheduler.hda/
    # .../PythonModule, onSchedule) before it was ever serialized -- reading
    # it off the item, rather than needing a handle on the scheduler node
    # itself, is exactly why that attribute exists (see its own comment).
    # Its farm-side outputs are in resultData; rppkg.localize_via_pathmap
    # turns each one back into the local path the ROP was aimed at by a
    # plain longest-prefix replacement (the same operation localizePath
    # does through PDG's global path map, just entirely self-contained here
    # so this node never needs the scheduler's own Python object).
    #
    # Planned per upstream item, not pooled across all of them: PDG_Generate
    # When is set to AllUpstreamCooked below (this node's own OnCreated/
    # builder), which for a connected input makes onGenerate a DYNAMIC
    # generation pass -- and pdg.WorkItemHolder.addWorkItem then requires an
    # explicit `parent` for every item it creates (verified live: omitting
    # it raises "Dynamic work items must have an explicitly specified
    # parent" -- see the Task 10 report). Keeping one upstream item's
    # outputs as their own build_download_items() call, rather than
    # flattening every upstream item's pairs into one list first, is what
    # makes a single, correct parent available for each resulting item.
    # One line, every generate pass. An upstream item with no output files is
    # the single most common reason this node plans nothing, and without this
    # the symptom is a silent zero -- which is how three rendered frames went
    # undownloaded for three live runs.
    _missing = [u.name for u in upstream_items if not list(u.resultData)]
    print("[rpfarm-download] generate: {} upstream item(s), {} without output "
          "files{}".format(len(upstream_items), len(_missing),
                           " ({})".format(", ".join(_missing[:5])) if _missing else ""),
          flush=True)
    for up in upstream_items:
        raw_map = up.stringAttribValue("rpfarm_pathmap") or ""
        try:
            path_map = json.loads(raw_map) if raw_map else {}
        except ValueError:
            path_map = {}
        item_pairs = []
        result_data = list(up.resultData)
        for rd in result_data:
            pair = rppkg.map_output_pair(rppkg.result_data_path(rd), path_map)
            if pair:
                item_pairs.append(pair)
        if not item_pairs:
            if result_data:
                # Reported outputs that map to nothing local is a real fault
                # (a missing or wrong rpfarm_pathmap), not "nothing to do" --
                # say so instead of quietly planning zero work items.
                node.addWarning(
                    "upstream item {} reported {} output(s), none of which mapped "
                    "back to a local path (rpfarm_pathmap has {} entry(ies))".format(
                        up.name, len(result_data), len(path_map)))
            continue
        sizes = _stat_sizes(sorted({r for r, _l in item_pairs}))
        for it in rppkg.build_download_items(mode, item_pairs, package_gb, sizes):
            planned.append((it, up))

    if not upstream_items:
        node.addWarning("Outputs mode with no upstream input: nothing to download")

elif mode == "custom":
    pairs = []
    sizes = {}
    for i in range(1, node.evalParm("rpfarm_custom") + 1):
        remote_dir = node.evalParm("rpfarm_remote{}".format(i))
        local_dir = node.evalParm("rpfarm_local{}".format(i))
        if not remote_dir or not local_dir:
            continue
        find_cmd = "find {} -type f -printf '%s %p\\\\n'".format(shlex.quote(remote_dir))
        result = sync_client.exec(find_cmd, timeout_s=rppkg._scaled_timeout(0))
        if result.get("exit_code") != 0:
            node.addWarning(
                "listing remote dir {} failed: {}".format(remote_dir, (result.get("stderr") or "").strip())
            )
            continue
        for line in result.get("stdout", "").splitlines():
            line = line.strip()
            if not line:
                continue
            size_str, _sep, remote_path = line.partition(" ")
            try:
                size = int(size_str)
            except ValueError:
                continue
            rel = posixpath.relpath(remote_path, remote_dir)
            if rel == ".":
                local_path = os.path.join(local_dir, os.path.basename(remote_path))
            else:
                local_path = os.path.join(local_dir, rel.replace("/", os.sep))
            pairs.append((remote_path, local_path))
            sizes[remote_path] = size
    for it in rppkg.build_download_items(mode, pairs, package_gb, sizes):
        planned.append((it, None))
else:
    raise hou.NodeError("unknown rpfarm_mode: {}".format(mode))

# Ruling R22 (download side, Task 10): must not block Houdini's UI, and
# progress must be visible per package -- so items dispatch OUT of process
# by default (rpfarm_inprocess off), through the SAME rpfarm.package_runner
# module the upload node uses (see this node's Help for why a shared runner
# was chosen). "kind": "download" in the payload is what routes it to
# rpfarm.packages.run_download_item instead of run_upload_item.
in_process = bool(node.evalParm("rpfarm_inprocess"))
# The interpreter is resolved explicitly, never off PATH. A Houdini launched
# from the macOS Dock inherits a minimal PATH where "python3" is Xcode's 3.9,
# which has no tomllib, so every package item died on `import rpfarm.config`
# before doing any work -- and every headless run went through a shell whose
# PATH started with a modern python, so this passed the smoke for the wrong
# reason. rphou.resolve_package_python prefers the plain python bundled with
# THIS running Houdini ($HFS), which is guaranteed present and takes no
# licence (hython would take one per package).
python3, python3_why = rphou.resolve_package_python(
    hfs=hou.getenv("HFS") or hou.expandString("$HFS"))
print("[rpfarm-download] package runner interpreter: {}  ({})".format(python3, python3_why))
items_dir = tempfile.mkdtemp(prefix="rpfarm_download_items_")
# See the upload node's onGenerate for why this has to be resolved from
# rppkg.__file__ (already imported into THIS process) rather than trusted
# to cwd or an inherited PYTHONPATH.
rpfarm_pkg_root = str(pathlib.Path(rppkg.__file__).resolve().parent.parent)


def _make_command(item_json_path):
    # No shell involved -- see the upload node's onGenerate for why a
    # "VAR=value cmd" shell prefix does not work here.
    return "{} -m rpfarm.package_runner {}".format(shlex.quote(python3), shlex.quote(item_json_path))


def _write_item_payload(name, it):
    path = os.path.join(items_dir, "{}.json".format(name))
    with open(path, "w") as f:
        json.dump({"kind": "download", "item": it, "overwrite": overwrite}, f)
    return path


for it, parent in planned:
    # Name from the parent, not a per-call counter: AllUpstreamCooked makes
    # this dynamic generation (see above), and PDG may invoke onGenerate
    # more than once as different upstream items finish cooking -- a plain
    # "download_{:03d}".format(n) restarting at 0 (or continuing from a
    # stale count) on every separate invocation could then collide with a
    # name an earlier invocation already used. it["index"] is only unique
    # WITHIN one upstream item's own build_download_items() call (outputs
    # mode plans each upstream item separately, see above); a given
    # upstream item is only ever a parent once (each work item cooks and
    # reports resultData exactly once), so parent.name + that per-parent
    # index is globally unique across however many onGenerate calls this
    # node ends up getting. "custom" mode has no parent and exactly one
    # build_download_items() call total, so its own it["index"] is already
    # globally unique on its own.
    name = "download_{}_{:03d}".format(parent.name, it["index"]) if parent is not None else "download_{:03d}".format(it["index"])
    kwargs = {"name": name, "inProcess": in_process}
    if parent is not None:
        kwargs["parent"] = parent
    wi = item_holder.addWorkItem(**kwargs)
    wi.setStringAttrib("rpfarm_item", json.dumps(it))
    wi.setStringAttrib("overwrite", overwrite)
    wi.setIntAttrib("bytes", it["bytes"])
    wi.setIntAttrib("files", len(it["files"]))
    if not in_process:
        wi.setCommand(_make_command(_write_item_payload(name, it)))
        wi.addEnvironmentVar("PYTHONPATH", rpfarm_pkg_root)
'''

COOKTASK_CODE = '''\
# Called when an in process work item needs to cook. In process work items
# are created by passing the [in_process] flag when constructing the item
# in the [onGenerate] callback -- onGenerate above only does that when the
# Cook in process toggle (rpfarm_inprocess) is on; by default (Ruling R22)
# every item instead carries a shell ".command" (rpfarm.package_runner) and
# cooks out of process through this node's own localscheduler, in parallel
# across its slots, without blocking Houdini's UI -- see this node's Help.
# This callback is the FALLBACK path for the toggle: kept working (and
# still fully unit-testable via run_download_item) because it costs nothing
# to keep, and is a straightforward way to debug a package's download logic
# directly in Houdini's own process without going through package_runner's
# subprocess + pdgcmd round trip.
#
# self              -   A reference to the current pdg.Node instance
# work_item         -   The work item being cooked by this callback

import json
import os
import pathlib
import sys
import time

_RPFARM_ROOT = pathlib.Path(os.environ.get("RPFARM_ROOT", pathlib.Path.home() / ".rpfarm" / "src"))
if str(_RPFARM_ROOT) not in sys.path:
    sys.path.insert(0, str(_RPFARM_ROOT))

from rpfarm import config as rpcfg
from rpfarm import packages as rppkg
from rpfarm import pods as rppods
from rpfarm import sync as rpsync
from rpfarm.runpod_api import RunPodAPI, pod_public_endpoint
from rpfarm.worker_client import WorkerClient

cfg = rpcfg.load()
api = RunPodAPI(cfg.api_key)
token = rpcfg.session_token()
with open(cfg.ssh_key_path + ".pub") as f:
    pubkey = f.read()

pod = rppods.ensure_sync_pod(api, cfg, token, pubkey)
ip, port = pod_public_endpoint(pod, 22)
sftp = rpsync.SftpTarget(host=ip, port=port, key_path=cfg.ssh_key_path)
sync_client = WorkerClient(pod["id"], token)

item = json.loads(work_item.stringAttribValue("rpfarm_item"))
overwrite = work_item.stringAttribValue("overwrite") or "newer"


def progress_cb(done, total, speed):
    work_item.setStringAttrib("progress", "{:.0f}/{:.0f} MB".format(done / 2**20, total / 2**20))


t0 = time.time()
stats = rppkg.run_download_item(item, cfg, sftp, sync_client, overwrite, progress_cb)
elapsed = time.time() - t0

work_item.setFloatAttrib("seconds", elapsed)
work_item.setFloatAttrib("mbps", stats["bytes"] / 2**20 / max(1e-3, elapsed))
work_item.setIntAttrib("bytes", stats["bytes"])
work_item.setIntAttrib("files", stats["files"])
'''

HELP_TEXT = '''\
= RunPodFarm Download =

#type: node
#context: top
#internal: runpodfarmdownload
#icon: TOP/pythonprocessor

"""Download files from the RunPodFarm volume to local disk, as a normal TOP
work item generator -- progress is visible per package as it cooks."""

Work item = one package of files. Each package cooks on PDG's *local*
scheduler (this node overrides `Scheduler` to its own internal
`localscheduler`, never `runpodfarm_scheduler` -- same reasoning as the
upload node: this download has to run on the machine that owns the local
disk it's downloading to, not dispatch onto the farm itself). The override
is a Python expression on the internal Python Processor's `Scheduler` parm
re-resolving the sibling `localscheduler` node's absolute path at cook
time -- see [Node:top/runpodfarm_upload]'s Help for why a bare relative
name doesn't work here. This node's `OnCreated` event re-asserts the same
expression once more when a new instance is made.

Packages cook *out of process* by default (Ruling R22), through the same
`rpfarm.package_runner` module the upload node uses (`rpfarm/package_runner.py`)
-- each work item's command is `python3 -m rpfarm.package_runner <item.json>`,
whose payload's `"kind": "download"` selects `rpfarm.packages.run_download_item`
over the upload path. See [Node:top/runpodfarm_upload]'s Help for why PDG's
Python Processor requires a shell `.command` for genuine out-of-process
dispatch, and why a plain `python3` (not `hython`) runs it. The Cook In
Process toggle below switches back to the old callback-only path (this
node's `cooktask`) for debugging.

Cooking this node when it has an upstream input (`Upstream outputs` mode):
cook THIS node -- the downstream-most node in the graph -- in one single
`cookWorkItems()` call. Do not cook the upstream farm-scheduler generator
node separately first and then cook this one: each top-level
`cookWorkItems()` call starts its own independent PDG cook, so a second
call does not treat the first call's already-`CookedSuccess` items as up
to date -- it recooks the upstream node from scratch too, on
`runpodfarm_scheduler`, which means a SECOND real (and separately billed)
GPU pod for work that already ran once. One cook of this node is also the
intended pattern: it lets `gen`'s item cook on the farm and this node's own
download item cook locally, in one graph cook, one GPU pod total (live-
verified; see the Task 10 report, `.superpowers/sdd/2026-09-02-rpfarm-v2/
task-10-report.md`).

@parameters

Mode:
    #id: rpfarm_mode
    `Upstream outputs` reads each upstream work item's `resultData` (farm
    paths reported while it ran) and localizes them via the `rpfarm_pathmap`
    attribute the scheduler stamps onto every item it schedules -- no
    upstream input means nothing to download (a warning, not an error).
    `Custom paths` downloads exactly the remote -> local directory pairs
    below; the files under each remote directory are listed on the sync pod
    (`find <dir> -type f -printf '%s %p\\n'`) at generate time, since they
    only exist on the farm volume.

Package Size (GB):
    #id: rpfarm_packagegb

    Files are grouped into work items no larger than this (a single file
    bigger than the limit still gets its own item). Packages never span two
    (local dir, farm dir) pairs -- see `rpfarm.packages.group_download_pairs`.

Overwrite:
    #id: rpfarm_overwrite

    `newer` (default) skips a local file that is not older than its remote
    counterpart (`rclone --update`). `always` adds no extra flag, which is
    rclone's own default comparison: it still skips a file whose size AND
    modification time already match the remote exactly, and re-transfers
    anything else -- "always" here means "no `--update`/`--ignore-existing`
    override", not "every file every time". `never` skips anything that
    already exists locally, regardless of mtime (`rclone --ignore-existing`).

Custom Paths:
    #id: rpfarm_custom

    Multiparm of remote -> local directory pairs, used in Custom mode.

Cook In Process (debug):
    #id: rpfarm_inprocess

    Off (default): packages download out of process, in parallel, without
    blocking Houdini (Ruling R22; see above). On: cook in this Houdini
    session instead -- blocks the UI, one package at a time, useful for
    stepping through `run_download_item()` directly while debugging.

@related

- [Node:top/runpodfarm_upload]
- [Node:top/runpodfarm_scheduler]
- [Node:top/pythonprocessor]
- [Node:top/localscheduler]
'''


def main():
    obj = hou.node("/obj")
    build_net = obj.createNode("topnet", "rpfarm_download_build")
    sn = build_net.createNode("subnet", "runpodfarmdownload_build")

    pp = sn.createNode("pythonprocessor", "pythonprocessor1")
    localsched = sn.createNode("localscheduler", "localscheduler")

    pp.setInput(0, sn.indirectInputs()[0])
    out0 = sn.node("output0")
    out0.setInput(0, pp, 0)

    # See the upload node's builder for why this must be an absolute-path
    # Python expression, not a bare relative node name.
    pp.parm("topscheduler").setExpression(
        'hou.pwd().parent().path() + "/localscheduler"', language=hou.exprLanguage.Python
    )
    # "outputs" mode's onGenerate reads upstream_items[i].resultData -- which
    # is only populated once an upstream item has actually COOKED, not
    # merely once it has been generated (PDG's default "Generate When" is
    # Automatic, which for this graph shape resolves to AllUpstreamGenerated
    # -- i.e. generate() would fire as soon as the upstream node's work
    # items structurally exist, before they ever run, so resultData would
    # always be empty; live-verified while building this node, see the
    # Task 10 report). AllUpstreamCooked makes PDG defer (and, for dynamic
    # upstream generation, re-invoke) this node's generate() until every
    # upstream item has actually finished cooking.
    pp.parm("pdg_workitemgeneration").set(int(pdg.generateWhen.AllUpstreamCooked))
    pp.parm("generate").set(GENERATE_CODE)
    pp.parm("cooktask").set(COOKTASK_CODE)

    pp.moveToGoodPosition()
    localsched.moveToGoodPosition()
    out0.moveToGoodPosition()

    ptg = hou.ParmTemplateGroup()

    mode_pt = hou.StringParmTemplate(
        "rpfarm_mode", "Mode", 1, default_value=("outputs",),
        menu_items=("outputs", "custom"), menu_labels=("Upstream Outputs", "Custom Paths"),
    )
    mode_pt.setHelp("Upstream outputs: resultData of every upstream work item, localized via rpfarm_pathmap. Custom paths: exactly the remote -> local pairs below.")

    packagegb_pt = hou.FloatParmTemplate(
        "rpfarm_packagegb", "Package Size (GB)", 1, default_value=(1.5,), min=0.1, max=16, max_is_strict=False
    )
    packagegb_pt.setHelp("Files are grouped into work items no larger than this (a single bigger file still gets its own item).")

    overwrite_pt = hou.StringParmTemplate(
        "rpfarm_overwrite", "Overwrite", 1, default_value=("newer",),
        menu_items=("newer", "always", "never"), menu_labels=("If Newer", "Always", "Never"),
    )
    overwrite_pt.setHelp(
        "newer: rclone --update (skip a local file that is not older than the remote). "
        "always: no extra flag -- rclone's own default comparison, which still skips a file "
        "whose size and mtime already match exactly. never: rclone --ignore-existing."
    )

    remote_pt = hou.StringParmTemplate("rpfarm_remote#", "Remote", 1, default_value=("",))
    local_pt = hou.StringParmTemplate(
        "rpfarm_local#", "Local", 1, default_value=("",),
        string_type=hou.stringParmType.FileReference, file_type=hou.fileType.Directory,
    )
    custom_pt = hou.FolderParmTemplate(
        "rpfarm_custom", "Custom Paths", parm_templates=(remote_pt, local_pt),
        folder_type=hou.folderType.MultiparmBlock, default_value=0,
    )
    custom_pt.setHelp("Used in Custom mode: remote directory on the farm volume -> local directory to download it into.")

    # Ruling R22: out of process (this off) is the default. Kept as a
    # toggle for the same debug reason as the upload node's.
    inprocess_pt = hou.ToggleParmTemplate("rpfarm_inprocess", "Cook In Process (debug)", default_value=False)
    inprocess_pt.setHelp(
        "Off (default): packages download out of process, in parallel, without blocking Houdini. "
        "On: cook in this Houdini session instead -- blocks the UI, one package at a time, useful for debugging."
    )

    for pt in (mode_pt, packagegb_pt, overwrite_pt, custom_pt, inprocess_pt):
        ptg.append(pt)

    sn.setParmTemplateGroup(ptg)

    # Baked into the generated CreateScript as an "opuserdata" line, so
    # every instance is the right shape from the moment it is created.
    sn.setUserData("nodeshape", NODE_SHAPE)

    if os.path.exists(OUT_HDA):
        os.remove(OUT_HDA)

    new_type = sn.createDigitalAsset(
        name="runpodfarmdownload",
        hda_file_name=OUT_HDA,
        description="RunPodFarm Download",
        min_num_inputs=0,
        max_num_inputs=1,
        ignore_external_references=True,
    )

    definition = new_type.type().definition()
    definition.addSection("Help", HELP_TEXT)
    # The icon rides inside the asset rather than as a file on disk:
    # nothing to install, nothing to lose, and it follows the .hda
    # wherever it is copied.
    definition.addSection("IconSVG", ICON_SVG)
    definition.setIcon("opdef:.?IconSVG")
    # See the upload node's builder for why this has to be set on the
    # DEFINITION's own template group too, not just the live instance.
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
