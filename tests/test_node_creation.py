"""Does the node come into existence at all?

The question a thousand other tests never asked. On 2026-09-07 the whole
suite was green while ``RunPodFarmScheduler.__init__`` raised on its first
line, so no farm node could be created in any scene -- every existing test
lifted METHODS out of the asset and drove them with stand-ins, which cannot
see a constructor that does not run.

Two layers, because they fail differently:

* the static one always runs and catches THIS bug's shape -- a guard in
  ``_reset_cook_state`` reading an attribute ``__init__`` has not set yet;
* the live one shells out to hython and really creates all four nodes plus
  the scheduler object. Skipped where Houdini is not installed (CI), which
  is exactly why the static test is not merely a duplicate of it.
"""

from __future__ import annotations

import ast
import os
import pathlib
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
MODULE = (REPO / "hda" / "runpodfarm_scheduler.hda"
          / "Top_1runpodfarmscheduler" / "PythonModule")


def _class_def():
    for node in ast.walk(ast.parse(MODULE.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ClassDef) and node.name == "RunPodFarmScheduler":
            return node
    raise AssertionError("the scheduler class is gone")


def _method(name):
    for node in _class_def().body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError("{} is gone".format(name))


def _attributes_read(fn):
    return {n.attr for n in ast.walk(fn)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
            and n.value.id == "self" and isinstance(n.ctx, ast.Load)}


def _class_attributes():
    return {t.id for node in _class_def().body
            if isinstance(node, ast.Assign) for t in node.targets
            if isinstance(t, ast.Name)}


def test_anything_init_calls_may_only_read_what_exists_already():
    """__init__ ends by calling _reset_cook_state, which therefore runs on an
    object with no instance attributes. Everything it reads has to be a CLASS
    attribute, or the node cannot be created -- safe by construction, not by
    the order of two lines somebody may reshuffle."""
    init = _method("__init__")
    called = {n.func.attr for n in ast.walk(init)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
              and isinstance(n.func.value, ast.Name) and n.func.value.id == "self"}
    assert "_reset_cook_state" in called, "the shape of __init__ changed -- recheck this test"

    available = _class_attributes()
    for name in sorted(called):
        try:
            method = _method(name)
        except AssertionError:
            continue  # inherited from a mixin; not ours to reason about
        unsafe = {a for a in _attributes_read(method) if a.startswith("_")} - available
        # Attributes assigned by the method itself before being read are fine.
        assigned = {t.attr for n in ast.walk(method) if isinstance(n, ast.Assign)
                    for t in n.targets if isinstance(t, ast.Attribute)}
        unsafe -= assigned
        assert not unsafe, (
            "{}() reads {} on an object __init__ has not finished building. "
            "Give them class-level defaults.".format(name, sorted(unsafe)))


def test_the_active_cook_key_is_a_class_attribute():
    """The specific fix, pinned: _ACTIVE_COOKS bookkeeping must not need an
    instance to exist before it can be asked about."""
    assert "_active_cook_key" in _class_attributes()


# -- the live one ------------------------------------------------------------


def _hython():
    for candidate in sorted(pathlib.Path("/Applications/Houdini").glob(
            "Houdini*/Frameworks/Houdini.framework/Versions/Current/Resources/bin/hython"),
            reverse=True):
        return str(candidate)
    return None


@pytest.mark.skipif(_hython() is None, reason="no local Houdini")
def test_every_farm_node_can_actually_be_created():
    """Creates all four nodes and constructs the scheduler in real Houdini.

    Slow (a Houdini start), and worth it: this is the only test that would
    have caught 2026-09-07 before the artist did.
    """
    env = dict(os.environ, RPFARM_ROOT=str(REPO))
    proc = subprocess.run([_hython(), str(REPO / "scripts" / "node_creation_smoke.py")],
                          capture_output=True, text=True, timeout=600, env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    for type_name in ("runpodfarmscheduler", "runpodfarmupload",
                      "runpodfarmdownload", "runpodfarmstats"):
        assert "[OK]   {} created clean".format(type_name) in proc.stdout, proc.stdout
    assert "RunPodFarmScheduler() constructed" in proc.stdout, proc.stdout
