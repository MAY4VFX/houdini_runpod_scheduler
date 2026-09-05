"""What a USD stage reads that ``hou.fileReferences()`` cannot see.

A Houdini file reference is a *parameter* holding a path. Everything a USD
layer references from inside itself has no parameter: no row in
``hou.fileReferences()``, no row in Houdini's own file-dependency dialog,
and -- until this module -- no row in a RunPodFarm upload. Measured on
``airship_v013.hip`` (2026-09-05): the stage behind
``/stage/render_shot0012`` reads two on-disk layers
(``Zeppelin_Balon_Test.usdc``, ``0012box_and_camera.usdc``) and 16 distinct
asset paths, of which eleven are ``<UDIM>`` templates. Not one of them was
in the upload. A render on the farm cannot survive that.

Two sources, deliberately unioned:

* **The composed stage** -- what this cook will really read, including
  overrides authored by LOPs (on that scene the materials point at
  ``tex_mip/*.exr``, not at the ``textures/*.png`` the layer file names).
  Fast: 0.04 s for the whole attribute walk.
* **``UsdUtils.ComputeAllDependencies`` per on-disk layer** -- what the
  layer FILE needs, which is a different question: it sees what the local
  stage never composed (an unloaded payload, an unselected variant, a
  purpose the viewport filtered out) and it expands UDIM itself. On that
  scene: 0.02 s, 71 assets, 1.60 GB.

The union is the safe direction -- an extra file costs bandwidth, a missing
one costs a rendered-nothing GPU hour -- and the confirmation window is
where anything unwanted gets unchecked.

``pxr`` and ``hou`` are imported lazily inside the functions that need
them, so the pure helpers here stay importable (and testable) anywhere.
"""

from __future__ import annotations

import glob as _glob
import os
import re
import time

# <UDIM> is USD's tile token; Houdini writes <udim> too. Any other <...>
# token (<ATTR:name>, <f>, ...) is a stand-in for something we cannot
# evaluate here, so it degrades to a plain wildcard -- glob only ever
# returns files that exist, so a too-wide pattern costs a directory listing,
# not a wrong upload.
_UDIM_RE = re.compile(r"<udim>", re.IGNORECASE)
_TOKEN_RE = re.compile(r"<[^<>]+>")


def is_template(path):
    """True when *path* names a family of files rather than one file."""
    return bool(path) and bool(_TOKEN_RE.search(path))


def glob_pattern(path):
    """A ``<UDIM>`` template as a glob: four digits, not a bare wildcard."""
    pattern = _UDIM_RE.sub("[0-9][0-9][0-9][0-9]", path)
    return _TOKEN_RE.sub("*", pattern)


def expand_asset(raw, resolved="", layer_dir="", glob_fn=None, isfile=os.path.isfile):
    """One USD asset path -> the real files it names.

    Order matters and each step is a case seen on the field scene:

    1. USD's own ``resolvedPath`` when it is set and real -- the resolver
       already did the work, including any asset-resolution plugin.
    2. A ``<UDIM>`` (or other ``<...>``) template -- ``resolvedPath`` comes
       back EMPTY for these, so they are globbed instead. Eleven of the
       sixteen asset paths on that stage are this case.
    3. A relative path (``./textures/overcast_soil_puresky_7k_hdr.hdr``) --
       resolved against the directory of the LAYER that authored it, never
       against the process's cwd. ``resolvedPath`` is empty here too.
    4. An absolute path that exists.

    Returns [] when nothing matches: a reference to a file that is not
    there is not an error here, it is simply nothing to upload.
    """
    globber = glob_fn or _glob.glob
    if resolved and isfile(resolved):
        return [os.path.normpath(resolved)]
    if not raw:
        return []
    candidate = raw if os.path.isabs(raw) or not layer_dir else os.path.join(layer_dir, raw)
    candidate = os.path.normpath(candidate)
    if is_template(raw):
        return sorted({os.path.normpath(p) for p in globber(glob_pattern(candidate))})
    if isfile(candidate):
        return [candidate]
    return []


def stage_node_of(rop):
    """The LOP whose stage *rop* renders: its input, else its ``loppath``."""
    try:
        wired = [n for n in rop.inputs() if n is not None]
    except Exception:
        wired = []
    if wired:
        return wired[0]
    try:
        parm = rop.parm("loppath")
        return rop.node(parm.evalAsString()) if parm else None
    except Exception:
        return None


def _authoring_layer_dir(attr, fallback=""):
    """Directory of the layer that authored *attr*, for relative paths."""
    from pxr import Usd

    try:
        for spec in attr.GetPropertyStack(Usd.TimeCode.Default()):
            real = spec.layer.realPath
            if real:
                return os.path.dirname(real)
    except Exception:
        pass
    return fallback


def _asset_values(attr):
    """Every ``Sdf.AssetPath`` on *attr*, default value and time samples."""
    from pxr import Sdf, Usd

    if attr.GetTypeName() not in (Sdf.ValueTypeNames.Asset, Sdf.ValueTypeNames.AssetArray):
        return []
    times = [Usd.TimeCode.Default()]
    try:
        times.extend(attr.GetTimeSamples())
    except Exception:
        pass
    out = []
    for when in times:
        try:
            value = attr.Get(when)
        except Exception:
            continue
        if value is None:
            continue
        items = value if hasattr(value, "__len__") and not isinstance(value, Sdf.AssetPath) else [value]
        for item in items:
            path = getattr(item, "path", "")
            if path:
                out.append((path, getattr(item, "resolvedPath", "")))
    return out


def _stage_of(lop, say):
    """The LOP's composed stage, cooking it first if nothing has yet.

    ``LopNode.stage()`` returns None on a node that has never cooked -- and
    in a batch cook or a freshly opened scene, nothing has. That is not an
    error and there is nothing on the node to report, so it looks exactly
    like "this branch has no USD in it", which is how a whole stage's worth
    of textures would go missing in silence. Cook once, then ask again.
    """
    for attempt in ("first", "after a cook"):
        try:
            stage = lop.stage()
        except Exception as exc:
            say("usd: {} has no stage ({})".format(lop.path(), exc))
            return None
        if stage is not None:
            return stage
        if attempt != "first":
            break
        try:
            lop.cook(force=False)
        except Exception as exc:
            say("usd: {} would not cook ({})".format(lop.path(), exc))
            return None
    errors = ""
    try:
        errors = "; ".join(lop.errors())
    except Exception:
        pass
    say("usd: {} produced no stage even after cooking{}".format(
        lop.path(), " -- " + errors if errors else ""))
    return None


def stage_dependencies(lop, log=None, deep=True):
    """Every on-disk file the stage behind *lop* needs.

    Returns a sorted list of paths. ``deep`` also runs
    ``UsdUtils.ComputeAllDependencies`` on each on-disk layer -- 0.02 s on
    the field scene, but the community reports it can be slow on large
    ones, so it is timed, logged, and never allowed to raise.
    """
    say = log if log is not None else (lambda _m: None)
    found = set()
    started = time.time()
    stage = _stage_of(lop, say)
    if stage is None:
        return []
    say("usd: stage of {} in {:.1f}s".format(lop.path(), time.time() - started))

    layers = []
    try:
        for layer in stage.GetUsedLayers():
            real = getattr(layer, "realPath", "")
            if real and os.path.isfile(real):
                layers.append(os.path.normpath(real))
    except Exception as exc:
        say("usd: cannot list layers ({})".format(exc))
    found.update(layers)

    root_dir = os.path.dirname(layers[0]) if layers else ""
    started = time.time()
    assets = 0
    try:
        for prim in stage.TraverseAll():
            for attr in prim.GetAttributes():
                for raw, resolved in _asset_values(attr):
                    assets += 1
                    found.update(expand_asset(
                        raw, resolved, _authoring_layer_dir(attr, root_dir)))
    except Exception as exc:
        say("usd: attribute walk stopped ({})".format(exc))
    say("usd: {} layer(s) on disk, {} asset value(s) in {:.2f}s".format(
        len(layers), assets, time.time() - started))

    if deep and layers:
        found.update(_layer_dependencies(layers, say))
    return sorted(found)


def _layer_dependencies(layers, say):
    """``UsdUtils.ComputeAllDependencies`` over each layer, timed and guarded."""
    try:
        from pxr import UsdUtils
    except ImportError as exc:
        say("usd: UsdUtils unavailable ({}) -- layer files only".format(exc))
        return []
    out = set()
    for layer in layers:
        started = time.time()
        try:
            sublayers, assets, unresolved = UsdUtils.ComputeAllDependencies(layer)
        except Exception as exc:
            say("usd: ComputeAllDependencies({}) failed ({})".format(os.path.basename(layer), exc))
            continue
        for path in list(sublayers) + list(assets):
            path = str(getattr(path, "identifier", path) or "")
            if path and os.path.isfile(path):
                out.add(os.path.normpath(path))
        say("usd: {} -> {} sublayer(s), {} asset(s), {} unresolved in {:.2f}s".format(
            os.path.basename(layer), len(sublayers), len(assets), len(unresolved),
            time.time() - started))
        if unresolved:
            say("usd: {} unresolved reference(s) in {}, first: {}".format(
                len(unresolved), os.path.basename(layer), list(unresolved)[:3]))
    return out


def collect_usd_refs(rops, log=None, deep=True):
    """The USD half of the dependency set, for every ROP this cook renders."""
    say = log if log is not None else (lambda _m: None)
    found = set()
    for rop in rops:
        lop = stage_node_of(rop)
        if lop is None:
            say("usd: {} renders no LOP -- nothing to walk".format(rop.path()))
            continue
        found.update(stage_dependencies(lop, log=log, deep=deep))
    if found:
        say("usd: {} file(s) no parameter points at".format(len(found)))
    return sorted(found)
