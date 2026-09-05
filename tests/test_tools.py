"""Resolving external binaries without trusting PATH.

The bug this is about (2026-09-05): an upload item died with
``FileNotFoundError: 'zstd'`` on a machine where zstd is installed at
/opt/homebrew/bin -- a directory a Dock-launched Houdini does not have on
its PATH. Same family as the Xcode-python3 defect: what the developer's
shell can find says nothing about what the cook can find.
"""

import os
import stat
import subprocess
from types import SimpleNamespace

import pytest

from rpfarm import tools


@pytest.fixture(autouse=True)
def _no_cache():
    tools.clear_cache()
    yield
    tools.clear_cache()


def _fake_run(good):
    """A subprocess.run that only succeeds for the paths in *good*."""
    def run(argv, **kwargs):
        if argv[0] not in good:
            raise FileNotFoundError(2, "No such file or directory", argv[0])
        return SimpleNamespace(returncode=0, stdout="zstd 1.5.6\n", stderr="")
    return run


def test_a_tool_off_the_path_is_still_found(tmp_path):
    """The whole point: PATH says no, the tool is right there."""
    brew = tmp_path / "opt" / "homebrew" / "bin"
    brew.mkdir(parents=True)
    binary = brew / "zstd"
    binary.write_text("#!/bin/sh\n")

    got = tools.resolve_tool("zstd", extra_dirs=(str(brew),),
                             which=lambda name, path=None: None,
                             run=_fake_run({str(binary)}))

    assert got.path == str(binary)
    assert got.how == str(brew)
    assert got.version == "zstd 1.5.6"


def test_path_wins_when_path_is_right(tmp_path):
    binary = tmp_path / "zstd"
    binary.write_text("#!/bin/sh\n")

    got = tools.resolve_tool("zstd", which=lambda name, path=None: str(binary),
                             run=_fake_run({str(binary)}))

    assert got.path == str(binary) and got.how == "on PATH"


def test_a_binary_that_exists_but_does_not_run_is_not_accepted(tmp_path):
    """Existing at the right path under the right name is not proof --
    the same rule resolve_package_python learned the hard way."""
    good = tmp_path / "good" / "zstd"
    good.parent.mkdir()
    good.write_text("#!/bin/sh\n")
    broken = tmp_path / "broken" / "zstd"
    broken.parent.mkdir()
    broken.write_text("not a binary")

    got = tools.resolve_tool("zstd", extra_dirs=(str(broken.parent), str(good.parent)),
                             which=lambda name, path=None: None,
                             run=_fake_run({str(good)}))

    assert got.path == str(good)


def test_nothing_anywhere_is_none_not_an_exception():
    """The caller decides what to do about a missing tool; the resolver
    never decides it by raising."""
    assert tools.resolve_tool("definitely-not-installed-xyz",
                              which=lambda name, path=None: None,
                              run=_fake_run(set())) is None


def test_the_answer_is_cached(tmp_path):
    binary = tmp_path / "zstd"
    binary.write_text("#!/bin/sh\n")
    calls = []

    def run(argv, **kwargs):
        calls.append(argv[0])
        return SimpleNamespace(returncode=0, stdout="zstd 1.5.6", stderr="")

    for _ in range(3):
        tools.resolve_tool("zstd", which=lambda name, path=None: str(binary), run=run)

    assert len(calls) == 1


def test_the_cook_s_path_can_be_asked_about_specifically():
    """`doctor` has to answer "would a COOK find this", not "can my shell".
    Restricting the PATH lookup is how the report stops flattering itself."""
    asked = {}

    def which(name, path=None):
        asked["path"] = path
        return None

    tools.resolve_tool("zstd", which=which, run=_fake_run(set()),
                       path=tools.HOUDINI_LIKE_PATH)

    assert asked["path"] == tools.HOUDINI_LIKE_PATH
    assert "/opt/homebrew/bin" not in tools.HOUDINI_LIKE_PATH, (
        "the PATH a Dock-launched Houdini gets is exactly the one missing Homebrew")


@pytest.mark.skipif(not os.path.exists("/bin/ls"), reason="POSIX only")
def test_it_resolves_a_real_binary_on_this_machine():
    got = tools.resolve_tool("ls", verify=("--help",))
    assert got is None or os.path.isabs(got.path)
