"""One-shot builder for hda/runpodfarm_upload.hda.

Builds a Top/subnet node containing a pythonprocessor1 (work item
generation/cook) and a localscheduler (forces this node's items to cook on
PDG's local scheduler regardless of the parent topnet's own scheduler --
see this node's Help), wires them up, sets the rpfarm_* parameter
interface (including the rpfarm_inprocess debug toggle), adds an
OnCreated event (Python, via ExtraFileOptions IsPython/IsScript -- see
main() below for how that's set) that re-asserts the scheduler-override
expression, converts the subnet to a digital asset, and saves it as a
single packed .hda file at the path given on the command line. By
default work items dispatch out of process through
rpfarm.package_runner (Ruling R22) rather than cooking in this node's own
cooktask callback.

Regenerate the checked-in asset with, e.g.::

    HYTHON=/Applications/Houdini/Houdini22.0.368/Frameworks/Houdini.framework/Versions/Current/Resources/bin/hython
    OUT=/tmp/runpodfarm_upload.hda
    "$HYTHON" scripts/build_runpodfarm_upload_hda.py "$OUT"
    rm -rf hda/runpodfarm_upload.hda
    hotl -t hda/runpodfarm_upload.hda "$OUT"          # git-tracked expanded form
    hotl -l hda/runpodfarm_upload.hda \\
        ~/Library/Preferences/houdini/22.0/otls/runpodfarm_upload.hda  # install

Only ``hotl -l`` (the counterpart of ``-t``) round-trips correctly for this
directory layout -- ``-c``/``-C`` are for the other (``-x``/``-X``) expanded
format and silently produce an interface-less asset here.
"""
import os
import sys

import hou

OUT_HDA = sys.argv[1] if len(sys.argv) > 1 else "/tmp/runpodfarm_upload.hda"

GENERATE_CODE = '''\
# Called when this node should generate new work items from upstream items.
#
# self             -   A reference to the current pdg.Node instance
# item_holder      -   A pdg.WorkItemHolder for constructing and adding work items
# upstream_items   -   The list of work items in the node above, or empty list if there are no inputs
# generation_type  -   The type of generation, e.g. pdg.generationType.Static, Dynamic, or Regenerate
#
# See this node's Help for the design: modes, the Install Houdini preset,
# why the post-command runs as one extra work item instead of once per
# package (Ruling R3), and why items dispatch out of process by default
# through rpfarm.package_runner (Ruling R22).

import json
import math
import os
import pathlib
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
from rpfarm import deps as rpdeps
from rpfarm import packages as rppkg
from rpfarm.runpod_api import RunPodAPI, RunPodError

node = self.topNode().parent()

mode = node.evalParm("rpfarm_mode")
preset = node.evalParm("rpfarm_preset")
job_dir = hou.getenv("JOB") or hou.expandString("$HIP")
project = node.evalParm("rpfarm_project") or os.path.basename(os.path.normpath(job_dir))
package_gb = node.evalParm("rpfarm_packagegb")

cfg = rpcfg.load()
user = cfg.user

custom = []
for i in range(1, node.evalParm("rpfarm_custom") + 1):
    local = node.evalParm("rpfarm_local{}".format(i))
    remote = node.evalParm("rpfarm_remote{}".format(i))
    if local and remote:
        custom.append((local, remote))
post_command = node.evalParm("rpfarm_postcmd")

if preset == "install_houdini":
    tar = node.evalParm("rpfarm_houtar")
    ver = node.evalParm("rpfarm_houver")
    custom, post_command = rppkg.houdini_install_preset(tar, ver)
    mode = "custom"

refs = rpdeps.collect_refs() if mode == "deps" else []

items = rppkg.build_upload_items(mode, job_dir, user, project, custom, refs, package_gb)

if mode == "deps":
    # runpodfarm_scheduler's _loadPathMap merges this in -- see
    # rpfarm.packages.write_pathmap. resolve_entries is pure/cheap
    # (filesystem stats only) so recomputing it here for the path map
    # alone, rather than widening build_upload_items' return shape, is a
    # deliberate small duplication.
    remote_project = "/workspace/projects/{}/{}".format(user, project)
    _, path_map = rpdeps.resolve_entries(refs, job_dir, remote_project)
    rppkg.write_pathmap(job_dir, path_map)

# Volume auto-grow (design spec 4.1): grow when usage would exceed ~85%.
# TODO(Task 12): this can only see the size of the packages THIS cook is
# about to upload, not real used-space on the volume -- housekeeping's
# usage index is still a stub. Until that lands this is a coarse guard
# (grow when this cook's own upload alone would already cross 85% of
# total capacity), not a real (used + upload) / total check. Replace the
# body of this block with a real usage lookup once Task 12 lands.
try:
    upload_bytes = sum(it["bytes"] for it in items)
    if upload_bytes and cfg.volume_id:
        api = RunPodAPI(cfg.api_key)
        vol = api.get_volume(cfg.volume_id) or {}
        total_gb = float(vol.get("size") or 0)
        upload_gb = upload_bytes / 2**30
        if total_gb and upload_gb > 0.85 * total_gb:
            new_gb = int(math.ceil((total_gb + upload_gb) / 10.0) * 10)
            api.resize_volume(cfg.volume_id, new_gb)
            node.addWarning("grew volume {} -> {} GB for this upload".format(cfg.volume_id, new_gb))
except RunPodError as e:
    node.addWarning("volume auto-grow check failed (continuing): {}".format(e))

compress = rppkg.resolve_compress_flag(node.evalParm("rpfarm_compress"))

# Ruling R22: uploads must not block Houdini's UI, and progress must be
# visible per package -- so items dispatch OUT of process by default
# (rpfarm_inprocess off). PDG's pythonprocessor only runs a callback-only
# item out of process when it never happens at all (see cooktask below);
# the actual out-of-process path needs a shell ".command" instead, so
# each item's command points at rpfarm.package_runner (rpfarm/package_runner.py),
# fed the item (+ this node's compress flag) as a small JSON file. That
# module is stdlib-only like the rest of rpfarm, so a plain "python3" is
# enough -- no need to pay hython's startup cost per package, and
# sys.executable here would be wrong anyway (this process is hython).
in_process = bool(node.evalParm("rpfarm_inprocess"))
python3 = shutil.which("python3") or "python3"
items_dir = tempfile.mkdtemp(prefix="rpfarm_upload_items_")
# "python3 -m rpfarm.package_runner" has to resolve the rpfarm package
# BEFORE any of package_runner's own code (its $RPFARM_ROOT/~/.rpfarm/src
# bootstrap included) ever runs -- -m resolution happens at interpreter
# startup, off sys.path, which for a plain "python3" subprocess is not
# this checkout unless something puts it there. $RPFARM_ROOT alone does
# NOT do that (verified live: it fixed nothing here -- see the Task 9
# report); PYTHONPATH does, so it's set explicitly and unconditionally
# from where `rppkg` -- already imported into THIS process -- actually
# lives, rather than trusting cwd or an env var to happen to line up.
rpfarm_pkg_root = str(pathlib.Path(rppkg.__file__).resolve().parent.parent)


def _make_command(item_json_path):
    # No shell involved -- the scheduler runs this via shlex.split() +
    # subprocess.Popen(..., no shell=True), so a "VAR=value cmd" shell
    # prefix does NOT set an env var here: it is parsed as the literal
    # (nonexistent) executable "VAR=value" and fails instantly with no
    # output at all (verified live -- see the Task 9 report). PYTHONPATH
    # goes through the item's own environment (addEnvironmentVar) instead.
    return "{} -m rpfarm.package_runner {}".format(shlex.quote(python3), shlex.quote(item_json_path))


def _write_item_payload(name, it, compress_flag):
    path = os.path.join(items_dir, "{}.json".format(name))
    with open(path, "w") as f:
        json.dump({"item": it, "compress": compress_flag}, f)
    return path


def _set_out_of_process(wi, item_json_path):
    wi.setCommand(_make_command(item_json_path))
    wi.addEnvironmentVar("PYTHONPATH", rpfarm_pkg_root)


pkg_items = []
for it in items:
    name = "upload_{:03d}".format(it["index"])
    wi = item_holder.addWorkItem(name=name, inProcess=in_process)
    wi.setStringAttrib("rpfarm_item", json.dumps(it))
    wi.setStringAttrib("rpfarm_role", "package")
    wi.setIntAttrib("bytes", it["bytes"])
    wi.setIntAttrib("files", len(it["files"]))
    wi.setIntAttrib("compress", 1 if compress else 0)
    if not in_process:
        _set_out_of_process(wi, _write_item_payload(name, it, compress))
    pkg_items.append(wi)

if post_command and pkg_items:
    post_dict = {
        "index": len(items),
        "local_root": "",
        "remote_root": "",
        "files": [],
        "bytes": 0,
        "post_command": post_command,
    }
    post_item = item_holder.addWorkItem(name="upload_post", inProcess=in_process)
    post_item.setStringAttrib("rpfarm_item", json.dumps(post_dict))
    post_item.setStringAttrib("rpfarm_role", "post")
    post_item.setIntAttrib("bytes", 0)
    post_item.setIntAttrib("files", 0)
    post_item.setIntAttrib("compress", 0)
    if not in_process:
        _set_out_of_process(post_item, _write_item_payload("upload_post", post_dict, False))
'''

ADDDEPS_CODE = '''\
# Called when the node has generated work items so that dependencies can
# be added between work items in this node.
#
# self              -   A reference to the current pdg.Node instance
# dependency_holder -   A pdg.WorkItemHolder for adding pairs of items that should have a dependency
# internal_items    -   The list of items, either all static items or a group of dynamic items
# is_static         -   Boolean indicating if the items list contains static items
#
# Ruling R3: the post-command work item ("upload_post", rpfarm_role=post)
# must cook after every package item, not interleaved with them. Item-to-
# item dependencies can't be wired up inline while items are still being
# created in onGenerate (PDG resolves them in this separate pass), so this
# is where the post item is made to depend on every package item.

posts = [it for it in internal_items if it.stringAttribValue("rpfarm_role") == "post"]
packages = [it for it in internal_items if it.stringAttribValue("rpfarm_role") == "package"]
for post_item in posts:
    for pkg_item in packages:
        dependency_holder.addDependency(post_item, pkg_item)
'''

COOKTASK_CODE = '''\
# Called when an in process work item needs to cook. In process work items
# are created by passing the [in_process] flag when constructing the item
# in the [onGenerate] callback -- onGenerate above only does that when the
# Cook in process toggle (rpfarm_inprocess) is on; by default (Ruling R22)
# every item instead carries a shell ".command" (rpfarm.package_runner)
# and cooks out of process through this node's own localscheduler, in
# parallel across its slots, without blocking Houdini's UI -- see this
# node's Help. This callback is the FALLBACK path for the toggle: kept
# working (and still fully unit-testable via run_upload_item) because it
# costs nothing to keep, and because it is a straightforward way to debug
# a package's upload logic directly in Houdini's own process without
# going through package_runner's subprocess + pdgcmd round trip.
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
compress = bool(work_item.intAttribValue("compress"))


def progress_cb(done, total, speed):
    work_item.setStringAttrib("progress", "{:.0f}/{:.0f} MB".format(done / 2**20, total / 2**20))


t0 = time.time()
stats = rppkg.run_upload_item(item, cfg, sftp, sync_client, compress, progress_cb)
elapsed = time.time() - t0

work_item.setFloatAttrib("seconds", elapsed)
work_item.setFloatAttrib("mbps", stats["bytes"] / 2**20 / max(1e-3, elapsed))
work_item.setIntAttrib("bytes", stats["bytes"])
work_item.setIntAttrib("files", stats["files"])
'''

HELP_TEXT = '''\
= RunPodFarm Upload =

#type: node
#context: top
#internal: runpodfarmupload
#icon: TOP/pythonprocessor

"""Package and upload files to the RunPodFarm sync pod, as a normal TOP
work item generator -- progress is visible per package as it cooks."""

Work item = one package of files. Each package cooks on PDG's *local*
scheduler (this node overrides `Scheduler` to its own internal
`localscheduler`, never `runpodfarm_scheduler` -- pointing it at that
scheduler would recurse: the upload has to finish before the farm cook
that needs its files can even start). The override is a Python expression
on the internal Python Processor's `Scheduler` parm re-resolving the
sibling `localscheduler` node's absolute path at cook time -- a bare
relative name silently falls back to "network default" instead (verified
empirically; see the Task 9 report if this ever needs re-deriving). This
node's `OnCreated` event re-asserts the same expression once more when a
new instance is made, belt-and-suspenders: a silently wrong scheduler here
means real recursion in production, not a cosmetic bug.

Packages cook *out of process* by default (Ruling R22 -- must not block
Houdini's UI, and progress must be visible per package): each work item's
command is `python3 -m rpfarm.package_runner <item.json>`
(`rpfarm/package_runner.py`), which PDG's local scheduler runs as a
genuine separate process, in parallel across its slots. PDG's Python
Processor only dispatches a work item out of process when it carries a
shell `.command` this way -- a callback-only item with neither `inProcess`
nor a command silently no-ops (PDG marks it succeeded in ~0s without ever
running the callback; this was live-verified, not theoretical, which is
why this node doesn't use the simpler callback-only path). `package_runner`
reports `bytes`/`files`/`seconds`/`mbps`/`progress` back onto the live
work item via `pdgcmd` (the standard mechanism for any out-of-process PDG
command item), and runs under a plain `python3` -- every `rpfarm` module
it touches is stdlib-only, so there's no reason to pay `hython`'s startup
cost per package, and the generating process's own `sys.executable` would
be wrong here anyway (that process is `hython`). The Cook In Process
toggle below switches back to the old callback-only path (this node's
`cooktask`) for debugging -- blocking, one item at a time, but easier to
step through directly in Houdini's own process.

@parameters

Mode:
    #id: rpfarm_mode
    `Project dependencies` walks `hou.fileReferences()` (via
    `rpfarm.deps.collect_refs`/`resolve_entries`) -- the hip file itself
    plus everything it references, expanded and de-duplicated. `Custom
    paths` uploads exactly the local -> remote pairs below.

Project:
    #id: rpfarm_project

    Remote project folder: `/workspace/projects/<user>/<project>`. Empty
    means the name of the `$JOB` directory.

Package Size (GB):
    #id: rpfarm_packagegb

    Files are grouped into work items no larger than this (a single file
    bigger than the limit still gets its own item).

Compression:
    #id: rpfarm_compress

    `auto` is meant to compress when a measured uplink is below 200 Mbps
    and skip it above that -- but no node or CLI measures uplink yet
    (Ruling R23), so today `auto` unconditionally compresses, the safe
    choice for an unknown connection. `rpfarm doctor` (Task 13) is where
    that measurement will come from; once it exists, `auto` starts
    comparing against it with no change needed here. `on`/`off` force it
    either way. Compressible files are staged to a temp dir with zstd and
    decompressed on the sync pod after upload -- see
    `rpfarm.sync.compress_stage`.

Custom Paths:
    #id: rpfarm_custom

    Multiparm of local -> remote pairs, used in Custom mode (and filled in
    automatically by the Install Houdini preset). `local` may be a file or
    a directory (walked recursively, subdirectories preserved under
    `remote`).

Post-command:
    #id: rpfarm_postcmd

    Shell command run on the sync pod once, after every package in this
    cook has uploaded -- not once per package. Implemented as one extra
    "upload_post" work item that PDG's `onAddInternalDependencies` callback
    makes depend on every package item (the first working option that
    doesn't need a second scheduler pass: package items are added first in
    `onGenerate`, then the post item's dependency on all of them is wired
    once every item exists). Ignored when empty. A non-zero exit fails the
    work item (`rpfarm.packages.run_upload_item` checks this command's
    exit code, same as the decompress step -- neither is allowed to fail
    silently), with a timeout scaled from the item's own byte size (a
    600s floor, since this item never carries files of its own).

Preset:
    #id: rpfarm_preset

    `Install Houdini from tarball` computes Custom Paths and Post-command
    from the two fields below via `rpfarm.packages.houdini_install_preset`
    at generate time (the visible Custom Paths / Post-command parms are
    left alone, not overwritten, in case a mistaken preset pick needs to
    be undone without losing hand-entered values).

Houdini Tarball:
    #id: rpfarm_houtar

    Local path to `houdini-<version>-linux_x86_64_gcc14.2.tar.gz`. Uploads
    to `/workspace/apps/dist/`; the post-command extracts it and runs the
    silent Linux installer into `/workspace/houdini/<version>`.

Houdini Version:
    #id: rpfarm_houver

Cook In Process (debug):
    #id: rpfarm_inprocess

    Off (default): packages upload out of process, in parallel, without
    blocking Houdini (Ruling R22; see above). On: cook in this Houdini
    session instead -- blocks the UI, one package at a time, useful for
    stepping through `run_upload_item()` directly while debugging.

@related

- [Node:top/runpodfarm_scheduler]
- [Node:top/pythonprocessor]
- [Node:top/localscheduler]
'''


def main():
    obj = hou.node("/obj")
    build_net = obj.createNode("topnet", "rpfarm_upload_build")
    sn = build_net.createNode("subnet", "runpodfarmupload_build")

    pp = sn.createNode("pythonprocessor", "pythonprocessor1")
    localsched = sn.createNode("localscheduler", "localscheduler")

    pp.setInput(0, sn.indirectInputs()[0])
    out0 = sn.node("output0")
    out0.setInput(0, pp, 0)

    # A plain relative string ("localscheduler") does NOT resolve here --
    # PDG's scheduler-override lookup for a node nested inside a subnet
    # only accepts an absolute node path (verified empirically: a bare
    # relative name silently falls back to "network default" with a
    # warning, meaning items dispatch to whatever scheduler governs the
    # OUTER topnet this asset is dropped into -- runpodfarm_scheduler in
    # real usage, causing exactly the recursion Ruling forbids). A Python
    # expression re-resolves the sibling localscheduler's absolute path at
    # cook time, so the override is correct regardless of where a user
    # instances this asset.
    pp.parm("topscheduler").setExpression(
        'hou.pwd().parent().path() + "/localscheduler"', language=hou.exprLanguage.Python
    )
    pp.parm("generate").set(GENERATE_CODE)
    pp.parm("addinternaldependencies").set(ADDDEPS_CODE)
    pp.parm("cooktask").set(COOKTASK_CODE)

    pp.moveToGoodPosition()
    localsched.moveToGoodPosition()
    out0.moveToGoodPosition()

    ptg = hou.ParmTemplateGroup()

    mode_pt = hou.StringParmTemplate(
        "rpfarm_mode", "Mode", 1, default_value=("deps",),
        menu_items=("deps", "custom"), menu_labels=("Project Dependencies", "Custom Paths"),
    )
    mode_pt.setHelp("Project dependencies: hou.fileReferences() plus the hip file itself. Custom paths: exactly the local -> remote pairs below.")
    # Matches runpodfarm_scheduler's own rpfarm_project parm: empty string,
    # with the "basename of $JOB" fallback implemented in onGenerate
    # (Python) rather than as a live default expression here.
    project_pt = hou.StringParmTemplate("rpfarm_project", "Project", 1, default_value=("",))
    project_pt.setHelp("Project folder on the network volume: /workspace/projects/<user>/<project>. Empty means the name of the $JOB directory.")
    packagegb_pt = hou.FloatParmTemplate(
        "rpfarm_packagegb", "Package Size (GB)", 1, default_value=(1.5,), min=0.1, max=16, max_is_strict=False
    )
    packagegb_pt.setHelp("Files are grouped into work items no larger than this (a single bigger file still gets its own item).")
    compress_pt = hou.StringParmTemplate(
        "rpfarm_compress", "Compression", 1, default_value=("auto",),
        menu_items=("auto", "on", "off"), menu_labels=("Auto", "On", "Off"),
    )
    compress_pt.setHelp(
        "auto compresses by default (Ruling R23: no live uplink measurement exists yet -- "
        "`rpfarm doctor` will supply one in Task 13, at which point auto starts actually "
        "comparing it against 200 Mbps instead of always compressing)."
    )

    local_pt = hou.StringParmTemplate(
        "rpfarm_local#", "Local", 1, default_value=("",),
        string_type=hou.stringParmType.FileReference, file_type=hou.fileType.Any,
    )
    remote_pt = hou.StringParmTemplate("rpfarm_remote#", "Remote", 1, default_value=("",))
    custom_pt = hou.FolderParmTemplate(
        "rpfarm_custom", "Custom Paths", parm_templates=(local_pt, remote_pt),
        folder_type=hou.folderType.MultiparmBlock, default_value=0,
    )
    custom_pt.setHelp("Used in Custom mode (and filled in automatically by the Install Houdini preset). Local may be a file or a directory.")

    postcmd_pt = hou.StringParmTemplate("rpfarm_postcmd", "Post-command", 1, default_value=("",))
    postcmd_pt.setHelp("Shell command run on the sync pod once, after every package in this cook has uploaded. Ignored when empty.")

    preset_pt = hou.StringParmTemplate(
        "rpfarm_preset", "Preset", 1, default_value=("none",),
        menu_items=("none", "install_houdini"), menu_labels=("None", "Install Houdini from tarball"),
    )
    preset_pt.setHelp("Install Houdini from tarball computes Custom Paths + Post-command from the two fields below at generate time.")
    houtar_pt = hou.StringParmTemplate(
        "rpfarm_houtar", "Houdini Tarball", 1, default_value=("",),
        string_type=hou.stringParmType.FileReference, file_type=hou.fileType.Any,
    )
    houtar_pt.setHelp("Local path to houdini-<version>-linux_x86_64_gcc14.2.tar.gz.")
    houtar_pt.setConditional(hou.parmCondType.HideWhen, "{ rpfarm_preset != install_houdini }")
    houver_pt = hou.StringParmTemplate("rpfarm_houver", "Houdini Version", 1, default_value=("22.0.393",))
    houver_pt.setConditional(hou.parmCondType.HideWhen, "{ rpfarm_preset != install_houdini }")

    # Ruling R22: out of process (this off) is the default -- uploads must
    # not block Houdini's UI. Kept as a toggle rather than removed because
    # it costs nothing and is a straightforward way to debug a package's
    # upload logic directly in Houdini's own process (see cooktask/Help).
    inprocess_pt = hou.ToggleParmTemplate("rpfarm_inprocess", "Cook In Process (debug)", default_value=False)
    inprocess_pt.setHelp(
        "Off (default): packages upload out of process, in parallel, without blocking Houdini. "
        "On: cook in this Houdini session instead -- blocks the UI, one package at a time, useful for debugging."
    )

    for pt in (
        mode_pt, project_pt, packagegb_pt, compress_pt, custom_pt, postcmd_pt,
        preset_pt, houtar_pt, houver_pt, inprocess_pt,
    ):
        ptg.append(pt)

    sn.setParmTemplateGroup(ptg)

    if os.path.exists(OUT_HDA):
        os.remove(OUT_HDA)

    new_type = sn.createDigitalAsset(
        name="runpodfarmupload",
        hda_file_name=OUT_HDA,
        description="RunPodFarm Upload",
        min_num_inputs=0,
        max_num_inputs=1,
        ignore_external_references=True,
    )

    definition = new_type.type().definition()
    definition.addSection("Help", HELP_TEXT)
    # setParmTemplateGroup on the node (above) only affects this live
    # instance -- new instances created from the saved asset get their
    # interface from the DEFINITION's own template group, which has to be
    # set explicitly here too, or every new instance is created with zero
    # parms (verified empirically: skipping this line produces a
    # DialogScript with no parm{} blocks at all).
    definition.setParmTemplateGroup(ptg)

    # Belt-and-suspenders for the scheduler override (see pp.parm
    # "topscheduler" above): the baked-in Python expression already
    # re-resolves the sibling localscheduler's absolute path on every
    # cook, so this OnCreated re-asserts the *same* expression once more
    # at instance-creation time rather than a static path (a static path
    # would go stale if the node were ever renamed or moved). Belt AND
    # suspenders because a silent wrong-scheduler fallback here means
    # real recursion into runpodfarm_scheduler in production, not just a
    # cosmetic bug -- see this node's Help.
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
