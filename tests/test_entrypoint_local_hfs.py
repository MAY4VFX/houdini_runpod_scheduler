"""pod/entrypoint.sh step 3a: render and host pods unpack the packed Houdini
image onto their own disk, and fall back to the volume on any trouble.

The block between the ``local-hfs`` markers runs under bash with a fake
``unsquashfs`` on PATH (it lays down a minimal tree instead of reading SquashFS),
so the branches are exercised for real without root or squashfs-tools.
"""
import os
import pathlib
import subprocess

import pytest

ENTRYPOINT = pathlib.Path(__file__).resolve().parent.parent / "pod" / "entrypoint.sh"
VERSION = "22.0.393"


def _block():
    text = ENTRYPOINT.read_text()
    start = text.index("# --- local-hfs begin")
    end = text.index("# --- local-hfs end ---")
    return text[start:end]


def _fake_unsquashfs(bindir, fail=False):
    # unsquashfs -q -n -f -p N -d DEST IMAGE
    script = bindir / "unsquashfs"
    body = "exit 1" if fail else (
        'while [ $# -gt 1 ]; do [ "$1" = "-d" ] && dest="$2"; shift; done\n'
        'mkdir -p "$dest" && touch "$dest/houdini_setup_bash"')
    script.write_text("#!/bin/bash\n" + body + "\n")
    script.chmod(0o755)


def _volume(tmp_path, with_image=True, shfs=True):
    vol = tmp_path / "workspace" / "houdini" / VERSION
    vol.mkdir(parents=True)
    (vol / "houdini_setup_bash").write_text("")
    if with_image:
        (vol / ".rpfarm-hfs.sqfs").write_bytes(b"hsqs")
    if shfs:
        (vol.parent / "shfs").mkdir()
    return vol


def _ready(tmp_path):
    return (tmp_path / "local" / VERSION / ".rpfarm-ok").exists()


def _run(tmp_path, vol, role="gpu", unsquashfs=True, fail=False, min_free_kb=1, repair_wait_s=10):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    if unsquashfs:
        _fake_unsquashfs(bindir, fail=fail)
    script = (
        'export HFS="{vol}" HOUDINI_VERSION={ver}\n'
        'export RPFARM_VOLUME_HFS="$HFS"\n'
        "{block}\n"
        "wait\n"  # the unpack runs in the background
        'echo "HFS=$HFS"; echo "SHFS=${{SHFS:-}}"; echo "LOCAL=${{RPFARM_LOCAL_HFS:-}}"\n'
    ).format(vol=vol, ver=VERSION, block=_block())
    env = {
        "PATH": "{}:/usr/bin:/bin".format(bindir),
        "RPFARM_ROLE": role,
        "RPFARM_LOCAL_HFS_ROOT": str(tmp_path / "local"),
        "RPFARM_LOCAL_HFS_MIN_FREE_KB": str(min_free_kb),
        "RPFARM_REPAIR_WAIT_S": str(repair_wait_s),
        "RPFARM_UNPACK_REPORT_S": "0.1",
    }
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    out = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line and line.split("=", 1)[0] in ("HFS", "SHFS", "LOCAL"))
    return out, proc.stdout


@pytest.mark.parametrize("role", ["gpu", "host"])
def test_render_and_host_pods_run_a_local_copy(tmp_path, role):
    vol = _volume(tmp_path)
    out, log = _run(tmp_path, vol, role=role)
    local = tmp_path / "local" / VERSION
    # the pod boots on the volume; the copy is announced to worker.py,
    # which switches new tasks to it once the marker is there
    assert out["HFS"] == str(vol)
    assert out["LOCAL"] == str(local)
    assert _ready(tmp_path)
    assert "unpacked" in log and "in the background" in log
    # named as a normalized path, never "$HFS/../shfs" (worker.py rewrites
    # the HFS prefix)
    assert out["SHFS"] == str((vol.parent / "shfs").resolve())


def test_sync_pod_keeps_the_volume_copy(tmp_path):
    vol = _volume(tmp_path)
    out, _ = _run(tmp_path, vol, role="sync")
    assert out["HFS"] == str(vol)
    assert out["LOCAL"] == ""
    assert not (tmp_path / "local").exists()


def test_no_image_runs_from_the_volume(tmp_path):
    vol = _volume(tmp_path, with_image=False)
    out, log = _run(tmp_path, vol)
    assert out["HFS"] == str(vol)
    assert "rpfarm houdini pack" in log


def test_failed_unpack_runs_from_the_volume_and_leaves_no_half_copy(tmp_path):
    vol = _volume(tmp_path)
    out, log = _run(tmp_path, vol, fail=True)
    assert out["HFS"] == str(vol)
    assert "unpack failed" in log
    assert not (tmp_path / "local" / VERSION).exists()
    assert not _ready(tmp_path)


def test_too_little_disk_runs_from_the_volume(tmp_path):
    vol = _volume(tmp_path)
    out, log = _run(tmp_path, vol, min_free_kb=10 ** 15)
    assert out["HFS"] == str(vol)
    assert "free on the pod disk" in log


def test_missing_unsquashfs_runs_from_the_volume(tmp_path):
    vol = _volume(tmp_path)
    out, log = _run(tmp_path, vol, unsquashfs=False)
    assert out["HFS"] == str(vol)
    assert "unsquashfs missing" in log


def test_a_second_boot_reuses_the_unpacked_copy(tmp_path):
    vol = _volume(tmp_path)
    _run(tmp_path, vol)
    assert _ready(tmp_path)
    out, log = _run(tmp_path, vol, fail=True)  # would fail if it unpacked again
    assert out["HFS"] == str(tmp_path / "local" / VERSION)
    assert "already" in log


def test_files_the_pack_could_not_read_are_copied_from_the_volume(tmp_path):
    vol = _volume(tmp_path)
    (vol / "toolkit").mkdir()
    (vol / "toolkit" / "UT_LinuxUtil.h").write_text("real")
    (vol / ".rpfarm-hfs.repair").write_text("toolkit/UT_LinuxUtil.h\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    # the image holds the file empty, as mksquashfs left it
    (bindir / "unsquashfs").write_text(
        "#!/bin/bash\n"
        'while [ $# -gt 1 ]; do [ "$1" = "-d" ] && dest="$2"; shift; done\n'
        'mkdir -p "$dest/toolkit" && touch "$dest/houdini_setup_bash" "$dest/toolkit/UT_LinuxUtil.h"\n')
    (bindir / "unsquashfs").chmod(0o755)
    _run(tmp_path, vol, unsquashfs=False)
    local = tmp_path / "local" / VERSION
    assert _ready(tmp_path)
    assert (local / "toolkit" / "UT_LinuxUtil.h").read_text() == "real"


def test_a_file_that_does_not_read_on_the_volume_either_stays_empty(tmp_path):
    """Live 2026-11-03: 21 of the 22 files the pack could not read do not
    read on the volume at all; the copy is still used, those stay empty."""
    vol = _volume(tmp_path)
    (vol / ".rpfarm-hfs.repair").write_text("toolkit/gone.h\n")
    out, log = _run(tmp_path, vol)
    assert _ready(tmp_path)
    assert "toolkit/gone.h does not read on the volume either" in log
    assert "1 file(s) left empty" in log


def test_a_repair_read_that_blocks_does_not_hold_up_the_copy(tmp_path):
    """A read off the volume can block in the kernel for minutes; the
    repair gives each file RPFARM_REPAIR_WAIT_S and moves on."""
    vol = _volume(tmp_path)
    (vol / "a.h").write_text("real")
    (vol / ".rpfarm-hfs.repair").write_text("a.h\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "cp").write_text("#!/bin/bash\nexec >/dev/null 2>&1\nsleep 30\n")
    (bindir / "cp").chmod(0o755)
    import time
    t0 = time.time()
    _, log = _run(tmp_path, vol, repair_wait_s=1)
    assert time.time() - t0 < 20
    assert _ready(tmp_path)
    assert "a.h does not read on the volume either" in log
