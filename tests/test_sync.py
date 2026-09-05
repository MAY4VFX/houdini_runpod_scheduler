import os

import pytest

import rpfarm.sync as rpsync

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
        assert "zstd -d --rm -f notes.txt.zst" in post_command
        assert "os.utime" in post_command and " notes.txt " in post_command, (
            "the farm copy must wear the original's mtime, or the next "
            "cook cannot tell it is already there")
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


# -- compression is the standard library (owner's requirement, 2026-09-05) -----
#
# "нам надо как-то это всё внутри нашего пакета держать, и нам же надо чтобы
# это работало мультиплатформенно" -- a repository someone clones, types
# their RunPod keys into, and uses. facebook/zstd ships binaries for Windows
# only, so a downloaded-binary approach like rclone's does not port; lzma is
# in the standard library everywhere, including Houdini's Python and the
# pod's (3.10.12, verified on the live sync pod).


def test_compression_needs_no_external_program_at_all(tmp_path, monkeypatch):
    from rpfarm import compression

    def no_subprocesses(*a, **k):
        raise AssertionError("compression must not shell out")

    monkeypatch.setattr(compression.subprocess, "run", no_subprocesses)
    # sync.py imported the name, so that is the reference that matters
    monkeypatch.setattr(rpsync, "select_codec", lambda **k: compression.CODEC_XZ)

    src = tmp_path / "scene.usdc"
    src.write_bytes(b"usd data " * 20000)
    package = [FileEntry(local=str(src), remote="/remote/root/scene.usdc", size=src.stat().st_size)]

    raw, staged, post = compress_stage(package, tmp_path / "staging", "/remote/root")

    assert raw == [] and len(staged) == 1
    assert staged[0].remote.endswith(".xz"), "the format travels with the file"
    assert staged[0].size < src.stat().st_size
    assert "python3 -c" in post, "the pod has lzma but no xz binary -- checked on the pod"


def test_the_package_says_which_format_it_is_in_and_what_mtime_to_restore(tmp_path):
    """The pod cannot guess either one. The extension names the codec, and
    the mtime travels because the farm's copy has to end up looking like the
    file it came from -- otherwise nothing can tell, next cook, that it is
    already there."""
    from rpfarm import compression

    zstd = compression.Codec("zstd", ".zst", "/opt/homebrew/bin/zstd")

    cmd = compression.decompress_command("/root", [("a.zst", 1767323045.0)], zstd)
    assert cmd.startswith("cd /root && zstd -d --rm -f a.zst && python3 -c ")
    assert " -f " in cmd, "the file is already there on any second upload"
    assert cmd.endswith(" a 1767323045"), "restores the ORIGINAL's mtime on the unpacked file"

    xz = compression.decompress_command(
        "/root", [("a.xz", 1767323045.0), ("b.xz", 1700000000.0)], compression.CODEC_XZ)
    assert xz.startswith("cd /root && python3 -c ")
    assert xz.endswith(" a.xz 1767323045 b.xz 1700000000")
    assert compression.decompress_command("/root", [], compression.CODEC_XZ) == ""


def test_a_round_trip_through_the_standard_library(tmp_path):
    from rpfarm import compression

    src = tmp_path / "geo.bgeo"
    payload = b"geometry" * 5000
    src.write_bytes(payload)
    staged = tmp_path / "staged" / "geo.bgeo.xz"
    back = tmp_path / "back" / "geo.bgeo"

    assert compression.compress_file(str(src), str(staged),
                                     compression.CompressionStrategy.COMPRESS,
                                     codec=compression.CODEC_XZ)
    assert staged.stat().st_size < len(payload)
    assert compression.decompress_file(str(staged), str(back),
                                       compression.CompressionStrategy.COMPRESS)
    assert back.read_bytes() == payload


def test_a_zstd_that_is_not_really_there_does_not_break_the_upload(tmp_path, monkeypatch):
    """The field failure, as a test: something claims a zstd binary, the
    binary is not there. The upload must still happen."""
    from rpfarm import compression

    ghost = compression.Codec("zstd", ".zst", str(tmp_path / "nowhere" / "zstd"))
    monkeypatch.setattr(rpsync, "select_codec", lambda **k: ghost)

    src = tmp_path / "cache.bgeo"
    src.write_bytes(b"g" * 8192)
    package = [FileEntry(local=str(src), remote="/remote/root/cache.bgeo", size=8192)]

    raw, staged, post = compress_stage(package, tmp_path / "staging", "/remote/root")

    assert raw == package, "every file still uploads"
    assert staged == [] and post == ""


def test_the_codec_is_the_standard_library_unless_a_real_binary_is_found():
    from rpfarm import compression, tools

    assert compression.select_codec(resolve=lambda name, **k: None) is compression.CODEC_XZ
    assert compression.select_codec(allow_external=False) is compression.CODEC_XZ
    found = tools.Tool("/opt/homebrew/bin/zstd", "/opt/homebrew/bin", "1.5.7")
    picked = compression.select_codec(resolve=lambda name, **k: found)
    assert picked.name == "zstd" and picked.binary == found.path


# -- compression must not defeat incremental upload (2026-09-05) ----------------
#
# A compressed file travels as an archive that is DELETED after unpacking, so
# rclone -- comparing archive name against archive name -- finds nothing on the
# far side and sends the whole set again. For the owner that is 2.8 GB over a
# 4.7 Mbps link: a minute if only the .hip moved, an hour and a half if not.


def _index_from(entries, size_delta=0, mtime_delta=0.0):
    return {os.path.basename(e.remote): (os.path.getsize(e.local) + size_delta,
                                         os.path.getmtime(e.local) + mtime_delta)
            for e in entries}


def test_a_file_already_on_the_farm_is_not_sent_again(tmp_path):
    src = tmp_path / "cache.bgeo"
    src.write_bytes(b"g" * 4096)
    entry = FileEntry(local=str(src), remote="/remote/root/cache.bgeo", size=4096)

    assert rpsync.already_on_farm(entry, _index_from([entry]), str(tmp_path), "/remote/root")


def test_a_changed_file_is_sent(tmp_path):
    src = tmp_path / "scene.hip"
    src.write_bytes(b"h" * 100)
    entry = FileEntry(local=str(src), remote="/remote/root/scene.hip", size=100)

    assert not rpsync.already_on_farm(entry, _index_from([entry], size_delta=-5),
                                      str(tmp_path), "/remote/root")
    assert not rpsync.already_on_farm(entry, _index_from([entry], mtime_delta=600),
                                      str(tmp_path), "/remote/root")


def test_a_file_the_farm_never_got_is_sent(tmp_path):
    """Half a package left by a cancelled cook is exactly this case, and it
    is why the farm is asked instead of a manifest being kept: whatever is
    actually there is the answer, with no state to drift."""
    src = tmp_path / "tex.rat"
    src.write_bytes(b"t")
    entry = FileEntry(local=str(src), remote="/remote/root/tex.rat", size=1)

    assert not rpsync.already_on_farm(entry, {}, str(tmp_path), "/remote/root")
    assert not rpsync.already_on_farm(entry, {"other.rat": (1, 0.0)}, str(tmp_path), "/remote/root")


def test_a_second_of_slack_is_not_a_change(tmp_path):
    """Filesystems and transports round timestamps differently; a second of
    slack costs nothing next to re-sending a gigabyte."""
    src = tmp_path / "a.exr"
    src.write_bytes(b"x")
    entry = FileEntry(local=str(src), remote="/remote/root/a.exr", size=1)

    assert rpsync.already_on_farm(entry, _index_from([entry], mtime_delta=1.0),
                                  str(tmp_path), "/remote/root")


def test_the_index_is_read_from_the_farm_itself(monkeypatch):
    seen = {}

    def fake_run(rclone_bin, args):
        seen["args"] = args
        return [{"Path": "BS/airship/scene.hip", "Size": 48866843,
                 "ModTime": "2026-09-05T11:50:00.123456789+00:00"},
                {"Path": "tex/a.rat", "Size": 10, "ModTime": "2026-01-02T03:04:05Z"}]

    index = rpsync.remote_index(SftpTarget(host="h", port=22, key_path="/k"),
                                "/bin/rclone", "/workspace/projects/may/airship",
                                run=fake_run)

    assert "lsjson" in seen["args"] and "--recursive" in seen["args"]
    assert index["BS/airship/scene.hip"][0] == 48866843
    assert index["tex/a.rat"] == (10, 1767323045.0), "RFC3339 with a Z, nanoseconds dropped"


def test_a_farm_that_cannot_be_listed_just_means_send_everything():
    """The listing is an optimisation. A first cook has nothing there, a
    broken rclone has nothing to say -- neither may fail the upload."""
    def boom(rclone_bin, args):
        raise rpsync.SyncError("no such project yet")

    assert rpsync.remote_index(SftpTarget(host="h", port=22, key_path="/k"),
                               "/bin/rclone", "/nope", run=boom) == {}
