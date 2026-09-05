import json
import os
import pathlib

import rpfarm.packages as rppkg

import pytest

from rpfarm.packages import (
    SYNC_TOUCH_COMMAND,
    build_download_items,
    build_upload_items,
    delocalize_via_pathmap,
    get_volume_size_gb,
    group_download_pairs,
    houdini_install_preset,
    localize_via_pathmap,
    map_output_pair,
    maybe_grow_volume,
    parse_stat_sizes,
    resolve_compress_flag,
    result_data_path,
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
    assert "tar xzf" in post
    assert "houdini-22.0.393-linux_x86_64_gcc14.2.tar.gz" in post
    assert "--accept-EULA 2021-10-13" in post


def test_houdini_preset_install_dir_is_positional_and_last():
    """Task 14, against the real 22.0.393 installer: ``houdini.install``
    has no ``--install-dir`` flag (usage is ``[options] [directory]``) and
    its option loop breaks on the first unknown token, so the target must
    be the final word of the invocation, with ``--make-dir`` to create it.
    """
    _pairs, post = houdini_install_preset("/dl/houdini-22.0.393-linux_x86_64_gcc14.2.tar.gz", "22.0.393")
    assert "--install-dir" not in post
    install = post.split("./houdini.install ", 1)[1].split(" && ", 1)[0]
    assert install.split()[-1] == "/workspace/houdini/22.0.393"
    assert "--make-dir /workspace/houdini/22.0.393" in install


def test_houdini_preset_disables_avahi_and_bin_symlink():
    """As root the installer defaults to installing Avahi via apt-get, and
    a bin symlink into /usr/local/bin; neither belongs on a farm pod."""
    _pairs, post = houdini_install_preset("/dl/houdini-22.0.393-linux_x86_64_gcc14.2.tar.gz", "22.0.393")
    assert "--no-install-avahi" in post
    assert "--no-install-bin-symlink" in post


def test_houdini_preset_accepts_the_install_by_running_it_not_by_ls():
    """Task 14 review, Critical #1. The preset used to end in
    `ls <dir>/bin/hython`, and that check PASSED on the broken install: an
    interrupted run left 11GB with `bin/hython` present but no `python/`,
    so the binary existed and died on startup with
    "libpython3.13.so.1.0: cannot open shared object file".

    Acceptance must therefore execute Houdini and check what it reports.
    """
    _pairs, post = houdini_install_preset("/dl/houdini-22.0.393-linux_x86_64_gcc14.2.tar.gz", "22.0.393")

    # the old existence-only check is gone as the acceptance step
    assert not post.rstrip().endswith("ls /workspace/houdini/22.0.393/bin/hython")

    check = post.split("invalidate houdini && ", 1)[1]
    assert "import hou" in check and "applicationVersionString" in check
    assert "./bin/hython" in check
    assert 'source ./houdini_setup_bash' in check  # the install we just made, not the pod's HFS
    assert '[ "$ver" = "22.0.393" ]' in check      # exact version, not merely "it started"
    assert "exit 1" in check                       # and it fails loudly


def test_houdini_preset_version_check_runs_last(tmp_path):
    """The check has to be the final step -- a verification that runs before
    the install finishes proves nothing."""
    _pairs, post = houdini_install_preset("/dl/houdini-22.0.393-linux_x86_64_gcc14.2.tar.gz", "22.0.393")
    assert post.index("./houdini.install") < post.index("applicationVersionString")
    assert post.index("invalidate houdini") < post.index("applicationVersionString")


def test_houdini_preset_version_check_shell_is_valid_and_gates_on_the_result(tmp_path):
    """Runs the generated check for real against a stub hython: it must pass
    a healthy install, and fail both a hython that cannot start (the Task 14
    breakage) and one reporting the wrong version."""
    import subprocess

    install_dir = tmp_path / "22.0.393"
    (install_dir / "bin").mkdir(parents=True)
    (install_dir / "houdini_setup_bash").write_text("")
    hython = install_dir / "bin" / "hython"

    _pairs, post = houdini_install_preset("/dl/houdini-22.0.393-linux_x86_64_gcc14.2.tar.gz", "22.0.393")
    check = post.split("invalidate houdini && ", 1)[1].replace("/workspace/houdini/22.0.393", str(install_dir))

    def run():
        return subprocess.run(["bash", "-c", check], capture_output=True, text=True)

    # healthy: a warning line first, then the version -- exactly what the real
    # hython prints ("opalias: ... is not a known operator")
    hython.write_text("#!/bin/bash\necho 'opalias: not a known operator.'\necho 22.0.393\n")
    hython.chmod(0o755)
    assert run().returncode == 0

    # broken install: binary present, cannot start
    hython.write_text("#!/bin/bash\necho 'libpython3.13.so.1.0: cannot open shared object file' >&2\nexit 127\n")
    hython.chmod(0o755)
    failed = run()
    assert failed.returncode != 0 and "install check FAILED" in failed.stderr

    # wrong version on the volume
    hython.write_text("#!/bin/bash\necho 20.5.684\n")
    hython.chmod(0o755)
    wrong = run()
    assert wrong.returncode != 0 and "20.5.684" in wrong.stderr


def test_houdini_preset_removes_uploaded_tarball_after_success():
    """The ~4.3GB tarball is dead weight on the volume once installed, but
    only removed on success (an && chain) so a failed install can be
    retried without re-uploading it."""
    _pairs, post = houdini_install_preset("/dl/houdini-22.0.393-linux_x86_64_gcc14.2.tar.gz", "22.0.393")
    rm = "rm -f /workspace/apps/dist/houdini-22.0.393-linux_x86_64_gcc14.2.tar.gz"
    assert rm in post
    assert post.index("./houdini.install") < post.index(rm)


def test_houdini_preset_invalidates_houdini_zone_cache(tmp_path):
    """Fix round 3, "2": an install adds bytes to the houdini zone but
    doesn't touch a project, so cmd_touch's upload-invalidation (fix
    round 2, "B") never fires for it -- the preset must invalidate that
    zone itself, and it must run *before* the final proof step
    (an install that fails partway shouldn't leave the cache invalidated
    based on a step that never actually ran)."""
    _pairs, post = houdini_install_preset("/dl/houdini-22.0.393-linux_x86_64_gcc14.2.tar.gz", "22.0.393")
    assert "housekeeping.py invalidate houdini" in post
    assert post.index("invalidate houdini") < post.index("applicationVersionString")


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
    """Stands in for :class:`rpfarm.worker_client.WorkerClient`.

    Implements both paths so tests can tell them apart: `exec` (short,
    synchronous) and `exec_wait` (detached + polled, Ruling R31). Long
    commands must take the second one -- see
    `test_run_upload_item_runs_post_command_detached_not_synchronously`.
    """

    def __init__(self, fail_commands=None, fail_output=None):
        self.commands = []
        self.calls = []  # (command, timeout_s), in call order
        self.detached = []  # (command, deadline_s) that went the detached way
        self.fail_commands = fail_commands or set()
        self.fail_output = fail_output

    def exec(self, command, timeout_s=600):
        self.commands.append(command)
        self.calls.append((command, timeout_s))
        if command in self.fail_commands:
            return {"exit_code": 1, "stdout": "", "stderr": "boom: " + command}
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    def exec_wait(self, command, deadline_s, **kw):
        """Mirrors the real detached contract: the pod merges stdout+stderr
        into one log file, so everything the command said comes back as
        `stdout` and `stderr` is always empty -- which is exactly how
        `_exec_checked` used to lose the diagnostics."""
        self.commands.append(command)
        self.calls.append((command, deadline_s))
        self.detached.append((command, deadline_s))
        log_path = "/workspace/.rpfarm/exec/exec-abc.log"
        if command in self.fail_commands:
            return {
                "exit_code": 1,
                "stdout": self.fail_output if self.fail_output is not None else "boom: " + command,
                "stderr": "",
                "log_path": log_path,
            }
        return {"exit_code": 0, "stdout": "", "stderr": "", "log_path": log_path}


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
    assert SYNC_TOUCH_COMMAND in sync_client.commands


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


# -- delocalize_via_pathmap / map_output_pair -------------------------------------

PMAP = {"/job": "/workspace/projects/may/shot"}


def test_delocalize_via_pathmap_basic():
    assert delocalize_via_pathmap("/job/render/a.exr", PMAP) == \
        "/workspace/projects/may/shot/render/a.exr"


def test_delocalize_via_pathmap_exact_root_match():
    assert delocalize_via_pathmap("/job", PMAP) == "/workspace/projects/may/shot"


def test_delocalize_via_pathmap_longest_prefix_wins():
    path_map = {"/job": "/workspace/projects/may/shot",
                "/job/render": "/elsewhere/render"}
    assert delocalize_via_pathmap("/job/render/a.exr", path_map) == "/elsewhere/render/a.exr"


def test_delocalize_via_pathmap_no_match_returns_none():
    assert delocalize_via_pathmap("/somewhere/else/a.exr", PMAP) is None


def test_delocalize_via_pathmap_does_not_match_a_sibling_prefix():
    assert delocalize_via_pathmap("/job2/a.exr", PMAP) is None


def test_map_output_pair_from_a_farm_path():
    assert map_output_pair("/workspace/projects/may/shot/render/a.exr", PMAP) == (
        "/workspace/projects/may/shot/render/a.exr", "/job/render/a.exr")


def test_map_output_pair_from_an_already_local_path():
    """PDG can hand the reported output over already translated for this
    machine. localizePath alone returns such a path unchanged, which is
    indistinguishable from "not mapped" -- and three rendered EXRs were
    dropped exactly that way."""
    assert map_output_pair("/job/render/a.exr", PMAP) == (
        "/workspace/projects/may/shot/render/a.exr", "/job/render/a.exr")


def test_map_output_pair_unrelated_path_is_none():
    assert map_output_pair("/tmp/whatever.exr", PMAP) is None


def test_map_output_pair_empty_is_none():
    assert map_output_pair("", PMAP) is None


# -- result_data_path -------------------------------------------------------------


class _FakeFile:
    """Stands in for pdg.File, whose repr is not its path."""

    def __init__(self, path, tag=""):
        self.path = path
        self.tag = tag

    def __repr__(self):
        return "<{} tag={!r} owned=true>".format(self.path, self.tag)


def test_result_data_path_from_a_pdg_file_object():
    assert result_data_path(_FakeFile("/workspace/a.exr", "file/image")) == "/workspace/a.exr"


def test_result_data_path_from_bytes():
    """str() on bytes yields the literal "b'/workspace/...'" and bytes[0] is
    an int -- both silently produce a path that matches nothing."""
    assert result_data_path(b"/workspace/a.exr") == "/workspace/a.exr"


def test_result_data_path_from_a_tuple():
    assert result_data_path(("/workspace/a.exr", "file/image", 0)) == "/workspace/a.exr"


def test_result_data_path_from_a_plain_string():
    assert result_data_path("/workspace/a.exr") == "/workspace/a.exr"


def test_result_data_path_falls_back_to_str():
    """pdg.File's __str__ is its path even though its repr is not, so str()
    stays the last resort rather than dropping an entry outright."""

    class Stringy:
        def __str__(self):
            return "/workspace/a.exr"

    assert result_data_path(Stringy()) == "/workspace/a.exr"
    assert map_output_pair(result_data_path(object()), PMAP) is None
    assert result_data_path(()) == ""


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
    assert SYNC_TOUCH_COMMAND in sync_client.commands


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
        c == "python3 /opt/rpfarm/housekeeping.py touch 'may/shotA' --event upload" for c in sync_client.commands
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
        c == "python3 /opt/rpfarm/housekeeping.py touch 'may/shotA' --event download" for c in sync_client.commands
    )


# -- get_volume_size_gb (Ruling R27: RunPod's own size, cached per session) --


class _GrowCfg:
    volume_id = "vol1"


class _FakeApi:
    def __init__(self, size_gb):
        self.size_gb = size_gb
        self.get_volume_calls = 0
        self.resized = []

    def get_volume(self, vid):
        self.get_volume_calls += 1
        return {"size": self.size_gb}

    def resize_volume(self, vid, size_gb):
        self.resized.append((vid, size_gb))


def test_get_volume_size_gb_fetches_and_caches(tmp_path, monkeypatch):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    api = _FakeApi(size_gb=50)
    assert get_volume_size_gb(api, _GrowCfg()) == 50
    assert api.get_volume_calls == 1
    assert get_volume_size_gb(api, _GrowCfg()) == 50
    assert api.get_volume_calls == 1  # second call served from the cache file


def test_get_volume_size_gb_persists_across_fresh_calls(tmp_path, monkeypatch):
    """The real point of the cache: package_runner.py is a fresh process
    per upload item, so a plain in-process cache would never be reused --
    only a call with no shared Python state in common proves the file
    cache actually crosses that boundary."""
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    api1 = _FakeApi(size_gb=50)
    get_volume_size_gb(api1, _GrowCfg())
    api2 = _FakeApi(size_gb=999)  # a second "process" -- must not be consulted
    assert get_volume_size_gb(api2, _GrowCfg()) == 50
    assert api2.get_volume_calls == 0


def test_get_volume_size_gb_refetches_when_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    api = _FakeApi(size_gb=50)
    assert get_volume_size_gb(api, _GrowCfg(), max_age_s=0) == 50
    api.size_gb = 60  # simulates a resize
    assert get_volume_size_gb(api, _GrowCfg(), max_age_s=0) == 60
    assert api.get_volume_calls == 2


def test_get_volume_size_gb_falls_back_to_stale_on_api_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    api = _FakeApi(size_gb=50)
    get_volume_size_gb(api, _GrowCfg())

    class ExplodingApi:
        def get_volume(self, vid):
            raise RuntimeError("network down")

    assert get_volume_size_gb(ExplodingApi(), _GrowCfg(), max_age_s=0) == 50


def test_get_volume_size_gb_none_with_no_cache_and_failing_api(tmp_path, monkeypatch):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))

    class ExplodingApi:
        def get_volume(self, vid):
            raise RuntimeError("network down")

    assert get_volume_size_gb(ExplodingApi(), _GrowCfg()) is None


# -- maybe_grow_volume ------------------------------------------------------


class _DiskUsageSyncClient:
    """Fake sync pod for maybe_grow_volume's `disk-usage` call (Ruling R26:
    not `ls` -- the decision only needs the zone-summed used bytes, never
    ls's per-project walk; Ruling R27: total comes from the caller's
    --volume-size-gb, never shutil.disk_usage)."""

    def __init__(self, used, exit_code=0, partial=False):
        self.used = used
        self.exit_code = exit_code
        self.partial = partial
        self.commands = []

    def exec(self, command, timeout_s=600):
        self.commands.append(command)
        if self.exit_code != 0:
            return {"exit_code": self.exit_code, "stdout": "", "stderr": "boom"}
        payload = json.dumps({"volume": {"used": self.used}, "partial": self.partial})
        # A real disk-usage --volume-size-gb N response computes total/used_pct
        # itself; parse N back out of the command so this fake matches that
        # contract instead of duplicating the arithmetic independently.
        if "--volume-size-gb" in command:
            gb = float(command.split("--volume-size-gb")[1].split()[0])
            total = int(gb * 2**30)
            payload = json.dumps(
                {"volume": {"used": self.used, "total": total}, "partial": self.partial}
            )
        return {"exit_code": 0, "stdout": payload, "stderr": ""}


@pytest.fixture
def rpfarm_home(tmp_path, monkeypatch):
    monkeypatch.setenv("RPFARM_HOME", str(tmp_path))
    return tmp_path


def test_maybe_grow_volume_passes_real_size_to_disk_usage(rpfarm_home):
    gb = 2**30
    api = _FakeApi(size_gb=50)
    sync_client = _DiskUsageSyncClient(used=10 * gb)
    maybe_grow_volume(api, _GrowCfg(), sync_client, needed_bytes=1 * gb)
    assert sync_client.commands == [
        "python3 /opt/rpfarm/housekeeping.py disk-usage --volume-size-gb 50 --budget-s 20"
    ]


def test_maybe_grow_volume_grows_past_threshold(rpfarm_home):
    gb = 2**30
    api = _FakeApi(size_gb=50)
    sync_client = _DiskUsageSyncClient(used=44 * gb)
    result = maybe_grow_volume(api, _GrowCfg(), sync_client, needed_bytes=2 * gb)
    assert api.resized == [("vol1", 60)]  # ceil(46/0.8/10)*10 = 60
    assert result == "grown to 60 GB"


def test_maybe_grow_volume_caches_new_size_after_resize(rpfarm_home):
    """Fix round 2, "D": the next item, within the 5-minute window, must
    see the *new* size -- not re-issue an already-satisfied resize
    against the stale pre-resize figure."""
    gb = 2**30
    api = _FakeApi(size_gb=50)
    sync_client = _DiskUsageSyncClient(used=44 * gb)
    maybe_grow_volume(api, _GrowCfg(), sync_client, needed_bytes=2 * gb)
    assert api.resized == [("vol1", 60)]
    assert api.get_volume_calls == 1  # only the very first lookup hit the API

    # A second item, same "session": used is now safely under 60GB's 85%,
    # so no second resize should fire, and it must not need another
    # get_volume() call either -- the cache already has the new number.
    sync_client2 = _DiskUsageSyncClient(used=44 * gb)
    result2 = maybe_grow_volume(api, _GrowCfg(), sync_client2, needed_bytes=2 * gb)
    assert result2 == "ok"
    assert api.resized == [("vol1", 60)]  # unchanged -- no re-issued resize
    assert api.get_volume_calls == 1  # still just the one API call, ever


def test_maybe_grow_volume_no_op_under_threshold(rpfarm_home):
    gb = 2**30
    api = _FakeApi(size_gb=50)
    sync_client = _DiskUsageSyncClient(used=10 * gb)
    result = maybe_grow_volume(api, _GrowCfg(), sync_client, needed_bytes=1 * gb)
    assert api.resized == []
    assert result == "ok"


def test_maybe_grow_volume_never_shrinks_or_repeats(rpfarm_home):
    gb = 2**30
    api = _FakeApi(size_gb=100)  # already bigger than the computed target
    sync_client = _DiskUsageSyncClient(used=44 * gb)
    result = maybe_grow_volume(api, _GrowCfg(), sync_client, needed_bytes=2 * gb)
    assert api.resized == []
    assert result == "ok"


def test_maybe_grow_volume_swallows_disk_usage_failure(rpfarm_home):
    api = _FakeApi(size_gb=50)
    sync_client = _DiskUsageSyncClient(used=0, exit_code=1)
    logs = []
    result = maybe_grow_volume(api, _GrowCfg(), sync_client, needed_bytes=1, log=logs.append)
    assert api.resized == []
    assert logs  # the failure was logged, not raised
    assert result.startswith("skipped:")


def test_maybe_grow_volume_skips_with_visible_note_when_size_unknown(rpfarm_home):
    class ExplodingApi:
        def get_volume(self, vid):
            raise RuntimeError("network down")

    logs = []
    sync_client = _DiskUsageSyncClient(used=0)
    result = maybe_grow_volume(ExplodingApi(), _GrowCfg(), sync_client, needed_bytes=1, log=logs.append)
    assert result.startswith("skipped:")
    assert logs
    assert sync_client.commands == []  # never even asked the pod without a real size


def test_maybe_grow_volume_swallows_unexpected_exec_exception(rpfarm_home):
    """The outer try/except is a safety net for anything unexpected, not
    just a failed RunPod lookup or a non-zero exit_code -- exercise it
    with sync_client.exec itself raising (WorkerClient.exec never does in
    practice, but this is the last line of defense regardless)."""
    api = _FakeApi(size_gb=50)
    gb = 2**30
    sync_client = _DiskUsageSyncClient(used=44 * gb)
    sync_client.exec = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("transport down"))
    logs = []
    result = maybe_grow_volume(api, _GrowCfg(), sync_client, needed_bytes=2 * gb, log=logs.append)
    assert logs
    assert result.startswith("error:")


def test_maybe_grow_volume_notes_partial_data(rpfarm_home):
    gb = 2**30
    api = _FakeApi(size_gb=50)
    sync_client = _DiskUsageSyncClient(used=10 * gb, partial=True)
    logs = []
    result = maybe_grow_volume(api, _GrowCfg(), sync_client, needed_bytes=1 * gb, log=logs.append)
    assert result == "ok (partial data)"
    assert any("partial" in m for m in logs)


def test_maybe_grow_volume_short_exec_timeout(rpfarm_home):
    """R26/R27: disk-usage's "used" comes from the cached zone sizes (fast
    when warm) and never walks anything to get "total" -- the exec
    timeout can stay short."""
    gb = 2**30
    api = _FakeApi(size_gb=50)
    sync_client = _DiskUsageSyncClient(used=10 * gb)
    calls = []
    real_exec = sync_client.exec

    def spying_exec(command, timeout_s=600):
        calls.append(timeout_s)
        return real_exec(command, timeout_s=timeout_s)

    sync_client.exec = spying_exec
    maybe_grow_volume(api, _GrowCfg(), sync_client, needed_bytes=1 * gb)
    assert calls and calls[0] <= 30


# -- Ruling R31: long post-commands must go the detached way -------------------


def _fake_rclone_copy(package, target, direction, rclone_bin, local_root, remote_root, progress_cb=None):
    from rpfarm.sync import SyncStats

    return SyncStats(files=len(package), bytes=sum(e.size for e in package), seconds=0.1)


def _upload_item(tmp_path, post_command, nbytes=10):
    src = tmp_path / "f.txt"
    src.write_bytes(b"x" * 10)
    return {
        "index": 0,
        "local_root": str(tmp_path),
        "remote_root": "/workspace/apps/dist",
        "files": [[str(src), "/workspace/apps/dist/f.txt", 10]],
        "bytes": nbytes,
        "post_command": post_command,
    }


def test_run_upload_item_runs_post_command_detached_not_synchronously(tmp_path, monkeypatch):
    """Task 14's failure, as a test.

    The Houdini-install post-command used to go through the synchronous
    `exec`, whose pod-side `subprocess.run(timeout=)` SIGKILLs the shell and
    closes its stdout pipe. The installer is a grandchild, so it survived the
    kill and then died of SIGPIPE at its next progress write -- leaving an
    11GB tree with no `python/` that still passed the preset's own
    `ls .../bin/hython` check. It must take the detached path instead.
    """
    monkeypatch.setattr("rpfarm.packages.rclone_copy", _fake_rclone_copy)
    _pairs, post = houdini_install_preset("/dl/houdini-22.0.393-linux_x86_64_gcc14.2.tar.gz", "22.0.393")
    item = _upload_item(tmp_path, post)
    client = FakeSyncClient()

    run_upload_item(item, FakeCfg(), SftpTarget(host="1.2.3.4", port=22, key_path="/k"), client, compress=False)

    assert [c for c, _ in client.detached] == [post]
    # the short bookkeeping touches stay on the synchronous path
    assert SYNC_TOUCH_COMMAND in client.commands


def test_run_upload_item_post_command_deadline_is_the_scaled_timeout(tmp_path, monkeypatch):
    """The size-derived timeout survives, but as a *watching* deadline handed
    to exec_wait -- never again as a pod-side kill."""
    from rpfarm.packages import _scaled_timeout

    monkeypatch.setattr("rpfarm.packages.rclone_copy", _fake_rclone_copy)
    nbytes = 4 * 2**30
    item = _upload_item(tmp_path, "slow-thing", nbytes=nbytes)
    client = FakeSyncClient()

    run_upload_item(item, FakeCfg(), SftpTarget(host="1.2.3.4", port=22, key_path="/k"), client, compress=False)

    assert client.detached == [("slow-thing", _scaled_timeout(nbytes))]


def test_run_upload_item_still_raises_when_a_detached_post_command_fails(tmp_path, monkeypatch):
    """Going detached must not turn a failed remote command into a silent
    success -- the whole reason _exec_checked exists.

    Asserts on what the command actually *said*, not on the command name
    echoed back: a detached run merges stdout+stderr into its log, so the
    diagnostics arrive as stdout with stderr empty. Matching the command
    name would pass even with the output thrown away -- which is how the
    "(no stderr)" regression slipped through the first time.
    """
    monkeypatch.setattr("rpfarm.packages.rclone_copy", _fake_rclone_copy)
    item = _upload_item(tmp_path, "boom")
    client = FakeSyncClient(
        fail_commands={"boom"},
        fail_output="Problem encountered installing Python's library dependencies.",
    )

    with pytest.raises(RuntimeError) as excinfo:
        run_upload_item(item, FakeCfg(), SftpTarget(host="1.2.3.4", port=22, key_path="/k"), client, compress=False)

    msg = str(excinfo.value)
    assert "Problem encountered installing Python's library dependencies." in msg
    assert "no output" not in msg and "no stderr" not in msg
    assert "/workspace/.rpfarm/exec/exec-abc.log" in msg  # where to go read the rest
    assert "exit 1" in msg


def test_exec_checked_prefers_stderr_when_the_client_supplies_it(tmp_path, monkeypatch):
    """The synchronous path still separates the streams; stderr stays the
    first choice so nothing regresses for callers that provide it."""
    from rpfarm.packages import _exec_checked

    class SplitStreams:
        def exec_wait(self, command, deadline_s, **kw):
            return {"exit_code": 2, "stdout": "chatty progress", "stderr": "the real error"}

    with pytest.raises(RuntimeError, match="the real error"):
        _exec_checked(SplitStreams(), "cmd", 60)


def test_exec_checked_says_no_output_only_when_there_really_is_none():
    from rpfarm.packages import _exec_checked

    class Silent:
        def exec_wait(self, command, deadline_s, **kw):
            return {"exit_code": 9, "stdout": "", "stderr": ""}

    with pytest.raises(RuntimeError, match=r"\(no output\)"):
        _exec_checked(Silent(), "cmd", 60)


# -- sync-pod idle stamp (final-review finding 1) ----------------------------


def _housekeeping():
    """The pod-side module, imported the same way tests/test_housekeeping.py
    imports it (pod/ is not a package)."""
    import sys

    pod_dir = os.path.join(os.path.dirname(__file__), "..", "pod")
    if pod_dir not in sys.path:
        sys.path.insert(0, pod_dir)
    import housekeeping

    return housekeeping


class RealTouchSyncClient(FakeSyncClient):
    """FakeSyncClient whose ``sync-touch`` really runs pod/housekeeping.py.

    Closes the loop the two-writers bug slipped through: the command the
    upload/download path actually sends is executed by the same code the
    pod would run, and the file it leaves behind is then handed to both
    readers.
    """

    def __init__(self, root, **kw):
        super().__init__(**kw)
        self.root = str(root)

    def exec(self, command, timeout_s=600):
        result = super().exec(command, timeout_s=timeout_s)
        if command == SYNC_TOUCH_COMMAND:
            _housekeeping().cmd_sync_touch(self.root)
        return result


def _stamp_readers(volume_root):
    """``(hda_reader_value, housekeeping_idle_seconds)`` for the stamp on
    ``volume_root``. The first is exactly what the scheduler HDA's
    ``_retireStaleSyncPod`` does: ``float(read_file(path).strip())``."""
    hk = _housekeeping()
    raw = open(os.path.join(volume_root, ".rpfarm", "sync_last_used")).read()
    return float(raw.strip()), hk.cmd_sync_idle(volume_root)["idle_seconds"]


def _touch_upload_item(tmp_path):
    src = tmp_path / "f.txt"
    src.write_bytes(b"x" * 10)
    return {
        "index": 0,
        "local_root": str(tmp_path),
        "remote_root": "/workspace/projects/may/shotA",
        "files": [[str(src), "/workspace/projects/may/shotA/f.txt", 10]],
        "bytes": 10,
        "post_command": "",
    }


def test_upload_stamp_is_parseable_by_both_readers(tmp_path, monkeypatch):
    """Finding 1: run_upload_item used to write the stamp with a bare
    ``touch``, which creates an EMPTY file -- unparseable by the scheduler
    HDA's reader and by housekeeping's ``sync-idle`` alike, so the sync pod
    could never be auto-retired."""
    monkeypatch.setattr("rpfarm.packages.rclone_copy", _fake_rclone_copy)
    volume = tmp_path / "workspace"
    client = RealTouchSyncClient(volume)

    run_upload_item(
        _touch_upload_item(tmp_path), FakeCfg(),
        SftpTarget(host="1.2.3.4", port=22, key_path="/k"), client, compress=False,
    )

    hda_value, idle_seconds = _stamp_readers(str(volume))
    assert hda_value > 0
    assert idle_seconds is not None and idle_seconds < 60


def test_download_stamp_is_parseable_by_both_readers(tmp_path, monkeypatch):
    monkeypatch.setattr("rpfarm.packages.rclone_copy", _fake_rclone_copy)
    volume = tmp_path / "workspace"
    client = RealTouchSyncClient(volume)
    item = {
        "index": 0,
        "local_root": str(tmp_path / "down"),
        "remote_root": "/workspace/projects/may/shotA/render",
        "files": [],
    }

    run_download_item(
        item, FakeCfg(), SftpTarget(host="1.2.3.4", port=22, key_path="/k"), client, "newer",
    )

    hda_value, idle_seconds = _stamp_readers(str(volume))
    assert hda_value > 0
    assert idle_seconds is not None and idle_seconds < 60


def test_upload_touches_the_stamp_before_the_transfer_too(tmp_path, monkeypatch):
    """A transfer that fails part-way must still have refreshed the stamp,
    and the stamp must not go stale for the whole length of a package."""
    order = []

    def spy_rclone_copy(package, target, direction, rclone_bin, local_root, remote_root, **kw):
        order.append("rclone")
        return _fake_rclone_copy(package, target, direction, rclone_bin, local_root, remote_root, **kw)

    monkeypatch.setattr("rpfarm.packages.rclone_copy", spy_rclone_copy)

    class OrderedClient(FakeSyncClient):
        def exec(self, command, timeout_s=600):
            if command == SYNC_TOUCH_COMMAND:
                order.append("touch")
            return super().exec(command, timeout_s=timeout_s)

    client = OrderedClient()
    run_upload_item(
        _touch_upload_item(tmp_path), FakeCfg(),
        SftpTarget(host="1.2.3.4", port=22, key_path="/k"), client, compress=False,
    )
    assert order[0] == "touch" and order[-1] == "touch"


def test_heartbeat_refreshes_the_stamp_during_a_long_transfer():
    """A package can transfer for longer than Sync Pod Idle; the stamp has
    to keep moving while it does, or a concurrent cook retires a sync pod
    that is in use."""
    from rpfarm.packages import _SyncTouchHeartbeat

    clock = [0.0]
    client = FakeSyncClient()
    forwarded = []
    hb = _SyncTouchHeartbeat(
        client, progress_cb=lambda *a: forwarded.append(a), interval_s=60.0, now=lambda: clock[0]
    )

    for _ in range(30):  # 30 stats lines, one per second
        clock[0] += 1.0
        hb(1, 2, 3)
    assert client.commands == [], "no touch yet -- under the interval"

    clock[0] += 31.0
    hb(1, 2, 3)
    assert client.commands == [SYNC_TOUCH_COMMAND]

    clock[0] += 61.0
    hb(1, 2, 3)
    assert client.commands == [SYNC_TOUCH_COMMAND, SYNC_TOUCH_COMMAND]
    assert len(forwarded) == 32, "every call still reaches the wrapped progress_cb"


def test_heartbeat_never_fails_a_transfer():
    """A touch is bookkeeping. If the sync pod refuses it, the transfer that
    is otherwise succeeding must not die with it."""
    from rpfarm.packages import _SyncTouchHeartbeat

    class Exploding:
        def exec(self, command, timeout_s=600):
            raise RuntimeError("pod gone")

    clock = [0.0]
    hb = _SyncTouchHeartbeat(Exploding(), progress_cb=None, interval_s=1.0, now=lambda: clock[0])
    clock[0] = 100.0
    hb(1, 2, 3)  # no raise


def test_heartbeat_wraps_a_none_progress_cb():
    from rpfarm.packages import _SyncTouchHeartbeat

    hb = _SyncTouchHeartbeat(FakeSyncClient(), progress_cb=None)
    hb(1, 2, 3)  # no raise


# -- a failed sync-touch must not be invisible --------------------------------
#
# Task 17, residual A. WorkerClient.exec never raises on a non-zero exit and
# every caller of touch_sync_pod discarded the result, so a pod whose
# /opt/rpfarm/housekeeping.py predates the `sync-touch` subcommand failed
# argparse ("invalid choice"), left the stamp unwritten, and silently
# reproduced the un-retirable sync pod the command exists to prevent -- with
# no line about it anywhere.


class StaleImageSyncClient(FakeSyncClient):
    """A sync pod running an image older than ``sync-touch``: argparse
    rejects the subcommand and exits 2, exactly as the real pod would."""

    def exec(self, command, timeout_s=600):
        self.commands.append(command)
        self.calls.append((command, timeout_s))
        if command == SYNC_TOUCH_COMMAND:
            return {
                "exit_code": 2,
                "stdout": "",
                "stderr": "housekeeping.py: error: argument command: invalid choice: 'sync-touch'",
            }
        return {"exit_code": 0, "stdout": "", "stderr": ""}


def test_a_failed_touch_is_reported_with_its_likely_cause():
    from rpfarm.packages import touch_sync_pod

    said = []
    result = touch_sync_pod(StaleImageSyncClient(), log=said.append)

    assert result["exit_code"] == 2
    assert len(said) == 1
    assert "invalid choice" in said[0], "the pod's own words have to survive"
    assert "older than the `sync-touch` command" in said[0], "and the likely cause"
    assert "rpfarm farm kill --sync" in said[0], "and the fix"


def test_a_working_touch_says_nothing():
    from rpfarm.packages import touch_sync_pod

    said = []
    result = touch_sync_pod(FakeSyncClient(), log=said.append)
    assert result["exit_code"] == 0
    assert said == []


def test_no_client_is_not_a_failure():
    from rpfarm.packages import touch_sync_pod

    said = []
    assert touch_sync_pod(None, log=said.append) is None
    assert said == []


def test_the_default_log_reaches_stderr(capsys):
    """The upload/download call sites pass no log of their own: the warning
    still has to land in the work item's log, which is package_runner's
    stdout/stderr."""
    from rpfarm.packages import touch_sync_pod

    touch_sync_pod(StaleImageSyncClient())
    assert "sync pod idle stamp NOT written" in capsys.readouterr().err


@pytest.mark.parametrize("run", ["upload", "download"])
def test_both_transfer_paths_report_a_failed_touch(tmp_path, monkeypatch, capsys, run):
    monkeypatch.setattr("rpfarm.packages.rclone_copy", _fake_rclone_copy)
    client = StaleImageSyncClient()
    sftp = SftpTarget(host="1.2.3.4", port=22, key_path="/k")

    if run == "upload":
        run_upload_item(_touch_upload_item(tmp_path), FakeCfg(), sftp, client, compress=False)
    else:
        item = {
            "index": 0,
            "local_root": str(tmp_path / "down"),
            "remote_root": "/workspace/projects/may/shotA/render",
            "files": [],
        }
        run_download_item(item, FakeCfg(), sftp, client, "newer")

    err = capsys.readouterr().err
    # Both the pre-transfer and the post-transfer touch report: there are
    # only two, and the first is the one that matters most (the transfer
    # about to start is what would go unstamped).
    assert err.count("sync pod idle stamp NOT written") == 2


def test_the_heartbeat_reports_a_persistent_failure_once():
    """A heartbeat still must not fail a transfer, but a beat a minute for
    the length of a big package must not bury the log either."""
    from rpfarm.packages import _SyncTouchHeartbeat

    clock = [0.0]
    said = []
    hb = _SyncTouchHeartbeat(
        StaleImageSyncClient(), progress_cb=None, interval_s=1.0, now=lambda: clock[0], log=said.append
    )
    for _ in range(10):
        clock[0] += 2.0
        hb(1, 2, 3)

    assert len(said) == 1, "once, not per beat"
    assert "invalid choice" in said[0]
    assert "not repeated" in said[0], "the reader has to know the rest were suppressed"


def test_the_heartbeat_reports_a_raising_client_once_too():
    from rpfarm.packages import _SyncTouchHeartbeat

    class Exploding:
        def exec(self, command, timeout_s=600):
            raise RuntimeError("pod gone")

    clock = [0.0]
    said = []
    hb = _SyncTouchHeartbeat(
        Exploding(), progress_cb=None, interval_s=1.0, now=lambda: clock[0], log=said.append
    )
    for _ in range(5):
        clock[0] += 2.0
        hb(1, 2, 3)  # no raise

    assert len(said) == 1 and "pod gone" in said[0]


# ---------------------------------------------------------------------------
# parse_stat_sizes
#
# The download node's generate() ran `stat -c '%s %n' <every expected output>`
# on the sync pod and checked the exit code first. `stat` exits non-zero when
# ANY path is missing while still printing the ones it found, so one absent
# frame threw away the sizes of all the others -- and the "failed" branch then
# called hou.Node.addWarning from inside PDG generation, which Houdini refuses
# ("Cannot set error badges on other nodes"), turning the warning into a hard
# generate() failure that left the node with ZERO work items. Observed live:
# one of eight rendered frames was missing and the whole node reported nothing.
# ---------------------------------------------------------------------------


def test_parse_stat_sizes_reads_every_line_it_was_given():
    stdout = "3626569 /w/render/f.0001.exr\n3629495 /w/render/f.0002.exr\n"
    remotes = ["/w/render/f.0001.exr", "/w/render/f.0002.exr"]

    sizes, missing = parse_stat_sizes(stdout, remotes)

    assert sizes == {"/w/render/f.0001.exr": 3626569, "/w/render/f.0002.exr": 3629495}
    assert missing == []


def test_parse_stat_sizes_keeps_the_files_that_do_exist_when_one_is_missing():
    """The whole point: one absent frame must not cost the other seven."""
    remotes = ["/w/a.exr", "/w/gone.exr", "/w/c.exr"]
    stdout = "100 /w/a.exr\n300 /w/c.exr\n"  # stat printed these, then exited 1

    sizes, missing = parse_stat_sizes(stdout, remotes)

    assert sizes == {"/w/a.exr": 100, "/w/c.exr": 300}
    assert missing == ["/w/gone.exr"]


def test_parse_stat_sizes_survives_empty_and_junk_output():
    remotes = ["/w/a.exr"]

    assert parse_stat_sizes("", remotes) == ({}, ["/w/a.exr"])
    assert parse_stat_sizes(None, remotes) == ({}, ["/w/a.exr"])
    assert parse_stat_sizes("\n\n", remotes) == ({}, ["/w/a.exr"])
    # a size that is not a number, and a line with no path at all
    assert parse_stat_sizes("? /w/a.exr\nnopath\n", remotes) == ({}, ["/w/a.exr"])


def test_parse_stat_sizes_handles_paths_with_spaces_after_the_size():
    remotes = ["/w/my frame.exr"]

    sizes, missing = parse_stat_sizes("42 /w/my frame.exr\n", remotes)

    assert sizes == {"/w/my frame.exr": 42}
    assert missing == []


def test_missing_sizes_pack_as_zero_rather_than_dropping_the_file():
    """A missing size must still produce a work item -- it fails at copy time,
    against that one file, instead of the node silently planning nothing."""
    pairs = [("/w/a.exr", "/local/a.exr"), ("/w/gone.exr", "/local/gone.exr")]
    sizes, _missing = parse_stat_sizes("100 /w/a.exr\n", ["/w/a.exr", "/w/gone.exr"])

    items = build_download_items("outputs", pairs, 1.0, sizes)

    # files are (local, remote, size) triples
    planned = [f for it in items for f in it["files"]]
    assert sorted(remote for _local, remote, _size in planned) == ["/w/a.exr", "/w/gone.exr"]
    assert [size for _local, remote, size in planned if remote == "/w/gone.exr"] == [0]
    assert [size for _local, remote, size in planned if remote == "/w/a.exr"] == [100]


# -- the USD path-map resolver ships with the Houdini install (2026-09-05) ------


def test_installing_houdini_also_builds_the_usd_resolver():
    """$HOUDINI_PATHMAP is read by Houdini's file layer and ignored by USD's
    resolver -- measured on a pod: pathmap -t mapped the path while
    Ar.Resolve returned '' and the layer would not load. SideFX ship the
    bridge as a toolkit sample; a deployment has to build it, not a human."""
    _pairs, post = rppkg.houdini_install_preset("/local/houdini-22.0.393.tar.gz", "22.0.393")

    assert "USD_HoudiniPathMapArResolver.C" in post
    assert "hcustom -U" in post
    assert "/workspace/houdini/22.0.393/houdini/dso/usd" in post, "into Houdini's own DSO path"
    assert "usd_plugins" in post, "USD cannot register the plugin without it"


def test_the_resolver_build_installs_what_hcustom_needs():
    """Facts from the pod, each one a failure that actually happened:
    no compiler at all; hcustom is a csh script ("bad interpreter"); and it
    links -lGL -lX11 -lXext -lXi even for a resolver that draws nothing."""
    _pairs, post = rppkg.houdini_install_preset("/local/h.tar.gz", "22.0.393")

    for package in ("build-essential", "csh", "libgl1-mesa-dev", "libx11-dev",
                    "libxext-dev", "libxi-dev"):
        assert package in post, package


def test_a_failed_resolver_build_does_not_fail_the_houdini_install():
    """A working Houdini is worth keeping even if the extra step fails --
    and the failure has to be visible, not swallowed."""
    _pairs, post = rppkg.houdini_install_preset("/local/h.tar.gz", "22.0.393")

    tail = post[post.index("USD path-map resolver installed"):]
    assert "||" in tail and "WARNING" in tail
    assert ">&2" in tail


def test_the_resolver_is_built_into_the_volume_not_the_image():
    """It is compiled against this Houdini's USD ABI, so it belongs with the
    Houdini it was built for; and the compiler stays out of an image that is
    deliberately thin."""
    _pairs, post = rppkg.houdini_install_preset("/local/h.tar.gz", "22.0.393")
    dockerfile = (pathlib.Path(__file__).resolve().parent.parent / "pod" / "Dockerfile").read_text()

    assert "toolkit/samples/USD/USD_HoudiniPathMapArResolver" in post
    assert "build-essential" not in dockerfile, "the image must stay thin"
