"""The checked-in expanded HDAs must match the builders that generate them.

House rule since the first HDA landed: ``hda/*.hda/`` is the git-tracked
expansion (``hotl -t``) of what ``scripts/build_runpodfarm_*_hda.py``
produces, and the two are edited together. Nothing enforced it, and
finding 2 of the final whole-branch review is what that cost: a
placeholder auto-grow block from before Task 12 stayed baked into
``runpodfarm_upload.hda`` long after the real check landed in
``rpfarm.packages.maybe_grow_volume``. It ran first, at ``onGenerate``,
with its own coarser formula -- a 45 GB upload onto a 50 GB volume grew
it to 100 GB where the real check grows it to 60 GB. RunPod volumes never
shrink and bill on allocated size, so that was money you could not get
back.

These tests read only tracked files -- no Houdini, no hython, no network.
"""

import ast
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent

# builder script -> the asset directory it generates
BUILDERS = {
    "build_runpodfarm_upload_hda.py": "runpodfarm_upload.hda",
    "build_runpodfarm_download_hda.py": "runpodfarm_download.hda",
    "build_runpodfarm_stats_hda.py": "runpodfarm_stats.hda",
}

# Which module-level string constants of a builder end up inside the asset.
# (Every builder's own docstring, path constants and so on do not.)
EMBEDDED = ("PYTHON_MODULE", "HELP_TEXT")


def _string_constants(builder):
    """Module-level ``NAME = "..."`` assignments in a builder, by name.

    Parsed rather than regexed so the value is the real runtime string --
    ``"\\n"`` in the builder source is a two-character ``\\n`` in the code
    that gets baked in, and comparing source text instead would never
    line up.
    """
    tree = ast.parse((REPO / "scripts" / builder).read_text())
    out = {}
    for node in tree.body:
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            out[target.id] = node.value.value
    return {k: v for k, v in out.items() if k.endswith("_CODE") or k in EMBEDDED}


def _asset_text(asset):
    """Every tracked text file of an expanded asset, concatenated."""
    out = []
    for path in sorted((REPO / "hda" / asset).rglob("*")):
        if not path.is_file():
            continue
        try:
            out.append(path.read_text())
        except (UnicodeDecodeError, OSError):
            continue  # .OPdummydefs and friends are binary
    return "\n".join(out)


def _mime_escape(code):
    """How ``hotl -t`` writes a node callback into ``Contents.mime``.

    Callbacks live inside a double-quoted field, so backslash and quote
    are escaped and nothing else changes. Section files (``PythonModule``,
    ``Help``) are written verbatim instead, which is why the assertions
    below accept either form.
    """
    return code.replace("\\", "\\\\").replace('"', '\\"')


@pytest.mark.parametrize("builder,asset", sorted(BUILDERS.items()))
def test_expanded_hda_matches_its_builder(builder, asset):
    shipped = _asset_text(asset)
    blocks = _string_constants(builder)
    assert blocks, f"{builder} embeds no code constants -- did the naming change?"
    for name, code in blocks.items():
        assert code in shipped or _mime_escape(code) in shipped, (
            f"{name} in scripts/{builder} is not what hda/{asset} ships -- the "
            f"checked-in asset is stale. Rebuild it:\n"
            f"  hython scripts/{builder} /tmp/out.hda\n"
            f"  rm -rf hda/{asset} && hotl -t hda/{asset} /tmp/out.hda"
        )


def test_upload_hda_does_not_resize_the_volume_itself():
    """Finding 2: the one volume auto-grow is packages.maybe_grow_volume.

    A second grower with its own formula is not a redundant check, it is a
    bigger bill: whichever runs first wins, and a RunPod volume never
    shrinks back.
    """
    shipped = _asset_text("runpodfarm_upload.hda")
    assert "resize_volume" not in shipped
    assert "TODO(Task 12)" not in shipped
    assert "maybe_grow_volume" in shipped, "the real check must be the one that is there"


def test_both_upload_paths_run_the_real_auto_grow_check():
    """In-process (debug) and out-of-process both go through the same check
    -- deleting the stale onGenerate block must not leave the in-process
    path with no guard at all."""
    builder = (REPO / "scripts" / "build_runpodfarm_upload_hda.py").read_text()
    runner = (REPO / "rpfarm" / "package_runner.py").read_text()
    assert "maybe_grow_volume" in _string_constants("build_runpodfarm_upload_hda.py")["COOKTASK_CODE"]
    assert "maybe_grow_volume" in runner
    assert "resize_volume" not in builder


def test_no_builder_resizes_the_volume_itself():
    for builder, asset in BUILDERS.items():
        assert "resize_volume" not in (REPO / "scripts" / builder).read_text(), builder
        assert "resize_volume" not in _asset_text(asset), asset


# -- the scheduler asset (hand-edited: no builder generates it) ---------------

SCHEDULER_MODULE = REPO / "hda" / "runpodfarm_scheduler.hda" / "Top_1runpodfarmscheduler" / "PythonModule"


def test_scheduler_python_module_is_valid_python():
    """It is edited by hand and never imported by the suite (it needs `hou`
    and `pdg`), so a syntax error would only show up in Houdini."""
    ast.parse(SCHEDULER_MODULE.read_text())


def test_scheduler_creates_pods_in_the_configured_datacenter():
    """Finding 5: a GPU pod must be created in the region its network volume
    lives in, and that region comes from the node's parm or the config --
    never from RunPodAPI's own fallback."""
    src = SCHEDULER_MODULE.read_text()
    assert "datacenter=self._datacenterId()" in src
    assert 'self["rpfarm_datacenter"].evaluateString() or self._cfg.datacenter' in src


SCHEDULER_DIALOG = REPO / "hda" / "runpodfarm_scheduler.hda" / "Top_1runpodfarmscheduler" / "DialogScript"


def test_datacenter_parm_defaults_to_the_config():
    """Datacenter must never carry a hardcoded region. It used to default to
    a literal EU-RO-1, which is how a US-KS-2 volume ends up with EU-RO-1
    pods that cannot mount it; Task 17 replaced that with an expression
    reading the config, so the field now SHOWS the region it will use."""
    dialog = SCHEDULER_DIALOG.read_text()
    assert (
        '            name    "rpfarm_datacenter"\n'
        '            label   "Datacenter"\n'
        '            type    string\n'
        '            default { [ "hou.phm().cfg_default(\\"datacenter\\")" python ] }\n'
    ) in dialog
    assert '"EU-RO-1"   "EU Romania"' in dialog, "still offered as a choice"
    assert 'default { "EU-RO-1" }' not in dialog, "but never as the default"


# -- the family look and the config-backed defaults (Task 17) ----------------

ASSETS = {
    "runpodfarm_scheduler": "Top_1runpodfarmscheduler",
    "runpodfarm_upload": "Top_1runpodfarmupload",
    "runpodfarm_download": "Top_1runpodfarmdownload",
    "runpodfarm_stats": "Top_1runpodfarmstats",
}


@pytest.mark.parametrize("asset,subdir", sorted(ASSETS.items()))
def test_every_asset_ships_its_own_icon(asset, subdir):
    """The icon rides inside the asset as an IconSVG section, so there is
    nothing to install and nothing to lose when the .hda is copied. The
    section has to be byte-identical to the source SVG, or the file in
    hda/icons/ is decoration and the asset is the real (drifted) icon."""
    source = (REPO / "hda" / "icons" / f"{asset}.svg").read_text()
    shipped = (REPO / "hda" / f"{asset}.hda" / subdir / "IconSVG").read_text()
    assert shipped == source
    index = (REPO / "hda" / f"{asset}.hda" / "INDEX__SECTION").read_text()
    optype = subdir.split("_1", 1)[1]
    assert f"Icon:         opdef:/Top/{optype}?IconSVG" in index


@pytest.mark.parametrize("asset,subdir", sorted(ASSETS.items()))
def test_every_asset_creates_itself_violet_and_the_family_shape(asset, subdir):
    """Colour comes from OnCreated (an HDA definition carries none) and the
    shape from CreateScript's opuserdata (so it survives even when an event
    script does not run). Both have to be there for all four, or the family
    is a family of three."""
    node_dir = REPO / "hda" / f"{asset}.hda" / subdir
    assert "node.setColor(hou.Color((0.549, 0.361, 0.882)))" in (node_dir / "OnCreated").read_text()
    assert "opuserdata -n 'nodeshape' -v 'rpfarm' $arg1" in (node_dir / "CreateScript").read_text()


def test_the_node_shape_is_shipped_and_installable():
    """Unlike the icons, a node shape cannot live inside an asset: Houdini
    resolves it by name out of config/NodeShapes on HOUDINI_PATH. So it has
    to be a real file that `rpfarm setup` installs."""
    import json

    shape = json.loads((REPO / "hda" / "nodeshapes" / "rpfarm.json").read_text())
    assert shape["name"] == "rpfarm"
    assert len(shape["outline"]) == 8, "a chamfered rectangle, not one of the stock shapes"
    assert sorted(shape["flags"]) == ["0", "1", "2", "3"], "all four flag regions or the flags do not draw"
    assert shape["inputs"] and shape["outputs"]

    from rpfarm import houdini_local

    assert houdini_local.node_shape_source().is_file()


def test_the_api_key_is_the_one_field_that_never_shows_its_value():
    """Substituting the key would put a secret on screen and, the moment a
    user "overrides" the field, bake it in cleartext into the .hip that
    travels to the farm and into backups."""
    dialog = SCHEDULER_DIALOG.read_text()
    block = dialog[dialog.index('name    "rpfarm_apikey"'):]
    block = block[:block.index("        }")]
    assert 'default { "" }' in block
    assert "cfg_default" not in block and "python ]" not in block
    # ...and the masked indicator is there instead.
    assert 'default { [ "hou.phm().apiKeyStatus()" python ] }' in dialog


@pytest.mark.parametrize("parm,field", [
    ("rpfarm_templateid", "template_id"),
    ("rpfarm_networkvolumeid", "volume_id"),
    ("rpfarm_datacenter", "datacenter"),
    ("rpfarm_gpulist", "gpu_priority"),
])
def test_the_farm_identity_parms_show_what_they_will_use(parm, field):
    dialog = SCHEDULER_DIALOG.read_text()
    assert f'default {{ [ "hou.phm().cfg_default(\\"{field}\\")" python ] }}' in dialog, parm


def test_the_scheduler_module_defines_what_those_expressions_call():
    """A default expression naming a function the PythonModule does not have
    is an empty field on every node, silently."""
    src = SCHEDULER_MODULE.read_text()
    names = {n.name for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef)}
    assert {"cfg_default", "project_default", "apiKeyStatus"} <= names


def test_the_first_tab_is_the_one_an_artist_uses():
    """33 parameters with the farm's identity first is not a first page."""
    dialog = SCHEDULER_DIALOG.read_text()
    first = dialog.index('label   "Cook"')
    farm = dialog.index('label   "Farm"')
    advanced = dialog.index('label   "Advanced"')
    assert first < farm < advanced
    page1 = dialog[first:farm]
    for name in ("rpfarm_minpods", "rpfarm_maxpods", "rpfarm_slots", "rpfarm_idletimeout",
                 "rpfarm_maxcost", "rpfarm_downloadoutputs"):
        assert name in page1, name
    tail = dialog[advanced:]
    for name in ("rpfarm_pretaskcmd", "rpfarm_posttaskcmd", "rpfarm_envmulti",
                 "rpfarm_houdinimaxthreads", "rpfarm_remoteworkingdir", "rpfarm_verbose"):
        assert name in tail, name


def test_no_parameter_was_lost_in_the_reshuffle():
    """The tab pass moved parms; it must not have dropped any."""
    import re

    dialog = SCHEDULER_DIALOG.read_text()
    names = set(re.findall(r'^\s+name\s+"([^"]+)"$', dialog, re.M))
    expected = {
        "rpfarm_apikey", "rpfarm_project", "rpfarm_gpulist", "rpfarm_templateid",
        "rpfarm_networkvolumeid", "rpfarm_datacenter", "rpfarm_minpods", "rpfarm_maxpods",
        "rpfarm_slots", "rpfarm_idletimeout", "rpfarm_syncidle", "rpfarm_maxcost",
        "rpfarm_minbalance", "rpfarm_downloadoutputs", "rpfarm_verbose", "rpfarm_killall",
        "rpfarm_syncledger", "rpfarm_status_text", "rpfarm_volume_refresh",
        "rpfarm_volume_target", "rpfarm_volume_delete", "rpfarm_volume_growgb",
        "rpfarm_volume_grow", "rpfarm_volume_text", "pdg_workingdir",
        "rpfarm_overrideremoteworkingdir", "rpfarm_remoteworkingdir",
        "pdg_workitemdatasource", "pdg_deletetempdir", "pdg_compressworkitemdata",
        "pdg_validateoutputs", "pdg_checkexpectedoutputs", "pdg_mapmode", "pdg_usemapzone",
        "pdg_mapzone", "submitjob", "usesubmitjobnode", "submitjobnode", "submitjobfile",
        "mqusage", "mqaddr", "usetaskcallbackport", "taskcallbackport", "usemqrelayport",
        "mqrelayport", "pdg_rpcignoreerrors", "pdg_rpcmaxerrors", "pdg_rpctimeout",
        "pdg_rpcretries", "pdg_rpcbackoff", "pdg_rpcbatch", "pdg_rpcrelease",
        "rpfarm_pretaskcmd", "rpfarm_posttaskcmd", "rpfarm_inheritlocalenv",
        "rpfarm_envunset", "rpfarm_envmulti", "rpfarm_envname#", "rpfarm_envvalue#",
        "rpfarm_usehoudinimaxthreads", "rpfarm_houdinimaxthreads",
    }
    assert expected <= names, sorted(expected - names)


def test_download_generate_never_warns_through_the_houdini_node():
    """`hou.Node.addWarning` from inside PDG generate() is not a warning.

    Houdini refuses to badge a node other than the one being cooked and raises
    hou.OperationFailed("Cannot set error badges on other nodes"), so the call
    aborts generate() and the node produces ZERO work items -- a strictly worse
    outcome than the condition being reported. Warnings go through `self`, the
    pdg.Node, which owns the TOP-side badge. Guarded here because the failure
    only appears on an error path that a green cook never touches.
    """
    generate = _download_generate_source()

    assert "node.addWarning(" not in generate
    assert "node.addError(" not in generate
    assert "_warn(" in generate
    assert "self.addWarning(message)" in generate


def test_download_generate_does_not_trust_stats_exit_code():
    """`stat` exits non-zero if any path is missing but still prints the rest."""
    generate = _download_generate_source()

    assert "parse_stat_sizes" in generate
    assert 'result.get("exit_code") == 0' not in generate


def _download_generate_source():
    """The generate script as it is checked into the expanded HDA."""
    path = (pathlib.Path(__file__).resolve().parent.parent / "hda"
            / "runpodfarm_download.hda" / "Top_1runpodfarmdownload"
            / "Contents.dir" / "Contents.mime")
    return path.read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# the stale-sys.modules guard is embedded, identically, in every asset that
# loads on scene open
# ---------------------------------------------------------------------------

_GUARDED_ASSETS = [
    ("runpodfarm_scheduler.hda", "Top_1runpodfarmscheduler"),
    ("runpodfarm_stats.hda", "Top_1runpodfarmstats"),
]


def _asset_python_module(asset, typedir):
    return (pathlib.Path(__file__).resolve().parent.parent / "hda" / asset
            / typedir / "PythonModule").read_text(encoding="utf-8")


@pytest.mark.parametrize("asset,typedir", _GUARDED_ASSETS)
def test_the_guard_is_embedded_verbatim(asset, typedir):
    """One source of truth, embedded at build time.

    It cannot live in `rpfarm` and be imported instead: an import is exactly
    what returns the cached module the guard exists to notice. So it is
    duplicated by construction, and this is what stops the copies drifting.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "hda_guard",
        pathlib.Path(__file__).resolve().parent.parent / "scripts" / "hda_guard.py")
    hda_guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hda_guard)

    src = _asset_python_module(asset, typedir)
    assert hda_guard.GUARD_SOURCE in src, (
        f"{asset}'s guard has drifted from scripts/hda_guard.py")


@pytest.mark.parametrize("asset,typedir", _GUARDED_ASSETS)
def test_the_guard_runs_before_any_rpfarm_symbol_is_imported(asset, typedir):
    """A guard after the import it protects is decoration."""
    src = _asset_python_module(asset, typedir)

    guard = src.index("_stale = _stale_module_message(")
    first_from_rpfarm = src.index("\nfrom rpfarm import ")
    assert guard < first_from_rpfarm


@pytest.mark.parametrize("asset,typedir", _GUARDED_ASSETS)
def test_the_embedded_guard_is_valid_python(asset, typedir):
    """The first attempt shipped a broken copy: the guard was injected into a
    NON-raw ''' literal in the builder, so its \\n escapes became real newlines
    and the emitted module had unterminated strings. Parsing the emitted text
    is what catches that class of mistake."""
    ast.parse(_asset_python_module(asset, typedir))


def test_prefirstcreate_explains_a_failed_module_instead_of_an_attributeerror():
    """When the PythonModule does not import, its class never exists, and
    registerScheduler re-raised that as an AttributeError about a name the
    artist has never heard of -- right after the message that did explain it."""
    src = (pathlib.Path(__file__).resolve().parent.parent / "hda"
           / "runpodfarm_scheduler.hda" / "Top_1runpodfarmscheduler"
           / "PreFirstCreate").read_text(encoding="utf-8")

    assert "except AttributeError:" in src
    assert "ПЕРЕЗАПУСТИТЕ HOUDINI" in src
    assert "from None" in src          # or the AttributeError comes back as context
    ast.parse(src)


def test_download_node_warns_when_the_scheduler_already_downloads_outputs():
    """Both mechanisms on fetches every output twice.

    The scheduler's "Download Outputs" pulls each item's outputs the moment it
    succeeds; this node in Outputs mode pulls the same files again at the end
    of the cook. The demo scene shipped with both on and an artist noticed the
    files arriving twice -- the second pass re-transferred every frame and
    re-acquired the sync pod to size them. Silence is the bug; the node has to
    say it.
    """
    generate = _download_generate_source()

    assert "_scheduler_downloads_outputs" in generate
    assert "rpfarm_downloadoutputs" in generate
    assert "a second time" in generate
    # and it is checked in the outputs branch, before anything is planned
    assert generate.index("_scheduler_downloads_outputs()") < generate.index(
        "Outputs mode with no upstream input")


def test_the_gpu_set_is_a_menu_not_a_string_to_type_from_memory():
    """`gpu_priority` was a comma-separated string whose semantics were "first
    of these that exists". The owner asked for a set -- "any of these" -- built
    from the live catalogue."""
    dialog = (pathlib.Path(__file__).resolve().parent.parent / "hda"
              / "runpodfarm_scheduler.hda" / "Top_1runpodfarmscheduler"
              / "DialogScript").read_text(encoding="utf-8")

    assert 'name    "rpfarm_gpuadd"' in dialog
    assert 'name    "rpfarm_gpuconsumer"' in dialog
    assert "hou.phm().gpuMenu(kwargs)" in dialog
    assert "hou.phm().onAddGpu(kwargs)" in dialog
    # A Houdini python menu script is an expression, not a function body:
    # a leading `return` is a SyntaxError and the menu silently comes back empty.
    assert "return hou.phm().gpuMenu" not in dialog


def test_the_scheduler_sorts_the_gpu_set_by_price_before_sending_it():
    """gpuTypePriority is custom, so RunPod walks our order -- which is what
    makes "any of these" mean "the cheapest of these that exists"."""
    src = (pathlib.Path(__file__).resolve().parent.parent / "hda"
           / "runpodfarm_scheduler.hda" / "Top_1runpodfarmscheduler"
           / "PythonModule").read_text(encoding="utf-8")

    assert "rpgpus.order_for_request(chosen, rows)" in src
    # and a catalogue it cannot fetch must not stop the cook
    assert "using it as given" in src
