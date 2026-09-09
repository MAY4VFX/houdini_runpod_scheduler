"""Tests for rpfarm.background_cook (Ruling R68, mode 2 of three cook
modes). The hou-dependent half (saving the scene, resolving the topnet's
own path, telling the artist where to watch) lives in the scheduler HDA
and is not importable here -- see test_scheduler_glue.py's own
onBackgroundCook source-order tests for that half.
"""

import datetime
import os
import subprocess

import pytest

from rpfarm import background_cook as rpbg


def test_find_topcook_returns_the_path_when_it_exists(tmp_path):
    hhp = tmp_path / "houdini" / "python3.13libs"
    (hhp / "pdgjob").mkdir(parents=True)
    topcook = hhp / "pdgjob" / "topcook.py"
    topcook.write_text("# stub")

    assert rpbg.find_topcook(hhp) == topcook


def test_find_topcook_raises_a_plain_message_when_missing(tmp_path):
    hhp = tmp_path / "houdini" / "python3.13libs"
    hhp.mkdir(parents=True)

    with pytest.raises(rpbg.BackgroundCookError, match="does not exist"):
        rpbg.find_topcook(hhp)


def test_build_command_mirrors_submitasjobs_own_flags():
    cmd = rpbg.build_command(
        "/opt/houdini/bin/hython", "/opt/houdini/houdini/python3.13libs/pdgjob/topcook.py",
        "/tmp/some_project/scene_v001.hip", "/obj/topnet1")

    assert cmd == [
        "/opt/houdini/bin/hython", "--pdg",
        "/opt/houdini/houdini/python3.13libs/pdgjob/topcook.py",
        "--report", "none",
        "--hip", "/tmp/some_project/scene_v001.hip",
        "--verbosity", "2",
        "--logs",
        "--toppath", "/obj/topnet1",
    ]


def test_build_command_verbosity_is_overridable():
    cmd = rpbg.build_command("hython", "topcook.py", "x.hip", "/obj/topnet1", verbosity=0)
    assert "--verbosity" in cmd
    assert cmd[cmd.index("--verbosity") + 1] == "0"


def test_default_log_path_is_timestamped_and_sanitises_the_topnet_path(tmp_path):
    clock = lambda: datetime.datetime(2026, 9, 9, 12, 34, 56)

    path = rpbg.default_log_path(tmp_path, "/obj/topnet1", clock=clock)

    assert path == tmp_path / "bgcook" / "20260909-123456-obj_topnet1.log"


def test_default_log_path_falls_back_when_the_topnet_path_is_empty(tmp_path):
    clock = lambda: datetime.datetime(2026, 9, 9, 12, 34, 56)

    path = rpbg.default_log_path(tmp_path, "/", clock=clock)

    assert path.name == "20260909-123456-topnet.log"


class _FakeProc:
    def __init__(self, pid):
        self.pid = pid


class _FakePopen:
    def __init__(self, pid=4321):
        self.calls = []
        self._pid = pid

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return _FakeProc(self._pid)


def test_launch_creates_the_log_files_parent_directory(tmp_path):
    log_file = tmp_path / "bgcook" / "run.log"
    fake = _FakePopen()

    rpbg.launch(["hython", "--pdg", "topcook.py"], log_file, popen=fake)

    assert log_file.parent.is_dir()
    assert log_file.is_file()  # opened for append, so it exists even empty


def test_launch_wires_stdout_and_stderr_to_the_log_file_and_closes_stdin(tmp_path):
    log_file = tmp_path / "run.log"
    fake = _FakePopen()

    rpbg.launch(["hython"], log_file, popen=fake)

    command, kwargs = fake.calls[0]
    assert command == ["hython"]
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stderr"] == subprocess.STDOUT
    assert kwargs["stdout"].name == str(log_file)
    # Closed here in the parent right after Popen starts the child --
    # correct, not a bug: Popen dups the fd for the child before this
    # returns, same as any other file object handed to it.
    assert kwargs["stdout"].closed


def test_launch_detaches_from_this_process_on_posix(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "name", "posix")
    log_file = tmp_path / "run.log"
    fake = _FakePopen()

    rpbg.launch(["hython"], log_file, popen=fake)

    _, kwargs = fake.calls[0]
    assert kwargs.get("start_new_session") is True
    assert "creationflags" not in kwargs


def test_launch_detaches_from_this_process_on_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    log_file = tmp_path / "run.log"
    fake = _FakePopen()

    rpbg.launch(["hython"], log_file, popen=fake)

    _, kwargs = fake.calls[0]
    # Windows-only subprocess attributes -- their real, documented Win32
    # values, since this test runs on macOS/Linux where subprocess does
    # not define them at all (see background_cook.launch's own comment).
    detached_process = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
    create_new_process_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    assert kwargs.get("creationflags") == detached_process | create_new_process_group
    assert "start_new_session" not in kwargs


def test_launch_returns_the_childs_pid(tmp_path):
    log_file = tmp_path / "run.log"
    fake = _FakePopen(pid=9999)

    pid = rpbg.launch(["hython"], log_file, popen=fake)

    assert pid == 9999


def test_launch_never_blocks_waiting_for_the_child():
    """Popen itself never blocks -- this is really just documenting the
    contract (no .wait()/.communicate() call anywhere in launch)."""
    import ast
    import pathlib
    import inspect

    source = inspect.getsource(rpbg.launch)
    tree = ast.parse(source)
    calls = {n.func.attr for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "wait" not in calls
    assert "communicate" not in calls
