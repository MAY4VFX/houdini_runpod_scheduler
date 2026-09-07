import os
import platform
import subprocess

import pytest

from rpfarm import houdini_local as hl


def _make_hfs(tmp_path, version_dir="hfs22.0.393"):
    hfs = tmp_path / version_dir
    (hfs / "bin").mkdir(parents=True)
    (hfs / "toolkit" / "include" / "SYS").mkdir(parents=True)
    (hfs / "bin" / "hython").write_text("#!/bin/sh\n")
    (hfs / "bin" / "hython").chmod(0o755)
    (hfs / "bin" / "hotl").write_text("#!/bin/sh\n")
    (hfs / "bin" / "hotl").chmod(0o755)
    return hfs


def test_houdini_install_detects_version_from_header(tmp_path):
    hfs = _make_hfs(tmp_path)
    header = hfs / "toolkit" / "include" / "SYS" / "SYS_Version.h"
    header.write_text(
        "#define SYS_VERSION_MAJOR 22\n#define SYS_VERSION_MINOR 0\n#define SYS_VERSION_BUILD 393\n"
    )
    inst = hl.HoudiniInstall(hfs)
    assert inst.version == "22.0.393"
    assert inst.major_minor == "22.0"
    assert inst.hython == hfs / "bin" / "hython"
    assert inst.hotl == hfs / "bin" / "hotl"


def test_houdini_install_falls_back_to_dirname_version(tmp_path):
    hfs = _make_hfs(tmp_path, version_dir="hfs20.5.370")
    inst = hl.HoudiniInstall(hfs)
    assert inst.version == "20.5.370"


def test_hotl_missing_when_no_binary_next_to_hython(tmp_path):
    hfs = tmp_path / "hfs22.0.393"
    (hfs / "bin").mkdir(parents=True)
    (hfs / "bin" / "hython").write_text("x")
    inst = hl.HoudiniInstall(hfs)
    assert inst.hython is not None
    assert inst.hotl is None


def test_find_houdini_installations_scans_platform_globs(tmp_path, monkeypatch):
    if platform.system() != "Darwin":
        pytest.skip("glob patterns under test are macOS-specific")
    apps = tmp_path / "Applications" / "Houdini"
    hfs = apps / "Houdini22.0.368"
    (hfs / "bin").mkdir(parents=True)
    (hfs / "bin" / "hython").write_text("x")
    orig_glob_expand = hl._glob_expand
    monkeypatch.setattr(
        hl, "_glob_expand",
        lambda patterns: orig_glob_expand([str(tmp_path) + p for p in patterns]),
    )
    installs = hl.find_houdini_installations()
    assert any(i.hfs == hfs.resolve() for i in installs)


def test_write_rpfarm_root_env_is_idempotent(tmp_path):
    prefs = tmp_path / "prefs"
    inst = hl.HoudiniInstall.__new__(hl.HoudiniInstall)
    inst.user_pref_dir = prefs
    repo = tmp_path / "repo"

    path1 = hl.write_rpfarm_root_env(inst, root=repo)
    text1 = path1.read_text()
    assert str(repo) in text1
    assert text1.count("RPFARM_ROOT") == 1

    # Existing unrelated content is preserved; a second call replaces only
    # the marker+line pair, not the whole file.
    path1.write_text("SOME_OTHER_VAR = \"1\"\n" + text1)
    other_repo = tmp_path / "checkout"
    path2 = hl.write_rpfarm_root_env(inst, root=other_repo)
    text2 = path2.read_text()
    assert "SOME_OTHER_VAR" in text2
    assert str(other_repo) in text2
    assert str(repo) not in text2
    assert text2.count("RPFARM_ROOT") == 1


def test_build_and_install_hdas_reports_per_hda_status(tmp_path):
    root = tmp_path / "repo"
    for name in hl.HDA_NAMES:
        (root / "hda" / f"{name}.hda").mkdir(parents=True)

    hfs = _make_hfs(tmp_path / "hfs")
    inst = hl.HoudiniInstall(hfs)

    calls = []

    def fake_runner(cmd, check, capture_output, text):
        calls.append(cmd)
        # Simulate hotl -l writing the dest file.
        dest = cmd[-1]
        with open(dest, "wb") as f:
            f.write(b"fake-hda-contents")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    results = hl.build_and_install_hdas(inst, tmp_path / "otls_cache", root=root, runner=fake_runner)
    assert len(results) == len(hl.HDA_NAMES)
    assert all(r["ok"] for r in results)
    assert len(calls) == len(hl.HDA_NAMES)
    for name in hl.HDA_NAMES:
        installed = inst.user_pref_dir / "otls" / f"{name}.hda"
        assert installed.is_file()
        assert installed.read_bytes() == b"fake-hda-contents"


def test_build_and_install_hdas_missing_hotl_reports_error_not_raise(tmp_path):
    root = tmp_path / "repo"
    for name in hl.HDA_NAMES:
        (root / "hda" / f"{name}.hda").mkdir(parents=True)

    hfs = tmp_path / "hfs"
    (hfs / "bin").mkdir(parents=True)
    (hfs / "bin" / "hython").write_text("x")  # no hotl next to it
    inst = hl.HoudiniInstall(hfs)

    results = hl.build_and_install_hdas(inst, tmp_path / "otls_cache", root=root)
    assert all(not r["ok"] and "hotl" in r["error"] for r in results)


def test_build_and_install_hdas_missing_source_reports_error(tmp_path):
    root = tmp_path / "repo"  # no hda/ dir at all
    hfs = _make_hfs(tmp_path / "hfs")
    inst = hl.HoudiniInstall(hfs)

    results = hl.build_and_install_hdas(inst, tmp_path / "otls_cache", root=root, runner=lambda *a, **k: None)
    assert all(not r["ok"] and "not found" in r["error"] for r in results)


# -- the custom node shape (Task 17) -----------------------------------------


def test_install_node_shape_copies_it_where_houdini_looks(tmp_path):
    """Unlike an icon, a node shape cannot ride inside the .hda: Houdini
    resolves it by name out of config/NodeShapes on HOUDINI_PATH. Without
    this copy the four farm nodes silently draw as plain rectangles."""
    hfs = _make_hfs(tmp_path)
    inst = hl.HoudiniInstall(hfs)
    inst.user_pref_dir = tmp_path / "prefs"

    result = hl.install_node_shape(inst)

    assert result["ok"] and result["error"] is None
    target = tmp_path / "prefs" / "config" / "NodeShapes" / "rpfarm.json"
    assert target.exists()
    assert target.read_text() == hl.node_shape_source().read_text()
    assert hl.node_shape_target(inst) == target


def test_install_node_shape_is_rerunnable(tmp_path):
    hfs = _make_hfs(tmp_path)
    inst = hl.HoudiniInstall(hfs)
    inst.user_pref_dir = tmp_path / "prefs"
    assert hl.install_node_shape(inst)["ok"]
    assert hl.install_node_shape(inst)["ok"]


def test_install_node_shape_reports_a_missing_source_instead_of_raising(tmp_path):
    """Same contract as build_and_install_hdas: setup surfaces this per item
    rather than aborting the whole run over a cosmetic file."""
    hfs = _make_hfs(tmp_path)
    inst = hl.HoudiniInstall(hfs)
    inst.user_pref_dir = tmp_path / "prefs"

    result = hl.install_node_shape(inst, root=tmp_path / "not-a-checkout")

    assert result["ok"] is False
    assert "node shape not found" in result["error"]


# ---------------------------------------------------------------------------
# resolve_package_python
#
# The out-of-process package runner used to be launched with
# `shutil.which("python3") or "python3"`. A Houdini launched from the macOS
# Dock inherits a minimal PATH where that is Xcode's python3.9, which has no
# tomllib, so every upload item died on `import rpfarm.config`. Headless runs
# went through a shell with a modern python first on PATH and so never saw it.
# These tests pin the resolution order that replaced it, and the rule that
# every candidate is executed before it is trusted.
# ---------------------------------------------------------------------------


def _fake_run(versions):
    """A subprocess.run stand-in reporting `versions[exe]`, or failing."""

    class _Result:
        def __init__(self, returncode, stdout):
            self.returncode = returncode
            self.stdout = stdout

    def run(cmd, **_kwargs):
        version = versions.get(cmd[0])
        if version is None:
            return _Result(1, "")
        return _Result(0, "{} {}".format(*version))

    return run


@pytest.fixture(autouse=True)
def _clear_version_cache():
    hl._VERSION_CACHE.clear()
    yield
    hl._VERSION_CACHE.clear()


def _mac_hfs(tmp_path, py="3.13"):
    """The real macOS layout: Python.framework is a SIBLING of Houdini.framework."""
    root = tmp_path / "Houdini22.0.368" / "Frameworks"
    hfs = root / "Houdini.framework" / "Versions" / "22.0" / "Resources"
    hfs.mkdir(parents=True)
    bindir = root / "Python.framework" / "Versions" / py / "bin"
    bindir.mkdir(parents=True)
    exe = bindir / ("python" + py)
    exe.write_text("#!/bin/sh\n")
    return hfs, exe


def test_bundled_python_found_beside_houdini_framework_on_macos(tmp_path):
    hfs, exe = _mac_hfs(tmp_path)

    assert hl.houdini_bundled_python(str(hfs), (3, 13), platform_name="darwin") == str(exe)


def test_bundled_python_version_is_never_hardcoded(tmp_path):
    """The version is the install's, not a constant and not the caller's.

    Houdini 22.0 ships 3.13 and older ones 3.11/3.10, so a hardcoded tag would
    resolve nothing on half the installs. The caller's own sys.version_info is
    the right hint only when the caller *is* Houdini (the generate script); for
    the CLI or a test it is some other python entirely, and silently missing
    there would fall through to PATH -- the exact failure this function exists
    to prevent. So a missed hint falls back to what the install really has.
    """
    hfs, exe = _mac_hfs(tmp_path, py="3.11")

    assert hl.houdini_bundled_python(str(hfs), (3, 11), platform_name="darwin") == str(exe)
    assert hl.houdini_bundled_python(str(hfs), (3, 13), platform_name="darwin") == str(exe)
    assert hl.houdini_bundled_python(str(hfs), platform_name="darwin") == str(exe)


def test_bundled_python_picks_the_newest_when_an_install_has_several(tmp_path):
    hfs, exe313 = _mac_hfs(tmp_path, py="3.13")
    versions = hfs.parents[3] / "Python.framework" / "Versions"
    older = versions / "3.11" / "bin"
    older.mkdir(parents=True)
    (older / "python3.11").write_text("")

    assert hl.houdini_bundled_python(str(hfs), (3, 9), platform_name="darwin") == str(exe313)


def test_bundled_python_linux_and_windows_layouts(tmp_path):
    lin = tmp_path / "hfs"
    (lin / "python" / "bin").mkdir(parents=True)
    (lin / "python" / "bin" / "python3").write_text("")
    assert hl.houdini_bundled_python(str(lin), (3, 13), platform_name="linux") == str(
        lin / "python" / "bin" / "python3")

    win = tmp_path / "hfsw"
    (win / "python").mkdir(parents=True)
    (win / "python" / "python.exe").write_text("")
    assert hl.houdini_bundled_python(str(win), (3, 13), platform_name="win32") == str(
        win / "python" / "python.exe")


def test_bundled_python_is_none_when_the_path_does_not_exist(tmp_path):
    assert hl.houdini_bundled_python(str(tmp_path / "nope"), (3, 13), platform_name="darwin") is None
    assert hl.houdini_bundled_python("", (3, 13)) is None
    assert hl.houdini_bundled_python(None, (3, 13)) is None


def test_resolve_prefers_the_bundled_interpreter(tmp_path):
    hfs, exe = _mac_hfs(tmp_path)

    got, why = hl.resolve_package_python(
        hfs=str(hfs), version=(3, 13), platform_name="darwin",
        which=lambda n: "/usr/bin/python3",
        run=_fake_run({str(exe): (3, 13), "/usr/bin/python3": (3, 14)}))

    assert got == str(exe)
    assert "bundled" in why and "licence" in why


def test_a_bundled_path_that_is_too_old_is_rejected_not_trusted(tmp_path):
    """Existing at the right path under the right name is not proof."""
    hfs, exe = _mac_hfs(tmp_path)

    got, why = hl.resolve_package_python(
        hfs=str(hfs), version=(3, 13), platform_name="darwin",
        which=lambda n: "/opt/py/python3" if n == "python3" else None,
        run=_fake_run({str(exe): (3, 9), "/opt/py/python3": (3, 12)}))

    assert got == "/opt/py/python3"
    assert "PATH" in why


def test_a_python3_on_path_that_is_too_old_is_skipped_for_a_named_one(tmp_path):
    """The exact Dock case: which('python3') is Xcode's 3.9."""
    which = {"python3": "/usr/bin/python3", "python3.13": "/opt/homebrew/bin/python3.13"}

    got, why = hl.resolve_package_python(
        hfs=None, version=(3, 13), platform_name="darwin",
        which=which.get,
        run=_fake_run({"/usr/bin/python3": (3, 9),
                       "/opt/homebrew/bin/python3.13": (3, 13)}))

    assert got == "/opt/homebrew/bin/python3.13"
    assert "python3.13" in why


def test_named_pythons_are_tried_newest_first(tmp_path):
    which = {"python3.11": "/bin/python3.11", "python3.13": "/bin/python3.13"}

    got, _why = hl.resolve_package_python(
        hfs=None, version=(3, 13), platform_name="darwin", which=which.get,
        run=_fake_run({"/bin/python3.11": (3, 11), "/bin/python3.13": (3, 13)}))

    assert got == "/bin/python3.13"


def test_resolve_refuses_rather_than_returning_a_bare_python3(tmp_path):
    with pytest.raises(hl.NoUsablePythonError) as excinfo:
        hl.resolve_package_python(
            hfs=str(tmp_path / "no-hfs"), version=(3, 13), platform_name="darwin",
            which=lambda n: "/usr/bin/python3" if n == "python3" else None,
            run=_fake_run({"/usr/bin/python3": (3, 9)}), search_dirs=[])

    message = str(excinfo.value)
    assert "3.11" in message
    assert "/usr/bin/python3 -> 3.9" in message   # names what it tried, and why it failed
    assert "$HFS" in message


def test_python_version_runs_the_interpreter_and_caches_the_answer():
    calls = []

    def run(cmd, **_kwargs):
        calls.append(cmd[0])

        class R:
            returncode = 0
            stdout = "3 13"
        return R()

    assert hl.python_version("/x/python3", run=run) == (3, 13)
    assert hl.python_version("/x/python3", run=run) == (3, 13)
    assert calls == ["/x/python3"]


def test_python_version_is_none_for_an_interpreter_that_will_not_run():
    def run(cmd, **_kwargs):
        raise OSError("no such file")

    assert hl.python_version("/nope/python3", run=run) is None


def test_discover_picks_the_newest_and_ignores_pre_3_11(tmp_path):
    old = tmp_path / "usr" / "bin"
    new = tmp_path / "brew" / "bin"
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    for name in ("python3.9", "python3.11", "python3-config", "python3"):
        (old / name).write_text("")
    (new / "python3.14").write_text("")
    (new / "python3.12").write_text("")

    assert hl.discover_python_on_disk(search_dirs=[str(old), str(new)]) == str(new / "python3.14")


def test_discover_returns_none_when_nothing_is_new_enough(tmp_path):
    d = tmp_path / "bin"
    d.mkdir()
    (d / "python3.9").write_text("")

    assert hl.discover_python_on_disk(search_dirs=[str(d)]) is None
    assert hl.discover_python_on_disk(search_dirs=[str(tmp_path / "missing")]) is None


def test_the_resolved_interpreter_really_imports_tomllib_on_this_machine():
    """The whole point, against this machine's real Houdini.

    A resolver that returns something without tomllib has not fixed the bug it
    exists for, so this runs the thing it chose.
    """
    installs = hl.find_houdini_installations()
    if not installs:
        pytest.skip("no local Houdini installation")
    hfs = str(getattr(installs[0], "hfs", "") or "")
    if not hfs:
        pytest.skip("install has no HFS path to derive from")
    try:
        exe, why = hl.resolve_package_python(hfs=hfs)
    except hl.NoUsablePythonError:
        pytest.skip("no usable python on this machine")
    out = subprocess.run(
        [exe, "-c", "import tomllib, sys; print(sys.version_info[0], sys.version_info[1])"],
        capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert tuple(int(x) for x in out.stdout.split()) >= hl.PACKAGE_PYTHON_MIN
    assert "bundled" in why, "expected Houdini's own python, got: " + why


# -- the TAB-menu tool -------------------------------------------------------


def test_the_shelf_document_registers_a_top_only_tab_tool():
    import xml.etree.ElementTree as ET

    root = ET.fromstring(hl.shelf_tool_source())
    tool = root.find("tool")
    assert tool.get("name") == hl.SHELF_TOOL_NAME
    assert tool.get("label") == hl.SHELF_TOOL_LABEL
    # Restricted to TOP networks, or it clutters every other network editor.
    assert tool.find("toolMenuContext").get("name") == "network"
    assert tool.find("toolMenuContext/contextNetType").text == "TOP"
    # A submenu is what puts it in the TAB menu rather than only on a shelf.
    assert tool.find("toolSubmenu").text == hl.SHELF_TOOL_SUBMENU
    assert tool.find("script").get("scriptType") == "python"


def test_the_shelf_scripts_payload_is_valid_python():
    """It is stored as text in an XML file that nothing compiles until an
    artist presses TAB, so it is compiled here instead."""
    import ast

    ast.parse(hl.SHELF_TOOL_SCRIPT)
    assert "scene_setup.run(kwargs)" in hl.SHELF_TOOL_SCRIPT
    # It must find the checkout the same way the HDAs do.
    assert "RPFARM_ROOT" in hl.SHELF_TOOL_SCRIPT


def test_installing_the_tool_writes_our_own_shelf_file_only(tmp_path):
    hfs = _make_hfs(tmp_path)
    inst = hl.HoudiniInstall(hfs)
    inst.user_pref_dir = tmp_path / "prefs"
    theirs = inst.user_pref_dir / "toolbar" / "default.shelf"
    theirs.parent.mkdir(parents=True)
    theirs.write_text("the artist's own shelves")

    result = hl.install_shelf_tool(inst)
    assert result["ok"]
    target = inst.user_pref_dir / "toolbar" / hl.SHELF_FILENAME
    assert target.read_text() == hl.shelf_tool_source()
    # Never the artist's file.
    assert theirs.read_text() == "the artist's own shelves"

    # Idempotent: a rerun rewrites our file and nothing else appears.
    hl.install_shelf_tool(inst)
    assert sorted(p.name for p in target.parent.iterdir()) == [
        "default.shelf", hl.SHELF_FILENAME]


# -- what an installed asset was built against -------------------------------


def test_the_bake_markers_match_the_guards():
    """houdini_local reads a block scripts/hda_guard.py writes. Two copies of
    a marker string are one typo away from a check that always says "cannot
    tell"."""
    import importlib.util

    path = hl.repo_root() / "scripts" / "hda_guard.py"
    spec = importlib.util.spec_from_file_location("hda_guard_probe", path)
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    assert hl.BAKE_BEGIN == guard.BAKE_BEGIN
    assert hl.BAKE_END == guard.BAKE_END


def _baked_text(fingerprint, version="9.9.9"):
    lines = [hl.BAKE_BEGIN,
             "_ASSET_BUILT_AGAINST_VERSION = {!r}".format(version),
             "_ASSET_FINGERPRINT = {"]
    for name in sorted(fingerprint):
        size, digest = fingerprint[name]
        lines.append("    {!r}: ({}, {!r}),".format(name, size, digest))
    lines.append("}")
    lines.append(hl.BAKE_END)
    return "\n".join(lines) + "\n"


def test_the_baked_block_is_read_back_out_of_binary_and_text():
    fp = {"cli.py": (10, "aaaa"), "config.py": (20, "bbbb")}
    text = _baked_text(fp)
    for payload in (text, ("\x00binary junk" + text + "more junk\x00").encode("utf-8")):
        got = hl.baked_fingerprint(payload)
        assert got["version"] == "9.9.9"
        assert got["fingerprint"] == fp


def test_an_asset_without_a_baked_block_is_cannot_tell_not_wrong(tmp_path):
    asset = tmp_path / "old.hda"
    asset.write_bytes(b"an asset from before the block existed")
    state = hl.asset_state(asset, {"cli.py": (10, "aaaa")})
    assert state == {"present": True, "baked": False, "stale": False, "off": []}


def test_a_changed_module_makes_the_installed_asset_stale(tmp_path):
    fp = {"cli.py": (10, "aaaa"), "config.py": (20, "bbbb")}
    asset = tmp_path / "runpodfarm_upload.hda"
    asset.write_text(_baked_text(fp))
    assert hl.asset_state(asset, fp)["stale"] is False

    moved = dict(fp, **{"config.py": (21, "cccc")})
    state = hl.asset_state(asset, moved)
    assert state["stale"] is True
    assert state["off"] == ["config.py"]


def test_a_missing_asset_is_reported_absent(tmp_path):
    assert hl.asset_state(tmp_path / "nope.hda", {})["present"] is False


def test_installed_states_cover_every_hda(tmp_path):
    hfs = _make_hfs(tmp_path)
    inst = hl.HoudiniInstall(hfs)
    inst.user_pref_dir = tmp_path / "prefs"
    otls = inst.user_pref_dir / "otls"
    otls.mkdir(parents=True)
    fp = {"cli.py": (10, "aaaa")}
    (otls / "runpodfarm_upload.hda").write_text(_baked_text(fp))

    states = hl.installed_asset_states(inst, fp)
    assert set(states) == set(hl.HDA_NAMES)
    assert states["runpodfarm_upload"]["present"] and not states["runpodfarm_upload"]["stale"]
    assert not states["runpodfarm_scheduler"]["present"]


def test_installing_touches_only_the_installation_it_was_given(tmp_path, monkeypatch):
    """It never fanned out on its own -- `rpfarm setup` and rebuild_assets.py
    loop over every found Houdini deliberately, and ~/.rpfarm/otls is the
    collapse cache they pass in, not a third install. What DID make "which
    version got it" unpredictable was hython exporting
    HOUDINI_USER_PREF_DIR, which HoudiniInstall honours: under hython every
    install resolved to the running one's preferences. scripts/rebuild_assets
    unpins it; this pins the other half of the claim."""
    monkeypatch.delenv("HOUDINI_USER_PREF_DIR", raising=False)
    root = tmp_path / "repo"
    for name in hl.HDA_NAMES:
        (root / "hda" / f"{name}.hda").mkdir(parents=True)

    mine = hl.HoudiniInstall(_make_hfs(tmp_path / "a"))
    mine.user_pref_dir = tmp_path / "prefs_22"
    other = tmp_path / "prefs_21"
    other.mkdir()

    def fake_runner(cmd, check, capture_output, text):
        with open(cmd[-1], "wb") as f:
            f.write(b"x")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    hl.build_and_install_hdas(mine, tmp_path / "cache", root=root, runner=fake_runner)

    assert sorted(p.name for p in (mine.user_pref_dir / "otls").iterdir()) == sorted(
        f"{n}.hda" for n in hl.HDA_NAMES)
    assert list(other.iterdir()) == [], "installed somewhere it was not asked to"
