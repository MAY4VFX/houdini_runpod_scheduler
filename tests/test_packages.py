import json
import os

import pytest

from rpfarm.packages import (
    build_download_items,
    build_upload_items,
    group_download_pairs,
    houdini_install_preset,
    localize_via_pathmap,
    maybe_grow_volume,
    resolve_compress_flag,
    run_download_item,
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


# -- localize_via_pathmap --------------------------------------------------------


def test_localize_via_pathmap_basic():
    path_map = {"/job": "/workspace/projects/may/shot"}
    assert (
        localize_via_pathmap("/workspace/projects/may/shot/render/a.exr", path_map)
        == "/job/render/a.exr"
    )


def test_localize_via_pathmap_exact_root_match():
    path_map = {"/job": "/workspace/projects/may/shot"}
    assert localize_via_pathmap("/workspace/projects/may/shot", path_map) == "/job"


def test_localize_via_pathmap_longest_prefix_wins():
    path_map = {
        "/job": "/workspace/projects/may/shot",
        "/job/render": "/workspace/projects/may/shot/render",
    }
    # Both entries' farm prefixes match; the longer (more specific) one wins.
    assert (
        localize_via_pathmap("/workspace/projects/may/shot/render/a.exr", path_map)
        == "/job/render/a.exr"
    )


def test_localize_via_pathmap_no_match_returns_none():
    path_map = {"/job": "/workspace/projects/may/shot"}
    assert localize_via_pathmap("/workspace/projects/someone_else/other", path_map) is None


def test_localize_via_pathmap_does_not_match_unrelated_sibling_prefix():
    # "/workspace/projects/may/shot2" must not match the "/shot" entry just
    # because it shares a string prefix -- only a real path-segment boundary
    # counts (the trailing "/" check in the implementation).
    path_map = {"/job": "/workspace/projects/may/shot"}
    assert localize_via_pathmap("/workspace/projects/may/shot2/x", path_map) is None


# -- group_download_pairs ------------------------------------------------------


def test_group_download_pairs_single_pair():
    pairs = [("/workspace/projects/may/shot/render/a.exr", "/job/render/a.exr")]
    groups = group_download_pairs(pairs, {"/workspace/projects/may/shot/render/a.exr": 5})
    assert groups == [
        (
            "/job/render",
            "/workspace/projects/may/shot/render",
            [FileEntry(local="/job/render/a.exr", remote="/workspace/projects/may/shot/render/a.exr", size=5)],
        )
    ]


def test_group_download_pairs_groups_by_directory_pair():
    pairs = [
        ("/workspace/projects/may/shot/render/a.exr", "/job/render/a.exr"),
        ("/workspace/projects/may/shot/render/b.exr", "/job/render/b.exr"),
        ("/workspace/projects/may/shot/logs/c.log", "/job/logs/c.log"),
    ]
    groups = group_download_pairs(pairs)
    keys = {(local_root, remote_root) for local_root, remote_root, _ in groups}
    assert keys == {
        ("/job/render", "/workspace/projects/may/shot/render"),
        ("/job/logs", "/workspace/projects/may/shot/logs"),
    }
    render_group = next(g for g in groups if g[0] == "/job/render")
    assert {e.remote for e in render_group[2]} == {
        "/workspace/projects/may/shot/render/a.exr",
        "/workspace/projects/may/shot/render/b.exr",
    }


def test_group_download_pairs_defaults_missing_size_to_zero():
    pairs = [("/workspace/projects/may/shot/render/a.exr", "/job/render/a.exr")]
    groups = group_download_pairs(pairs)
    assert groups[0][2][0].size == 0
    groups_partial = group_download_pairs(pairs, {"/some/other/remote": 99})
    assert groups_partial[0][2][0].size == 0


# -- build_download_items -------------------------------------------------------


def test_download_items_custom_single_file():
    items = build_download_items(
        "custom",
        [("/workspace/projects/may/shot/render/a.exr", "/job/render/a.exr")],
        1,
        {"/workspace/projects/may/shot/render/a.exr": 5},
    )
    assert len(items) == 1
    assert items[0]["files"] == [["/job/render/a.exr", "/workspace/projects/may/shot/render/a.exr", 5]]
    assert items[0]["local_root"] == "/job/render"
    assert items[0]["remote_root"] == "/workspace/projects/may/shot/render"
    assert items[0]["bytes"] == 5
    assert items[0]["index"] == 0
    assert items[0]["post_command"] == ""


def test_download_items_groups_different_folders_separately():
    pairs = [
        ("/workspace/projects/may/shot/render/a.exr", "/job/render/a.exr"),
        ("/workspace/projects/may/shot/logs/run.log", "/job/logs/run.log"),
    ]
    sizes = {
        "/workspace/projects/may/shot/render/a.exr": 10,
        "/workspace/projects/may/shot/logs/run.log": 20,
    }
    items = build_download_items("outputs", pairs, 1, sizes)
    roots = {it["remote_root"] for it in items}
    assert roots == {"/workspace/projects/may/shot/render", "/workspace/projects/may/shot/logs"}


def test_download_items_splits_by_package_size():
    pairs = [
        ("/workspace/projects/may/shot/render/a.exr", "/job/render/a.exr"),
        ("/workspace/projects/may/shot/render/b.exr", "/job/render/b.exr"),
    ]
    sizes = {
        "/workspace/projects/may/shot/render/a.exr": 700,
        "/workspace/projects/may/shot/render/b.exr": 700,
    }
    items = build_download_items("outputs", pairs, 1000 / 2**30, sizes)
    assert len(items) == 2
    assert all(it["bytes"] <= 1000 for it in items)
    assert [it["index"] for it in items] == [0, 1]


def test_download_items_no_sizes_defaults_to_zero_bytes():
    pairs = [("/workspace/projects/may/shot/render/a.exr", "/job/render/a.exr")]
    items = build_download_items("outputs", pairs, 1)
    assert items[0]["bytes"] == 0
    assert items[0]["files"] == [["/job/render/a.exr", "/workspace/projects/may/shot/render/a.exr", 0]]


def test_build_download_items_rejects_unknown_mode():
    with pytest.raises(ValueError, match="unknown download mode"):
        build_download_items("bogus", [], 1)


# -- run_download_item ----------------------------------------------------------


def test_run_download_item_no_files(tmp_path, monkeypatch):
    item = {"index": 0, "local_root": str(tmp_path), "remote_root": "/workspace/x", "files": [], "bytes": 0}

    def fake_rclone_copy(*a, **kw):
        raise AssertionError("rclone_copy should not be called for an empty item")

    monkeypatch.setattr("rpfarm.packages.rclone_copy", fake_rclone_copy)
    sync_client = FakeSyncClient()
    stats = run_download_item(
        item, FakeCfg(), SftpTarget(host="1.2.3.4", port=22, key_path="/k"), sync_client, "newer", progress_cb=None
    )
    assert stats == {"files": 0, "bytes": 0, "seconds": 0.0}
    assert any("sync_last_used" in c for c in sync_client.commands)


def test_run_download_item_newer_uses_update_flag(tmp_path, monkeypatch):
    dst_root = tmp_path / "render"
    item = {
        "index": 0,
        "local_root": str(dst_root),
        "remote_root": "/workspace/projects/may/shot/render",
        "files": [[str(dst_root / "a.exr"), "/workspace/projects/may/shot/render/a.exr", 10]],
        "bytes": 10,
    }
    calls = []

    def fake_rclone_copy(package, target, direction, rclone_bin, local_root, remote_root, progress_cb=None, extra_args=()):
        calls.append((direction, local_root, remote_root, tuple(extra_args)))
        from rpfarm.sync import SyncStats

        return SyncStats(files=len(package), bytes=sum(e.size for e in package), seconds=0.1)

    monkeypatch.setattr("rpfarm.packages.rclone_copy", fake_rclone_copy)
    sync_client = FakeSyncClient()
    stats = run_download_item(
        item, FakeCfg(), SftpTarget(host="1.2.3.4", port=22, key_path="/k"), sync_client, "newer", progress_cb=None
    )
    assert stats == {"files": 1, "bytes": 10, "seconds": pytest.approx(0.1)}
    assert calls == [("down", str(dst_root), "/workspace/projects/may/shot/render", ("--update",))]
    assert os.path.isdir(dst_root)


def test_run_download_item_always_has_no_extra_flag(tmp_path, monkeypatch):
    item = {
        "index": 0,
        "local_root": str(tmp_path),
        "remote_root": "/workspace/x",
        "files": [[str(tmp_path / "a"), "/workspace/x/a", 1]],
        "bytes": 1,
    }
    calls = []

    def fake_rclone_copy(package, target, direction, rclone_bin, local_root, remote_root, progress_cb=None, extra_args=()):
        calls.append(tuple(extra_args))
        from rpfarm.sync import SyncStats

        return SyncStats(files=len(package), bytes=1, seconds=0.1)

    monkeypatch.setattr("rpfarm.packages.rclone_copy", fake_rclone_copy)
    run_download_item(
        item, FakeCfg(), SftpTarget(host="1.2.3.4", port=22, key_path="/k"), FakeSyncClient(), "always", progress_cb=None
    )
    assert calls == [()]


def test_run_download_item_never_uses_ignore_existing(tmp_path, monkeypatch):
    item = {
        "index": 0,
        "local_root": str(tmp_path),
        "remote_root": "/workspace/x",
        "files": [[str(tmp_path / "a"), "/workspace/x/a", 1]],
        "bytes": 1,
    }
    calls = []

    def fake_rclone_copy(package, target, direction, rclone_bin, local_root, remote_root, progress_cb=None, extra_args=()):
        calls.append(tuple(extra_args))
        from rpfarm.sync import SyncStats

        return SyncStats(files=len(package), bytes=1, seconds=0.1)

    monkeypatch.setattr("rpfarm.packages.rclone_copy", fake_rclone_copy)
    run_download_item(
        item, FakeCfg(), SftpTarget(host="1.2.3.4", port=22, key_path="/k"), FakeSyncClient(), "never", progress_cb=None
    )
    assert calls == [("--ignore-existing",)]


def test_run_download_item_rejects_unknown_overwrite(tmp_path):
    item = {"index": 0, "local_root": str(tmp_path), "remote_root": "/workspace/x", "files": [], "bytes": 0}
    with pytest.raises(ValueError, match="unknown overwrite mode"):
        run_download_item(
            item, FakeCfg(), SftpTarget(host="1.2.3.4", port=22, key_path="/k"), FakeSyncClient(), "bogus"
        )


# -- touch of the project index -------------------------------------------


def test_run_upload_item_touches_project_index(tmp_path, monkeypatch):
    src = tmp_path / "f.txt"
    src.write_bytes(b"x" * 10)
    item = {
        "index": 0,
        "local_root": str(tmp_path),
        "remote_root": "/workspace/projects/may/shotA",
        "files": [[str(src), "/workspace/projects/may/shotA/f.txt", 10]],
        "bytes": 10,
        "post_command": "",
    }

    def fake_rclone_copy(package, target, direction, rclone_bin, local_root, remote_root, progress_cb=None):
        from rpfarm.sync import SyncStats

        return SyncStats(files=len(package), bytes=10, seconds=0.1)

    monkeypatch.setattr("rpfarm.packages.rclone_copy", fake_rclone_copy)
    sync_client = FakeSyncClient()
    run_upload_item(
        item, FakeCfg(), SftpTarget(host="1.2.3.4", port=22, key_path="/k"), sync_client, compress=False, progress_cb=None
    )
    assert any(
        c == "python3 /opt/rpfarm/housekeeping.py touch may/shotA --event upload" for c in sync_client.commands
    )


def test_run_upload_item_skips_touch_outside_projects(tmp_path, monkeypatch):
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

    def fake_rclone_copy(package, target, direction, rclone_bin, local_root, remote_root, progress_cb=None):
        from rpfarm.sync import SyncStats

        return SyncStats(files=len(package), bytes=10, seconds=0.1)

    monkeypatch.setattr("rpfarm.packages.rclone_copy", fake_rclone_copy)
    sync_client = FakeSyncClient()
    run_upload_item(
        item, FakeCfg(), SftpTarget(host="1.2.3.4", port=22, key_path="/k"), sync_client, compress=False, progress_cb=None
    )
    assert not any("housekeeping.py touch" in c for c in sync_client.commands)


def test_run_download_item_touches_project_index(tmp_path, monkeypatch):
    dst_root = tmp_path / "out"
    item = {
        "index": 0,
        "local_root": str(dst_root),
        "remote_root": "/workspace/projects/may/shotA/render",
        "files": [["/workspace/projects/may/shotA/render/f.exr", str(dst_root / "f.exr"), 10]],
        "bytes": 10,
    }

    def fake_rclone_copy(package, target, direction, rclone_bin, local_root, remote_root, progress_cb=None, extra_args=()):
        from rpfarm.sync import SyncStats

        return SyncStats(files=len(package), bytes=10, seconds=0.1)

    monkeypatch.setattr("rpfarm.packages.rclone_copy", fake_rclone_copy)
    sync_client = FakeSyncClient()
    run_download_item(
        item, FakeCfg(), SftpTarget(host="1.2.3.4", port=22, key_path="/k"), sync_client, "newer", progress_cb=None
    )
    assert any(
        c == "python3 /opt/rpfarm/housekeeping.py touch may/shotA --event download" for c in sync_client.commands
    )


# -- maybe_grow_volume ------------------------------------------------------


class _LsSyncClient:
    def __init__(self, used, total, exit_code=0):
        self.used = used
        self.total = total
        self.exit_code = exit_code
        self.commands = []

    def exec(self, command, timeout_s=600):
        self.commands.append(command)
        if self.exit_code != 0:
            return {"exit_code": self.exit_code, "stdout": "", "stderr": "boom"}
        payload = json.dumps({"volume": {"used": self.used, "total": self.total}})
        return {"exit_code": 0, "stdout": payload, "stderr": ""}


class _GrowCfg:
    volume_id = "vol1"


class _FakeApi:
    def __init__(self, size_gb):
        self.size_gb = size_gb
        self.resized = []

    def get_volume(self, vid):
        return {"size": self.size_gb}

    def resize_volume(self, vid, size_gb):
        self.resized.append((vid, size_gb))


def test_maybe_grow_volume_grows_past_threshold():
    gb = 2**30
    api = _FakeApi(size_gb=50)
    sync_client = _LsSyncClient(used=44 * gb, total=50 * gb)
    maybe_grow_volume(api, _GrowCfg(), sync_client, needed_bytes=2 * gb)
    assert api.resized == [("vol1", 60)]  # ceil(46/0.8/10)*10 = 60


def test_maybe_grow_volume_no_op_under_threshold():
    gb = 2**30
    api = _FakeApi(size_gb=50)
    sync_client = _LsSyncClient(used=10 * gb, total=50 * gb)
    maybe_grow_volume(api, _GrowCfg(), sync_client, needed_bytes=1 * gb)
    assert api.resized == []


def test_maybe_grow_volume_never_shrinks_or_repeats():
    gb = 2**30
    api = _FakeApi(size_gb=100)  # already bigger than the computed target
    sync_client = _LsSyncClient(used=44 * gb, total=50 * gb)
    maybe_grow_volume(api, _GrowCfg(), sync_client, needed_bytes=2 * gb)
    assert api.resized == []


def test_maybe_grow_volume_swallows_ls_failure():
    api = _FakeApi(size_gb=50)
    sync_client = _LsSyncClient(used=0, total=0, exit_code=1)
    logs = []
    maybe_grow_volume(api, _GrowCfg(), sync_client, needed_bytes=1, log=logs.append)
    assert api.resized == []
    assert logs  # the failure was logged, not raised


def test_maybe_grow_volume_swallows_api_exception():
    class ExplodingApi:
        def get_volume(self, vid):
            raise RuntimeError("network down")

    gb = 2**30
    sync_client = _LsSyncClient(used=44 * gb, total=50 * gb)
    logs = []
    maybe_grow_volume(ExplodingApi(), _GrowCfg(), sync_client, needed_bytes=2 * gb, log=logs.append)
    assert logs
