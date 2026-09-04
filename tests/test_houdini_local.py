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
# These tests pin the resolution order that replaced it.
# ---------------------------------------------------------------------------


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

    got = hl.houdini_bundled_python(str(hfs), (3, 13), platform_name="darwin")

    assert got == str(exe)


def test_bundled_python_version_comes_from_the_caller_not_a_hardcoded_one(tmp_path):
    # A Houdini on python3.11 must resolve python3.11, not 3.13.
    hfs, exe = _mac_hfs(tmp_path, py="3.11")

    assert hl.houdini_bundled_python(str(hfs), (3, 11), platform_name="darwin") == str(exe)
    assert hl.houdini_bundled_python(str(hfs), (3, 13), platform_name="darwin") is None


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
        running="/usr/bin/hython", running_version=(3, 13),
        discover=lambda: "/opt/homebrew/bin/python3.14")

    assert got == str(exe)
    assert "bundled" in why and "licence" in why


def test_resolve_falls_back_to_the_running_interpreter_when_new_enough(tmp_path):
    got, why = hl.resolve_package_python(
        hfs=str(tmp_path / "no-such-hfs"), version=(3, 13), platform_name="darwin",
        running="/opt/hfs/bin/hython", running_version=(3, 13),
        discover=lambda: "/opt/homebrew/bin/python3.14")

    assert got == "/opt/hfs/bin/hython"
    # The licence cost of that choice has to be visible in the log.
    assert "hython" in why and "licence" in why


def test_resolve_skips_a_too_old_running_interpreter_and_searches_disk(tmp_path):
    got, why = hl.resolve_package_python(
        hfs=None, version=(3, 9), platform_name="darwin",
        running="/usr/bin/python3", running_version=(3, 9),
        discover=lambda: "/opt/homebrew/bin/python3.14")

    assert got == "/opt/homebrew/bin/python3.14"
    assert "on disk" in why


def test_resolve_last_resort_is_bare_python3_and_says_it_is_a_warning():
    got, why = hl.resolve_package_python(
        hfs=None, version=(3, 9), platform_name="darwin",
        running="/usr/bin/python3", running_version=(3, 9),
        discover=lambda: None)

    assert got == "python3"
    assert why.startswith("WARNING")
    assert "tomllib" in why


def test_discover_picks_the_newest_and_ignores_pre_3_11(tmp_path):
    old = tmp_path / "usr" / "bin"
    new = tmp_path / "brew" / "bin"
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    for name in ("python3.9", "python3.11", "python3-config", "python3"):
        (old / name).write_text("")
    (new / "python3.14").write_text("")
    (new / "python3.12").write_text("")

    got = hl.discover_python_on_disk(search_dirs=[str(old), str(new)])

    assert got == str(new / "python3.14")


def test_discover_returns_none_when_nothing_is_new_enough(tmp_path):
    d = tmp_path / "bin"
    d.mkdir()
    (d / "python3.9").write_text("")

    assert hl.discover_python_on_disk(search_dirs=[str(d)]) is None
    assert hl.discover_python_on_disk(search_dirs=[str(tmp_path / "missing")]) is None


def test_the_resolved_interpreter_can_actually_import_tomllib(tmp_path):
    """The whole point, checked against this machine's real Houdini.

    Skipped where no Houdini is installed; where one is, a resolver that
    returns something without tomllib has not fixed the bug it exists for.
    """
    installs = hl.find_houdini_installations()
    if not installs:
        pytest.skip("no local Houdini installation")
    hfs = str(installs[0].hfs) if hasattr(installs[0], "hfs") else None
    if not hfs:
        pytest.skip("install has no HFS path to derive from")
    exe = hl.houdini_bundled_python(hfs, _houdini_python_version(hfs))
    if not exe:
        pytest.skip("no bundled python found for {}".format(hfs))
    out = subprocess.run(
        [exe, "-c", "import tomllib, sys; print(sys.version_info[0], sys.version_info[1])"],
        capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    major, minor = (int(x) for x in out.stdout.split())
    assert (major, minor) >= hl.PACKAGE_PYTHON_MIN


def _houdini_python_version(hfs):
    """(major, minor) of the Python shipped with the install at ``hfs``."""
    import glob as _glob
    versions = _glob.glob(os.path.join(hfs, "..", "..", "..", "..", "Python.framework", "Versions", "3.*"))
    versions += _glob.glob(os.path.join(hfs, "python", "lib", "python3.*"))
    best = None
    for v in versions:
        name = os.path.basename(v.rstrip("/"))
        digits = name.replace("python", "")
        try:
            major, minor = (int(x) for x in digits.split(".")[:2])
        except ValueError:
            continue
        if best is None or (major, minor) > best:
            best = (major, minor)
    return best or (3, 13)
