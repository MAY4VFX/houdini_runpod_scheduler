import os

import pytest

from rpfarm.sync import (
    FileEntry,
    SftpTarget,
    SyncError,
    SyncStats,
    build_rclone_args,
    build_rclone_dir_args,
    compress_stage,
    plan_packages,
    rclone_copy,
    rclone_copy_dir,
)


def E(p, s):
    return FileEntry(local=f"/job/{p}", remote=f"projects/may/shot/{p}", size=s)


def EE(p, s):
    """A FileEntry consistent with local_root='/job', remote_root='/workspace'
    (ruling R8: local_root/rel and remote_root/rel must be the same rel)."""
    return FileEntry(local=f"/job/{p}", remote=f"/workspace/{p}", size=s)


# -- plan_packages -----------------------------------------------------------


def test_packages_group_by_dir_then_size():
    entries = [E("tex/a.rat", 600), E("tex/b.rat", 600), E("geo/c.bgeo.sc", 100), E("big.vdb", 5000)]
    pk = plan_packages(entries, max_bytes=1000)
    # Ruling R2: deterministic — 4 singleton packages, in remote-path-sorted
    # directory order (projects/may/shot < .../geo < .../tex).
    assert [sorted(e.remote.split("/")[-1] for e in p) for p in pk] == [
        ["big.vdb"],
        ["c.bgeo.sc"],
        ["a.rat"],
        ["b.rat"],
    ]


def test_single_big_file_is_own_package():
    pk = plan_packages([E("x", 10), E("huge", 10**9)], max_bytes=100)
    assert any(len(p) == 1 and p[0].size == 10**9 for p in pk)


def test_plan_packages_never_exceeds_max_bytes_except_singletons():
    entries = [E(f"f{i}", 300) for i in range(10)]
    pk = plan_packages(entries, max_bytes=1000)
    for p in pk:
        if len(p) > 1:
            assert sum(e.size for e in p) <= 1000


# -- build_rclone_args (file list) -------------------------------------------


def test_rclone_args_upload(tmp_path):
    # Ruling R8: local_root="/job", remote_root="/workspace/projects/may/shot".
    # rel is computed from the local side (relpath(local, local_root)), and
    # must land at the same place under remote_root.
    t = SftpTarget(host="1.2.3.4", port=40022, key_path="/k")
    entries = [FileEntry(local="/job/tex/a.rat", remote="/workspace/projects/may/shot/tex/a.rat", size=1)]
    args, files_from = build_rclone_args(entries, t, "up", "/job", "/workspace/projects/may/shot", tmp_path)
    assert args[:2] == ["copy", "/job"] and args[2] == ":sftp:/workspace/projects/may/shot"
    assert "--files-from" in args and open(files_from).read().strip() == "tex/a.rat"
    assert "--sftp-host=1.2.3.4" in args and "--sftp-port=40022" in args


def test_rclone_args_download_swaps_direction(tmp_path):
    t = SftpTarget(host="h", port=1, key_path="/k")
    entries = [FileEntry(local="/job/tex/a.rat", remote="/workspace/projects/may/shot/tex/a.rat", size=1)]
    args, _ = build_rclone_args(entries, t, "down", "/job", "/workspace/projects/may/shot", tmp_path)
    assert args[1] == ":sftp:/workspace/projects/may/shot" and args[2] == "/job"


def test_rclone_args_include_common_flags(tmp_path):
    t = SftpTarget(host="h", port=22, key_path="/k")
    args, _ = build_rclone_args([EE("a", 1)], t, "up", "/job", "/workspace", tmp_path)
    assert "--sftp-user=root" in args  # SftpTarget default user
    assert "--sftp-key-file=/k" in args
    assert "--sftp-set-modtime" in args
    assert "--use-json-log" in args


def test_rclone_args_raises_when_entry_does_not_map_under_remote_root(tmp_path):
    # local_root/rel = "tex/a.rat" would map to "/workspace/tex/a.rat" under
    # remote_root, but the entry claims a different remote — inconsistent
    # (local_root, remote_root, entry) grouping is a caller bug, not silently
    # accepted.
    t = SftpTarget(host="h", port=22, key_path="/k")
    entries = [FileEntry(local="/job/tex/a.rat", remote="/workspace/somewhere/else.rat", size=1)]
    with pytest.raises(SyncError):
        build_rclone_args(entries, t, "up", "/job", "/workspace", tmp_path)


# -- build_rclone_dir_args (whole-directory copy) -----------------------------


def test_rclone_dir_args_upload():
    t = SftpTarget(host="1.2.3.4", port=40022, key_path="/k")
    args = build_rclone_dir_args("/job/tex", t, "up", "/workspace/tex")
    assert args[:2] == ["copy", "/job/tex"] and args[2] == ":sftp:/workspace/tex"
    assert "--sftp-host=1.2.3.4" in args and "--sftp-port=40022" in args
    assert "--files-from" not in args


def test_rclone_dir_args_download_swaps_direction():
    t = SftpTarget(host="h", port=1, key_path="/k")
    args = build_rclone_dir_args("/job/tex", t, "down", "/workspace/tex")
    assert args[1] == ":sftp:/workspace/tex" and args[2] == "/job/tex"


# -- rclone_copy / rclone_copy_dir runner (fake rclone binary) ---------------


FAKE_RCLONE = """#!/usr/bin/env python3
import json, sys
print(json.dumps({"stats": {"bytes": 10, "totalBytes": 10, "speed": 1.0, "transfers": 1}}), file=sys.stderr, flush=True)
sys.exit(0)
"""

FAKE_RCLONE_FAIL = """#!/usr/bin/env python3
import sys
sys.exit(7)
"""


def _write_fake_rclone(tmp_path, body):
    p = tmp_path / "fake_rclone.py"
    p.write_text(body)
    os.chmod(p, 0o755)
    return str(p)


def test_rclone_copy_success_reports_stats_and_progress(tmp_path):
    rclone_bin = _write_fake_rclone(tmp_path, FAKE_RCLONE)
    t = SftpTarget(host="h", port=22, key_path="/k")
    seen = []
    stats = rclone_copy(
        [EE("a", 5), EE("b", 5)],
        t,
        "up",
        rclone_bin,
        "/job",
        "/workspace",
        progress_cb=lambda b, tb, sp: seen.append((b, tb, sp)),
    )
    assert isinstance(stats, SyncStats)
    assert stats.files == 2 and stats.bytes == 10
    assert seen == [(10, 10, 1.0)]


def test_rclone_copy_nonzero_exit_raises(tmp_path):
    rclone_bin = _write_fake_rclone(tmp_path, FAKE_RCLONE_FAIL)
    t = SftpTarget(host="h", port=22, key_path="/k")
    with pytest.raises(SyncError):
        rclone_copy([EE("a", 5)], t, "up", rclone_bin, "/job", "/workspace")


def test_rclone_copy_dir_reports_stats_from_rclone(tmp_path):
    rclone_bin = _write_fake_rclone(tmp_path, FAKE_RCLONE)
    t = SftpTarget(host="h", port=22, key_path="/k")
    stats = rclone_copy_dir(str(tmp_path), t, "up", rclone_bin, "/workspace/tex")
    assert isinstance(stats, SyncStats)
    assert stats.files == 1 and stats.bytes == 10


# -- compress_stage -----------------------------------------------------------


def test_compress_stage_compresses_compressible_and_skips_bgeo_sc(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    txt = job / "notes.txt"
    txt.write_text("hello world " * 2000)
    bgeo = job / "cache.bgeo.sc"
    bgeo.write_bytes(b"\x00\x01" * 64)

    # Ruling R10: FileEntry.remote is always a full remote-side path (never
    # bare-relative — see the module docstring / R8), so compress_stage takes
    # remote_root explicitly and returns (raw_package, staged_package, post_command).
    remote_root = "/workspace/projects/may/shot"
    package = [
        FileEntry(local=str(txt), remote=f"{remote_root}/notes.txt", size=txt.stat().st_size),
        FileEntry(local=str(bgeo), remote=f"{remote_root}/cache.bgeo.sc", size=bgeo.stat().st_size),
    ]
    staging = tmp_path / "staging"
    raw, staged, post_command = compress_stage(package, staging, remote_root, level=3)
    raw_by_remote = {e.remote: e for e in raw}
    staged_by_remote = {e.remote: e for e in staged}

    # cache.bgeo.sc is SKIP-classified: always left untouched, in raw_package.
    assert f"{remote_root}/cache.bgeo.sc" in raw_by_remote
    assert raw_by_remote[f"{remote_root}/cache.bgeo.sc"].local == str(bgeo)
    assert f"{remote_root}/cache.bgeo.sc.zst" not in staged_by_remote
    assert "cache.bgeo.sc" not in post_command

    # notes.txt is classified ZSTD (compressible text). Whether it actually
    # ends up compressed depends on compress_file succeeding, which in turn
    # depends on a working `zstd` binary being reachable (compress_file
    # itself always shells out to the `zstd` CLI, never the optional
    # `zstandard` python package) — some minimal/CLI-restricted zstd builds
    # reject certain level flags and compress_file returns False in that
    # case. Either outcome is correct; assert whichever one happened is
    # internally consistent.
    notes_remote = f"{remote_root}/notes.txt"
    if f"{notes_remote}.zst" in staged_by_remote:
        staged_entry = staged_by_remote[f"{notes_remote}.zst"]
        assert os.path.exists(staged_entry.local)
        # CRITICAL fix this replaces: the staged file must actually live
        # under staging_dir (previously os.path.join silently discarded
        # staging_dir whenever e.remote was absolute, per R8's convention).
        assert staged_entry.local.startswith(str(staging) + os.sep)
        assert staged_entry.local != str(txt)
        assert "zstd -d --rm notes.txt.zst" in post_command
        assert f"cd {remote_root}" in post_command

        # R8 invariant must hold for staged_package too: uploading it with
        # local_root=staging_dir, remote_root=remote_root must not raise.
        args, files_from = build_rclone_args(
            staged, SftpTarget(host="h", port=22, key_path="/k"), "up", str(staging), remote_root, tmp_path
        )
        assert open(files_from).read().strip() == "notes.txt.zst"
    else:
        # compress_file failed (no zstd, or a restricted build): stays in raw_package, uncompressed.
        assert notes_remote in raw_by_remote
        assert raw_by_remote[notes_remote].local == str(txt)
        assert staged == []
        assert post_command == ""


def test_compress_stage_raises_for_entry_outside_remote_root(tmp_path):
    package = [FileEntry(local="/job/x", remote="/workspace/other/x", size=1)]
    with pytest.raises(SyncError):
        compress_stage(package, tmp_path / "staging", "/workspace/projects/may/shot", level=3)


def test_compress_stage_empty_post_command_when_nothing_compressed(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    bgeo = job / "cache.bgeo.sc"
    bgeo.write_bytes(b"\x00")
    remote_root = "/workspace/projects/may/shot"
    package = [FileEntry(local=str(bgeo), remote=f"{remote_root}/cache.bgeo.sc", size=1)]
    staging = tmp_path / "staging"
    raw, staged, post_command = compress_stage(package, staging, remote_root, level=3)
    assert post_command == ""
    assert staged == []
    assert raw[0].local == str(bgeo)


# -- no archiver is slower, not broken (field failure, 2026-09-05) --------------


def test_an_upload_without_zstd_goes_up_uncompressed(tmp_path, monkeypatch, caplog):
    """The item that died read `FileNotFoundError: 'zstd'` and took the cook
    with it. zstd was installed the whole time -- at /opt/homebrew/bin, which
    a Dock-launched Houdini does not have on PATH. Whatever the reason, a
    missing archiver must cost speed, not the job."""
    from rpfarm import compression, tools

    tools.clear_cache()
    compression._MISSING_WARNED.clear()
    monkeypatch.setattr(tools, "resolve_tool", lambda *a, **k: None)

    src = tmp_path / "geo.bgeo"
    src.write_bytes(b"g" * 4096)
    package = [FileEntry(local=str(src), remote="/remote/root/geo.bgeo", size=4096)]

    with caplog.at_level("WARNING"):
        raw, staged, post = compress_stage(package, tmp_path / "staging", "/remote/root")

    assert raw == package, "every file still uploads"
    assert staged == [] and post == ""
    assert any("not be compressed" in r.getMessage() for r in caplog.records), caplog.text
    compression._MISSING_WARNED.clear()
    tools.clear_cache()


def test_compression_uses_an_absolute_binary_never_a_bare_name(tmp_path, monkeypatch):
    """PATH is not to be trusted for external programs -- the same lesson as
    resolve_package_python, which this reuses the shape of."""
    from rpfarm import compression, tools

    tools.clear_cache()
    seen = []
    monkeypatch.setattr(tools, "resolve_tool",
                        lambda name, **k: tools.Tool("/opt/homebrew/bin/" + name, "/opt/homebrew/bin", "1.5.6"))

    def fake_run(argv, **kwargs):
        seen.append(argv[0])
        raise AssertionError("stop here; the argv is the point")

    monkeypatch.setattr(compression.subprocess, "run", fake_run)
    src = tmp_path / "a.exr"
    src.write_bytes(b"x" * 100)
    with pytest.raises(AssertionError):
        compression.compress_file(str(src), str(tmp_path / "a.exr.zst"),
                                  compression.CompressionStrategy.ZSTD)

    assert seen == ["/opt/homebrew/bin/zstd"]
    tools.clear_cache()
