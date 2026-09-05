import json
import os
import sys
from types import SimpleNamespace

from rpfarm import deps
from rpfarm.deps import _ext_suffix, _pathmap_key, collect_refs, pathmap_env, resolve_entries


# -- from the task brief -------------------------------------------------------


def test_inside_job_and_external(tmp_path):
    job = tmp_path / "job"
    (job / "tex").mkdir(parents=True)
    (job / "tex" / "a.rat").write_bytes(b"x" * 10)
    ext = tmp_path / "lib"
    ext.mkdir()
    (ext / "b.abc").write_bytes(b"y")
    entries, pmap = resolve_entries(
        [str(job / "tex"), str(ext / "b.abc"), str(tmp_path / "missing.exr")],
        str(job),
        "/workspace/projects/may/shot",
    )
    remotes = sorted(e.remote for e in entries)
    assert remotes[0] == "/workspace/projects/may/shot/_ext" + str(ext / "b.abc")
    assert remotes[1] == "/workspace/projects/may/shot/tex/a.rat"
    assert pmap[str(job)] == "/workspace/projects/may/shot"
    assert pmap[str(ext)] == "/workspace/projects/may/shot/_ext" + str(ext)
    # no path-map rule keyed on a bare "/" (or drive root) -- see
    # test_pathmap_key_avoids_root_and_drive_root_keys for the direct check
    assert "/" not in pmap


def test_dedup(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    (job / "s.hip").write_bytes(b"h")
    entries, _ = resolve_entries([str(job / "s.hip"), str(job / "s.hip"), str(job)], str(job), "/w/p")
    assert len(entries) == 1


# -- skip rules -----------------------------------------------------------------


def test_skips_backup_pdgtemp_git_pycache_dirs(tmp_path):
    job = tmp_path / "job"
    (job / "keep").mkdir(parents=True)
    (job / "keep" / "good.exr").write_bytes(b"g")
    for junk_dir in ("backup", "pdgtemp", ".git", "__pycache__"):
        d = job / junk_dir
        d.mkdir()
        (d / "junk.exr").write_bytes(b"j")
    entries, _ = resolve_entries([str(job)], str(job), "/w/p")
    remotes = {e.remote for e in entries}
    assert remotes == {"/w/p/keep/good.exr"}


def test_skips_hip_backup_and_tilde_files(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    (job / "scene.hip").write_bytes(b"h")
    (job / "scene.hip.bak").write_bytes(b"b")
    (job / "scene.hiplc.bak").write_bytes(b"b")
    (job / "scene.hipnc.bak").write_bytes(b"b")
    (job / "scene.hip~").write_bytes(b"b")
    entries, _ = resolve_entries([str(job)], str(job), "/w/p")
    remotes = {e.remote for e in entries}
    assert remotes == {"/w/p/scene.hip"}


def test_does_not_descend_into_symlinked_directory(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    (job / "real.exr").write_bytes(b"r")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "hidden.exr").write_bytes(b"h")
    try:
        os.symlink(str(outside), str(job / "link_dir"), target_is_directory=True)
    except (OSError, NotImplementedError):
        return  # symlinks unsupported in this environment (e.g. no perms) -- skip
    entries, _ = resolve_entries([str(job)], str(job), "/w/p")
    remotes = {e.remote for e in entries}
    assert remotes == {"/w/p/real.exr"}


def test_follows_file_symlink(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    real = tmp_path / "real.exr"
    real.write_bytes(b"r" * 5)
    try:
        os.symlink(str(real), str(job / "link.exr"))
    except (OSError, NotImplementedError):
        return
    entries, _ = resolve_entries([str(job / "link.exr")], str(job), "/w/p")
    assert len(entries) == 1
    assert entries[0].remote == "/w/p/link.exr"
    assert entries[0].size == 5


def test_top_level_skip_dir_passed_directly_yields_nothing(tmp_path):
    job = tmp_path / "job"
    backup = job / "backup"
    backup.mkdir(parents=True)
    (backup / "junk.exr").write_bytes(b"j")
    entries, _ = resolve_entries([str(backup)], str(job), "/w/p")
    assert entries == []


def test_symlinked_job_dir_resolves_before_prefix_check(tmp_path):
    # job_dir passed as a symlink; the file is referenced via its already
    # -resolved real path (e.g. as if hou.text.expandString had expanded
    # $JOB through the symlink already). A plain literal prefix check would
    # miss this and misclassify the file as external.
    real_job = tmp_path / "real_job"
    (real_job / "tex").mkdir(parents=True)
    (real_job / "tex" / "a.rat").write_bytes(b"x")
    job_link = tmp_path / "job_link"
    try:
        os.symlink(str(real_job), str(job_link), target_is_directory=True)
    except (OSError, NotImplementedError):
        return
    entries, pmap = resolve_entries([str(real_job / "tex" / "a.rat")], str(job_link), "/w/p")
    assert len(entries) == 1
    assert entries[0].remote == "/w/p/tex/a.rat"
    # FileEntry.local keeps the original (non-realpath'd) path as given
    assert entries[0].local == str(real_job / "tex" / "a.rat")
    assert pmap[str(job_link)] == "/w/p"


# -- _ext_suffix ------------------------------------------------------------------


def test_ext_suffix_posix_passthrough():
    assert _ext_suffix("/Users/may/lib/b.abc") == "/Users/may/lib/b.abc"


def test_ext_suffix_windows_drive_letter():
    assert _ext_suffix(r"C:\lib\b.abc") == "/C/lib/b.abc"


# -- _pathmap_key -----------------------------------------------------------------


def test_pathmap_key_avoids_root_and_drive_root_keys():
    # a bare "/" (or drive root) key would be applied by pdgcmd.py's
    # unanchored str.replace fixed-point loop to *every* path on the
    # worker -- so these must fall back to the file's own full path.
    assert _pathmap_key("/", "/b.abc") == "/b.abc"
    assert _pathmap_key("C:\\", r"C:\b.abc") == r"C:\b.abc"
    assert _pathmap_key("C:/", "C:/b.abc") == "C:/b.abc"
    assert _pathmap_key("C:", r"C:\b.abc") == r"C:\b.abc"
    # a normal (non-degenerate) parent directory is used as-is
    assert _pathmap_key("/Users/may/lib", "/Users/may/lib/b.abc") == "/Users/may/lib"
    assert _pathmap_key(r"C:\lib", r"C:\lib\b.abc") == r"C:\lib"


# -- pathmap_env ------------------------------------------------------------------


def test_pathmap_env_json_shape():
    raw = pathmap_env({"/Users/may/job": "/workspace/projects/may/shot", "/Users/may/lib": "/workspace/projects/may/shot/_ext/Users/may/lib"})
    data = json.loads(raw)
    assert set(data.keys()) == {"paths"}
    assert isinstance(data["paths"], list)
    entries = {k: v for e in data["paths"] for k, v in e.items()}
    assert entries["/Users/may/job"] == {"zone": "LINUX", "path": "/workspace/projects/may/shot"}
    assert entries["/Users/may/lib"] == {"zone": "LINUX", "path": "/workspace/projects/may/shot/_ext/Users/may/lib"}
    # every element of "paths" is a single-key dict, matching pdgcmd.py's
    # `for from_path, v in e.items()` parse loop
    assert all(len(e) == 1 for e in data["paths"])


# -- collect_refs (no real hou available -- exercised via a stub) -----------------


class _StubText:
    @staticmethod
    def expandString(s):
        return s.replace("$JOB", "/job").replace("$HIP", "/job/hip")


def test_collect_refs_import_guard(monkeypatch):
    refs = [
        (None, ""),
        (None, "op:/obj/geo1"),
        (None, "opdef:/Sop/mynode"),
        (None, "temp:/foo"),
        (None, "$JOB/tex/a.rat"),
        (None, "/job/render/frame.$F4.exr"),
        (None, "/job/other/plain.abc"),
    ]
    stub = SimpleNamespace(
        hipFile=SimpleNamespace(path=lambda: "/job/hip/scene.hip"),
        fileReferences=lambda *a, **k: refs,
        text=_StubText(),
        nodeType=lambda _name: None,
        getenv=lambda _name: None,
    )
    monkeypatch.setitem(sys.modules, "hou", stub)

    result = collect_refs(**_ANY_PATH)

    assert result[0] == "/job/hip/scene.hip"
    assert "/job/tex/a.rat" in result
    assert "/job/render" in result  # sequence token reduced to containing dir
    assert "/job/other/plain.abc" in result
    assert not any(r.startswith(("op:", "opdef:", "temp:")) for r in result)
    assert len(result) == 4  # hip + 3 valid refs (empty/op:/opdef:/temp: skipped)


# -- scope: whole scene vs. this cook's branch (field finding, 2026-09-05) --------
#
# On /Users/may/BS/airship/airship_v013.hip the upload node planned 794
# files / 9.97 GB against a scene that references 113 files / 1.32 GB.
# One parameter did it: `pdg_workingdir` on `/stage/lookdev_pdg/localscheduler`
# -- a *different* TOP network's scratch root, pointing at the project
# folder, which hou.fileReferences() hands over like any other file
# reference and resolve_entries then walks recursively (827 files, 11.54 GB:
# ten .hip versions, 467 finished EXRs and a 1.47 GB zip).
#
# Two independent defences, both tested here: a scheduler's working
# directory is not a dependency of anything (parm-name filter, applies in
# both scopes), and an upload only needs what the branch being cooked
# actually reads (scope filter).


class _StubTemplate:
    def __init__(self, string_type=None):
        self._string_type = string_type

    def stringType(self):
        if self._string_type is None:
            raise AttributeError("not a string parm")
        return self._string_type


class _StubParm:
    def __init__(self, name, node, value="", string_type=None):
        self._name = name
        self._node = node
        self._value = value
        self._template = _StubTemplate(string_type)

    def unexpandedString(self):
        return self._value

    def name(self):
        return self._name

    def node(self):
        return self._node

    def parmTemplate(self):
        return self._template

    def evalAsString(self):
        return self._value


class _StubNode:
    """Just enough hou.Node for the branch walk: path, parms, inputs, kids."""

    def __init__(self, path, scene, parms=None, ancestors=(), children=(), refs=(),
                 type_name="stub"):
        self._path = path
        self._scene = scene
        self._type_name = type_name
        self._parms = {}
        self._ancestors = list(ancestors)
        self._children = list(children)
        scene[path] = self
        for name, value in (parms or {}).items():
            self._parms[name] = _StubParm(name, self, value)
        for i, target in enumerate(refs):
            name = "ref{}".format(i)
            self._parms[name] = _StubParm(name, self, target, string_type="noderef")

    def path(self):
        return self._path

    def type(self):
        return SimpleNamespace(name=lambda: self._type_name)

    def parent(self):
        return self._scene.get(self._path.rsplit("/", 1)[0])

    def parm(self, name):
        return self._parms.get(name)

    def parms(self):
        return list(self._parms.values())

    def evalParm(self, name):
        p = self._parms.get(name)
        return p.evalAsString() if p else ""

    def node(self, path):
        return self._scene.get(path)

    def inputAncestors(self, **kwargs):
        return [self._scene[p] for p in self._ancestors]

    def children(self):
        return [self._scene[p] for p in self._children]

    def allSubChildren(self, **kwargs):
        out = []
        for p in self._children:
            out.append(self._scene[p])
            out.extend(self._scene[p].allSubChildren())
        return out


# The stub scenes below name paths that are not on this machine, so the
# existence check every reference now goes through is stubbed out too --
# these tests are about which references survive the filters, not about
# what is on disk (that is expand_reference's own tests, above).
_ANY_PATH = {"exists": lambda _p: True, "isdir": lambda _p: True}


class _StubNodeType:
    def __init__(self, nodes):
        self._nodes = list(nodes)

    def instances(self):
        return self._nodes


def _stub_hou(scene, refs, node_types=None):
    types = node_types or {}
    return SimpleNamespace(
        hipFile=SimpleNamespace(path=lambda: "/job/hip/scene.hip"),
        fileReferences=lambda *a, **k: refs,
        text=_StubText(),
        node=scene.get,
        nodeType=types.get,
        getenv=lambda name: {"HIP": "/job/hip", "JOB": "/job"}.get(name),
        stringParmType=SimpleNamespace(NodeReference="noderef", NodeReferenceList="noderefs"),
    )


def _airship_like_scene():
    """The shape of the field scene: two TOP networks, one shared project."""
    scene = {}
    stage = _StubNode("/stage", scene)

    # the branch this cook renders: a render ROP fed by one geometry LOP
    geo = _StubNode("/stage/geo_airship", scene)
    render = _StubNode("/stage/render_shot0012", scene, ancestors=["/stage/geo_airship"])

    # a lookdev network that has nothing to do with this cook
    lookdev_geo = _StubNode("/stage/lookdev_geo", scene)
    lookdev_rop = _StubNode("/stage/probe_render_nx", scene, ancestors=["/stage/lookdev_geo"])
    lookdev_sched = _StubNode("/stage/lookdev_pdg/localscheduler", scene)
    lookdev_pdg = _StubNode(
        "/stage/lookdev_pdg", scene, children=["/stage/lookdev_pdg/localscheduler"]
    )

    # this cook's TOP network: upload -> gate -> ropfetch -> render_shot0012
    fetch = _StubNode("/stage/shots_pdg/fetch_shot0012", scene, parms={"roppath": "/stage/render_shot0012"})
    upload = _StubNode("/stage/shots_pdg/upload", scene)
    shots_pdg = _StubNode(
        "/stage/shots_pdg", scene,
        children=["/stage/shots_pdg/fetch_shot0012", "/stage/shots_pdg/upload"],
    )
    stage._children = ["/stage/shots_pdg", "/stage/lookdev_pdg"]
    return scene, upload, geo, lookdev_geo, lookdev_sched


def test_a_schedulers_working_directory_is_not_a_dependency(monkeypatch):
    """The 11.54 GB: `pdg_workingdir` pointed at the whole project folder."""
    scene, upload, geo, lookdev_geo, sched = _airship_like_scene()
    refs = [
        (_StubParm("file", geo), "/job/tex/a.rat"),
        (_StubParm("pdg_workingdir", sched), "/job"),
        (_StubParm("pdg_transferroot", sched), "/job"),
    ]
    monkeypatch.setitem(sys.modules, "hou", _stub_hou(scene, refs))

    result = collect_refs(**_ANY_PATH)  # whole scene -- the filter is not the scope

    assert "/job/tex/a.rat" in result
    assert "/job" not in result


def test_an_output_path_is_not_a_dependency(monkeypatch):
    """Where a render writes is not what the scene needs to read: a
    `$F`-carrying outputimage reduces to its containing directory, and that
    directory is the finished-frames folder (467 EXRs in the field case)."""
    scene, upload, geo, _lookdev, _sched = _airship_like_scene()
    refs = [
        (_StubParm("file", geo), "/job/tex/a.rat"),
        (_StubParm("outputimage", geo), "/job/render/shot0012/beauty.$F4.exr"),
        (_StubParm("savetodirectory_directory", geo), "/job/usd"),
        (_StubParm("taskgraphfile", geo), "/job/pdg/graph.py"),
    ]
    monkeypatch.setitem(sys.modules, "hou", _stub_hou(scene, refs))

    result = collect_refs(**_ANY_PATH)

    assert result == ["/job/hip/scene.hip", "/job/tex/a.rat"]


def test_branch_scope_keeps_only_what_this_cook_reads(monkeypatch):
    scene, upload, geo, lookdev_geo, _sched = _airship_like_scene()
    refs = [
        (_StubParm("file", geo), "/job/tex/airship.rat"),
        (_StubParm("file", lookdev_geo), "/job/tex/probe.rat"),
        (None, "/prefs/otls/runpodfarm_upload.hda"),
    ]
    monkeypatch.setitem(sys.modules, "hou", _stub_hou(scene, refs))

    scoped = collect_refs(scope="branch", node=upload, **_ANY_PATH)
    whole = collect_refs(scope="scene", node=upload, **_ANY_PATH)

    assert "/job/tex/airship.rat" in scoped
    assert "/job/tex/probe.rat" not in scoped, "another network's lookdev is not this cook"
    # a reference no parm owns cannot be attributed to a branch, so it stays
    assert "/prefs/otls/runpodfarm_upload.hda" in scoped
    assert "/job/tex/probe.rat" in whole


def test_branch_scope_follows_node_references_not_just_inputs(monkeypatch):
    """A LOP reaches a SOP through a node-reference parm, not an input
    wire -- miss those and the branch loses real geometry."""
    scene, upload, geo, _lookdev, _sched = _airship_like_scene()
    sop = _StubNode("/obj/geo1/cache", scene)
    geo._parms["soppath"] = _StubParm("soppath", geo, "/obj/geo1/cache", string_type="noderef")
    refs = [(_StubParm("file", sop), "/job/geo/airship.bgeo.sc")]
    monkeypatch.setitem(sys.modules, "hou", _stub_hou(scene, refs))

    assert "/job/geo/airship.bgeo.sc" in collect_refs(scope="branch", node=upload, **_ANY_PATH)


def test_branch_scope_falls_back_to_the_whole_scene_when_it_finds_no_rop(monkeypatch):
    """Nothing to narrow to means upload everything, loudly -- never
    upload nothing."""
    scene = {}
    stage = _StubNode("/stage", scene)
    geo = _StubNode("/stage/geo", scene)
    upload = _StubNode("/stage/empty_pdg/upload", scene)
    _StubNode("/stage/empty_pdg", scene, children=["/stage/empty_pdg/upload"])
    refs = [(_StubParm("file", geo), "/job/tex/a.rat")]
    monkeypatch.setitem(sys.modules, "hou", _stub_hou(scene, refs))
    said = []

    result = collect_refs(scope="branch", node=upload, log=said.append, **_ANY_PATH)

    assert "/job/tex/a.rat" in result
    assert any("whole scene" in m for m in said), said


# -- plan_refs: what the confirmation window shows -------------------------------


def test_plan_refs_weighs_a_directory_as_one_row(tmp_path):
    """The 1.47 GB zip must be the first line of the window, not one of 827."""
    d = tmp_path / "export"
    (d / "sub").mkdir(parents=True)
    (d / "big.zip").write_bytes(b"x" * 100)
    (d / "sub" / "small.abc").write_bytes(b"y" * 10)
    single = tmp_path / "tex.rat"
    single.write_bytes(b"z" * 5)

    rows, missing = deps.plan_refs([str(d), str(single), str(tmp_path / "gone.exr")])

    assert [r.path for r in rows] == [str(d), str(single)]
    assert rows[0].kind == "dir" and rows[0].files == 2 and rows[0].bytes == 110
    assert rows[1].kind == "file" and rows[1].files == 1 and rows[1].bytes == 5
    assert missing == [str(tmp_path / "gone.exr")]


def test_plan_refs_dedups_and_honours_the_skip_rules(tmp_path):
    job = tmp_path / "job"
    (job / "backup").mkdir(parents=True)
    (job / "backup" / "old.hip").write_bytes(b"o")
    (job / "keep.exr").write_bytes(b"k")
    (job / "keep.exr~").write_bytes(b"b")

    rows, _ = deps.plan_refs([str(job), str(job), str(job / "backup"), str(job / "keep.exr~")])

    assert len(rows) == 1
    assert rows[0].path == str(job) and rows[0].files == 1  # backup/ and ~ pruned


# -- resolving one reference to real paths (field measurement, 2026-09-05) -------
#
# On airship_v013.hip, of 115 references: 38 resolve ONLY through
# hou.text.expandString, 0 only through parm.evalAsString(), 26 through both
# identically, 0 through both differently. The 38 are FBX: evaluating the
# parameter returns "/Users/may/Downloads/airship_v06.fbx#Airship_...,convertoff",
# an address INTO the file, which does not exist on disk. SideFX's own advice
# in the displayFileDependencyDialog docs -- "evaluate the parameter instead of
# calling hou.expandString" -- would have dropped all 38. The rule is to try
# both and keep what exists.


class _EvalParm:
    def __init__(self, value):
        self._value = value

    def evalAsString(self):
        return self._value


def _expand_env(text):
    return text.replace("$HIP", "/job").replace("$JOB", "/job")


def test_an_fbx_fragment_resolves_through_the_pattern(tmp_path):
    fbx = tmp_path / "airship.fbx"
    fbx.write_bytes(b"f")
    parm = _EvalParm(str(fbx) + "#Airship_fullBindings,convertoff")

    got = deps.expand_reference(parm, str(fbx), expand=lambda s: s)

    assert got == [str(fbx)]


def test_a_parameter_that_only_evaluates_is_not_lost(tmp_path):
    """The mirror case: $OS, channel references and other things only the
    parameter knows how to expand."""
    real = tmp_path / "geo.bgeo"
    real.write_bytes(b"g")

    got = deps.expand_reference(_EvalParm(str(real)), "$HIP/`chs('x')`.bgeo", expand=_expand_env)

    assert got == [str(real)]


def test_two_different_real_paths_are_both_kept(tmp_path):
    a = tmp_path / "a.rat"
    a.write_bytes(b"a")
    b = tmp_path / "b.rat"
    b.write_bytes(b"b")

    got = deps.expand_reference(_EvalParm(str(a)), str(b), expand=lambda s: s)

    assert sorted(got) == sorted([str(a), str(b)])


def test_the_same_path_twice_is_one_path(tmp_path):
    a = tmp_path / "a.rat"
    a.write_bytes(b"a")

    assert deps.expand_reference(_EvalParm(str(a)), str(a), expand=lambda s: s) == [str(a)]


def test_a_sequence_becomes_its_directory_only_if_that_exists(tmp_path):
    seq = tmp_path / "cache"
    seq.mkdir()
    (seq / "c.0001.bgeo").write_bytes(b"c")

    assert deps.expand_reference(None, str(seq / "c.$F4.bgeo"), expand=lambda s: s) == [str(seq)]
    assert deps.expand_reference(None, str(tmp_path / "gone" / "c.$F4.bgeo"), expand=lambda s: s) == []


def test_a_reference_that_resolves_to_nothing_yields_nothing(tmp_path):
    assert deps.expand_reference(None, str(tmp_path / "missing.exr"), expand=lambda s: s) == []


# -- outputs are shown, not hidden ----------------------------------------------


def test_an_output_reference_is_offered_unchecked_rather_than_dropped(monkeypatch):
    """Houdini's own dialog cannot show these (its rows are (Parm, pattern)
    pairs it picks itself), so our window has to -- and it can only show
    what the scan hands over separately instead of swallowing."""
    scene, upload, geo, _lookdev, sched = _airship_like_scene()
    refs = [
        (_StubParm("file", geo), "$JOB/tex/a.rat"),
        (_StubParm("pdg_workingdir", sched), "$JOB"),
    ]
    monkeypatch.setitem(sys.modules, "hou", _stub_hou(scene, refs))

    got = deps.scan_refs(**_ANY_PATH)

    assert got.paths == ["/job/hip/scene.hip", "/job/tex/a.rat"]
    assert got.output_paths == ["/job"], "kept, but on the other list"


def test_a_udim_texture_on_a_parameter_is_globbed_not_reduced_to_its_folder(tmp_path):
    """The field case that made this a defect rather than a nicety: an
    mtlximage parameter holding tex_mip/Balon_Base_color_<UDIM>.exr. The
    literal path never exists, so an existence check alone drops it -- and
    reducing to the containing directory (what this did before) uploaded
    all 72 files / 995 MB of that folder to get six tiles."""
    tex = tmp_path / "tex_mip"
    tex.mkdir()
    for tile in ("1011", "1012"):
        (tex / "Balon_Base_color_{}.exr".format(tile)).write_bytes(b"x")
    (tex / "unrelated_map.exr").write_bytes(b"y")

    got = deps.expand_reference(None, str(tex / "Balon_Base_color_<UDIM>.exr"),
                                expand=lambda s: s)

    assert got == [str(tex / "Balon_Base_color_1011.exr"),
                   str(tex / "Balon_Base_color_1012.exr")]
    assert str(tex) not in got, "the folder is not the answer, the tiles are"


def test_a_frame_sequence_still_reduces_to_its_directory(tmp_path):
    """Different question, different answer: the frames on disk now are not
    the frames the farm will read, so the directory is the honest superset."""
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "sim.0001.bgeo").write_bytes(b"c")

    assert deps.expand_reference(None, str(cache / "sim.$F4.bgeo"), expand=lambda s: s) == [str(cache)]


# -- what Houdini does not report, and what we must not report to ourselves ------
#
# Conductor (ciohoudini/assets.py) hit both of these in production and
# solved them the same way. Their comment is the finding in one line:
# "Parms that don't have a string type of 'File' won't be returned by
# hou.fileReferences()".


def test_a_reference_lop_path_is_scanned_although_houdini_hides_it(monkeypatch, tmp_path):
    """filepath1 on a Reference LOP is stringParmType.Regular, so it is
    absent from hou.fileReferences() -- and it is the .usdc the render is
    built from. No flag fixes that: the table of node type -> parm is the
    fix."""
    usd = tmp_path / "airship.usdc"
    usd.write_bytes(b"u")
    scene = {}
    ref_node = _StubNode("/stage/airship_ref", scene, type_name="reference::2.0")
    ref_node._parms["filepath1"] = _StubParm("filepath1", ref_node, str(usd))
    stub = _stub_hou(scene, [], node_types={"Lop/reference::2.0": _StubNodeType([ref_node])})
    monkeypatch.setitem(sys.modules, "hou", stub)

    assert str(usd) in deps.scan_refs().paths


def test_every_instance_of_a_multiparm_path_is_followed(monkeypatch, tmp_path):
    """A Reference LOP with three references has filepath1..3."""
    scene = {}
    node = _StubNode("/stage/refs", scene, type_name="reference::2.0")
    made = []
    for i in (1, 2, 3):
        path = tmp_path / "layer{}.usdc".format(i)
        path.write_bytes(b"u")
        made.append(str(path))
        node._parms["filepath{}".format(i)] = _StubParm("filepath{}".format(i), node, str(path))
    stub = _stub_hou(scene, [], node_types={"Lop/reference::2.0": _StubNodeType([node])})
    monkeypatch.setitem(sys.modules, "hou", stub)

    assert set(made).issubset(set(deps.scan_refs().paths))


def test_we_do_not_scan_our_own_nodes(monkeypatch):
    """The upload asset contains its own localscheduler, whose
    pdg_workingdir is $HIP. Scan ourselves and the whole project folder
    turns up as a row the artist never put there."""
    scene = {}
    stage = _StubNode("/stage", scene)
    ours = _StubNode("/stage/shots_pdg/upload", scene, type_name="runpodfarmupload")
    inner = _StubNode("/stage/shots_pdg/upload/localscheduler", scene, type_name="localscheduler")
    refs = [(_StubParm("pdg_workingdir", inner), "$JOB")]
    monkeypatch.setitem(sys.modules, "hou", _stub_hou(scene, refs))
    said = []

    got = deps.scan_refs(log=said.append, **_ANY_PATH)

    assert got.paths == ["/job/hip/scene.hip"]
    assert got.output_paths == [], "not even as an unchecked row -- it is not the scene's"
    assert any("own nodes" in m for m in said), said


def test_the_exclude_pattern_drops_matching_paths(monkeypatch, tmp_path):
    scene, upload, geo, _lookdev, _sched = _airship_like_scene()
    refs = [
        (_StubParm("file", geo), "/job/tex/a.rat"),
        (_StubParm("file", geo), "/job/backup/old.hip"),
    ]
    monkeypatch.setitem(sys.modules, "hou", _stub_hou(scene, refs))

    got = deps.scan_refs(exclude_pattern="*/backup/*", **_ANY_PATH)

    assert "/job/tex/a.rat" in got.paths
    assert "/job/backup/old.hip" not in got.paths


def test_a_broken_exclude_or_asset_pattern_does_not_break_the_cook(monkeypatch):
    scene, upload, geo, _lookdev, _sched = _airship_like_scene()
    refs = [(_StubParm("file", geo), "/job/tex/a.rat")]
    monkeypatch.setitem(sys.modules, "hou", _stub_hou(scene, refs))
    said = []

    got = deps.scan_refs(asset_regex="(unclosed", log=said.append, **_ANY_PATH)

    assert "/job/tex/a.rat" in got.paths
    assert any("does not compile" in m for m in said), said


def test_a_frame_number_globs_the_whole_sequence(tmp_path):
    """Houdini expands $F4 to the CURRENT FRAME before anything here sees
    it -- sim.$F4.bgeo arrives as sim.0001.bgeo. Treated literally, that
    uploads one frame of a cache and the farm renders the rest of the shot
    against nothing."""
    cache = tmp_path / "cache"
    cache.mkdir()
    for frame in ("0001", "0002", "0003"):
        (cache / "sim.{}.bgeo".format(frame)).write_bytes(b"c")
    (cache / "notes.txt").write_bytes(b"n")

    got = deps.expand_reference(None, str(cache / "sim.0001.bgeo"), expand=lambda s: s)

    assert got == [str(cache / "sim.{}.bgeo".format(f)) for f in ("0001", "0002", "0003")]
    assert str(cache / "notes.txt") not in got
