import json
import os

import pytest

from rpfarm.packages import (
    build_upload_items,
    houdini_install_preset,
    resolve_compress_flag,
    run_upload_item,
    write_pathmap,
)
from rpfarm.sync import FileEntry, SftpTarget, SyncError


# -- build_upload_items: custom mode -----------------------------------------


def test_custom_items(tmp_path):
    src = tmp_path / "plug"
    src.mkdir()
    (src / "a.so").write_bytes(b"1" * 100)
    (src / "b.so").write_bytes(b"2" * 100)
    items = build_upload_items(
        "custom", str(tmp_path), "may", "shot", [(str(src), "/workspace/apps/plug")], [], package_gb=1
    )
    assert len(items) == 1 and items[0]["remote_root"] == "/workspace/apps/plug" and items[0]["bytes"] == 200
    assert sorted(f[1] for f in items[0]["files"]) == ["/workspace/apps/plug/a.so", "/workspace/apps/plug/b.so"]
    assert items[0]["local_root"] == str(src)
    assert items[0]["index"] == 0
    assert items[0]["post_command"] == ""


def test_custom_items_nested_subdirs_stay_under_one_root(tmp_path):
    src = tmp_path / "plug"
    (src / "sub").mkdir(parents=True)
    (src / "a.so").write_bytes(b"1" * 10)
    (src / "sub" / "b.so").write_bytes(b"2" * 10)
    items = build_upload_items(
        "custom", str(tmp_path), "may", "shot", [(str(src), "/workspace/apps/plug")], [], package_gb=1
    )
    remotes = sorted(f[1] for it in items for f in it["files"])
    assert remotes == ["/workspace/apps/plug/a.so", "/workspace/apps/plug/sub/b.so"]


def test_custom_items_single_file_pair(tmp_path):
    tar = tmp_path / "houdini-22.0.393-linux_x86_64_gcc14.2.tar.gz"
    tar.write_bytes(b"x" * 50)
    items = build_upload_items(
        "custom", str(tmp_path), "may", "shot", [(str(tar), "/workspace/apps/dist/")], [], package_gb=1
    )
    assert len(items) == 1
    assert items[0]["files"] == [[str(tar), "/workspace/apps/dist/houdini-22.0.393-linux_x86_64_gcc14.2.tar.gz", 50]]
    # R8 (rpfarm.sync): local_root must be the file's own containing
    # directory, not the file itself -- relpath(file, file) is ".", which
    # does not reproduce basename(file) when joined with remote_root.
    # Caught live against the real sync pod (CookedFail on a single-file
    # custom pair) before this assertion existed -- see the Task 9 report.
    assert items[0]["local_root"] == str(tmp_path)


def _assert_r8(items):
    """rpfarm.sync's R8 invariant, the way rclone_copy's build_rclone_args
    checks it: relpath(local, local_root) joined onto remote_root must
    reproduce remote, for every file in every item this function returns.
    """
    import posixpath

    for it in items:
        for local, remote, _size in it["files"]:
            rel = os.path.relpath(local, it["local_root"]).replace(os.sep, "/")
            assert posixpath.join(it["remote_root"], rel) == remote, (it, local, remote)


def test_custom_items_satisfy_r8_dir_and_file_pairs(tmp_path):
    src = tmp_path / "plug"
    src.mkdir()
    (src / "a.so").write_bytes(b"1" * 10)
    tar = tmp_path / "houdini-22.0.393-linux_x86_64_gcc14.2.tar.gz"
    tar.write_bytes(b"x" * 10)
    items = build_upload_items(
        "custom",
        str(tmp_path),
        "may",
        "shot",
        [(str(src), "/workspace/apps/plug"), (str(tar), "/workspace/apps/dist/")],
        [],
        package_gb=1,
    )
    _assert_r8(items)


def test_deps_items_satisfy_r8(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    (job / "x.hip").write_bytes(b"h")
    ext_dir = tmp_path / "lib"
    ext_dir.mkdir()
    ext_file = ext_dir / "tex.exr"
    ext_file.write_bytes(b"e" * 20)
    items = build_upload_items(
        "deps", str(job), "may", "shot", [], [str(job / "x.hip"), str(ext_file)], package_gb=1
    )
    _assert_r8(items)


def test_custom_items_splits_by_package_size(tmp_path):
    src = tmp_path / "plug"
    src.mkdir()
    (src / "a.bin").write_bytes(b"1" * 700)
    (src / "b.bin").write_bytes(b"2" * 700)
    items = build_upload_items(
        "custom", str(tmp_path), "may", "shot", [(str(src), "/workspace/apps/plug")], [], package_gb=1000 / 2**30
    )
    assert len(items) == 2
    assert all(it["bytes"] <= 1000 for it in items)
    assert [it["index"] for it in items] == [0, 1]


def test_custom_items_multiple_pairs_are_separate_groups(tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    (a / "x.so").write_bytes(b"1" * 10)
    b = tmp_path / "b"
    b.mkdir()
    (b / "y.so").write_bytes(b"2" * 10)
    items = build_upload_items(
        "custom",
        str(tmp_path),
        "may",
        "shot",
        [(str(a), "/workspace/apps/a"), (str(b), "/workspace/apps/b")],
        [],
        package_gb=1,
    )
    assert {it["remote_root"] for it in items} == {"/workspace/apps/a", "/workspace/apps/b"}


# -- build_upload_items: deps mode -------------------------------------------


def test_deps_items_use_project_root(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    (job / "x.hip").write_bytes(b"h")
    items = build_upload_items("deps", str(job), "may", "shot", [], [str(job / "x.hip")], package_gb=1)
    assert items[0]["remote_root"] == "/workspace/projects/may/shot" and items[0]["files"][0][1].endswith("/shot/x.hip")
    assert items[0]["local_root"] == str(job)


def test_deps_items_group_external_refs_separately(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    (job / "x.hip").write_bytes(b"h")
    ext_dir = tmp_path / "lib"
    ext_dir.mkdir()
    ext_file = ext_dir / "tex.exr"
    ext_file.write_bytes(b"e" * 20)
    items = build_upload_items(
        "deps", str(job), "may", "shot", [], [str(job / "x.hip"), str(ext_file)], package_gb=1
    )
    roots = {it["remote_root"] for it in items}
    assert "/workspace/projects/may/shot" in roots
    assert any(r.startswith("/workspace/projects/may/shot/_ext") for r in roots)


def test_build_upload_items_rejects_unknown_mode(tmp_path):
    with pytest.raises(ValueError):
        build_upload_items("bogus", str(tmp_path), "may", "shot", [], [], package_gb=1)


# -- houdini_install_preset ---------------------------------------------------


def test_houdini_preset():
    pairs, post = houdini_install_preset("/dl/houdini-22.0.393-linux_x86_64_gcc14.2.tar.gz", "22.0.393")
    assert pairs == [("/dl/houdini-22.0.393-linux_x86_64_gcc14.2.tar.gz", "/workspace/apps/dist/")]
    assert "--install-dir /workspace/houdini/22.0.393" in post and "tar xzf" in post
    assert "houdini-22.0.393-linux_x86_64_gcc14.2.tar.gz" in post
    assert "--accept-EULA" in post


def test_houdini_preset_pair_feeds_build_upload_items(tmp_path):
    tar = tmp_path / "houdini-22.0.393-linux_x86_64_gcc14.2.tar.gz"
    tar.write_bytes(b"x" * 10)
    pairs, post = houdini_install_preset(str(tar), "22.0.393")
    items = build_upload_items("custom", str(tmp_path), "may", "shot", pairs, [], package_gb=1)
    assert items[0]["files"][0][1] == "/workspace/apps/dist/" + tar.name


# -- resolve_compress_flag ----------------------------------------------------


def test_resolve_compress_flag_explicit():
    assert resolve_compress_flag("on") is True
    assert resolve_compress_flag("off") is False


def test_resolve_compress_flag_auto():
    assert resolve_compress_flag("auto", measured_mbps=50) is True
    assert resolve_compress_flag("auto", measured_mbps=500) is False
    assert resolve_compress_flag("auto", measured_mbps=None) is True


# -- write_pathmap -------------------------------------------------------------


def test_write_pathmap(tmp_path):
    write_pathmap(str(tmp_path), {"/local/a": "/workspace/a", "/local/b": "/workspace/b"})
    with open(tmp_path / ".rpfarm_pathmap.json") as f:
        data = json.load(f)
    assert data == {"/local/a": "/workspace/a", "/local/b": "/workspace/b"}


# -- run_upload_item -----------------------------------------------------------


class FakeCfg:
    rclone_path = "/bin/true"


class FakeSyncClient:
    def __init__(self, fail_commands=None):
        self.commands = []
        self.calls = []  # (command, timeout_s), in call order
        self.fail_commands = fail_commands or set()

    def exec(self, command, timeout_s=600):
        self.commands.append(command)
        self.calls.append((command, timeout_s))
        if command in self.fail_commands:
            return {"exit_code": 1, "stdout": "", "stderr": "boom: " + command}
        return {"exit_code": 0, "stdout": "", "stderr": ""}


def test_run_upload_item_no_compress(tmp_path, monkeypatch):
    src = tmp_path / "f.txt"
    src.write_bytes(b"x" * 10)
    item = {
        "index": 0,
        "local_root": str(tmp_path),
        "remote_root": "/workspace/apps/plug",
        "files": [[str(src), "/workspace/apps/plug/f.txt", 10]],
        "bytes": 10,
        "post_command": "",
    }

    calls = []

    def fake_rclone_copy(package, target, direction, rclone_bin, local_root, remote_root, progress_cb=None):
        calls.append((direction, local_root, remote_root, len(package)))
        from rpfarm.sync import SyncStats

        return SyncStats(files=len(package), bytes=sum(e.size for e in package), seconds=0.1)

    monkeypatch.setattr("rpfarm.packages.rclone_copy", fake_rclone_copy)

    sync_client = FakeSyncClient()
    stats = run_upload_item(
        item,
        FakeCfg(),
        SftpTarget(host="1.2.3.4", port=22, key_path="/k"),
        sync_client,
        compress=False,
        progress_cb=None,
    )
    assert stats["files"] == 1 and stats["bytes"] == 10 and stats["seconds"] == pytest.approx(0.1)
    assert calls == [("up", str(tmp_path), "/workspace/apps/plug", 1)]
    assert any("sync_last_used" in c for c in sync_client.commands)


def test_run_upload_item_runs_post_command(tmp_path, monkeypatch):
    src = tmp_path / "f.txt"
    src.write_bytes(b"x" * 10)
    item = {
        "index": 0,
        "local_root": str(tmp_path),
        "remote_root": "/workspace/apps/dist/",
        "files": [[str(src), "/workspace/apps/dist/f.txt", 10]],
        "bytes": 10,
        "post_command": "echo installed",
    }

    def fake_rclone_copy(package, target, direction, rclone_bin, local_root, remote_root, progress_cb=None):
        from rpfarm.sync import SyncStats

        return SyncStats(files=len(package), bytes=10, seconds=0.1)

    monkeypatch.setattr("rpfarm.packages.rclone_copy", fake_rclone_copy)

    sync_client = FakeSyncClient()
    run_upload_item(
        item, FakeCfg(), SftpTarget(host="1.2.3.4", port=22, key_path="/k"), sync_client, compress=False, progress_cb=None
    )
    assert "echo installed" in sync_client.commands


def test_run_upload_item_compress_runs_two_transfers_and_decompress(tmp_path, monkeypatch):
    src = tmp_path / "scene.hip"
    src.write_bytes(b"x" * 10)
    item = {
        "index": 0,
        "local_root": str(tmp_path),
        "remote_root": "/workspace/projects/may/shot",
        "files": [[str(src), "/workspace/projects/may/shot/scene.hip", 10]],
        "bytes": 10,
        "post_command": "",
    }

    calls = []

    def fake_rclone_copy(package, target, direction, rclone_bin, local_root, remote_root, progress_cb=None):
        calls.append((local_root, remote_root, len(package)))
        from rpfarm.sync import SyncStats

        return SyncStats(files=len(package), bytes=sum(e.size for e in package), seconds=0.1)

    def fake_compress_stage(package, staging_dir, remote_root, level=3):
        # nothing left uncompressed, one staged entry, decompress post command
        staged = [FileEntry(local=os.path.join(str(staging_dir), "scene.hip.zst"), remote=package[0].remote + ".zst", size=5)]
        return [], staged, "cd {} && zstd -d --rm scene.hip.zst".format(remote_root)

    monkeypatch.setattr("rpfarm.packages.rclone_copy", fake_rclone_copy)
    monkeypatch.setattr("rpfarm.packages.compress_stage", fake_compress_stage)

    sync_client = FakeSyncClient()
    stats = run_upload_item(
        item, FakeCfg(), SftpTarget(host="1.2.3.4", port=22, key_path="/k"), sync_client, compress=True, progress_cb=None
    )
    # only the staged package transferred (raw_package empty), and the decompress ran
    assert len(calls) == 1
    assert calls[0][0] != str(tmp_path)  # staged local_root is the staging dir, not local_root
    assert any("zstd -d --rm" in c for c in sync_client.commands)
    assert stats["bytes"] == 5


# -- run_upload_item: exec() exit_code must not be swallowed (fix round 2) ----


def _no_op_rclone_copy(monkeypatch):
    def fake(package, target, direction, rclone_bin, local_root, remote_root, progress_cb=None):
        from rpfarm.sync import SyncStats

        return SyncStats(files=len(package), bytes=sum(e.size for e in package), seconds=0.1)

    monkeypatch.setattr("rpfarm.packages.rclone_copy", fake)


def test_run_upload_item_raises_on_post_command_failure(tmp_path, monkeypatch):
    src = tmp_path / "f.txt"
    src.write_bytes(b"x" * 10)
    item = {
        "index": 0,
        "local_root": str(tmp_path),
        "remote_root": "/workspace/apps/dist/",
        "files": [[str(src), "/workspace/apps/dist/f.txt", 10]],
        "bytes": 10,
        "post_command": "false",
    }
    _no_op_rclone_copy(monkeypatch)
    sync_client = FakeSyncClient(fail_commands={"false"})

    with pytest.raises(RuntimeError, match="boom"):
        run_upload_item(
            item, FakeCfg(), SftpTarget(host="1.2.3.4", port=22, key_path="/k"), sync_client, compress=False, progress_cb=None
        )


def test_run_upload_item_raises_on_decompress_failure(tmp_path, monkeypatch):
    src = tmp_path / "scene.hip"
    src.write_bytes(b"x" * 10)
    item = {
        "index": 0,
        "local_root": str(tmp_path),
        "remote_root": "/workspace/projects/may/shot",
        "files": [[str(src), "/workspace/projects/may/shot/scene.hip", 10]],
        "bytes": 10,
        "post_command": "",
    }
    _no_op_rclone_copy(monkeypatch)

    decompress_cmd = "cd /workspace/projects/may/shot && zstd -d --rm scene.hip.zst"

    def fake_compress_stage(package, staging_dir, remote_root, level=3):
        staged = [FileEntry(local=os.path.join(str(staging_dir), "scene.hip.zst"), remote=package[0].remote + ".zst", size=5)]
        return [], staged, decompress_cmd

    monkeypatch.setattr("rpfarm.packages.compress_stage", fake_compress_stage)
    sync_client = FakeSyncClient(fail_commands={decompress_cmd})

    with pytest.raises(RuntimeError, match="zstd"):
        run_upload_item(
            item, FakeCfg(), SftpTarget(host="1.2.3.4", port=22, key_path="/k"), sync_client, compress=True, progress_cb=None
        )


def test_run_upload_item_scales_exec_timeout_from_item_bytes(tmp_path, monkeypatch):
    src = tmp_path / "f.txt"
    src.write_bytes(b"x" * 10)
    big_bytes = 200 * 2**30  # 200 GB -- should push well past the 600s floor
    item = {
        "index": 0,
        "local_root": str(tmp_path),
        "remote_root": "/workspace/apps/dist/",
        "files": [[str(src), "/workspace/apps/dist/f.txt", 10]],
        "bytes": big_bytes,
        "post_command": "echo installed",
    }
    _no_op_rclone_copy(monkeypatch)
    sync_client = FakeSyncClient()

    run_upload_item(
        item, FakeCfg(), SftpTarget(host="1.2.3.4", port=22, key_path="/k"), sync_client, compress=False, progress_cb=None
    )
    post_calls = [t for c, t in sync_client.calls if c == "echo installed"]
    assert len(post_calls) == 1 and post_calls[0] > 600


def test_run_upload_item_post_command_timeout_has_a_floor(tmp_path, monkeypatch):
    src = tmp_path / "f.txt"
    src.write_bytes(b"x" * 10)
    item = {
        "index": 0,
        "local_root": str(tmp_path),
        "remote_root": "/workspace/apps/dist/",
        "files": [[str(src), "/workspace/apps/dist/f.txt", 10]],
        "bytes": 10,
        "post_command": "echo installed",
    }
    _no_op_rclone_copy(monkeypatch)
    sync_client = FakeSyncClient()

    run_upload_item(
        item, FakeCfg(), SftpTarget(host="1.2.3.4", port=22, key_path="/k"), sync_client, compress=False, progress_cb=None
    )
    post_calls = [t for c, t in sync_client.calls if c == "echo installed"]
    assert post_calls == [600]


# -- _group_by_pathmap: realpath fallback for a symlinked $JOB (fix round 2) --


def test_group_by_pathmap_matches_via_realpath_for_symlinked_root(tmp_path):
    from rpfarm.packages import _group_by_pathmap

    real_dir = tmp_path / "real_job"
    real_dir.mkdir()
    link_dir = tmp_path / "job_link"
    link_dir.symlink_to(real_dir)

    # resolve_entries would store FileEntry.local through the symlinked
    # path (its docstring: "FileEntry.local keeps the original normalized
    # (not realpath'd) path"), but the path_map key can end up being the
    # job_dir the caller passed in -- here, deliberately, the *real* path,
    # so a literal-prefix match against the entry's symlinked local fails
    # and only a realpath comparison finds it. A second, unrelated but
    # LONGER-named root is included so a naive "fall back to some root
    # rather than drop the entry" implementation (sorted longest-first)
    # would silently misattribute this entry to it instead -- catching
    # exactly the bug this test is for, not just exercising the code path.
    decoy_root = str(tmp_path / "an_unrelated_decoy_root_with_a_long_name")
    entry = FileEntry(local=str(link_dir / "x.hip"), remote="/workspace/projects/may/shot/x.hip", size=1)
    path_map = {
        decoy_root: "/workspace/projects/someone_else/decoy",
        str(real_dir): "/workspace/projects/may/shot",
    }

    groups = _group_by_pathmap([entry], path_map)
    assert groups == [(str(real_dir), "/workspace/projects/may/shot", [entry])]


def test_group_by_pathmap_raises_for_entry_outside_every_root(tmp_path):
    from rpfarm.packages import _group_by_pathmap

    entry = FileEntry(local="/completely/unrelated/path.hip", remote="/workspace/projects/may/shot/path.hip", size=1)
    path_map = {str(tmp_path / "job"): "/workspace/projects/may/shot"}

    with pytest.raises(ValueError, match="not under any"):
        _group_by_pathmap([entry], path_map)
