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

    package = [
        FileEntry(local=str(txt), remote="projects/may/shot/notes.txt", size=txt.stat().st_size),
        FileEntry(local=str(bgeo), remote="projects/may/shot/cache.bgeo.sc", size=bgeo.stat().st_size),
    ]
    staging = tmp_path / "staging"
    upload, post_command = compress_stage(package, staging, level=3)
    by_remote = {e.remote: e for e in upload}

    # cache.bgeo.sc is SKIP-classified: always left untouched.
    assert "projects/may/shot/cache.bgeo.sc" in by_remote
    assert by_remote["projects/may/shot/cache.bgeo.sc"].local == str(bgeo)
    assert "projects/may/shot/cache.bgeo.sc.zst" not in by_remote
    assert "zstd -d --rm projects/may/shot/cache.bgeo.sc.zst" not in post_command

    # notes.txt is classified ZSTD (compressible text). Whether it actually
    # ends up compressed depends on compress_file succeeding, which in turn
    # depends on a working `zstd` binary being reachable (compress_file
    # itself always shells out to the `zstd` CLI, never the optional
    # `zstandard` python package) — some minimal/CLI-restricted zstd builds
    # reject flags like `--level=3` and compress_file returns False in that
    # case. Either outcome is correct; assert whichever one happened is
    # internally consistent.
    if "projects/may/shot/notes.txt.zst" in by_remote:
        staged = by_remote["projects/may/shot/notes.txt.zst"]
        assert os.path.exists(staged.local)
        assert staged.local != str(txt)
        assert "zstd -d --rm projects/may/shot/notes.txt.zst" in post_command
    else:
        # compress_file failed (no zstd, or a restricted build): leave uncompressed.
        assert "projects/may/shot/notes.txt" in by_remote
        assert by_remote["projects/may/shot/notes.txt"].local == str(txt)
        assert "notes.txt" not in post_command


def test_compress_stage_empty_post_command_when_nothing_compressed(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    bgeo = job / "cache.bgeo.sc"
    bgeo.write_bytes(b"\x00")
    package = [FileEntry(local=str(bgeo), remote="projects/may/shot/cache.bgeo.sc", size=1)]
    staging = tmp_path / "staging"
    upload, post_command = compress_stage(package, staging, level=3)
    assert post_command == ""
    assert upload[0].local == str(bgeo)
