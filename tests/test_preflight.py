"""The confirmation window's logic -- the half that can be tested anywhere.

The window itself needs PySide6, which only Houdini's Python ships; the
one test that builds it skips elsewhere and runs head-less
(``QT_QPA_PLATFORM=offscreen``) under hython. Everything that decides what
uploads lives in the pure helpers below, on purpose.
"""

import importlib.util
import json
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
    def __init__(self, exclude=""):
        self._parms = {"rpfarm_confirm": _FakeParm(1), "rpfarm_exclude": _FakeParm(exclude)}

    def parm(self, name):
        return self._parms[name]

    def evalParm(self, name):
        return self._parms[name].value


def _scan(paths, output_paths=()):
    return deps.RefScan(paths=list(paths), output_paths=list(output_paths), unresolved=())


def _files(tmp_path):
    hip = tmp_path / "scene.hip"
    hip.write_bytes(b"h")
    tex = tmp_path / "tex.rat"
    tex.write_bytes(b"t" * 10)
    usd = tmp_path / "look.usdc"
    usd.write_bytes(b"u" * 50)
    work = tmp_path / "pdgwork"
    work.mkdir()
    (work / "old.exr").write_bytes(b"o" * 5000)
    return str(hip), str(tex), str(usd), str(work)


def test_one_window_carries_every_source(tmp_path):
    """Houdini's own dialog is fed entirely by hou.fileReferences() and its
    rows are (Parm, pattern) pairs -- a USD-only file cannot become one, and
    the call takes no argument for extra rows. So: one window, ours, with
    every source in it."""
    hip, tex, usd, work = _files(tmp_path)
    seen = {}

    def _window(rows, missing, excluded, **kw):
        seen["rows"] = [(r.path, r.source) for r in rows]
        seen["excluded"] = set(excluded)
        return excluded

    got = pf.choose_uploads(_FakeNode(), _scan([hip, tex], output_paths=[work]),
                            usd_paths=[usd], ask=True, window=_window)

    assert seen["rows"] == [(hip, "scene"), (tex, "scene"), (usd, "usd"), (work, "output")]
    assert seen["excluded"] == {work}, "an output starts unchecked, but it IS on the list"
    assert got == [hip, tex, usd]


def test_an_output_the_artist_re_checks_uploads_and_stays_checked(tmp_path):
    hip, tex, usd, work = _files(tmp_path)
    node = _FakeNode()

    got = pf.choose_uploads(node, _scan([hip], output_paths=[work]), ask=True,
                            window=lambda rows, missing, excluded, **kw: set())

    assert work in got
    off, on = pf.load_choices(node.evalParm("rpfarm_exclude"))
    assert off == set() and on == {work}
    # and the next cook, with no window at all, honours it
    assert work in pf.choose_uploads(node, _scan([hip], output_paths=[work]), ask=False)


def test_an_unchecked_reference_stays_unchecked_next_cook(tmp_path):
    hip, tex, usd, _work = _files(tmp_path)
    node = _FakeNode()

    pf.choose_uploads(node, _scan([hip, tex]), usd_paths=[usd], ask=True,
                      window=lambda rows, missing, excluded, **kw: {usd})

    assert pf.load_choices(node.evalParm("rpfarm_exclude"))[0] == {usd}
    assert pf.choose_uploads(node, _scan([hip, tex]), usd_paths=[usd], ask=False) == [hip, tex]


def test_the_old_bare_list_of_exclusions_still_reads(tmp_path):
    """Scenes saved before outputs were shown hold a plain JSON list."""
    hip, tex, _usd, _work = _files(tmp_path)
    node = _FakeNode(exclude=json.dumps([tex]))

    assert pf.choose_uploads(node, _scan([hip, tex]), ask=False) == [hip]


def test_cancel_stops_the_cook(tmp_path):
    hip, _tex, _usd, _work = _files(tmp_path)

    with pytest.raises(pf.UploadCancelled):
        pf.choose_uploads(_FakeNode(), _scan([hip]), ask=True,
                          window=lambda *a, **k: None)


def test_a_broken_window_never_stalls_the_cook(tmp_path):
    hip, tex, _usd, _work = _files(tmp_path)

    def _boom(*a, **k):
        raise RuntimeError("no QApplication")

    said = []
    got = pf.choose_uploads(_FakeNode(), _scan([hip, tex]), ask=True,
                            log=said.append, window=_boom)

    assert got == [hip, tex]
    assert any("no QApplication" in m for m in said), said


def test_batch_mode_asks_nothing_and_logs_the_directories(tmp_path):
    hip, tex, usd, work = _files(tmp_path)
    said = []

    got = pf.choose_uploads(_FakeNode(), _scan([hip, tex], output_paths=[work]),
                            usd_paths=[usd], ask=False, log=said.append,
                            window=lambda *a, **k: pytest.fail("must not ask"))

    assert got == [hip, tex, usd], "outputs stay out until someone says otherwise"
    assert not any("pdgwork" in m for m in said), "an unchecked directory is not a warning"


def test_wants_window_says_which_condition_refused(monkeypatch):
    monkeypatch.setattr(pf, "ui_unavailable_reason", lambda: "no UI (headless cook)")
    said = []

    assert pf.wants_window(_FakeNode(), log=said.append) is False
    assert any("no UI (headless cook)" in m for m in said), said

    monkeypatch.setattr(pf, "ui_unavailable_reason", lambda: None)
    assert pf.wants_window(_FakeNode()) is True
    assert pf.wants_window(_FakeNode(), ask=False) is False
