"""Hip-file dependency collection and PDG path mapping.

Ports the manifest-building half of ``_create_manifest`` from the v1 HDA
(``hda/runpodfarm_scheduler.hda/Top_1runpodfarmscheduler/PythonModule``,
roughly lines 965-1093): walk ``hou.fileReferences()``, expand ``$HIP``/
``$JOB``, and reduce sequence/UDIM references to their containing
directory. The SHA-256 content hashing that lived alongside that logic in
v1 is intentionally *not* ported here — dedup in this module is by
normalized local path only, not content hash.

Only :func:`collect_refs` may ``import hou``, and it does so lazily inside
the function body. Everything else (:func:`resolve_entries`,
:func:`pathmap_env`, and the small helpers) is pure stdlib and importable/
testable without Houdini installed.
"""

from __future__ import annotations

import fnmatch
import glob
import json
import os
import re
from dataclasses import dataclass

from .sync import FileEntry

# $F, $F4, %04d, ####, $(F4), ${F4} -- a per-FRAME sequence. The frames
# that exist on disk are not the frames the farm will read (a cache may be
# partially written, a render range may start anywhere), so these reduce to
# the containing directory, which is the honest superset.
_SEQ = re.compile(r"\$F\d*|%0?\d*d|#{2,}|\$\(F\d*\)|\$\{F\d*\}")

# Everything in an evaluated path that stands for "this part varies": UDIM
# and tile tokens, and a run of digits fenced by _ or . (frame numbers,
# `_1001_`, `.0001.`). Matches become `*` and the result is globbed, so only
# files that really exist come back.
#
# The default is Conductor's (ciohoudini/assets.py, an open-source
# production submitter), verbatim, and for the same reason they made it a
# user-editable parameter: one mechanism for every varying part of a path
# beats a special case per token, and a studio with a naming convention
# nobody anticipated can fix it themselves.
#
# It also closes a hole a token-by-token approach cannot see. Houdini
# expands `$F4` to the CURRENT FRAME (`hou.text.expandString("sim.$F4.bgeo")`
# -> `sim.0001.bgeo`, verified), so by the time any of this code sees a
# cache sequence it looks like one concrete file -- and one frame is what
# used to upload. `.0001.` matches this regex; `$F4` never survived to match
# anything.
ASSET_REGEX = r"(_|\.)\d+(_|\.)|<udim>|<u>|<v>|<u2>|<v2>|<obj_name>"

# Parms that hold a path but which hou.fileReferences() does not report,
# because their string type is not FileReference. Houdini has no flag to
# fix this from outside, so the list is explicit and extensible -- exactly
# what Conductor does (ADDITIONAL_PATH_PARMS in ciohoudini/assets.py), with
# the same first entry: `filepath1` on a Reference LOP is
# stringParmType.Regular, which is why the .usdc an entire render is built
# from never appeared in any scan.
#
# A name ending in a digit is treated as the first instance of a multiparm
# block and its siblings (filepath2, filepath3, ...) are followed until one
# is missing.
ADDITIONAL_PATH_PARMS = {
    "Lop/reference::2.0": ("filepath1",),
}

# Node types whose parameters are ours, not the scene's: scanning our own
# upload/scheduler assets adds their internal localscheduler's
# `pdg_workingdir` ($HIP -- the whole project) to the plan as a row nobody
# asked about. Conductor skips their own submitter node the same way.
_OUR_NODE_PREFIX = "runpodfarm"

# Directories never worth uploading: PDG's own scratch dir, hand-rolled
# backup folders, VCS metadata, bytecode cache. Pruned at any depth while
# walking a directory in resolve_entries.
_SKIP_DIR_NAMES = {"pdgtemp", "backup", ".git", "__pycache__"}

# Houdini's own autosave/backup files, plus the generic "~" editor-backup
# suffix. Skipped regardless of whether they're discovered via directory
# walk or passed directly.
_SKIP_FILE_SUFFIXES = (".hip.bak", ".hiplc.bak", ".hipnc.bak", "~")

# Windows drive letter at the start of a path, e.g. "C:\" or "C:/". Matched
# with a plain regex (not `ntpath`/`os.path.splitdrive`) so _ext_suffix
# behaves the same regardless of which OS is actually running the code.
_DRIVE_RE = re.compile(r"^([A-Za-z]):[\\/]")

# A bare drive root with nothing after it, e.g. "C:\" or "C:/" (or "C:"
# with no trailing separator, which os.path.dirname can produce). Used by
# _pathmap_key to detect the same degenerate-root problem POSIX "/" has.
_DRIVE_ROOT_RE = re.compile(r"^[A-Za-z]:[\\/]?$")


# -- what is not a dependency ----------------------------------------------------
#
# ``hou.fileReferences()`` answers "which parameters hold a path", which is
# not the question an upload asks -- "what does this scene need in order to
# render". Field case (2026-09-05, ``airship_v013.hip``): ``pdg_workingdir``
# on a TOP scheduler in an unrelated network held the project folder, and
# because :func:`resolve_entries` walks a directory reference recursively,
# the upload planned 794 files / 9.97 GB for a scene that reads 113 files /
# 1.32 GB -- ten .hip versions, 467 finished EXRs and a 1.47 GB export zip.
#
# Every name below says where results GO, or where a job runs; nothing ever
# reads them back. Matched by parameter name alone, deliberately: the name
# means the same thing on every node type that has it, where a (type, parm)
# table would go stale on the next HDA version. ``file``/``filename`` are
# emphatically NOT here -- those are the File SOP/COP inputs, the commonest
# real dependency in any scene.
_NON_DEPENDENCY_PARMS = frozenset({
    # PDG: where a scheduler runs its jobs and stages their data
    "pdg_workingdir", "pdg_transferroot", "pdg_tempdir", "pdg_scriptdir",
    # PDG: scratch the topnet itself writes
    "taskgraphfile", "checkpointfile",
    # Houdini's own render-gallery database
    "rendergallerysource",
    # where a render writes
    "outputimage", "outputfilepath1", "savetodirectory_directory",
    "picture", "vm_picture", "soho_diskfile",
    "sopoutput", "dopoutput", "copoutput", "lopoutput", "usdoutput",
    # Karma cryptomatte side-cars
    "cryptopicture", "mtlcryptofile", "primcryptofile",
    "kindcryptofile1", "primvarcryptofile1",
})

# collect_refs scopes. SCOPE_BRANCH narrows to the nodes the cook actually
# reads (see _branch_node_paths); SCOPE_SCENE is every reference in the hip
# file, which is what this module did unconditionally before 2026-09-05.
SCOPE_SCENE = "scene"
SCOPE_BRANCH = "branch"


@dataclass(frozen=True)
class PlanRow:
    """One line of the upload plan: a reference, weighed but not expanded.

    ``kind`` is ``"file"`` or ``"dir"``. A directory stays ONE row carrying
    the full weight of everything under it (``files``/``bytes``), because a
    directory reference is how a single parameter turns into gigabytes and
    that has to be visible as one obvious line -- not lost among the 827
    individual files it would otherwise become.
    """

    path: str
    kind: str
    files: int
    bytes: int
    source: str = "scene"


def globbed(value, asset_re, glob_fn=None, exists=os.path.exists, isdir=os.path.isdir):
    """Every real file *value* names, with its varying parts globbed.

    Three cases, in this order:

    1. Something in the path varies (``asset_re`` matched) -- substitute
       ``*`` and glob. Only files that exist come back, so a pattern that is
       too wide costs a directory listing, not a wrong upload.
    2. A Houdini variable survived expansion (``$F`` in a string nothing
       evaluated) -- fall back to the containing directory, which is the
       honest superset when the frames cannot be enumerated.
    3. A plain path -- keep it if it is there.
    """
    globber = glob_fn or glob.glob
    if asset_re is not None and asset_re.search(value):
        return sorted({os.path.normpath(p) for p in globber(asset_re.sub("*", value))})
    if _SEQ.search(value):
        parent = os.path.dirname(value)
        return [os.path.normpath(parent)] if isdir(parent) else []
    return [os.path.normpath(value)] if exists(value) else []


def expand_reference(parm, pattern, expand, exists=os.path.exists, isdir=os.path.isdir,
                     glob_fn=None, asset_re=None, roots=()):
    """Every real path one ``(parm, pattern)`` file reference resolves to.

    Two ways to resolve a reference, and neither is a superset of the other:

    * ``parm.evalAsString()`` -- what the *parameter* means. It knows ``$OS``,
      channel references and everything else only the node can expand. SideFX
      recommend it in the ``displayFileDependencyDialog`` docs.
    * ``hou.text.expandString(pattern)`` -- what the *string* means.

    Following the recommendation alone loses files. Measured on
    ``airship_v013.hip``: of 115 references, 38 resolve only through the
    string and none only through the parameter. Those 38 are FBX, where
    evaluating the parameter returns an address INTO the file --
    ``/Users/may/Downloads/airship_v06.fbx#Airship_fullBindings,convertoff``
    -- which is not a path on disk. So: try both, keep what exists, and when
    both exist and disagree keep both. Uploading one file too many costs
    bandwidth; missing one costs a failed render on a rented GPU.

    Whatever varies in the path -- a UDIM tile, a frame number -- is
    globbed rather than checked literally (see :func:`globbed`).

    ``expand``/``exists``/``isdir`` are injected so this function is testable
    without Houdini; the caller passes ``hou.text.expandString``.
    """
    if asset_re is None:
        asset_re = re.compile(ASSET_REGEX, re.IGNORECASE)
    out = []
    candidates = []
    if parm is not None:
        try:
            candidates.append(parm.evalAsString())
        except Exception:
            pass
    if pattern:
        try:
            candidates.append(expand(pattern) if "$" in pattern else pattern)
        except Exception:
            pass
    for value in list(candidates):
        # A relative path is resolved against $HIP and against $JOB, keeping
        # whichever exists -- Conductor's _resolve_absolute_existing_paths
        # does the same, and "it's the best we can do for now" is their own
        # honest comment on it.
        if value and not value.startswith(("op:", "opdef:", "temp:")) and not os.path.isabs(value):
            candidates.extend(os.path.join(root, value) for root in roots if root)

    for value in candidates:
        if not value or value.startswith(("op:", "opdef:", "temp:")):
            continue  # internal references, not files (Conductor skips these too)
        if not os.path.isabs(value):
            continue  # only the rooted forms above can be checked
        # Globbed BEFORE any existence check: a path with <UDIM> or a frame
        # number in it never exists literally, so checking first is how a
        # whole material's textures -- or every frame but one -- goes missing.
        for item in globbed(value, asset_re, glob_fn=glob_fn, exists=exists, isdir=isdir):
            if item not in out:
                out.append(item)
    return out


@dataclass(frozen=True)
class RefScan:
    """What one pass over ``hou.fileReferences()`` found.

    ``paths`` -- real local paths, deduplicated, hip file first.
    ``output_paths`` -- paths of references that name where output GOES
    (:data:`_NON_DEPENDENCY_PARMS`). Kept apart rather than dropped: they
    become rows in the confirmation window, unchecked, so the decision is
    visible and the artist can overrule it. That is where the 11.54 GB
    ``pdg_workingdir`` folder shows up as one line with its real weight.
    ``unresolved`` -- patterns that resolved to nothing on disk, kept for
    the log: a reference that will not upload is worth one line.
    """

    paths: list
    output_paths: list
    unresolved: tuple


def expand_pairs(pairs, expand, exists=os.path.exists, isdir=os.path.isdir, glob_fn=None,
                 asset_re=None, roots=()):
    """``(parm, pattern)`` pairs -> ``(paths, unresolved patterns)``.

    Used for both halves of the flow: the scan below, and the selection
    Houdini's dependency dialog hands back (which is the same shape).
    """
    paths = []
    unresolved = []
    seen = set()
    for parm, pattern in pairs:
        if not pattern or pattern.startswith(("op:", "opdef:", "temp:")):
            continue
        found = expand_reference(parm, pattern, expand, exists=exists, isdir=isdir,
                                 glob_fn=glob_fn, asset_re=asset_re, roots=roots)
        if not found:
            unresolved.append(pattern)
            continue
        for path in found:
            if path not in seen:
                seen.add(path)
                paths.append(path)
    return paths, unresolved


def additional_ref_pairs(log=None):
    """``(parm, value)`` pairs for parameters ``hou.fileReferences()`` misses.

    Houdini decides what is a file reference by the parameter's string
    TYPE, not by what it holds. ``filepath1`` on a Reference LOP is
    ``stringParmType.Regular``, so the .usdc a whole render is built from is
    simply not in the list, and no flag fixes that from outside. The answer
    -- Conductor's, arrived at independently and running in production --
    is an explicit table of node type -> parameter names, walked through
    ``hou.nodeType(name).instances()``.

    A declared name ending in a digit is the first entry of a multiparm
    block, so its siblings are followed too: a Reference LOP with three
    references has ``filepath1``, ``filepath2``, ``filepath3``.
    """
    import hou

    say = log if log is not None else (lambda _m: None)
    out = []
    for type_name, parm_names in ADDITIONAL_PATH_PARMS.items():
        try:
            node_type = hou.nodeType(type_name)
        except Exception as exc:  # a supplement must never break the scan
            say("cannot look up node type {} ({})".format(type_name, exc))
            continue
        if node_type is None:
            say("no node type {} in this Houdini -- skipped".format(type_name))
            continue
        for node in node_type.instances():
            for parm_name in parm_names:
                for parm in _multiparm_series(node, parm_name):
                    try:
                        value = parm.unexpandedString()
                    except Exception:
                        continue
                    if value:
                        out.append((parm, value))
    if out:
        say("{} path parm(s) Houdini does not report as file references".format(len(out)))
    return out


def _multiparm_series(node, parm_name):
    """``filepath1`` -> that parm and every ``filepath2..N`` that exists."""
    parm = node.parm(parm_name)
    if parm is None:
        return []
    out = [parm]
    stem = parm_name.rstrip("0123456789")
    digits = parm_name[len(stem):]
    if not digits:
        return out
    index = int(digits) + 1
    while True:
        nxt = node.parm("{}{}".format(stem, index))
        if nxt is None:
            return out
        out.append(nxt)
        index += 1


def remove_pattern(paths, pattern):
    """Drop paths matching a glob *pattern* -- the node's exclusion field.

    ``fnmatch`` semantics against the whole path, so ``*/backup/*`` and
    ``*.abc`` both work. An empty pattern removes nothing.
    """
    if not pattern:
        return list(paths)
    parts = [p.strip() for p in pattern.split(",") if p.strip()]
    if not parts:
        return list(paths)
    return [p for p in paths if not any(fnmatch.fnmatch(p, part) for part in parts)]


def scan_refs(scope=SCOPE_SCENE, node=None, log=None, project_dir_variable="HIP",
              exists=os.path.exists, isdir=os.path.isdir, glob_fn=None,
              asset_regex=ASSET_REGEX, exclude_pattern=""):
    """Scan the scene's file references and resolve them to real paths.

    The only function in this module allowed to ``import hou`` -- done
    lazily inside the function body so the rest of ``rpfarm.deps`` stays
    pure and importable/testable without Houdini installed.

    - The hip file itself (``hou.hipFile.path()``) is always first.
    - References named in :data:`_NON_DEPENDENCY_PARMS` come back separately
      as ``output_paths``: they are where results go, so they are not
      uploaded by default, but they are shown -- and overrulable -- rather
      than dropped out of sight.
    - With ``scope=SCOPE_BRANCH`` and ``node`` set to the upload node, only
      references owned by a node this cook reads survive -- see
      :func:`_branch_node_paths`. A reference no parameter owns (Houdini
      reports installed .hda files that way) cannot be attributed to a
      branch, so it is kept.
    - Each surviving reference is resolved by :func:`expand_reference`,
      both ways, keeping what exists.

    ``log`` -- if given, a one-argument callable receiving human-readable
    diagnostics. Nothing is printed by default.
    """
    import hou

    say = log if log is not None else (lambda _message: None)
    branch = _branch_node_paths(node, say) if scope == SCOPE_BRANCH else None

    pairs = list(hou.fileReferences(project_dir_variable)) + additional_ref_pairs(say)
    kept = []
    outputs = []
    off_branch = 0
    ours = 0
    for parm, pattern in pairs:
        if parm is not None:
            if _is_our_own(parm):
                ours += 1
                continue
            owner = _parm_node_path(parm)
            if branch is not None and owner is not None and owner not in branch:
                off_branch += 1
                continue
            if _parm_name(parm) in _NON_DEPENDENCY_PARMS:
                outputs.append((parm, pattern))
                continue
        kept.append((parm, pattern))

    try:
        asset_re = re.compile(asset_regex, re.IGNORECASE) if asset_regex else None
    except re.error as exc:
        say("asset pattern {!r} does not compile ({}) -- using the default".format(asset_regex, exc))
        asset_re = re.compile(ASSET_REGEX, re.IGNORECASE)
    expand = dict(expand=hou.text.expandString, exists=exists, isdir=isdir,
                  glob_fn=glob_fn, asset_re=asset_re,
                  roots=(hou.getenv("HIP"), hou.getenv("JOB")))
    paths, unresolved = expand_pairs(kept, **expand)
    output_paths, _ = expand_pairs(outputs, **expand)
    paths.insert(0, hou.hipFile.path())
    if exclude_pattern:
        before = len(paths) + len(output_paths)
        paths = remove_pattern(paths, exclude_pattern)
        output_paths = remove_pattern(output_paths, exclude_pattern)
        dropped = before - len(paths) - len(output_paths)
        if dropped:
            say("{} path(s) dropped by the exclude pattern {!r}".format(dropped, exclude_pattern))
    if ours:
        say("skipped {} reference(s) on RunPodFarm's own nodes".format(ours))
    if outputs:
        say("{} reference(s) name outputs, not inputs -- offered unchecked".format(len(outputs)))
    if off_branch:
        say("skipped {} reference(s) outside this cook's branch".format(off_branch))
    if unresolved:
        say("{} reference(s) resolved to nothing on disk".format(len(unresolved)))
    return RefScan(paths=paths, output_paths=output_paths, unresolved=tuple(unresolved))


def collect_refs(scope=SCOPE_SCENE, node=None, log=None, **kwargs):
    """:func:`scan_refs` when only the paths are wanted."""
    return scan_refs(scope=scope, node=node, log=log, **kwargs).paths


def _is_our_own(parm):
    """True for a parameter inside a RunPodFarm asset.

    Our upload node contains its own localscheduler, whose `pdg_workingdir`
    is `$HIP` -- scan ourselves and the whole project directory turns up in
    the plan as a row the artist never put there.
    """
    try:
        node = parm.node()
        while node is not None:
            if node.type().name().startswith(_OUR_NODE_PREFIX):
                return True
            node = node.parent()
    except Exception:
        pass
    return False


def _parm_name(parm):
    try:
        return parm.name()
    except Exception:
        return ""


def _parm_node_path(parm):
    """The path of the node owning *parm*, or None when it cannot be read.

    None means "unattributable", and an unattributable reference is kept by
    every scope -- narrowing must never drop something it failed to
    understand.
    """
    try:
        return parm.node().path()
    except Exception:
        return None


def _resolve_node(base, path):
    """``path`` as a node, resolved relative to *base* first, then absolutely."""
    if not path:
        return None
    for lookup in (getattr(base, "node", None), _hou_node):
        if lookup is None:
            continue
        try:
            found = lookup(path)
        except Exception:
            continue
        if found is not None:
            return found
    return None


def _hou_node(path):
    import hou

    return hou.node(path)


def _network_nodes(net):
    """Every node in *net*, nested networks included."""
    try:
        return list(net.allSubChildren())
    except Exception:
        try:
            return list(net.children())
        except Exception:
            return []


def _referenced_nodes(node):
    """Nodes *node* names through a node-reference parameter.

    Input wires are not the only way one node reaches another: a LOP names
    a SOP through ``soppath``, a ROP Fetch names its ROP through
    ``roppath``. Those are ``hou.stringParmType.NodeReference`` parameters,
    which is a fact about the parameter's type rather than a guess from its
    name -- so the walk finds them without a list to maintain.
    """
    import hou

    wanted = tuple(
        t for t in (
            getattr(hou.stringParmType, "NodeReference", None),
            getattr(hou.stringParmType, "NodeReferenceList", None),
        ) if t is not None
    )
    if not wanted:
        return []
    try:
        parms = node.parms()
    except Exception:
        return []
    out = []
    for parm in parms:
        try:
            string_type = parm.parmTemplate().stringType()
        except Exception:
            continue  # not a string parm at all
        if string_type not in wanted:
            continue
        try:
            raw = parm.evalAsString()
        except Exception:
            continue
        for token in raw.split():
            found = _resolve_node(node, token)
            if found is not None:
                out.append(found)
    return out


def cook_rops(node, log=None):
    """The ROPs the TOP network containing *node* fetches.

    One list, two consumers: the branch walk narrows file references to
    what these ROPs read, and the USD walk needs the very same ROPs to find
    the stages they render. Computing it twice, differently, is how the two
    halves would drift apart.
    """
    say = log if log is not None else (lambda _m: None)
    try:
        topnet = node.parent()
    except Exception:
        return []
    if topnet is None:
        return []
    rops = []
    for candidate in _network_nodes(topnet):
        try:
            has_roppath = candidate.parm("roppath") is not None
        except Exception:
            continue
        if not has_roppath:
            continue
        target = candidate.evalParm("roppath")
        rop = _resolve_node(candidate, target)
        if rop is None:
            say("{} names {!r}, which is not a node".format(candidate.path(), target))
            continue
        if not any(r.path() == rop.path() for r in rops):
            rops.append(rop)
    return rops


def _branch_node_paths(node, say):
    """Paths of the nodes this TOP network's cook actually reads, or None.

    Starts where the cook ends -- every ROP Fetch in the network the upload
    node lives in -- resolves each ``roppath`` to its ROP, then walks
    *upstream*: input ancestors, node-reference parameters, and the contents
    of every node reached (a subnet's children are part of what it reads).

    None means "could not be narrowed" and the caller must fall back to the
    whole scene: an upload that ships too much costs bandwidth, one that
    ships too little costs a failed render on a rented GPU.

    Known limit, and it is not a small one: assets a USD layer references
    from *inside* itself never appear in ``hou.fileReferences()`` at all, in
    any scope. Narrowing does not lose them -- they were never there.
    """
    if node is None:
        say("branch scope: no node to start from -- using the whole scene")
        return None
    try:
        topnet = node.parent()
    except Exception:
        topnet = None
    if topnet is None:
        say("branch scope: this node has no parent network -- using the whole scene")
        return None

    rops = cook_rops(node, say)

    if not rops:
        say("branch scope: nothing in this network fetches a ROP -- using the whole scene")
        return None

    seen = {}
    queue = list(rops)
    while queue:
        current = queue.pop()
        try:
            path = current.path()
        except Exception:
            continue
        if path in seen:
            continue
        seen[path] = current
        try:
            queue.extend(x for x in current.inputAncestors(
                include_ref_inputs=True, follow_subnets=True) if x is not None)
        except Exception:
            pass
        try:
            queue.extend(current.children())
        except Exception:
            pass
        queue.extend(_referenced_nodes(current))

    say("branch scope: {} -> {} node(s)".format(
        ", ".join(sorted(r.path() for r in rops)), len(seen)))
    return set(seen)


def _dir_weight(path):
    """``(files, bytes)`` under *path*, with the same skip rules as the upload."""
    files = 0
    total = 0
    for root, dirs, names in os.walk(path, followlinks=False):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIR_NAMES]
        for name in names:
            fp = os.path.join(root, name)
            if _is_skipped_file(fp):
                continue
            try:
                total += os.path.getsize(fp)
            except OSError:
                continue  # broken symlink, vanished mid-walk
            files += 1
    return files, total


def plan_refs(paths, source="scene"):
    """Weigh each reference for the confirmation window, without expanding it.

    Returns ``(rows, missing)``: one :class:`PlanRow` per surviving
    reference in the order given, plus the references that name nothing on
    disk (dropped, exactly as :func:`resolve_entries` drops them, but
    counted so the window can say so instead of staying quiet).

    A directory is ONE row carrying its whole recursive weight. That is the
    point of this function: the parameter that cost 11.54 GB in the field
    was a single directory reference, and it has to read as a single line
    the artist can uncheck.

    The same skip rules as :func:`resolve_entries` apply, so the weight
    shown is the weight that would upload. Dedup is by normalized path;
    a file that is also inside a listed directory is counted in both rows,
    where :func:`resolve_entries` would upload it once -- so a plan's total
    is an upper bound, never an underestimate.
    """
    rows = []
    missing = []
    seen = set()
    for raw in paths:
        path = os.path.normpath(raw)
        if path in seen:
            continue
        seen.add(path)
        if os.path.isdir(path):
            if os.path.basename(path) in _SKIP_DIR_NAMES:
                continue
            files, total = _dir_weight(path)
            rows.append(PlanRow(path=path, kind="dir", files=files, bytes=total, source=source))
        elif os.path.isfile(path):
            if _is_skipped_file(path):
                continue
            rows.append(PlanRow(path=path, kind="file", files=1,
                                bytes=os.path.getsize(path), source=source))
        else:
            missing.append(path)
    return rows, missing


def _ext_suffix(path):
    """POSIX-safe suffix appended after ``"_ext"`` for a path outside ``job_dir``.

    A plain POSIX-style absolute path is returned unchanged -- it already
    starts with ``/``, so ``remote_project + "/_ext" + suffix`` concatenates
    cleanly (e.g. ``/Users/may/lib/b.abc`` -> ``/Users/may/lib/b.abc``).

    A Windows-style absolute path loses its drive colon and gets its
    backslashes flipped to forward slashes, so the whole remote path stays
    a single valid POSIX-style string:
    ``C:\\lib\\b.abc`` -> ``/C/lib/b.abc``.
    """
    m = _DRIVE_RE.match(path)
    if not m:
        return path
    drive = m.group(1)
    rest = path[m.end():].replace("\\", "/")
    return f"/{drive}/{rest}"


def _is_skipped_file(path):
    return path.endswith(_SKIP_FILE_SUFFIXES)


def _pathmap_key(parent_dir, fp):
    """Pick the path-map key for an external file's parent directory.

    Normally the key is just ``parent_dir`` -- one path-map entry per
    external root, reused by every file discovered under that same
    directory. But pdgcmd.py's ``_applyPathMapForZone`` (pdgcmd.py:926)
    applies each path-map entry with an *unanchored* ``str.replace`` in a
    fixed-point loop: a rule keyed on ``/`` (or a bare drive root like
    ``C:\\``) would then rewrite the ``/`` inside every path the worker
    touches, not just this one file's prefix -- silently corrupting the
    whole path map. So when ``parent_dir`` is exactly ``/`` or a bare
    drive root, the key is the file's own full path instead, which keeps
    the rule specific to that single file.
    """
    if parent_dir == "/" or _DRIVE_ROOT_RE.match(parent_dir):
        return fp
    return parent_dir


def resolve_entries(paths, job_dir, remote_project):
    """Turn a list of local paths into upload entries and a PDG path map.

    Pure function (no ``hou``). Each item in ``paths`` may be a file or a
    directory; directories are walked recursively. Nonexistent paths are
    silently dropped (a reference to a file that hasn't been rendered yet
    is not an error here).

    Remote layout:

    - A file under ``job_dir`` maps to ``remote_project/<rel path>``.
    - A file outside ``job_dir`` ("external") maps to
      ``remote_project/_ext/<abs path>`` (drive letters lose their colon
      on Windows -- see :func:`_ext_suffix`).

    Skip rules (applied whether a path is discovered via directory walk or
    passed directly):

    - Directories named ``pdgtemp``, ``backup``, ``.git`` or
      ``__pycache__`` are pruned at any depth (their contents are never
      walked or uploaded).
    - Files ending in ``.hip.bak``, ``.hiplc.bak``, ``.hipnc.bak`` or ``~``
      are skipped.

    Symlinks: a file symlink is followed and uploaded like a normal file
    (``os.path.isfile`` follows symlinks). A directory symlink is never
    descended into (``os.walk(..., followlinks=False)``) -- this also
    guards against symlink loops, not just links that escape ``job_dir``.

    A file is classified "inside job_dir" by a plain literal prefix check
    first (so e.g. a file symlink that physically lives outside job_dir
    but is *referenced* through a job-local path -- a common
    linked-library pattern -- still uploads as a job-relative file, matching
    what its reference path says). Only when that literal check fails does
    it fall back to comparing ``os.path.realpath`` of both the file and
    ``job_dir`` -- this is what makes a symlinked ``$JOB`` itself resolve
    correctly regardless of whether a given path is expressed through the
    symlink or already through its real target. Either way,
    ``FileEntry.local`` keeps the original normalized (not realpath'd)
    path -- only the inside/outside decision and the resulting ``rel``
    path use the resolved form.

    Skip rules apply to a directory passed directly in ``paths`` too, not
    just to directories discovered while walking: ``resolve_entries(["/proj/backup"],
    ...)`` yields nothing.

    Dedup is by normalized local path: the same path passed twice (or
    reachable both directly and via a directory walk) produces one entry.

    Returns ``(entries, path_map)`` where ``path_map`` always contains
    ``{job_dir: remote_project}`` plus one entry per distinct external
    root directory (the immediate parent of each external file) -- except
    when that parent is ``/`` or a bare drive root, where the file's own
    full path is used as the key instead (see :func:`_pathmap_key`).
    """
    job_dir = os.path.normpath(job_dir)
    job_dir_real = os.path.realpath(job_dir)
    seen = set()
    entries = []
    pmap = {job_dir: remote_project}

    def add(raw_fp):
        fp = os.path.normpath(raw_fp)
        if fp in seen or _is_skipped_file(fp) or not os.path.isfile(fp):
            return
        seen.add(fp)

        if fp == job_dir or fp.startswith(job_dir + os.sep):
            rel = os.path.relpath(fp, job_dir)
        else:
            fp_real = os.path.realpath(fp)
            if fp_real == job_dir_real or fp_real.startswith(job_dir_real + os.sep):
                rel = os.path.relpath(fp_real, job_dir_real)
            else:
                rel = None

        if rel is not None:
            remote = f"{remote_project}/{rel.replace(os.sep, '/')}"
        else:
            remote = f"{remote_project}/_ext{_ext_suffix(fp)}"
            root = os.path.dirname(fp)
            key = _pathmap_key(root, fp)
            pmap.setdefault(key, f"{remote_project}/_ext{_ext_suffix(key)}")

        entries.append(FileEntry(local=fp, remote=remote, size=os.path.getsize(fp)))

    for p in paths:
        if os.path.isdir(p):
            if os.path.basename(os.path.normpath(p)) in _SKIP_DIR_NAMES:
                continue
            for root, dirs, files in os.walk(p, followlinks=False):
                dirs[:] = [d for d in dirs if d not in _SKIP_DIR_NAMES]
                for f in files:
                    add(os.path.join(root, f))
        else:
            add(p)
    return entries, pmap


def pathmap_env(path_map):
    """Render a ``{local_prefix: remote_prefix}`` dict as a ``$PDG_PATHMAP`` string.

    Format derived from the actual PDG_PATHMAP *consumer* in this Houdini
    install -- ``_buildPathMap()`` in
    ``$HFS/houdini/python3.13libs/pdgjob/pdgcmd.py`` (Houdini 22.0.368,
    lines 888-930), specifically the parse loop at lines 913-918::

        paths = pathmap['paths']
        for e in paths:
            for from_path, v in e.items():
                e_zone = v['zone']
                ...
                to_path  = v['path']

    So ``"paths"`` is a list where each element is a single-key dict:
    ``{from_path: {"zone": <zone>, "path": <to_path>}}`` -- NOT the flat
    ``{"path": from, "to": to, "zone": zone}`` shape one might guess.
    ``resolveEnvParams``/``resolvePathMapping`` in
    ``pdg/scheduler.py:590-608`` is what serializes ``_pdg.File.pathMapJSON()``
    into the ``PDG_PATHMAP`` job-env var in the first place.

    RunPod workers run Linux, so every mapping here targets the ``"LINUX"``
    zone -- this is what ``localizePath`` on the worker side (running under
    ``sys.platform == "linux"``) will match against.
    """
    return json.dumps({"paths": [{local: {"zone": "LINUX", "path": remote}} for local, remote in path_map.items()]})
