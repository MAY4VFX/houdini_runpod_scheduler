"""The checked-in expanded HDAs must match the builders that generate them.

House rule since the first HDA landed: ``hda/*.hda/`` is the git-tracked
expansion (``hotl -t``) of what ``scripts/build_runpodfarm_*_hda.py``
produces, and the two are edited together. Nothing enforced it, and
finding 2 of the final whole-branch review is what that cost: a
placeholder auto-grow block from before Task 12 stayed baked into
``runpodfarm_upload.hda`` long after the real check landed in
``rpfarm.packages.maybe_grow_volume``. It ran first, at ``onGenerate``,
with its own coarser formula -- a 45 GB upload onto a 50 GB volume grew
it to 100 GB where the real check grows it to 60 GB. RunPod volumes never
shrink and bill on allocated size, so that was money you could not get
back.

These tests read only tracked files -- no Houdini, no hython, no network.
"""

import ast
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent

# builder script -> the asset directory it generates
BUILDERS = {
    "build_runpodfarm_upload_hda.py": "runpodfarm_upload.hda",
    "build_runpodfarm_download_hda.py": "runpodfarm_download.hda",
    "build_runpodfarm_stats_hda.py": "runpodfarm_stats.hda",
}

# Which module-level string constants of a builder end up inside the asset.
# (Every builder's own docstring, path constants and so on do not.)
EMBEDDED = ("PYTHON_MODULE", "HELP_TEXT")


def _string_constants(builder):
    """Module-level ``NAME = "..."`` assignments in a builder, by name.

    Parsed rather than regexed so the value is the real runtime string --
    ``"\\n"`` in the builder source is a two-character ``\\n`` in the code
    that gets baked in, and comparing source text instead would never
    line up.
    """
    tree = ast.parse((REPO / "scripts" / builder).read_text())
    out = {}
    for node in tree.body:
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            out[target.id] = node.value.value
    return {k: v for k, v in out.items() if k.endswith("_CODE") or k in EMBEDDED}


def _asset_text(asset):
    """Every tracked text file of an expanded asset, concatenated."""
    out = []
    for path in sorted((REPO / "hda" / asset).rglob("*")):
        if not path.is_file():
            continue
        try:
            out.append(path.read_text())
        except (UnicodeDecodeError, OSError):
            continue  # .OPdummydefs and friends are binary
    return "\n".join(out)


def _mime_escape(code):
    """How ``hotl -t`` writes a node callback into ``Contents.mime``.

    Callbacks live inside a double-quoted field, so backslash and quote
    are escaped and nothing else changes. Section files (``PythonModule``,
    ``Help``) are written verbatim instead, which is why the assertions
    below accept either form.
    """
    return code.replace("\\", "\\\\").replace('"', '\\"')


@pytest.mark.parametrize("builder,asset", sorted(BUILDERS.items()))
def test_expanded_hda_matches_its_builder(builder, asset):
    shipped = _asset_text(asset)
    blocks = _string_constants(builder)
    assert blocks, f"{builder} embeds no code constants -- did the naming change?"
    for name, code in blocks.items():
        assert code in shipped or _mime_escape(code) in shipped, (
            f"{name} in scripts/{builder} is not what hda/{asset} ships -- the "
            f"checked-in asset is stale. Rebuild it:\n"
            f"  hython scripts/{builder} /tmp/out.hda\n"
            f"  rm -rf hda/{asset} && hotl -t hda/{asset} /tmp/out.hda"
        )


def test_upload_hda_does_not_resize_the_volume_itself():
    """Finding 2: the one volume auto-grow is packages.maybe_grow_volume.

    A second grower with its own formula is not a redundant check, it is a
    bigger bill: whichever runs first wins, and a RunPod volume never
    shrinks back.
    """
    shipped = _asset_text("runpodfarm_upload.hda")
    assert "resize_volume" not in shipped
    assert "TODO(Task 12)" not in shipped
    assert "maybe_grow_volume" in shipped, "the real check must be the one that is there"


def test_both_upload_paths_run_the_real_auto_grow_check():
    """In-process (debug) and out-of-process both go through the same check
    -- deleting the stale onGenerate block must not leave the in-process
    path with no guard at all."""
    builder = (REPO / "scripts" / "build_runpodfarm_upload_hda.py").read_text()
    runner = (REPO / "rpfarm" / "package_runner.py").read_text()
    assert "maybe_grow_volume" in _string_constants("build_runpodfarm_upload_hda.py")["COOKTASK_CODE"]
    assert "maybe_grow_volume" in runner
    assert "resize_volume" not in builder


def test_no_builder_resizes_the_volume_itself():
    for builder, asset in BUILDERS.items():
        assert "resize_volume" not in (REPO / "scripts" / builder).read_text(), builder
        assert "resize_volume" not in _asset_text(asset), asset
