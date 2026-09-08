import os
import platform
import subprocess

import pytest

from rpfarm import cli
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


# ---------------------------------------------------------------------------
# The default root must not clobber a deliberately different value
# (2026-09-08). rebuild_assets.py and `rpfarm setup` both call
# write_rpfarm_root_env(install) with no `root=` -- so a run for a completely
# unrelated fix used to silently switch the artist's houdini.env back to
# this checkout, out from under a stable installed copy someone had pointed
# it at on purpose, mid-session.
# ---------------------------------------------------------------------------


def test_the_default_root_never_overwrites_a_deliberately_different_value(tmp_path):
    prefs = tmp_path / "prefs"
    inst = hl.HoudiniInstall.__new__(hl.HoudiniInstall)
    inst.user_pref_dir = prefs

    stable_copy = tmp_path / "stable" / "pkg"
    hl.write_rpfarm_root_env(inst, root=stable_copy)  # someone's deliberate choice

    hl.write_rpfarm_root_env(inst)  # the default every real caller uses

    text = (prefs / "houdini.env").read_text()
    assert str(stable_copy) in text
    assert str(hl.repo_root()) not in text
    assert text.count("RPFARM_ROOT") == 1


def test_the_default_root_reports_that_it_left_an_existing_value_alone(tmp_path):
    prefs = tmp_path / "prefs"
    inst = hl.HoudiniInstall.__new__(hl.HoudiniInstall)
    inst.user_pref_dir = prefs
    stable_copy = tmp_path / "stable" / "pkg"
    hl.write_rpfarm_root_env(inst, root=stable_copy)

    said = []
    hl.write_rpfarm_root_env(inst, log=said.append)

    assert said and str(stable_copy) in said[0]


def test_an_explicit_root_still_always_wins(tmp_path):
    """The escape hatch: a developer (or a future installer) choosing a
    SPECIFIC checkout on purpose is not "the default", and must still be
    able to override whatever is already there -- this is what makes an
    explicit RPFARM_ROOT= a real switch and not a one-way ratchet."""
    prefs = tmp_path / "prefs"
    inst = hl.HoudiniInstall.__new__(hl.HoudiniInstall)
    inst.user_pref_dir = prefs
    stable_copy = tmp_path / "stable" / "pkg"
    hl.write_rpfarm_root_env(inst, root=stable_copy)

    dev_checkout = tmp_path / "dev" / "checkout"
    hl.write_rpfarm_root_env(inst, root=dev_checkout)

    text = (prefs / "houdini.env").read_text()
    assert str(dev_checkout) in text
    assert str(stable_copy) not in text


def test_the_default_root_is_written_when_nothing_is_configured_yet(tmp_path):
    """First-time setup, or a file that never had the line: no existing
    value to defer to, so the default -- the stable package copy, NOT this
    checkout (2026-09-08) -- is written."""
    prefs = tmp_path / "prefs"
    home = tmp_path / "home" / ".rpfarm"
    inst = hl.HoudiniInstall.__new__(hl.HoudiniInstall)
    inst.user_pref_dir = prefs

    hl.write_rpfarm_root_env(inst, home=home)

    text = (prefs / "houdini.env").read_text()
    assert str(hl.stable_package_root(home)) in text
    assert str(hl.repo_root()) not in text


def test_a_hand_written_line_without_the_marker_is_still_recognised(tmp_path):
    """Someone (or a future installer) may set RPFARM_ROOT by hand, without
    ever having gone through this function -- it still must not be
    clobbered by a later default call."""
    prefs = tmp_path / "prefs"
    prefs.mkdir(parents=True)
    hand_set = tmp_path / "hand" / "set"
    (prefs / "houdini.env").write_text('RPFARM_ROOT = "{}"\n'.format(hand_set))
    inst = hl.HoudiniInstall.__new__(hl.HoudiniInstall)
    inst.user_pref_dir = prefs

    hl.write_rpfarm_root_env(inst, home=tmp_path / "home" / ".rpfarm")

    text = (prefs / "houdini.env").read_text()
    assert str(hand_set) in text
    assert str(hl.repo_root()) not in text


# ---------------------------------------------------------------------------
# install_package_copy / stable_package_root (2026-09-08)
#
# The other half of the fix: an artist's Houdini reading rpfarm/*.py live
# out of a git checkout an agent might be mid-edit on. This copies the
# package into a stable location instead, atomically.
# ---------------------------------------------------------------------------


def _fake_checkout(tmp_path, content="X = 1\n"):
    root = tmp_path / "checkout"
    pkg = root / "rpfarm"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(content)
    (pkg / "config.py").write_text("Y = 2\n")
    return root


def test_install_package_copy_reproduces_the_package_exactly(tmp_path):
    root = _fake_checkout(tmp_path)
    home = tmp_path / "home" / ".rpfarm"

    stable_root = hl.install_package_copy(home, root=root)

    assert stable_root == hl.stable_package_root(home)
    assert (stable_root / "rpfarm" / "__init__.py").read_text() == "X = 1\n"
    assert (stable_root / "rpfarm" / "config.py").read_text() == "Y = 2\n"


def test_install_package_copy_replaces_a_previous_copy_completely(tmp_path):
    """A second install must not leave any file from the first version
    behind -- a file removed from the package must actually disappear."""
    root = _fake_checkout(tmp_path, content="X = 1\n")
    (root / "rpfarm" / "old_module.py").write_text("stale\n")
    home = tmp_path / "home" / ".rpfarm"
    hl.install_package_copy(home, root=root)
    assert (hl.stable_package_root(home) / "rpfarm" / "old_module.py").exists()

    root2 = _fake_checkout(tmp_path / "v2", content="X = 2\n")
    hl.install_package_copy(home, root=root2)

    dest = hl.stable_package_root(home) / "rpfarm"
    assert (dest / "__init__.py").read_text() == "X = 2\n"
    assert not (dest / "old_module.py").exists()


def test_install_package_copy_leaves_no_temp_directories_behind(tmp_path):
    root = _fake_checkout(tmp_path)
    home = tmp_path / "home" / ".rpfarm"

    hl.install_package_copy(home, root=root)
    hl.install_package_copy(home, root=root)  # a second, ordinary run

    leftovers = [p.name for p in hl.stable_package_root(home).iterdir() if p.name != "rpfarm"]
    assert leftovers == []


def test_install_package_copy_rolls_back_on_a_failed_swap(tmp_path, monkeypatch):
    """If the final rename fails, the artist must not be left with no
    package at all -- the old copy goes back."""
    root = _fake_checkout(tmp_path, content="X = 1\n")
    home = tmp_path / "home" / ".rpfarm"
    hl.install_package_copy(home, root=root)  # a first, real copy in place

    root2 = _fake_checkout(tmp_path / "v2", content="X = 2\n")
    real_replace = os.replace
    calls = {"n": 0}

    def _flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:  # the swap-in of the NEW copy, after the old was moved aside
            raise OSError("simulated failure")
        return real_replace(src, dst)

    monkeypatch.setattr(hl.os, "replace", _flaky_replace)

    with pytest.raises(OSError):
        hl.install_package_copy(home, root=root2)

    dest = hl.stable_package_root(home) / "rpfarm"
    assert dest.exists()
    assert (dest / "__init__.py").read_text() == "X = 1\n", "the old copy must still be there"


def test_ensure_package_symlink_installs_the_copy_and_points_at_it(tmp_path, monkeypatch):
    root = _fake_checkout(tmp_path)
    monkeypatch.setattr(cli.houdini_local, "repo_root", lambda: root)
    home = tmp_path / "home" / ".rpfarm"
    home.mkdir(parents=True)

    cli._ensure_package_symlink(home, log=lambda m: None)

    link = home / "src"
    assert link.is_symlink()
    assert link.resolve() == hl.stable_package_root(home).resolve()
    assert (link / "rpfarm" / "__init__.py").read_text() == "X = 1\n"


def test_ensure_package_symlink_migrates_the_old_checkout_default(tmp_path, monkeypatch):
    """A symlink left over from before this fix -- pointing at repo_root(),
    this function's own historical default -- gets moved to the stable
    copy. That is a migration, not a customization to protect."""
    root = _fake_checkout(tmp_path)
    monkeypatch.setattr(cli.houdini_local, "repo_root", lambda: root)
    home = tmp_path / "home" / ".rpfarm"
    home.mkdir(parents=True)
    (home / "src").symlink_to(root)

    cli._ensure_package_symlink(home, log=lambda m: None)

    assert (home / "src").resolve() == hl.stable_package_root(home).resolve()


def test_ensure_package_symlink_leaves_a_real_customization_alone(tmp_path, monkeypatch):
    root = _fake_checkout(tmp_path)
    monkeypatch.setattr(cli.houdini_local, "repo_root", lambda: root)
    home = tmp_path / "home" / ".rpfarm"
    home.mkdir(parents=True)
    custom = tmp_path / "someone_elses_checkout"
    custom.mkdir()
    (home / "src").symlink_to(custom)

    cli._ensure_package_symlink(home, log=lambda m: None)

    assert (home / "src").resolve() == custom.resolve()


# ---------------------------------------------------------------------------
# cook_is_running (2026-09-08) -- the installer's side of "do not swap the
# package out from under a running cook". The scheduler holds an flock on
# the same path for a cook's whole duration; this probes it non-blocking.
# ---------------------------------------------------------------------------


def test_cook_is_running_is_false_when_nothing_ever_cooked(tmp_path):
    home = tmp_path / "home" / ".rpfarm"
    assert hl.cook_is_running(home) is False


@pytest.mark.skipif(platform.system() == "Windows", reason="fcntl is POSIX-only")
def test_cook_is_running_is_true_while_the_lock_is_held(tmp_path):
    import fcntl

    home = tmp_path / "home" / ".rpfarm"
    path = hl.cook_lock_path(home)
    path.parent.mkdir(parents=True)
    fh = open(path, "a+")
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert hl.cook_is_running(home) is True
    finally:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


@pytest.mark.skipif(platform.system() == "Windows", reason="fcntl is POSIX-only")
def test_cook_is_running_is_false_once_the_lock_is_released(tmp_path):
    import fcntl

    home = tmp_path / "home" / ".rpfarm"
    path = hl.cook_lock_path(home)
    path.parent.mkdir(parents=True)
    fh = open(path, "a+")
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    fh.close()

    assert hl.cook_is_running(home) is False


def test_setup_checks_for_a_running_cook_before_installing_anything():
    """2026-09-08: a reinstall mid-cook is what left a live session's
    onTick raising NameError and cost a rendered frame. rpfarm setup must
    see that and refuse before touching the package, not after -- `cmd_setup`
    is expensive to run end to end (real RunPod calls, prompts), so this
    checks the guard's own position the same way the scheduler's onStartCook/
    onStopCook ordering is checked elsewhere in this repo."""
    import inspect

    src = inspect.getsource(cli)
    setup_start = src.index("def cmd_setup(")
    guard = src.index("cook_is_running(home)", setup_start)
    refusal = src.index("return 1", guard)
    install_call = src.index("_ensure_package_symlink(home)", setup_start)
    assert guard < refusal < install_call


# ---------------------------------------------------------------------------
# isolated_child_env (Ruling R63) -- forcing a child hython to read the
# package it was explicitly asked for, on a machine whose houdini.env would
# otherwise silently override RPFARM_ROOT after the process starts.
# ---------------------------------------------------------------------------


def _fake_real_install(tmp_path, with_houdini_env=True, with_otls=True):
    real_prefs = tmp_path / "real_prefs"
    real_prefs.mkdir(parents=True)
    if with_otls:
        (real_prefs / "otls").mkdir()
        (real_prefs / "otls" / "runpodfarm_scheduler.hda").write_bytes(b"fake hda")
    if with_houdini_env:
        (real_prefs / "houdini.env").write_text(
            'SOME_OTHER_VAR = "1"\nRPFARM_ROOT = "/some/other/pkg"\n')
    install = hl.HoudiniInstall.__new__(hl.HoudiniInstall)
    install.user_pref_dir = real_prefs
    install.major_minor = "22.0"
    return install


def test_isolated_child_env_forces_root_over_a_copied_houdini_env(tmp_path):
    install = _fake_real_install(tmp_path)
    scratch = tmp_path / "scratch"
    root = tmp_path / "checkout"

    env = hl.isolated_child_env(root, scratch, install)

    scratch_prefs = scratch / "prefs_v22.0"
    env_text = (scratch_prefs / "houdini.env").read_text()
    assert 'RPFARM_ROOT = "{}"'.format(root) in env_text
    assert "/some/other/pkg" not in env_text
    assert "SOME_OTHER_VAR" in env_text, "the rest of the real houdini.env is preserved"
    assert env["HOUDINI_USER_PREF_DIR"] == str(scratch / "prefs_v__HVER__"), (
        "the __HVER__ placeholder form -- a literal path is silently ignored by Houdini")


def test_isolated_child_env_works_with_no_real_houdini_env_at_all(tmp_path):
    install = _fake_real_install(tmp_path, with_houdini_env=False)
    scratch = tmp_path / "scratch"
    root = tmp_path / "checkout"

    env = hl.isolated_child_env(root, scratch, install)

    scratch_prefs = scratch / "prefs_v22.0"
    assert 'RPFARM_ROOT = "{}"'.format(root) in (scratch_prefs / "houdini.env").read_text()


def test_isolated_child_env_symlinks_the_real_otls_by_default(tmp_path):
    install = _fake_real_install(tmp_path)
    scratch = tmp_path / "scratch"

    hl.isolated_child_env(tmp_path / "checkout", scratch, install)

    scratch_otls = scratch / "prefs_v22.0" / "otls"
    assert scratch_otls.is_symlink()
    assert (scratch_otls / "runpodfarm_scheduler.hda").read_bytes() == b"fake hda"


def test_isolated_child_env_skips_the_symlink_when_told_to(tmp_path):
    install = _fake_real_install(tmp_path)
    scratch = tmp_path / "scratch"

    hl.isolated_child_env(tmp_path / "checkout", scratch, install, keep_real_otls=False)

    assert not (scratch / "prefs_v22.0" / "otls").exists()


def test_isolated_child_env_passes_through_extra_env(tmp_path):
    install = _fake_real_install(tmp_path)
    env = hl.isolated_child_env(tmp_path / "checkout", tmp_path / "scratch", install,
                                extra_env={"RPFARM_TOKEN": "abc"})
    assert env["RPFARM_TOKEN"] == "abc"


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
