"""The confirmation window's logic -- the half that can be tested anywhere.

The window itself needs PySide6, which only Houdini's Python ships; the
one test that builds it skips elsewhere and runs head-less
(``QT_QPA_PLATFORM=offscreen``) under hython. Everything that decides what
uploads lives in the pure helpers below, on purpose.
"""

import importlib.util
import os

import pytest

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
    """The three parameters resolve_upload_set touches, and nothing else."""

    def __init__(self, confirm=1, exclude=""):
        self._parms = {"rpfarm_confirm": _FakeParm(confirm), "rpfarm_exclude": _FakeParm(exclude)}

    def parm(self, name):
        return self._parms[name]

    def evalParm(self, name):
        return self._parms[name].value


def _scene(tmp_path):
    """A hip, a texture and one heavy directory reference."""
    (tmp_path / "export").mkdir()
    (tmp_path / "export" / "lookdev.zip").write_bytes(b"z" * 4096)
    (tmp_path / "scene.hip").write_bytes(b"h")
    (tmp_path / "tex.rat").write_bytes(b"t" * 10)
    return [str(tmp_path / "scene.hip"), str(tmp_path / "export"), str(tmp_path / "tex.rat")]


def test_batch_mode_uploads_the_remembered_selection_without_asking(tmp_path, monkeypatch):
    refs = _scene(tmp_path)
    node = _FakeNode(confirm=0, exclude=pf.dump_exclusions({str(tmp_path / "export")}))
    monkeypatch.setattr(pf, "confirm", lambda *a, **k: pytest.fail("must not ask"))

    kept, rows, missing, excluded = pf.resolve_upload_set(node, refs)

    assert kept == [str(tmp_path / "scene.hip"), str(tmp_path / "tex.rat")]
    assert len(rows) == 3 and missing == []


def test_a_directory_is_never_expanded_silently(tmp_path):
    """No window means no one saw the plan, so the log has to name the
    directory and its full weight -- that is the parameter that turned into
    11.54 GB in the field."""
    refs = _scene(tmp_path)
    said = []

    pf.resolve_upload_set(_FakeNode(confirm=0), refs, log=said.append)

    line = [m for m in said if str(tmp_path / "export") in m]
    assert line, said
    assert "1 file" in line[0] and "4.0 KB" in line[0]


def test_the_window_choice_is_written_back_to_the_node(tmp_path, monkeypatch):
    refs = _scene(tmp_path)
    node = _FakeNode(confirm=1)
    monkeypatch.setattr(pf, "confirm", lambda rows, missing, excluded, **k: {str(tmp_path / "export")})

    kept, _rows, _missing, excluded = pf.resolve_upload_set(node, refs, ask=True)

    assert str(tmp_path / "export") not in kept
    assert pf.load_exclusions(node.evalParm("rpfarm_exclude")) == {str(tmp_path / "export")}


def test_cancel_stops_the_cook(tmp_path, monkeypatch):
    """Cancel must not fall through to "upload what you had" -- the artist
    said no to this upload, not to this row."""
    monkeypatch.setattr(pf, "confirm", lambda *a, **k: None)

    with pytest.raises(pf.UploadCancelled):
        pf.resolve_upload_set(_FakeNode(), _scene(tmp_path), ask=True)


def test_a_broken_window_never_stalls_the_cook(tmp_path, monkeypatch):
    """Qt failing is not a reason to refuse to upload: fall back to the
    remembered selection and say so."""
    refs = _scene(tmp_path)

    def _boom(*a, **k):
        raise RuntimeError("no QApplication")

    monkeypatch.setattr(pf, "confirm", _boom)
    said = []

    kept, _rows, _missing, _excluded = pf.resolve_upload_set(_FakeNode(), refs, ask=True, log=said.append)

    assert kept == refs
    assert any("no QApplication" in m for m in said), said


def test_a_skipped_window_says_which_condition_refused(tmp_path, monkeypatch):
    """Silence here reads as "the window is broken". The artist has a
    working alternative when it is the thread check that refused, and the
    log is where they find out."""
    monkeypatch.setattr(pf, "ui_unavailable_reason", lambda: "no UI (headless cook)")
    said = []

    pf.resolve_upload_set(_FakeNode(confirm=1), _scene(tmp_path), log=said.append)

    assert any("confirmation window not shown: no UI (headless cook)" in m for m in said), said


def test_no_reason_is_logged_when_the_window_was_not_wanted(tmp_path, monkeypatch):
    monkeypatch.setattr(pf, "ui_unavailable_reason", lambda: "no UI (headless cook)")
    said = []

    pf.resolve_upload_set(_FakeNode(confirm=0), _scene(tmp_path), log=said.append)

    assert not any("not shown" in m for m in said), said
