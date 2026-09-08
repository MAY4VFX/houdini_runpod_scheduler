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
import shutil
import subprocess
import types

import pytest

from rpfarm import houdini_local as hl

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
#
# Ruling R63: forcing RPFARM_ROOT through a child hython's os.environ is not
# proof of anything on a machine whose houdini.env sets its own -- houdini.env
# is documented to override the environment, and it does so AFTER the child
# process has already started, so an env= override handed to subprocess.run
# is exactly what it silently defeats. Measured live, 2026-09-08: with an
# ambient houdini.env in place, this test's own RPFARM_ROOT override was
# ignored and the child imported a different package than the one asked for
# -- caught only by reading a built .hda's baked fingerprint with `strings`,
# never by this test, which stayed green throughout.
#
# The fix is HOUDINI_USER_PREF_DIR pointed at a directory with no
# houdini.env in it at all, so there is nothing left to override anything --
# but that alone is not enough (verified live): a fresh prefs directory also
# has no otls, so node creation fails for an unrelated reason, and Houdini
# silently IGNORES a HOUDINI_USER_PREF_DIR value that does not carry Houdini's
# own "__HVER__" placeholder token and falls back to the real prefs dir --
# both would-be fixes that "look right" and are not. So this builds a real,
# isolated prefs directory, installs THIS checkout's own HDAs into it, and
# only then points a child hython there.


def _local_install():
    installs = hl.find_houdini_installations()
    return installs[0] if installs else None


def _isolated_prefs_env(tmp_path, install, extra=None):
    """``(user_pref_dir, subprocess_env)`` for a child hython that reads
    ONLY what this call explicitly gives it: no houdini.env, no otls but
    what is installed into it here. ``user_pref_dir`` already has
    ``__HVER__`` substituted (for local use, e.g. installing HDAs into it);
    the env dict carries the placeholder form, exactly as Houdini itself
    requires it (verified live -- a literal, already-substituted path in
    HOUDINI_USER_PREF_DIR is silently ignored, logged as "EnvControl:
    HOUDINI_USER_PREF_DIR missing __HVER__, ignored", and Houdini falls
    back to the real prefs dir -- the ambient houdini.env this whole thing
    exists to get away from).
    """
    placeholder = str(tmp_path / "prefs_v__HVER__")
    user_pref_dir = pathlib.Path(placeholder.replace("__HVER__", install.major_minor))
    env = dict(os.environ, **(extra or {}))
    env["HOUDINI_USER_PREF_DIR"] = placeholder
    return user_pref_dir, env


def _install_checkout_hdas(user_pref_dir, install, root, tmp_path):
    fake_install = types.SimpleNamespace(hotl=install.hotl, hython=install.hython,
                                         user_pref_dir=user_pref_dir)
    cache = tmp_path / "otls_cache"
    cache.mkdir(exist_ok=True)
    results = hl.build_and_install_hdas(fake_install, cache, root=root)
    assert all(r["ok"] for r in results), results
    return results


@pytest.mark.skipif(_local_install() is None, reason="no local Houdini")
def test_every_farm_node_can_actually_be_created(tmp_path):
    """Creates all four nodes and constructs the scheduler in real Houdini.

    Slow (a Houdini start), and worth it: this is the only test that would
    have caught 2026-09-07 before the artist did. Isolated (see the module
    docstring above): this checkout's own HDAs are installed into a scratch
    prefs directory with no houdini.env, so the result reflects THIS
    checkout regardless of what any real houdini.env on the machine says.
    """
    install = _local_install()
    user_pref_dir, env = _isolated_prefs_env(tmp_path, install, {"RPFARM_ROOT": str(REPO)})
    _install_checkout_hdas(user_pref_dir, install, REPO, tmp_path)

    proc = subprocess.run([str(install.hython), str(REPO / "scripts" / "node_creation_smoke.py")],
                          capture_output=True, text=True, timeout=600, env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    for type_name in ("runpodfarmscheduler", "runpodfarmupload",
                      "runpodfarmdownload", "runpodfarmstats"):
        assert "[OK]   {} created clean".format(type_name) in proc.stdout, proc.stdout
    assert "RunPodFarmScheduler() constructed" in proc.stdout, proc.stdout


@pytest.mark.skipif(_local_install() is None, reason="no local Houdini")
def test_the_isolation_actually_defeats_an_ambient_houdini_env(tmp_path):
    """The bar (verbatim): "the fix is only real if a test fails when the
    child loads the wrong copy. Make the checkout and the installed copy
    differ in something observable, then prove the child reports the one
    that was asked for."

    Builds a second, fake "installed" package with an observable marker
    (a patched VERSION), and a real houdini.env naming it as RPFARM_ROOT.
    First proves the OLD bug is real: a plain env= override, with that
    houdini.env in place, silently reads the wrong package. Then proves the
    isolated approach reads the checkout regardless.
    """
    install = _local_install()

    wrong_pkg = tmp_path / "wrong_pkg"
    shutil.copytree(REPO / "rpfarm", wrong_pkg / "rpfarm")
    init_py = wrong_pkg / "rpfarm" / "__init__.py"
    marked = init_py.read_text().replace('VERSION = "', 'VERSION = "WRONG-COPY-')
    assert marked != init_py.read_text(), "VERSION assignment not found to mark"
    init_py.write_text(marked)

    ambient_prefs = tmp_path / "ambient_v{}".format(install.major_minor)
    ambient_prefs.mkdir(parents=True)
    (ambient_prefs / "houdini.env").write_text(
        'RPFARM_ROOT = "{}"\n'.format(wrong_pkg))

    read_version = (
        "import os, sys\n"
        "_root = os.environ.get('RPFARM_ROOT')\n"
        "if _root and _root not in sys.path:\n"
        "    sys.path.insert(0, _root)\n"
        "import rpfarm\n"
        "print('VERSION=' + rpfarm.VERSION)\n"
    )

    # The old, defeated approach: HOUDINI_USER_PREF_DIR pointed straight at
    # a directory that HAS a houdini.env -- exactly an artist's real prefs.
    old_env = dict(os.environ, RPFARM_ROOT=str(REPO),
                   HOUDINI_USER_PREF_DIR=str(ambient_prefs))
    old_proc = subprocess.run([str(install.hython), "-c", read_version],
                              capture_output=True, text=True, timeout=120, env=old_env)
    assert "VERSION=WRONG-COPY-" in old_proc.stdout, (
        "the old bug did not reproduce -- this test's premise is wrong: "
        + old_proc.stdout + old_proc.stderr)

    # The fix: an isolated prefs directory with no houdini.env at all.
    _user_pref_dir, new_env = _isolated_prefs_env(tmp_path, install, {"RPFARM_ROOT": str(REPO)})
    new_proc = subprocess.run([str(install.hython), "-c", read_version],
                              capture_output=True, text=True, timeout=120, env=new_env)
    assert "VERSION=WRONG-COPY-" not in new_proc.stdout, (
        "the isolated env still read the wrong package: " + new_proc.stdout + new_proc.stderr)
    import rpfarm as _rpfarm
    assert "VERSION={}".format(_rpfarm.VERSION) in new_proc.stdout, (
        new_proc.stdout + new_proc.stderr)
