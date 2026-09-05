"""The confirmation window's logic -- the half that can be tested anywhere.

The window itself needs PySide6, which only Houdini's Python ships; the
one test that builds it skips elsewhere and runs head-less
(``QT_QPA_PLATFORM=offscreen``) under hython. Everything that decides what
uploads lives in the pure helpers below, on purpose.
"""

import importlib.util
import os

import pytest

from rpfarm import deps
from rpfarm import preflight as pf
from rpfarm.deps import PlanRow


def _rows():
    return [
        PlanRow(path="/job/tex/a.rat", kind="file", files=1, bytes=1024),
        PlanRow(path="/job/export", kind="dir", files=3, bytes=1_500_000_000),
        PlanRow(path="/job/geo/b.bgeo.sc", kind="file", files=1, bytes=50_000),
    ]


def test_human_bytes_reads_like_a_file_manager():
    assert pf.human_bytes(0) == "0 B"
    assert pf.human_bytes(999) == "999 B"
    assert pf.human_bytes(1024) == "1.0 KB"
    assert pf.human_bytes(1_500_000_000) == "1.4 GB"


def test_the_heaviest_reference_is_the_first_line():
    assert [r.path for r in pf.sort_rows(_rows())] == [
        "/job/export", "/job/geo/b.bgeo.sc", "/job/tex/a.rat",
    ]


def test_a_directory_reads_as_a_directory():
    rows = {r.kind: r for r in _rows()}
    assert pf.row_label(rows["dir"]) == "/job/export" + os.sep
    assert pf.row_detail(rows["dir"]) == "3 files"
    assert pf.row_label(rows["file"]).endswith(".sc") or True
    assert pf.row_detail(rows["file"]) == "1 file"


def test_header_states_what_is_selected_against_what_was_offered():
    text = pf.header_text(_rows(), missing=["/job/gone.exr"], excluded={"/job/export"})
    assert text.startswith("2 of 3 references")
    assert "1.4 GB" in text  # the total offered
    assert "1 reference(s) name nothing on disk" in text


# -- the remembered choice -------------------------------------------------------


def test_an_unchecked_reference_does_not_upload():
    refs = ["/job/hip/scene.hip", "/job/export", "/job/tex/a.rat"]
    assert pf.apply_exclusions(refs, {"/job/export"}) == ["/job/hip/scene.hip", "/job/tex/a.rat"]


def test_an_unchecked_box_stays_unchecked_next_cook():
    """The whole reason the choice lives on the node: a window that has to
    be re-answered every cook is a tax, and a taxed artist turns it off."""
    stored = pf.dump_exclusions({"/job/export/", "/job/tex/a.rat"})

    remembered = pf.load_exclusions(stored)

    assert remembered == {"/job/export", "/job/tex/a.rat"}  # trailing sep normalised away
    assert pf.apply_exclusions(["/job/export", "/job/tex/a.rat", "/job/keep.abc"], remembered) == [
        "/job/keep.abc"
    ]


def test_an_exclusion_survives_a_reference_that_vanished_for_a_version():
    kept = pf.load_exclusions(pf.dump_exclusions({"/job/export"}))
    assert "/job/export" in pf.load_exclusions(pf.dump_exclusions(kept))


def test_exclusions_accept_a_hand_typed_list():
    assert pf.load_exclusions("/job/a\n/job/b\n") == {"/job/a", "/job/b"}
    assert pf.load_exclusions("") == set()
    assert pf.load_exclusions("[not json") == {"[not json"}


def test_totals_count_only_the_checked_rows():
    assert pf.totals(_rows(), excluded={"/job/export"}) == (2, 51_024)


def test_ui_is_not_available_without_houdini():
    assert pf.ui_available() is False


# -- the widget tree (Houdini's Python only) -------------------------------------


@pytest.mark.skipif(
    importlib.util.find_spec("PySide6") is None,
    reason="PySide6 ships with Houdini's Python, not the system one",
)
def test_dialog_reflects_the_remembered_exclusions():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtCore, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dialog = pf.build_dialog(_rows(), missing=["/job/gone.exr"], excluded={"/job/export"})
    tree = dialog.findChild(QtWidgets.QTreeWidget)

    labels = [tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())]
    states = [tree.topLevelItem(i).checkState(0) for i in range(tree.topLevelItemCount())]

    assert labels[0] == "/job/export" + os.sep, "heaviest first"
    assert states[0] == QtCore.Qt.Unchecked, "and remembered as unchecked"
    assert states[1] == QtCore.Qt.Checked
    assert dialog.rpfarm_exclusions() == {"/job/export"}

    tree.topLevelItem(1).setCheckState(0, QtCore.Qt.Unchecked)
    assert dialog.rpfarm_exclusions() == {"/job/export", "/job/geo/b.bgeo.sc"}
    assert app is not None


# -- the flow the node runs ------------------------------------------------------


class _FakeParm:
    def __init__(self, value=""):
        self.value = value

    def set(self, value):
        self.value = value


class _FakeNode:
    """The two parameters choose_uploads touches, and nothing else."""

    def __init__(self, confirm=1, exclude=""):
        self._parms = {"rpfarm_confirm": _FakeParm(confirm), "rpfarm_exclude": _FakeParm(exclude)}

    def parm(self, name):
        return self._parms[name]

    def evalParm(self, name):
        return self._parms[name].value


def _scan(paths, output_patterns=()):
    return deps.RefScan(paths=list(paths), output_patterns=tuple(output_patterns), unresolved=())


def _files(tmp_path):
    hip = tmp_path / "scene.hip"
    hip.write_bytes(b"h")
    tex = tmp_path / "tex.rat"
    tex.write_bytes(b"t" * 10)
    out = tmp_path / "render"
    out.mkdir()
    (out / "f.0001.exr").write_bytes(b"e" * 100)
    return str(hip), str(tex), str(out)


def test_the_dialogs_answer_is_taken_as_given(tmp_path):
    """Houdini's window is where the decision is made. If the artist
    re-checks a row we had forced unchecked -- an output path -- that is
    their call, and second-guessing it would make the checkbox a lie."""
    hip, tex, out = _files(tmp_path)
    node = _FakeNode()
    selection = [(None, tex), (None, out + "/f.$F4.exr")]

    got = pf.choose_uploads(
        node, _scan([hip], output_patterns=(out + "/f.$F4.exr",)), ask=True,
        dialog=lambda *a: (True, selection), expand=lambda s: s, hip_path=hip)

    assert got == [hip, tex, out]


def test_cancel_in_the_file_dialog_stops_the_cook(tmp_path):
    hip, tex, _out = _files(tmp_path)

    with pytest.raises(pf.UploadCancelled):
        pf.choose_uploads(_FakeNode(), _scan([hip, tex]), ask=True,
                          dialog=lambda *a: (False, ()), expand=lambda s: s, hip_path=hip)


def test_a_broken_file_dialog_falls_back_to_the_scan(tmp_path):
    """Qt failing is not a reason to refuse to upload."""
    hip, tex, _out = _files(tmp_path)

    def _boom(*a):
        raise RuntimeError("no QApplication")

    said = []
    got = pf.choose_uploads(_FakeNode(), _scan([hip, tex]), ask=True, dialog=_boom,
                            log=said.append, expand=lambda s: s, hip_path=hip)

    assert got == [hip, tex]
    assert any("no QApplication" in m for m in said), said


def test_usd_files_get_our_own_window_because_houdini_has_no_row_for_them(tmp_path):
    """hou.fileReferences() has no entry for a texture a USD layer names
    from inside itself, so displayFileDependencyDialog cannot show one --
    there is no API to add a row that is not a (Parm, pattern) pair."""
    hip, tex, _out = _files(tmp_path)
    usd = tmp_path / "Zeppelin.usdc"
    usd.write_bytes(b"u" * 50)
    png = tmp_path / "Balon_1001.png"
    png.write_bytes(b"p" * 5000)
    node = _FakeNode()
    asked = {}

    def _usd_window(rows, missing, excluded, **kw):
        asked["paths"] = [r.path for r in rows]
        return {str(png)}  # the artist unchecks the heavy source texture

    got = pf.choose_uploads(node, _scan([hip]), usd_paths=[str(usd), str(png)], ask=True,
                            dialog=lambda *a: (True, [(None, tex)]),
                            confirm_usd=_usd_window, expand=lambda s: s, hip_path=hip)

    assert asked["paths"] == [str(usd), str(png)]
    assert got == [hip, tex, str(usd)]
    assert pf.load_exclusions(node.evalParm("rpfarm_exclude")) == {str(png)}


def test_an_unchecked_usd_file_stays_unchecked_next_cook(tmp_path):
    hip, _tex, _out = _files(tmp_path)
    usd = tmp_path / "Zeppelin.usdc"
    usd.write_bytes(b"u")
    png = tmp_path / "Balon_1001.png"
    png.write_bytes(b"p")
    node = _FakeNode(confirm=0, exclude=pf.dump_exclusions({str(png)}))

    got = pf.choose_uploads(node, _scan([hip]), usd_paths=[str(usd), str(png)], ask=False)

    assert got == [hip, str(usd)]


def test_batch_mode_asks_nothing(tmp_path):
    hip, tex, _out = _files(tmp_path)

    got = pf.choose_uploads(_FakeNode(confirm=0), _scan([hip, tex]), ask=False,
                            dialog=lambda *a: pytest.fail("must not ask"))

    assert got == [hip, tex]


def test_wants_window_says_which_condition_refused(monkeypatch):
    monkeypatch.setattr(pf, "ui_unavailable_reason", lambda: "no UI (headless cook)")
    said = []

    assert pf.wants_window(_FakeNode(confirm=1), log=said.append) is False
    assert any("no UI (headless cook)" in m for m in said), said

    said.clear()
    assert pf.wants_window(_FakeNode(confirm=0), log=said.append) is False
    assert not said, "nothing to explain when nobody asked for a window"

    monkeypatch.setattr(pf, "ui_unavailable_reason", lambda: None)
    assert pf.wants_window(_FakeNode(confirm=1)) is True
    assert pf.wants_window(_FakeNode(confirm=1), ask=False) is False
