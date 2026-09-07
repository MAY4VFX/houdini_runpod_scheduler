"""One TAB-menu command that builds the whole farm graph in the current scene.

Why this exists, and why it builds rather than copies. Both of the day's
failures on 2026-09-07 came from an artist moving our nodes between scenes:
a copied ``runpodfarm_upload`` carried another project's cached work items
and silently uploaded nothing, and a copied node carried an old *definition*
whose parameters no longer matched the installed code, so "Add A GPU" died
with an ``AttributeError``. Both are properties of the copy, not of the
node. A node created fresh in the scene it will cook in has neither: no
cache to inherit, and the definition is whatever is installed right now.

So the fix is to make building the graph cheaper than copying it.

What it makes, and what it deliberately does not:

    runpodfarm_upload  ->  waitforall  ->  ropfetch

plus a ``runpodfarm_scheduler`` set as the topnet's own ``TOP Scheduler``.
The gate is not decoration: upload emits one work item per package, every
downstream node generates per incoming item, and two packages therefore
render every frame twice (observed -- six renders for three frames). See
the README's "Первый кук из Houdini".

**No download node.** The scheduler's own ``Download Outputs`` is on by
default and brings each output home the moment the MQ reports it, during
the cook rather than after it. A second node in ``Upstream Outputs`` mode
walks the same outputs again: with ``If Newer`` that is mostly an extra
rclone pass per package that transfers nothing and keeps the sync pod
awake longer, and with ``Always`` it genuinely downloads everything twice.
The download node earns its place in the manual case -- re-fetching a
folder later, or collecting something no ROP declared -- which is a
deliberate act, not part of the default graph. :func:`report` says so.

Everything below the ``hou``-free line is pure so it can be tested without
Houdini; the graph builder takes a duck-typed parent node, which is what
the tests hand it.
"""

from __future__ import annotations

import math
import os
import posixpath
import re

#: Node type names of the assets this builds with.
SCHEDULER_TYPE = "runpodfarmscheduler"
UPLOAD_TYPE = "runpodfarmupload"
DOWNLOAD_TYPE = "runpodfarmdownload"
GATE_TYPE = "waitforall"
#: The scheduler a topnet creates for itself, and points at, when made.
LOCAL_SCHEDULER_TYPE = "localscheduler"
FETCH_TYPE = "ropfetch"

#: Names given to the nodes this creates. Fixed, because idempotency is by
#: TYPE rather than by name -- but a predictable name is what an artist
#: reads in an error message.
SCHEDULER_NAME = "rpfarm"
UPLOAD_NAME = "upload"
GATE_NAME = "gate"
FETCH_NAME = "render"

#: Folders that name a *kind* of file rather than a project. When the .hip
#: sits in one of these, the project is one level up: ".../airship/scenes"
#: is the airship project, not the "scenes" project.
GENERIC_DIRS = frozenset({
    "scenes", "scene", "hip", "hips", "houdini", "hou", "work", "wip",
    "shots", "shot", "sequences", "seq", "3d", "cg", "temp", "tmp",
})

#: Version suffixes on a scene file: airship_v016.hip is the airship project.
_VERSION_SUFFIX = re.compile(r"[._-]v\d+$", re.IGNORECASE)

#: An unsaved scene is called this by Houdini, and names no project at all.
UNTITLED = "untitled.hip"

#: How many pods a fresh graph is allowed to reach for. The asset's own
#: default; the ceiling below only ever lowers it.
DEFAULT_MAX_PODS = 4


# -- pure: what to preconfigure, derived from the scene ----------------------


def project_name(hip_dir: str, hip_file: str = "") -> str:
    """The farm project folder for a scene: where its files land under
    ``/workspace/projects/<user>/``.

    The scene's own directory, because that is the folder whose structure
    travels to the farm -- climbing past a generic name like ``scenes/``,
    and falling back to the scene file's name (minus a ``_v016`` suffix)
    when the directory says nothing. Returns "" for an unsaved scene, which
    leaves the node's own default expression in place rather than writing a
    wrong literal over it.

    Deliberately NOT ``basename($JOB)``, which is what the node's default
    does: an artist whose ``$JOB`` is their home directory gets "may" as a
    project name and a farm laid out as a mirror of their home. That is a
    real configuration, and it was this artist's.
    """
    hip_file = (hip_file or "").strip()
    if hip_file and os.path.basename(hip_file).lower() == UNTITLED:
        return ""

    parts = [p for p in _split_all(hip_dir) if p not in ("", "/", ".")]
    while parts and parts[-1].lower() in GENERIC_DIRS:
        parts.pop()
    if parts and parts[-1] not in ("/", ""):
        candidate = parts[-1]
        # A home directory is not a project either -- same failure as $JOB.
        if not _looks_like_home(hip_dir, candidate):
            return candidate

    if hip_file:
        stem = os.path.splitext(os.path.basename(hip_file))[0]
        stem = _VERSION_SUFFIX.sub("", stem)
        if stem:
            return stem
    return ""


def _split_all(path: str) -> list[str]:
    """Path components, for both separators -- a Windows scene path reaches
    this module as backslashes even though the farm side is posix."""
    return [p for p in re.split(r"[\\/]+", path or "") if p]


def _looks_like_home(hip_dir: str, candidate: str) -> bool:
    home = os.path.expanduser("~")
    return bool(home) and os.path.normpath(hip_dir) == os.path.normpath(home) \
        and candidate == os.path.basename(os.path.normpath(home))


def pod_ceiling(frames: int, default_max: int = DEFAULT_MAX_PODS) -> int:
    """Never reach for more machines than there are frames.

    Ruling R46 already stops the scheduler raising pods it has no work for,
    at cook time. This is the same fact one step earlier: a graph set up on
    a single-frame scene should not *say* 4 either, because the number an
    artist reads in the parameter is the number they believe.
    """
    if frames is None or frames < 1:
        return 1
    return max(1, min(int(default_max), int(frames)))


def frame_count(start, end, step=1.0) -> int:
    """Frames in an inclusive range, the way the playbar counts them."""
    try:
        start, end, step = float(start), float(end), float(step or 1.0)
    except (TypeError, ValueError):
        return 1
    if step <= 0:
        step = 1.0
    return max(1, int(math.floor((end - start) / step)) + 1)


def choose_rop(candidates) -> str:
    """The one ROP to fetch, or "" when the scene does not answer by itself.

    One candidate is an answer. Zero or several is a question, and the tool
    asks it in its report rather than guessing -- an empty ``ROP Path`` is
    obviously unfinished, while the wrong one renders the wrong thing.
    """
    candidates = list(candidates)
    return candidates[0] if len(candidates) == 1 else ""


# -- pure: what already exists ----------------------------------------------


def existing_by_type(children):
    """Map node type -> the first existing node of it.

    ``children`` is a sequence of ``(type_name, node)``. Idempotency keys on
    the TYPE, not the name: an artist who renamed their scheduler still has
    a scheduler, and a second one would be a second farm identity in one
    network.
    """
    found = {}
    for type_name, node in children:
        found.setdefault(type_name, node)
    return found


# -- the graph ---------------------------------------------------------------


class Result:
    """What a run did, in a form both the log and the tests can read."""

    def __init__(self):
        self.created = []        # [(role, path)]
        self.reused = []         # [(role, path)]
        self.project = ""
        self.max_pods = DEFAULT_MAX_PODS
        self.rop = ""
        self.rop_candidates = []
        self.scheduler_assigned = False
        self.notes = []

    @property
    def anything_created(self):
        return bool(self.created)


def build(parent, project="", frames=1, rop_candidates=(), max_pods=DEFAULT_MAX_PODS):
    """Create (or adopt) the farm graph inside ``parent``, a TOP network.

    ``parent`` is duck-typed on purpose -- ``createNode``, ``children``,
    ``parm``, ``path`` -- so the tests can drive the whole builder without
    Houdini. Nothing here is destructive: an existing node of a type is
    adopted and reported, never replaced, and existing wiring is only added
    to where an input is empty.
    """
    result = Result()
    result.project = project
    result.rop_candidates = list(rop_candidates)
    result.rop = choose_rop(result.rop_candidates)
    result.max_pods = pod_ceiling(frames, max_pods)

    existing = existing_by_type(
        [(_type_name(c), c) for c in parent.children()])

    def adopt_or_create(type_name, node_name, role):
        node = existing.get(type_name)
        if node is not None:
            result.reused.append((role, node.path()))
            return node, False
        node = parent.createNode(type_name, node_name)
        result.created.append((role, node.path()))
        return node, True

    scheduler, _ = adopt_or_create(SCHEDULER_TYPE, SCHEDULER_NAME, "scheduler")
    upload, upload_is_new = adopt_or_create(UPLOAD_TYPE, UPLOAD_NAME, "upload")
    gate, gate_is_new = adopt_or_create(GATE_TYPE, GATE_NAME, "gate")
    fetch, fetch_is_new = adopt_or_create(FETCH_TYPE, FETCH_NAME, "render")

    # -- preconfigure from the scene, and only where the scene has an answer.
    #    A parm left alone keeps the asset's own default expression, which
    #    is live and follows the config; a literal written here would freeze
    #    it. So "" means "the scene did not say", not "set it to empty".
    if project:
        _set(scheduler, "rpfarm_project", project)
        _set(upload, "rpfarm_project", project)
    _set(scheduler, "rpfarm_maxpods", result.max_pods)

    # The asset's defaults are already the right answer for these two
    # (Dependencies = "This cook's branch", Compression = "Auto"), so they
    # are asserted rather than assumed: a graph built by this tool has the
    # scope and compression the artist was promised even if a future default
    # changes underneath.
    _set(upload, "rpfarm_mode", "deps")
    _set(upload, "rpfarm_scope", "branch")
    _set(upload, "rpfarm_compress", "auto")

    if result.rop:
        _set(fetch, "roppath", result.rop)
    # "Frame Range", not "ROP Node Configuration": the latter hands the whole
    # range to the ROP as ONE work item that renders every frame in one
    # hython -- on one pod. One item per frame is what makes this a farm.
    # range1/range2 keep their $FSTART/$FEND expressions so the graph follows
    # the scene's range instead of freezing today's.
    _set(fetch, "framegeneration", 1)

    # -- wiring. Only ever fills an empty input, so a rerun on a graph the
    #    artist has since rearranged does not rewire it behind their back.
    _wire(gate, 0, upload, result)
    _wire(fetch, 0, gate, result)

    topscheduler = parent.parm("topscheduler")
    if topscheduler is not None:
        # A FRESH topnet is not blank here: Houdini creates it already
        # pointing at the ``localscheduler`` it makes for itself, so "leave a
        # non-empty value alone" refused to assign the farm in the commonest
        # case of all. Nor is it "at default" -- checked live: the topnet
        # SETS the parm on creation, so isAtDefault() is False.
        #
        # What actually separates Houdini's own value from a choice is where
        # it points: the localscheduler inside this very network is the one
        # the topnet made for itself. A Deadline, a Tractor, or a scheduler
        # somewhere else in the scene is somebody's decision and is left
        # alone, with a line in the report saying so.
        current = _eval_string(topscheduler)
        current_node = _resolve(parent, current)
        ours = current_node is not None and current_node.path() == scheduler.path()
        houdinis_own = (current_node is not None
                        and _type_name(current_node) == LOCAL_SCHEDULER_TYPE
                        and current_node.path().startswith(parent.path() + "/"))
        # A value pointing at a node that is not there is broken, not a
        # choice -- replacing it is a repair.
        dangling = bool(current) and current_node is None
        if not current or dangling or ours or houdinis_own:
            topscheduler.set(scheduler.path())
            result.scheduler_assigned = True
        else:
            result.notes.append(
                "the topnet's TOP Scheduler is already {} -- left alone; set it to {} "
                "by hand if the farm is what you meant".format(current, scheduler.path()))

    if fetch_is_new or upload_is_new or gate_is_new:
        _call(parent, "layoutChildren")
    _call(fetch, "setDisplayFlag", True)
    return result


def _eval_string(parm):
    return parm.evalAsString() if hasattr(parm, "evalAsString") else ""


def _resolve(parent, path):
    """The node a TOP Scheduler parm points at, relative to ``parent``."""
    if not path:
        return None
    finder = getattr(parent, "node", None)
    if finder is None:
        return None
    try:
        return finder(path)
    except Exception:
        return None


def _type_name(node):
    try:
        return node.type().name()
    except Exception:  # pragma: no cover - a node that cannot say is not ours
        return ""


def _set(node, parm_name, value):
    parm = node.parm(parm_name)
    if parm is not None:
        parm.set(value)


def _wire(node, index, source, result):
    try:
        current = node.inputs()[index]
    except (AttributeError, IndexError):
        current = None
    if current is None:
        node.setInput(index, source)
    elif current.path() != source.path():
        result.notes.append(
            "{} already had {} on input {} -- left alone".format(
                node.path(), current.path(), index))


def _call(node, method, *args):
    fn = getattr(node, method, None)
    if fn is not None:
        fn(*args)


# -- what the artist reads ---------------------------------------------------


def report(result):
    """The whole outcome as lines, so the log and the dialog say the same
    thing and the tests can assert on it."""
    lines = []
    if result.created:
        lines.append("Created: " + ", ".join(
            "{} ({})".format(path, role) for role, path in result.created))
    if result.reused:
        lines.append("Already there, reused: " + ", ".join(
            "{} ({})".format(path, role) for role, path in result.reused))
    if not result.created:
        lines.append("Nothing to create -- this network already has the farm "
                     "graph. Nothing was duplicated.")

    lines.append("Project: {}".format(result.project or
                                      "(scene not saved -- left on the node's own default)"))
    lines.append("Max Pods: {} (never more machines than frames)".format(result.max_pods))
    if result.rop:
        lines.append("ROP Path: {}".format(result.rop))
    elif result.rop_candidates:
        lines.append("ROP Path: EMPTY -- the scene has {} renderable ROPs and this "
                     "tool will not guess: {}".format(
                         len(result.rop_candidates), ", ".join(result.rop_candidates[:8])))
    else:
        lines.append("ROP Path: EMPTY -- no renderable ROP found in this scene; "
                     "fill it in on the render node.")
    if result.scheduler_assigned:
        lines.append("TOP Scheduler: set to the farm scheduler for this network.")

    lines.extend(result.notes)
    lines.append("Cook the BOTTOM node only, in one go. Cooking two nodes in two "
                 "calls starts two PDG cooks and pays for a second GPU pod.")
    lines.append("No download node on purpose: the scheduler's Download Outputs "
                 "already brings frames home during the cook. Add a RunPodFarm "
                 "Download by hand only to re-fetch a folder later.")
    return lines


# -- the entry point the shelf tool calls ------------------------------------


def run(kwargs=None, ui=True):
    """Entry point for the TAB-menu tool. Needs Houdini."""
    import hou

    parent = _target_network(kwargs)
    if parent is None:
        raise hou.NodeError(
            "RunPod Farm Setup could not tell which network to build in. "
            "Press TAB inside a TOP network.")

    hip_dir = hou.expandString("$HIP") or ""
    hip_file = hou.hipFile.basename() or ""
    start, end = hou.playbar.frameRange()
    result = build(
        parent,
        project=project_name(hip_dir, hip_file),
        frames=frame_count(start, end),
        rop_candidates=rop_candidates(),
    )

    lines = report(result)
    for line in lines:
        print("[rpfarm-setup] {}".format(line))
    if ui and hasattr(hou, "ui") and hou.isUIAvailable():
        hou.ui.displayMessage("\n".join(lines), title="RunPod Farm Setup")
    return result


def _target_network(kwargs):
    """The network the TAB was pressed in."""
    import hou

    if kwargs:
        pane = kwargs.get("pane")
        if pane is not None and hasattr(pane, "pwd"):
            return pane.pwd()
        node = kwargs.get("node")
        if node is not None:
            return node
    try:
        import toolutils

        pane = toolutils.activePane(kwargs or {})
        if pane is not None and hasattr(pane, "pwd"):
            return pane.pwd()
    except Exception:
        pass
    pane = hou.ui.paneTabOfType(hou.paneTabType.NetworkEditor) if hasattr(hou, "ui") else None
    return pane.pwd() if pane is not None else None


#: ROP types worth fetching. Named rather than "everything in /out" because
#: /out also holds fetch, merge, switch and wedge nodes, none of which
#: render anything by themselves.
RENDER_ROP_TYPES = frozenset({
    "karma", "usdrender", "usdrender_rop", "ifd", "arnold", "Redshift_ROP",
    "vray_renderer", "opengl", "baketexture", "geometry", "filecache",
    "alembic", "comp", "dop", "channel",
})


def rop_candidates():
    """Renderable ROPs in this scene, as node paths. Needs Houdini."""
    import hou

    found = []
    out = hou.node("/out")
    if out is not None:
        for node in out.children():
            if node.type().name() in RENDER_ROP_TYPES:
                found.append(node.path())
    # A modern Karma setup renders from a LOP network, where the ROP is a
    # usdrender_rop inside /stage rather than a node in /out.
    stage = hou.node("/stage")
    if stage is not None:
        for node in stage.allSubChildren() if hasattr(stage, "allSubChildren") else stage.children():
            if node.type().name() in ("usdrender_rop", "usdrender"):
                found.append(node.path())
    return found


def farm_path(project, user):
    """Where a project's files land on the volume -- for the report only."""
    return posixpath.join("/workspace/projects", user or "<user>", project or "<project>")
