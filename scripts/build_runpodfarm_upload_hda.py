"""One-shot builder for hda/runpodfarm_upload.hda.

Builds a Top/subnet node containing a pythonprocessor1 (work item
generation/cook) and a localscheduler (forces this node's items to cook on
PDG's local scheduler regardless of the parent topnet's own scheduler --
see this node's Help), wires them up, sets the rpfarm_* parameter
interface, converts the subnet to a digital asset, and saves it as a
single packed .hda file at the path given on the command line.

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
# and why the post-command runs as one extra work item instead of once per
# package (Ruling R3).

import json
import math
import os
import pathlib
import sys

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
        vol = api.get_volume(cfg.volume_id)
        total_gb = float(vol.get("size") or 0)
        upload_gb = upload_bytes / 2**30
        if total_gb and upload_gb > 0.85 * total_gb:
            new_gb = int(math.ceil((total_gb + upload_gb) / 10.0) * 10)
            api.resize_volume(cfg.volume_id, new_gb)
            node.addWarning("grew volume {} -> {} GB for this upload".format(cfg.volume_id, new_gb))
except RunPodError as e:
    node.addWarning("volume auto-grow check failed (continuing): {}".format(e))

compress = rppkg.resolve_compress_flag(node.evalParm("rpfarm_compress"))

pkg_items = []
for it in items:
    wi = item_holder.addWorkItem(name="upload_{:03d}".format(it["index"]), inProcess=True)
    wi.setStringAttrib("rpfarm_item", json.dumps(it))
    wi.setStringAttrib("rpfarm_role", "package")
    wi.setIntAttrib("bytes", it["bytes"])
    wi.setIntAttrib("files", len(it["files"]))
    wi.setIntAttrib("compress", 1 if compress else 0)
    pkg_items.append(wi)

if post_command and pkg_items:
    post_item = item_holder.addWorkItem(name="upload_post", inProcess=True)
    post_item.setStringAttrib(
        "rpfarm_item",
        json.dumps(
            {
                "index": len(items),
                "local_root": "",
                "remote_root": "",
                "files": [],
                "bytes": 0,
                "post_command": post_command,
            }
        ),
    )
    post_item.setStringAttrib("rpfarm_role", "post")
    post_item.setIntAttrib("bytes", 0)
    post_item.setIntAttrib("files", 0)
    post_item.setIntAttrib("compress", 0)
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
# are created by passing the [in_process] flag when constructing the item in
# the [onGenerate] callback -- onGenerate above does, for every item this
# node creates.
#
# Why in-process rather than out-of-process (PDG's other mode, which would
# spawn a fresh hython per item and keep Houdini's own UI thread free):
# tried first, but pythonprocessor's out-of-process path is command-based
# (it needs a work item ".command" shell string, like genericgenerator) --
# a callback-only item with neither `inProcess` nor a command set silently
# no-ops (PDG marks it CookedSuccess in ~0s without ever calling this
# callback; verified live, see the Task 9 report). Making this genuinely
# out-of-process would mean adding a small `rpfarm` CLI entry point this
# callback shells out to instead of calling run_upload_item() directly --
# left for a follow-up, noted in the Task 9 report for whoever picks it up.
# For now: uploads block Houdini's UI while they run, same as any other
# in-process TOP node (e.g. a plain Python Processor without its own
# threading). Independent work items still cook one after another, not
# concurrently, until that follow-up lands.
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
empirically; see the Task 9 report if this ever needs re-deriving).

Packages cook *in process* (blocking Houdini's UI while a package
uploads, one after another): PDG's Python Processor only dispatches a
work item out of process when it carries a shell `.command` -- a
callback-only item with neither `inProcess` nor a command silently no-ops
(PDG marks it succeeded in ~0s without ever running the callback; this
was live-verified, not theoretical). Making uploads genuinely
non-blocking would mean adding an `rpfarm` CLI entry point this node
shells out to instead of calling `run_upload_item()` directly from
Python -- left as a follow-up (see the Task 9 report).

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

    `auto` compresses when no uplink measurement is available (the safe
    default for an unknown connection) or when a measured uplink is below
    200 Mbps; `on`/`off` force it. Compressible files are staged to a temp
    dir with zstd and decompressed on the sync pod after upload -- see
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
    once every item exists). Ignored when empty.

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
    compress_pt.setHelp("auto compresses when no uplink measurement is available, or when uplink < 200 Mbps.")

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

    for pt in (
        mode_pt, project_pt, packagegb_pt, compress_pt, custom_pt, postcmd_pt,
        preset_pt, houtar_pt, houver_pt,
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
    definition.save(OUT_HDA, template_node=new_type)

    print("OK built", OUT_HDA, "type", new_type.type().name())

    build_net.destroy()


if __name__ == "__main__":
    main()
