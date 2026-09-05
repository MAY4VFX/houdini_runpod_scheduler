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
    (tmp_path / "Balon_Base_color_notatile.exr").write_bytes(b"x")
    raw = str(tmp_path / "Balon_Base_color_<UDIM>.exr")

    # USD hands back an EMPTY resolvedPath for these -- measured, not assumed
    got = usddeps.expand_asset(raw, resolved="", layer_dir=str(tmp_path))

    assert got == sorted(str(tmp_path / "Balon_Base_color_{}.exr".format(t))
                         for t in ("1001", "1002", "1011"))


def test_udim_matches_four_digits_not_anything(tmp_path):
    assert usddeps.glob_pattern("/t/a_<UDIM>.exr") == "/t/a_[0-9][0-9][0-9][0-9].exr"
    assert usddeps.glob_pattern("/t/a_<udim>.exr") == "/t/a_[0-9][0-9][0-9][0-9].exr"
    # any other token degrades to a wildcard; glob still only returns real files
    assert usddeps.glob_pattern("/t/<ATTR:name>.exr") == "/t/*.exr"
    assert not usddeps.is_template("/t/plain.exr")


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
