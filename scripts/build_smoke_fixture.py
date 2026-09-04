"""Rebuild ``tests/fixtures/smoke/smoke.hip`` -- the scene ``rpfarm smoke`` cooks.

The fixture is checked in (a few tens of KB) so a smoke run needs nothing but
a clone; this script is its provenance. Regenerate it with Houdini's
``hython``, never by hand-editing the ``.hip``::

    RPFARM_ROOT=$PWD /Applications/Houdini/Houdini22.0.368/Frameworks/\\
Houdini.framework/Versions/Current/Resources/bin/hython \\
        scripts/build_smoke_fixture.py

What it builds, and why each choice is the way it is:

- ``/obj/smoke_geo`` -- one low-res polygon sphere, plus ``/obj/cam1``
  (320x240) and ``/obj/envlight1``. Small enough that the render time is
  dominated by ``hython`` startup, which is the point: the smoke test is
  measuring the *farm*, not Karma.
- ``/out/karma1`` -- Karma **CPU** (``engine`` is ``cpu`` by default), 3
  frames, writing ``$HIP/render/smoke.$F4.exr``.

  ``$HIP``, not ``$JOB``, deliberately: the pod's worker runs each task with
  the container's own working directory, and nothing sets ``JOB`` in the task
  environment, so ``$JOB`` inside a pod-side ``hython`` expands to whatever
  that cwd happens to be -- *not* the project directory (checked: with ``JOB``
  unset, Houdini resolves ``$JOB`` to the startup cwd, not to ``$HIP``).
  ``$HIP`` is always the directory of the loaded hip file, which on the pod is
  the mapped project directory, so the ROP writes exactly where the path map
  says it should. The fixture is copied to the root of the run directory, so
  locally ``$HIP`` and ``$JOB`` are the same directory anyway.
- ``/obj/topnet1`` -- the four-node production graph from the design spec:

      upload (deps) -> gate (waitforall) -> probe -> render -> download

  ``gate`` is not decoration. ``runpodfarm_upload`` emits **one work item per
  package**, and every downstream node then generates per upstream item: with
  two packages (this scene's own files, and the referenced HDA libraries under
  ``_ext/``) the ROP Fetch produced 2 x 3 = 6 render items and rendered every
  frame twice. A ``Wait For All`` collapses the packages into a single
  partition, so the render is planned once regardless of how the upload was
  split. Any real graph that puts ``runpodfarm_upload`` upstream of a renderer
  needs the same thing. (Partition and merge items carry no command and are
  resolved by PDG itself -- verified: they never reach a scheduler's
  ``onSchedule`` -- so the gate costs nothing on the farm.)

  ``probe`` is one trivial shell item that writes a file under ``$PDG_DIR``
  and declares it with ``pdgcmd.addOutputFile``. It costs a second on the same
  pod and it is the canary: sitting *ahead* of the render, it proves pod
  dispatch, the MQ result channel and the output-download path before Karma
  gets a chance to be the thing that failed. Its file comes back through the
  scheduler's own "Download Outputs" path, the three EXRs come back through
  both that and the ``runpodfarm_download`` node -- so one cook exercises both
  download routes.

  ``topscheduler`` is the ``runpodfarmscheduler``; ``upload``/``download``
  override their *own* internal scheduler to ``localscheduler`` in their
  ``OnCreated`` (Ruling R22), so their packages run as local out-of-process
  jobs while the ROP frames run on the farm -- one cook, one GPU pod.

Only the bottom-most node (``download``) carries the display flag: cooking
anything else, or cooking two nodes in two ``cookWorkItems()`` calls, starts a
second independent PDG cook and pays for a second GPU pod (Task 10 found this
live; it is in both nodes' Help).

Nothing here embeds an API key, a user name or a machine-specific path: the
project name is a literal ``smoke`` and the GPU list is left empty so it comes
from the runner's own ``~/.rpfarm/config.toml``.
"""

from __future__ import annotations

import os
import sys

import hou

FIXTURE_REL = os.path.join("tests", "fixtures", "smoke", "smoke.hip")

# Frames rendered by the fixture. Kept in one place because rpfarm.smoke
# asserts exactly this many EXRs came back.
FRAME_START = 1
FRAME_END = 3

RESX = 320
RESY = 240

# The probe item's output, relative to $PDG_DIR (the project dir on the pod,
# $JOB locally). rpfarm.smoke checks for it by this name.
PROBE_DIR = "smoke_probe"


def _probe_command():
    """The one shell command the probe work item runs on the pod.

    The backslashes keep Houdini's own parm expansion off the variables, so
    the literal ``$PDG_...`` text reaches ``bash`` on the pod, which expands
    it from the task environment the scheduler sends (same technique as
    ``scripts/smoke_scheduler_headless.py``).
    """
    out = '\\$PDG_DIR/{}/\\$PDG_ITEM_NAME.txt'.format(PROBE_DIR)
    report = (
        '__PDG_PYTHON__ -c \'import os, sys; '
        'sys.path.insert(0, os.environ["PDG_SCRIPTDIR"]); '
        'import pdgcmd; pdgcmd.addOutputFile(sys.argv[1])\' "{}"'.format(out)
    )
    return (
        'mkdir -p "\\$PDG_DIR/{}" && '
        'echo "rpfarm smoke probe from \\$PDG_ITEM_NAME on \\$(hostname)" > "{}" && '
        '{}'.format(PROBE_DIR, out, report)
    )


def _set_static(node, parm_name, value):
    """Set a parm to a literal value, dropping any expression first.

    Houdini stores an expression as a keyframe, and ``Parm.set()`` on a
    keyframed parm does nothing at all -- no error, no change. Every frame
    range in this fixture ships as ``$FSTART``/``$FEND``, so every one of them
    needs this.
    """
    parm = node.parm(parm_name)
    parm.deleteAllKeyframes()
    parm.set(value)
    return parm


def build_scene():
    """The renderable half: sphere, camera, light, Karma CPU ROP."""
    obj = hou.node("/obj")

    geo = obj.createNode("geo", "smoke_geo")
    sphere = geo.createNode("sphere", "sphere1")
    sphere.parm("type").set("polymesh")
    sphere.parm("rows").set(24)
    sphere.parm("cols").set(24)
    sphere.setDisplayFlag(True)
    sphere.setRenderFlag(True)

    cam = obj.createNode("cam", "cam1")
    cam.parmTuple("t").set((0.0, 0.0, 6.0))
    cam.parm("resx").set(RESX)
    cam.parm("resy").set(RESY)

    obj.createNode("envlight", "envlight1")

    karma = hou.node("/out").createNode("karma", "karma1")
    karma.parm("camera").set(cam.path())
    karma.parm("picture").set("$HIP/render/smoke.$F4.exr")
    karma.parm("trange").set(1)  # Render Frame Range
    _set_static(karma, "f1", FRAME_START)
    _set_static(karma, "f2", FRAME_END)
    _set_static(karma, "f3", 1)
    karma.parm("samplesperpixel").set(4)
    # The Karma ROP's intermediate USD defaults to
    # $HOUDINI_TEMP_DIR/usd_renders/$RENDERID -- a directory reference that
    # hou.fileReferences() reports, that resolve_entries() then walks, and
    # that on a machine which has rendered before contains other people's
    # leftovers. Pointing it inside $HIP keeps the upload deterministic (the
    # directory does not exist yet in a fresh run directory, so it is simply
    # dropped) and keeps the pod's intermediate file in the project.
    karma.parm("savetodirectory").set("$HIP/usd/$RENDERID")
    karma.parm("denoiser").set("off")
    karma.parm("alfprogress").set(1)

    obj.layoutChildren()
    return karma


def build_topnet(karma):
    """The four-node farm graph."""
    topnet = hou.node("/obj").createNode("topnet", "topnet1")

    sched = topnet.createNode("runpodfarmscheduler", "rpfarm")
    sched.parm("rpfarm_project").set("smoke")
    # Empty GPU list -> gpu_priority from the runner's own config.toml, so the
    # fixture carries no machine-specific choice.
    sched.parm("rpfarm_gpulist").set("")
    sched.parm("rpfarm_minpods").set(1)
    sched.parm("rpfarm_maxpods").set(1)
    sched.parm("rpfarm_slots").set(4)
    sched.parm("rpfarm_idletimeout").set(120)
    sched.parm("rpfarm_maxcost").set(1.0)
    sched.parm("rpfarm_verbose").set(1)
    topnet.parm("topscheduler").set(sched.path())

    upload = topnet.createNode("runpodfarmupload", "upload")
    upload.parm("rpfarm_mode").set("deps")
    upload.parm("rpfarm_project").set("smoke")
    # "auto" compresses (Ruling R23) and would need zstd on the artist's
    # machine; the fixture's payload is a few tens of KB, so there is nothing
    # to gain and one dependency to lose.
    upload.parm("rpfarm_compress").set("off")

    # One partition for however many packages the upload split into -- see
    # this module's docstring for what happens without it.
    gate = topnet.createNode("waitforall", "gate")
    gate.setInput(0, upload)

    probe = topnet.createNode("genericgenerator", "probe")
    probe.parm("itemcount").set(1)
    probe.parm("shellcommand").set(1)
    probe.parm("pdg_command").set(_probe_command())
    probe.setInput(0, gate)

    render = topnet.createNode("ropfetch", "render")
    render.parm("roppath").set(karma.path())
    # "Frame Range", not "ROP Node Configuration": the latter hands the whole
    # range to the ROP and produces ONE work item that renders all three
    # frames in one hython. One item per frame is the point here -- it is what
    # makes the cook a real dispatch test (three tasks, three ledger rows) and
    # what a real shot looks like.
    render.parm("framegeneration").set(1)  # Frame Range
    # range1/range2 ship as the *expressions* $FSTART/$FEND, and a Houdini
    # expression is a keyframe: Parm.set() on one is a silent no-op. Without
    # the delete, this node quietly keeps the scene range and the smoke test
    # renders 240 frames instead of 3 (which is exactly what it did once).
    _set_static(render, "range1", FRAME_START)
    _set_static(render, "range2", FRAME_END)
    _set_static(render, "range3", 1)
    render.setInput(0, probe)

    download = topnet.createNode("runpodfarmdownload", "download")
    download.parm("rpfarm_mode").set("outputs")
    download.parm("rpfarm_overwrite").set("newer")
    download.setInput(0, render)
    download.setDisplayFlag(True)

    # runpodfarm_upload and runpodfarm_download are subnets whose OnCreated
    # unlocks their contents (Ruling R22 needs to point their internal
    # scheduler at the topnet's localscheduler). An unlocked HDA instance
    # saves its *whole internal network* into the .hip, so the fixture would
    # otherwise carry a frozen copy of whatever generate script was installed
    # the day it was built -- and go on using it no matter how many times the
    # HDA is rebuilt. That is not hypothetical: three live runs downloaded
    # nothing because the fixture was still running Task 10's original
    # generate. Re-syncing to the current definition here (the definition
    # already carries the scheduler override, see the builder scripts) keeps
    # the checked-in scene honest; _verify() asserts it stuck.
    for node in (upload, download):
        node.matchCurrentDefinition()

    topnet.layoutChildren()
    return topnet


def main(argv):
    dest = argv[0] if argv else os.path.join(
        os.environ.get("RPFARM_ROOT", os.getcwd()), FIXTURE_REL)
    dest = os.path.abspath(dest)
    os.makedirs(os.path.dirname(dest), exist_ok=True)

    hou.hipFile.clear(suppress_save_prompt=True)
    karma = build_scene()
    build_topnet(karma)

    # A .hip stores its own variable table, and $JOB from it wins over the
    # environment when the file is loaded elsewhere. Without this it would
    # save whatever directory this build ran from. `rpfarm smoke` overrides
    # $JOB after loading anyway (rpfarm.smoke.hython_main); this only keeps
    # the checked-in file from carrying an accidental build path.
    hou.putenv("JOB", os.path.dirname(dest))

    hou.hipFile.save(dest)
    print("wrote {} ({} bytes)".format(dest, os.path.getsize(dest)))
    return _verify(dest)


def _verify(dest):
    """Reload the saved file and assert the things that are expensive to get
    wrong -- a frame range that silently stayed at the scene's 1-240 costs a
    paid pod 240 renders to discover."""
    hou.hipFile.clear(suppress_save_prompt=True)
    hou.hipFile.load(dest, suppress_save_prompt=True)
    checks = [
        ("/out/karma1", {"f1": FRAME_START, "f2": FRAME_END, "f3": 1, "trange": 1}),
        ("/obj/topnet1/render", {"range1": FRAME_START, "range2": FRAME_END,
                                 "range3": 1, "framegeneration": 1}),
    ]
    ok = True
    for path, expected in checks:
        node = hou.node(path)
        for name, want in expected.items():
            got = node.parm(name).eval()
            if abs(float(got) - float(want)) > 1e-6:
                print("FAIL: {}/{} = {!r}, expected {!r}".format(path, name, got, want))
                ok = False
    roppath = hou.node("/obj/topnet1/render").parm("roppath").eval()
    if roppath != "/out/karma1":
        print("FAIL: ropfetch roppath = {!r}".format(roppath))
        ok = False
    for name in ("upload", "download"):
        node = hou.node("/obj/topnet1/" + name)
        if not node.matchesCurrentDefinition():
            print("FAIL: {} has an edited/frozen internal network -- it would "
                  "keep using it and ignore every future HDA rebuild".format(name))
            ok = False
    print("verify: {}".format("OK" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
