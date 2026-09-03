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
    def __init__(self):
        self.commands = []

    def exec(self, command, timeout_s=600):
        self.commands.append(command)
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
