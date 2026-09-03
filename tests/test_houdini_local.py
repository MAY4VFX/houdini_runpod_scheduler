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
