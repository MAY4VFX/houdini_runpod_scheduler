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

import math
import os
import re
import time

from . import deps

def _asset_re(asset_regex=None):
    return re.compile(asset_regex or deps.ASSET_REGEX, re.IGNORECASE)


def expand_asset(raw, resolved="", layer_dir="", glob_fn=None, isfile=os.path.isfile,
                 asset_regex=None):
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
    if resolved and isfile(resolved):
        return [os.path.normpath(resolved)]
    if not raw:
        return []
    candidate = raw if os.path.isabs(raw) or not layer_dir else os.path.join(layer_dir, raw)
    return deps.globbed(os.path.normpath(candidate), _asset_re(asset_regex),
                        glob_fn=glob_fn, exists=isfile, isdir=lambda _p: False)


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


#: How much of what a stage references counts as needed. One menu on
#: rpfarm_upload, three answers to "which textures does this render read":
#:
#: * ``referenced`` -- everything any layer names, including what this
#:   session never composed (an unloaded payload, an unselected variant,
#:   an inactive prim) plus ``UsdUtils.ComputeAllDependencies`` per layer
#:   file when deep. The old behaviour, and the safest: on BS sto_0050 it is
#:   the ship's whole look library, 698 files / 14.4 GB, for one frame.
#: * ``stage`` -- what the composed stage reads: active prims only, and of
#:   the materials only those bound to geometry that renders. All UDIM
#:   tiles of those textures. sto_0050: 470 files / 10.2 GB.
#: * ``udim`` -- as ``stage``, but a ``<UDIM>`` texture keeps only the tiles
#:   the bound geometry's ``st`` actually lands on. Exact rather than a
#:   guess: any ray -- camera, shadow, reflection -- reads a texture at the
#:   UV of the mesh it hit, and no mesh has UVs on the other tiles. A shader
#:   that reads through its own texcoord (a projection, a manipulated
#:   ``st``) keeps the texture whole. sto_0050: 382 files / 8.2 GB.
SCOPE_REFERENCED = "referenced"
SCOPE_STAGE = "stage"
SCOPE_UDIM = "udim"
TEXTURE_SCOPES = (SCOPE_REFERENCED, SCOPE_STAGE, SCOPE_UDIM)

_UDIM_TOKEN = re.compile(r"<udim>", re.IGNORECASE)
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]+://")
_LEFTOVER_VAR = re.compile(r"\$\{?[A-Za-z_][A-Za-z0-9_]*")


def udim_tile(u, v):
    """The UDIM number a UV lands on: 1001 + U column + 10 * V row."""
    return 1001 + int(math.floor(u)) + 10 * int(math.floor(v))


def udim_tiles(st):
    """The set of UDIM tiles an ``st`` array covers.

    Inside Houdini -- the only place a stage is walked -- numpy is always
    there, and a ``Vt.Vec2fArray`` hands it its buffer without a copy: the
    BS sto_0050 ship crop is 250 meshes and 0.3 s this way, against 308 s
    walking ``Gf.Vec2f`` objects one by one. rpfarm itself still takes no
    numpy: outside Houdini (tests, a plain list of pairs) the pure loop runs.
    """
    try:
        import numpy
        arr = numpy.asarray(memoryview(st), dtype=numpy.float64).reshape(-1, 2)
    except Exception:
        arr = None
    if arr is not None:
        cells = numpy.floor(arr).astype(numpy.int64)
        return set((1001 + cells[:, 0] + 10 * cells[:, 1]).tolist())
    tiles = set()
    for u, v in st:
        tiles.add(1001 + int(math.floor(u)) + 10 * int(math.floor(v)))
    return tiles


def keep_tiles(raw, files, tiles):
    """``files`` expanded from template ``raw``, narrowed to ``tiles``.

    ``tiles`` None keeps everything; a template with no ``<UDIM>`` token is
    not a tile set and is kept whole. The tile number is read from the
    position the token held in the template, never guessed from "the last
    four digits": a frame number or a version sits there just as happily.
    """
    if tiles is None or not _UDIM_TOKEN.search(raw):
        return list(files)
    head, tail = _UDIM_TOKEN.split(os.path.basename(raw), maxsplit=1)
    pattern = re.compile("^" + re.escape(head) + r"(\d{4})" + re.escape(tail) + "$")
    out = []
    for path in files:
        match = pattern.match(os.path.basename(path))
        if match is None or int(match.group(1)) in tiles:
            out.append(path)  # not a tile of this template: never drop it silently
    return out


def asset_path_problems(raw):
    """Why an asset path cannot travel to the farm as is, or ''.

    A URI scheme (``ayon://``, ``omniverse://``) needs that system's resolver
    on the pod; a ``$VARIABLE`` left in the path needs that variable there.
    Both fail without an error -- a black texture, a missing layer -- so
    they are named before the upload, not discovered after the render.
    """
    if not raw:
        return ""
    if _SCHEME.match(raw) and not raw.lower().startswith("file://"):
        return "{} needs its own resolver on the farm".format(raw)
    if _LEFTOVER_VAR.search(raw):
        return "{} still has an unexpanded variable".format(raw)
    return ""


def stage_dependencies(lop, log=None, deep=True, asset_regex=None,
                       texture_scope=SCOPE_REFERENCED, problems=None):
    """Every on-disk file the stage behind *lop* needs.

    Returns a sorted list of paths. ``deep`` (only in the ``referenced``
    scope) also runs ``UsdUtils.ComputeAllDependencies`` on each on-disk
    layer -- 0.02 s on the field scene, but the community reports it can be
    slow on large ones, so it is timed, logged, and never allowed to raise.
    ``problems``, if a list, collects :func:`asset_path_problems` answers.
    """
    say = log if log is not None else (lambda _m: None)
    started = time.time()
    stage = _stage_of(lop, say)
    if stage is None:
        return []
    say("usd: stage of {} in {:.1f}s".format(lop.path(), time.time() - started))
    return walk_stage(stage, log=log, deep=deep, asset_regex=asset_regex,
                      texture_scope=texture_scope, problems=problems)


def file_dependencies(paths, log=None, asset_regex=None, texture_scope=SCOPE_UDIM,
                      problems=None):
    """Every on-disk file a husk render of these USD files needs.

    The husk chain's half: ``ropusd`` wrote the delta in the artist's own
    session, so the farm renders FILES, not a hip -- and a file is the
    whole truth about what it reads. The files themselves are included.
    """
    say = log if log is not None else (lambda _m: None)
    try:
        from pxr import Usd
    except ImportError as exc:
        say("usd: pxr unavailable ({}) -- the USD files alone".format(exc))
        return sorted(os.path.normpath(p) for p in paths if os.path.isfile(p))
    found = set()
    for path in paths:
        if not os.path.isfile(path):
            say("usd: {} is not on disk -- did the ROP USD Output cook?".format(path))
            continue
        started = time.time()
        try:
            stage = Usd.Stage.Open(path)
        except Exception as exc:
            say("usd: cannot open {} ({})".format(path, exc))
            continue
        say("usd: opened {} in {:.1f}s".format(os.path.basename(path), time.time() - started))
        found.add(os.path.normpath(path))
        if problems is not None:
            for why in relative_escapes(path, _composition_refs(stage.GetRootLayer())):
                if why not in problems:
                    problems.append(why)
        found.update(walk_stage(stage, log=log, deep=False, asset_regex=asset_regex,
                                texture_scope=texture_scope, problems=problems))
    return sorted(found)


USD_EXTENSIONS = (".usd", ".usda", ".usdc", ".usdz")


def upstream_usd_files(work_items):
    """USD files the upstream work items wrote (``ropusd`` in the husk chain).

    Outputs first, then expected outputs -- an item that has not cooked yet
    only has the latter, and then the scan below says the file is missing
    rather than silently scanning nothing. Order kept, duplicates dropped.
    """
    out = []
    for item in work_items or ():
        files = []
        for name in ("outputFiles", "expectedOutputFiles"):
            try:
                files.extend(getattr(item, name) or ())
            except Exception:
                pass
        for f in files:
            path = getattr(f, "path", "") or ""
            if path.lower().endswith(USD_EXTENSIONS) and path not in out:
                out.append(path)
    return out


def husk_usd_inputs(node, upstream_items=None):
    """The USD files a husk chain renders, for the upload node *node*.

    In a cook the upstream work items say it (:func:`upstream_usd_files`).
    The preview button has no work items, so there it is the ROP USD Output
    nodes wired above -- their Output File, evaluated now. Empty means this
    is not a husk chain, and the upload scans the hip as it always has.
    """
    files = upstream_usd_files(upstream_items) if upstream_items else []
    if files or upstream_items:
        return files
    try:
        ancestors = node.inputAncestors()
    except Exception:
        return []
    for ancestor in ancestors:
        try:
            parm = ancestor.parm("lopoutput")
            path = parm.eval() if parm is not None else ""
        except Exception:
            path = ""
        if path and path.lower().endswith(USD_EXTENSIONS) and path not in files:
            files.append(path)
    return files


def relative_escapes(layer_path, refs):
    """Composition arcs of *layer_path* that are relative and climb out of its folder.

    On the farm a file inside ``$JOB`` lands in the project mirror and a
    file outside it under ``_ext/<absolute path>`` -- two trees, so
    ``../../../../publish/x.usd`` written next to a ``$JOB`` file points
    into nothing there. Houdini's ROP USD Output writes exactly that by
    default ("Use Relative Paths", ``enableoutputprocessor_simplerelativepaths``).
    Absolute paths travel: the pod's USD path-map resolver maps them.
    """
    out = []
    for ref in refs:
        if not ref or os.path.isabs(ref) or _SCHEME.match(ref):
            continue
        if os.path.normpath(ref).startswith(".."):
            out.append("{} references {} relatively, out of its own folder -- turn off "
                       "Use Relative Paths on the ROP USD Output".format(
                           os.path.basename(layer_path), ref))
    return out


def _composition_refs(layer):
    """Sublayer, reference and payload asset paths authored in one layer."""
    refs = list(layer.subLayerPaths)

    def visit(path):
        spec = layer.GetPrimAtPath(path)
        if spec is None:
            return
        for arcs in (spec.referenceList, spec.payloadList):
            try:
                refs.extend(item.assetPath for item in arcs.GetAddedOrExplicitItems())
            except Exception:
                pass

    layer.Traverse("/", visit)
    return [r for r in refs if r]


def walk_stage(stage, log=None, deep=True, asset_regex=None,
               texture_scope=SCOPE_REFERENCED, problems=None):
    """The files *stage* reads, at the requested :data:`TEXTURE_SCOPES` scope."""
    say = log if log is not None else (lambda _m: None)
    if texture_scope not in TEXTURE_SCOPES:
        say("usd: unknown texture scope {!r} -- using {!r}".format(texture_scope, SCOPE_REFERENCED))
        texture_scope = SCOPE_REFERENCED
    found = set()

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

    def note(raw):
        why = asset_path_problems(raw)
        if why and problems is not None and why not in problems:
            problems.append(why)

    started = time.time()
    assets = 0
    if texture_scope == SCOPE_REFERENCED:
        try:
            for prim in stage.TraverseAll():
                for attr in prim.GetAttributes():
                    for raw, resolved in _asset_values(attr):
                        assets += 1
                        note(raw)
                        found.update(expand_asset(
                            raw, resolved, _authoring_layer_dir(attr, root_dir),
                            asset_regex=asset_regex))
        except Exception as exc:
            say("usd: attribute walk stopped ({})".format(exc))
        say("usd: {} layer(s) on disk, {} asset value(s) in {:.2f}s".format(
            len(layers), assets, time.time() - started))
        if deep and layers:
            found.update(_layer_dependencies(layers, say))
        return sorted(found)

    tiles_by_material = _bound_materials(stage, want_tiles=texture_scope == SCOPE_UDIM, say=say)
    material_roots = set(tiles_by_material)
    textures = 0
    try:
        from pxr import Usd, UsdShade
        for prim in stage.Traverse(Usd.TraverseInstanceProxies()):
            owner = _material_owner(prim)
            if owner is not None and owner not in material_roots:
                continue  # a material nothing renders with
            tiles = tiles_by_material.get(owner) if owner is not None else None
            whole = owner is None or tiles is None or _reads_own_texcoord(prim, UsdShade)
            for attr in prim.GetAttributes():
                for raw, resolved in _asset_values(attr):
                    assets += 1
                    note(raw)
                    files = expand_asset(raw, resolved, _authoring_layer_dir(attr, root_dir),
                                         asset_regex=asset_regex)
                    if owner is not None:
                        textures += 1
                        if not whole:
                            files = keep_tiles(raw, files, tiles)
                    found.update(files)
    except Exception as exc:
        say("usd: attribute walk stopped ({})".format(exc))
    say("usd: scope {} -- {} layer(s), {} bound material(s), {} asset value(s) "
        "({} texture) in {:.2f}s".format(texture_scope, len(layers), len(material_roots),
                                          assets, textures, time.time() - started))
    return sorted(found)


def _material_owner(prim):
    """Path of the UsdShade.Material *prim* is or sits inside, else None."""
    from pxr import UsdShade

    current = prim
    while current and current.IsValid() and not current.IsPseudoRoot():
        if current.IsA(UsdShade.Material):
            return current.GetPath()
        current = current.GetParent()
    return None


def _reads_own_texcoord(prim, UsdShade):
    """A shader that feeds its texture lookup something other than ``st``."""
    try:
        shader = UsdShade.Shader(prim)
        if not shader:
            return False
        for name in ("texcoord", "st", "uv"):
            port = shader.GetInput(name)
            if port and port.HasConnectedSource():
                return True
    except Exception:
        return True  # cannot tell -- keep the texture whole
    return False


def _bound_materials(stage, want_tiles, say):
    """``{material path: tiles or None}`` for materials bound to rendering geometry.

    Rendering geometry: active, not invisible, purpose default or render --
    a proxy or guide never reaches a final frame. GeomSubset bindings count,
    with the tiles of the mesh they split. ``None`` means "all tiles": no
    ``st`` to read, or the scope did not ask for tiles.
    """
    from pxr import Usd, UsdGeom, UsdShade

    out = {}
    gprims = 0
    started = time.time()
    for prim in stage.Traverse(Usd.TraverseInstanceProxies()):
        if not prim.IsA(UsdGeom.Gprim):
            continue
        imageable = UsdGeom.Imageable(prim)
        try:
            if imageable.ComputeVisibility() == UsdGeom.Tokens.invisible:
                continue
            if imageable.ComputePurpose() in (UsdGeom.Tokens.proxy, UsdGeom.Tokens.guide):
                continue
        except Exception:
            pass
        gprims += 1
        bound = []
        material, _ = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial(UsdShade.Tokens.full)
        if material:
            bound.append(material.GetPath())
        for subset in UsdGeom.Subset.GetAllGeomSubsets(UsdGeom.Imageable(prim)):
            sub_mat, _ = UsdShade.MaterialBindingAPI(subset.GetPrim()).ComputeBoundMaterial(
                UsdShade.Tokens.full)
            if sub_mat:
                bound.append(sub_mat.GetPath())
        if not bound:
            continue
        tiles = None
        if want_tiles:
            st = None
            try:
                primvar = UsdGeom.PrimvarsAPI(prim).GetPrimvar("st")
                st = primvar.Get() if primvar else None
            except Exception:
                st = None
            tiles = udim_tiles(st) if st else None
        for path in bound:
            if path in out and out[path] is None:
                continue
            if tiles is None:
                out[path] = None
            else:
                out[path] = (out.get(path) or set()) | tiles
    say("usd: {} rendering gprim(s) -> {} bound material(s) in {:.2f}s".format(
        gprims, len(out), time.time() - started))
    return out


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


def collect_usd_refs(rops, log=None, deep=True, asset_regex=None,
                     texture_scope=SCOPE_REFERENCED, problems=None):
    """The USD half of the dependency set, for every ROP this cook renders."""
    say = log if log is not None else (lambda _m: None)
    found = set()
    for rop in rops:
        lop = stage_node_of(rop)
        if lop is None:
            say("usd: {} renders no LOP -- nothing to walk".format(rop.path()))
            continue
        found.update(stage_dependencies(lop, log=log, deep=deep,
                                        asset_regex=asset_regex,
                                        texture_scope=texture_scope, problems=problems))
    if found:
        say("usd: {} file(s) no parameter points at".format(len(found)))
    return sorted(found)
