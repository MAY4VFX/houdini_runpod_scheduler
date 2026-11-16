"""The USD half of the dependency set.

Everything here is the pure resolution logic -- ``pxr`` is not importable
outside Houdini's own Python, and the stage walk is verified against the
owner's real scene instead (see the task report). What IS unit-tested is
what actually went wrong on that scene: UDIM templates whose resolvedPath
comes back empty, and relative asset paths that must resolve against the
layer that authored them rather than the process's working directory.
"""

import os

from rpfarm import usddeps


def test_a_udim_template_is_not_a_file(tmp_path):
    for tile in ("1001", "1002", "1011"):
        (tmp_path / "Balon_Base_color_{}.exr".format(tile)).write_bytes(b"x")
    raw = str(tmp_path / "Balon_Base_color_<UDIM>.exr")

    # USD hands back an EMPTY resolvedPath for these -- measured, not assumed
    got = usddeps.expand_asset(raw, resolved="", layer_dir=str(tmp_path))

    assert got == sorted(str(tmp_path / "Balon_Base_color_{}.exr".format(t))
                         for t in ("1001", "1002", "1011"))


def test_the_tile_pattern_is_a_wildcard_and_that_is_a_deliberate_trade(tmp_path):
    """One mechanism for every varying part of a path (Conductor's, adopted
    whole) means <UDIM> becomes `*` rather than `[0-9][0-9][0-9][0-9]`.
    Looser: a sibling sharing the prefix comes along. Accepted on purpose --
    the pattern is a node parameter, so a studio whose naming this bites can
    tighten it without waiting for us, and every extra file is a visible,
    uncheckable row in the window."""
    for name in ("Balon_Base_color_1001.exr", "Balon_Base_color_notatile.exr"):
        (tmp_path / name).write_bytes(b"x")
    raw = str(tmp_path / "Balon_Base_color_<UDIM>.exr")

    assert len(usddeps.expand_asset(raw, resolved="", layer_dir=str(tmp_path))) == 2
    # and tightening it is one parameter away
    tight = usddeps.expand_asset(raw, resolved="", layer_dir=str(tmp_path),
                                 asset_regex=r"<udim>")
    assert len(tight) == 2, "the token still becomes a wildcard; only the rest is narrower"


def test_a_relative_asset_resolves_against_its_layer_not_the_cwd(tmp_path):
    layer_dir = tmp_path / "Zeppelin_Balon_Test_Usd"
    (layer_dir / "textures").mkdir(parents=True)
    hdr = layer_dir / "textures" / "overcast_soil_puresky_7k_hdr.hdr"
    hdr.write_bytes(b"h")

    got = usddeps.expand_asset("./textures/overcast_soil_puresky_7k_hdr.hdr",
                               resolved="", layer_dir=str(layer_dir))

    assert got == [str(hdr)]


def test_usds_own_resolved_path_wins_when_it_is_real(tmp_path):
    real = tmp_path / "storm_graded.exr"
    real.write_bytes(b"s")

    assert usddeps.expand_asset("anything.exr", resolved=str(real), layer_dir="/nowhere") == [str(real)]


def test_a_resolved_path_that_is_gone_falls_through(tmp_path):
    real = tmp_path / "a.exr"
    real.write_bytes(b"a")

    assert usddeps.expand_asset(str(real), resolved=str(tmp_path / "gone.exr"),
                                layer_dir=str(tmp_path)) == [str(real)]


def test_nothing_on_disk_is_nothing_to_upload(tmp_path):
    assert usddeps.expand_asset("./missing.exr", resolved="", layer_dir=str(tmp_path)) == []
    assert usddeps.expand_asset("", resolved="", layer_dir=str(tmp_path)) == []


def test_stage_node_prefers_the_wired_input():
    class _Node:
        def __init__(self, path, inputs=(), loppath=None):
            self._path, self._inputs, self._loppath = path, list(inputs), loppath

        def path(self):
            return self._path

        def inputs(self):
            return self._inputs

        def parm(self, name):
            if name != "loppath" or self._loppath is None:
                return None
            return type("P", (), {"evalAsString": lambda _s: self._loppath})()

        def node(self, path):
            return _Node(path)

    lop = _Node("/stage/crypto_shot0012")
    assert usddeps.stage_node_of(_Node("/stage/render", inputs=[lop])) is lop
    assert usddeps.stage_node_of(_Node("/stage/render", loppath="/stage/other")).path() == "/stage/other"
    assert usddeps.stage_node_of(_Node("/stage/render")) is None


# -- texture scopes (BS sto_0050, 2026-11-16) ---------------------------------

def test_udim_tile_numbering():
    assert usddeps.udim_tile(0.5, 0.5) == 1001
    assert usddeps.udim_tile(1.2, 0.1) == 1002
    assert usddeps.udim_tile(0.3, 1.7) == 1011
    assert usddeps.udim_tile(5.9, 1.0) == 1016  # a UV exactly on the seam is the next tile


def test_udim_tiles_from_plain_pairs():
    assert usddeps.udim_tiles([(0.1, 0.1), (0.9, 0.2), (3.5, 1.5)]) == {1001, 1014}


def _tiles(tmp_path, name, numbers):
    for n in numbers:
        (tmp_path / "{}.{}.exr".format(name, n)).write_bytes(b"x")
    raw = str(tmp_path / "{}.<UDIM>.exr".format(name))
    return raw, usddeps.expand_asset(raw, resolved="", layer_dir=str(tmp_path))


def test_keep_tiles_keeps_only_tiles_under_uv(tmp_path):
    raw, files = _tiles(tmp_path, "mid_BaseColor_ACES - ACEScg", ("1001", "1002", "1012"))
    kept = usddeps.keep_tiles(raw, files, {1012})
    assert [os.path.basename(p) for p in kept] == ["mid_BaseColor_ACES - ACEScg.1012.exr"]


def test_keep_tiles_reads_the_token_position_not_the_last_digits(tmp_path):
    # the tile sits BEFORE a version number; "last four digits" would read 0003
    for n in ("1001", "1002"):
        (tmp_path / "deck_{}_v0003.exr".format(n)).write_bytes(b"x")
    raw = str(tmp_path / "deck_<UDIM>_v0003.exr")
    files = usddeps.expand_asset(raw, resolved="", layer_dir=str(tmp_path), asset_regex=r"<udim>")
    assert [os.path.basename(p) for p in usddeps.keep_tiles(raw, files, {1002})] == ["deck_1002_v0003.exr"]


def test_keep_tiles_never_drops_what_is_not_a_tile(tmp_path):
    raw, files = _tiles(tmp_path, "back1", ("1001",))
    stranger = tmp_path / "back1.notatile.exr"
    stranger.write_bytes(b"x")
    kept = usddeps.keep_tiles(raw, files + [str(stranger)], {1005})
    assert kept == [str(stranger)], "a match the template cannot place is kept, visible in the window"


def test_keep_tiles_none_and_non_udim_keep_everything(tmp_path):
    raw, files = _tiles(tmp_path, "bottom1", ("1001", "1002"))
    assert usddeps.keep_tiles(raw, files, None) == files
    flat = str(tmp_path / "wetness_deck_v001.exr")
    assert usddeps.keep_tiles(flat, [flat], {1001}) == [flat]


def test_asset_path_problems_names_schemes_and_leftover_variables():
    assert "resolver" in usddeps.asset_path_problems("ayon://blacksilk/assets/ship?product=usdAsset")
    assert "variable" in usddeps.asset_path_problems("$FOLDER/textures/a.<UDIM>.exr")
    assert "variable" in usddeps.asset_path_problems("${JOB}/tex.exr")
    assert usddeps.asset_path_problems("/studio/show/assets/tex.<UDIM>.exr") == ""
    assert usddeps.asset_path_problems("file:///abs/tex.exr") == ""
    assert usddeps.asset_path_problems("") == ""


def test_scopes_are_a_closed_menu():
    assert usddeps.TEXTURE_SCOPES == ("referenced", "stage", "udim")


class _F:
    def __init__(self, path):
        self.path = path


class _Item:
    def __init__(self, outputs=(), expected=()):
        self.outputFiles = [_F(p) for p in outputs]
        self.expectedOutputFiles = [_F(p) for p in expected]


def test_upstream_usd_files_takes_usd_outputs_once_in_order():
    items = [_Item(outputs=["/a/delta.usd", "/a/render.exr"]),
             _Item(expected=["/a/delta.usd", "/b/next.usdc"])]
    assert usddeps.upstream_usd_files(items) == ["/a/delta.usd", "/b/next.usdc"]
    assert usddeps.upstream_usd_files([]) == []


def test_relative_escapes_flags_only_paths_that_climb_out():
    refs = ["../../../../publish/usd/usdAsset/hero/ship_usd_usdAsset.usd",
            "/Users/x/abs.usd", "./sibling.usd", "sub/child.usd", "ayon://p/a"]
    got = usddeps.relative_escapes("/w/_gate/delta.usd", refs)
    assert len(got) == 1 and "Use Relative Paths" in got[0]
