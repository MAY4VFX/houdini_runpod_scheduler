import json
import os
import sys
from types import SimpleNamespace

from rpfarm import deps
from rpfarm.deps import _ext_suffix, collect_refs, pathmap_env, resolve_entries


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


# -- _ext_suffix ------------------------------------------------------------------


def test_ext_suffix_posix_passthrough():
    assert _ext_suffix("/Users/may/lib/b.abc") == "/Users/may/lib/b.abc"


def test_ext_suffix_windows_drive_letter():
    assert _ext_suffix(r"C:\lib\b.abc") == "/C/lib/b.abc"


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
        fileReferences=lambda: refs,
        text=_StubText(),
    )
    monkeypatch.setitem(sys.modules, "hou", stub)

    result = collect_refs()

    assert result[0] == "/job/hip/scene.hip"
    assert "/job/tex/a.rat" in result
    assert "/job/render" in result  # sequence token reduced to containing dir
    assert "/job/other/plain.abc" in result
    assert not any(r.startswith(("op:", "opdef:", "temp:")) for r in result)
    assert len(result) == 4  # hip + 3 valid refs (empty/op:/opdef:/temp: skipped)
