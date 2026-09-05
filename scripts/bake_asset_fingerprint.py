"""Bake "what this asset was built against" into every guarded asset.

An asset and the ``rpfarm`` package are one product shipped in two files.
The guard compares them at cook time, so the asset has to carry a record of
the package it was built against -- and that record has to be regenerated
whenever the package changes, or the guard is comparing against history.

Nobody has to remember to run this: ``tests/test_hda_assets.py`` recomputes
the fingerprint and fails when a shipped asset's copy is stale, which is the
same discipline problem the version number failed at, solved the way it
should have been the first time.

    python3 scripts/bake_asset_fingerprint.py          # rewrite the blocks
    python3 scripts/bake_asset_fingerprint.py --check  # just report drift

After baking, rebuild the two generated assets (upload, stats) and reinstall
all three -- see each builder's docstring.
"""

import argparse
import importlib.util
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent

#: Every file that carries a baked block. Two are builder SOURCES (the block
#: is inside the PYTHON_MODULE string they emit), one is a hand-edited asset.
TARGETS = (
    REPO / "scripts" / "build_runpodfarm_upload_hda.py",
    REPO / "scripts" / "build_runpodfarm_stats_hda.py",
    REPO / "hda" / "runpodfarm_scheduler.hda" / "Top_1runpodfarmscheduler" / "PythonModule",
)


def _hda_guard():
    spec = importlib.util.spec_from_file_location("hda_guard", REPO / "scripts" / "hda_guard.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def current_block(guard):
    """The block as it should be right now, for the package in this checkout."""
    sys.path.insert(0, str(REPO))
    import rpfarm

    return guard.fingerprint_block(rpfarm.fingerprint(str(REPO / "rpfarm")), rpfarm.VERSION)


def _indent_of(text, marker):
    line = next(l for l in text.splitlines() if l.strip().startswith(marker))
    return line[:len(line) - len(line.lstrip())]


def rewrite(path, guard, block, check=False):
    """Replace the baked block in *path*. Returns True when it changed."""
    text = path.read_text(encoding="utf-8")
    if guard.BAKE_BEGIN not in text:
        return False
    indent = _indent_of(text, guard.BAKE_BEGIN)
    body = "\n".join((indent + line if line else "") for line in block.splitlines())
    # A builder holds the block inside a non-raw '''...''' literal, where a
    # backslash would be re-interpreted; the block is plain ASCII digits and
    # quotes, so there is nothing to escape -- asserted rather than assumed.
    assert "\\" not in body, "a fingerprint block must never contain a backslash"
    pattern = re.compile(
        re.escape(indent + guard.BAKE_BEGIN) + r".*?" + re.escape(guard.BAKE_END),
        re.S)
    new_text = pattern.sub(lambda _m: body.rstrip("\n"), text, count=1)
    if new_text == text:
        return False
    if not check:
        path.write_text(new_text, encoding="utf-8")
    return True


def main(argv=None):
    args = argparse.ArgumentParser(description=__doc__).parse_args(argv)
    guard = _hda_guard()
    block = current_block(guard)
    stale = [p for p in TARGETS if rewrite(p, guard, block, check=False)]
    for path in stale:
        print("baked", path.relative_to(REPO))
    if not stale:
        print("all baked fingerprints were already current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
