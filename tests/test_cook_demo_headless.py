"""Ruling R63, the third of the three affected call sites: cook_demo_headless.py
forced RPFARM_ROOT through a child hython's env= the same way rpfarm.smoke did,
which a real houdini.env (every artist's, since the R62 decoupling) silently
overrides after the child starts.

--dock-env is deliberately untouched here: that mode does not want to force
any particular package, it wants to see what the artist's own real houdini.env
actually does -- forcing it would test the wrong thing.
"""

import importlib.util
import os
import pathlib
import sys
import types

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "cook_demo_headless.py"


def _load():
    spec = importlib.util.spec_from_file_location("cook_demo_headless", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


demo = _load()


class FakePopen:
    """Captures env= instead of actually launching anything."""

    last_env = None

    def __init__(self, cmd, cwd=None, env=None, **kwargs):
        FakePopen.last_env = env
        self.stdout = []
        self.returncode = 0

    def poll(self):
        return self.returncode

    def wait(self):
        return self.returncode


def _fake_install(tmp_path):
    real_prefs = tmp_path / "real_prefs"
    real_prefs.mkdir()
    (real_prefs / "houdini.env").write_text('RPFARM_ROOT = "/some/other/pkg"\n')
    return types.SimpleNamespace(
        hython=tmp_path / "hython", user_pref_dir=real_prefs, major_minor="22.0")


def test_dock_env_mode_does_not_force_rpfarm_root(monkeypatch, tmp_path):
    """Unaffected by R63 on purpose -- proven so this stays true."""
    monkeypatch.setattr(demo.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(demo, "threading", types.SimpleNamespace(
        Timer=lambda *a, **k: types.SimpleNamespace(daemon=False, start=lambda: None, cancel=lambda: None)))
    inst = _fake_install(tmp_path)

    demo._run_hython(inst, str(tmp_path / "payload.json"), 10, lambda m: None, dock_env=True)

    assert "RPFARM_ROOT" not in FakePopen.last_env
    assert FakePopen.last_env["PATH"] == "/usr/bin:/bin"


def test_non_dock_env_mode_forces_rpfarm_root_over_a_real_houdini_env(monkeypatch, tmp_path):
    monkeypatch.setattr(demo.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(demo, "threading", types.SimpleNamespace(
        Timer=lambda *a, **k: types.SimpleNamespace(daemon=False, start=lambda: None, cancel=lambda: None)))
    inst = _fake_install(tmp_path)

    demo._run_hython(inst, str(tmp_path / "payload.json"), 10, lambda m: None,
                     dock_env=False, scratch_dir=tmp_path / "scratch")

    env = FakePopen.last_env
    scratch_env = pathlib.Path(env["HOUDINI_USER_PREF_DIR"].replace("__HVER__", "22.0")) / "houdini.env"
    text = scratch_env.read_text()
    assert 'RPFARM_ROOT = "{}"'.format(demo.REPO) in text
    assert "/some/other/pkg" not in text
