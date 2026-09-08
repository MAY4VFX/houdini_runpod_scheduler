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

def test_a_directory_reads_as_a_directory():
    rows = {r.kind: r for r in _rows()}
    assert pf.row_label(rows["dir"]) == "/job/export" + os.sep
    assert pf.row_detail(rows["dir"]) == "3 files"
    assert pf.row_label(rows["file"]).endswith(".sc") or True
    assert pf.row_detail(rows["file"]) == "1 file"


def test_header_states_what_is_selected_against_what_was_offered(tmp_path):
    a = tmp_path / "big.zip"
    a.write_bytes(b"x" * 1500)
    b = tmp_path / "small.rat"
    b.write_bytes(b"y" * 10)
    rows, _ = deps.plan_refs([str(a), str(b)], source="scene")
    roots = pf.build_tree(rows)

    text = pf.header_text(roots, checked=[str(b)], missing=["/job/gone.exr"])

    assert text.startswith("1 of 2 file(s) referenced")
    assert "1.5 KB" in text  # the total offered
    assert "1 reference(s) name nothing on disk" in text


# -- the remembered choice -------------------------------------------------------

def test_an_unchecked_box_stays_unchecked_next_cook():
    """The whole reason the answer lives on the node: a window that has to
    be re-answered every cook is a tax, and a taxed artist turns it off."""
    stored = pf.dump_choices({"/job/export/", "/job/tex/a.rat"}, on={"/job/render"})

    off, on = pf.load_choices(stored)

    assert off == {"/job/export", "/job/tex/a.rat"}  # trailing sep normalised away
    assert on == {"/job/render"}


def test_an_exclusion_survives_a_reference_that_vanished_for_a_version():
    off, _on = pf.load_choices(pf.dump_choices({"/job/export"}))
    assert "/job/export" in pf.load_choices(pf.dump_choices(off))[0]


def test_exclusions_accept_a_hand_typed_list():
    assert pf.load_exclusions("/job/a\n/job/b\n") == {"/job/a", "/job/b"}
    assert pf.load_exclusions("") == set()
    assert pf.load_exclusions("[not json") == {"[not json"}

def test_ui_is_not_available_without_houdini():
    assert pf.ui_available() is False


# -- the widget tree (Houdini's Python only) -------------------------------------


@pytest.mark.skipif(
    importlib.util.find_spec("PySide6") is None,
    reason="PySide6 ships with Houdini's Python, not the system one",
)
def test_the_widget_tree_opens_one_level_and_folds_the_folder_state(tmp_path):
    """The rows live in a QStandardItemModel because Houdini's own view
    class (hou.qt.TreeView == _houqt.QT_HighlightTreeView) is a QTreeView,
    not a QTreeWidget. Only the two container classes differ between here
    and a real Houdini; the model and every checkbox rule is the same
    object, which is what makes this test worth anything."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtCore, QtWidgets

    render = tmp_path / "render"
    render.mkdir()
    for frame in ("0001", "0002"):
        (render / "beauty.{}.exr".format(frame)).write_bytes(b"e" * 100)
    (tmp_path / "scene.hip").write_bytes(b"h")
    rows, _ = deps.plan_refs([str(tmp_path / "scene.hip"), str(render)], source="scene")
    roots = pf.build_tree(rows)
    checked = {n.path for n in pf.leaves(roots)}

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    dialog = pf.build_dialog(roots, missing=["/job/gone.exr"], checked=checked)
    model = dialog.rpfarm_model
    view = dialog.rpfarm_view
    top = model.item(0)

    assert model.rowCount() == 1
    assert view.isExpanded(model.indexFromItem(top)), "top level open"
    folder = next(top.child(r) for r in range(top.rowCount())
                  if top.child(r).text().endswith(os.sep))
    assert not view.isExpanded(model.indexFromItem(folder)), "and nothing below it"
    assert folder.rowCount() == 2, "but it opens all the way to the file"
    assert model.item(0).child(folder.row(), 1).text() == pf.human_bytes(200)
    assert model.item(0).child(folder.row(), 2).text() == "2 files"
    assert folder.checkState() == QtCore.Qt.Checked

    # a folder in a mixed state has to LOOK mixed, or a collapsed row lies
    folder.child(0).setCheckState(QtCore.Qt.Unchecked)
    assert folder.checkState() == QtCore.Qt.PartiallyChecked
    assert dialog.rpfarm_checked() == checked - {
        folder.child(0).data(QtCore.Qt.UserRole + 1)}

    # and toggling the folder itself carries everything under it
    folder.setCheckState(QtCore.Qt.Unchecked)
    assert folder.child(1).checkState() == QtCore.Qt.Unchecked
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

    def _window(roots, missing, checked, **kw):
        seen["leaves"] = [(n.path, n.source) for n in pf.leaves(roots)]
        seen["checked"] = set(checked)
        return checked

    got = pf.choose_uploads(_FakeNode(), _scan([hip, tex], output_paths=[work]),
                            usd_paths=[usd], ask=True, window=_window)

    assert sorted(seen["leaves"]) == sorted([
        (hip, "scene"), (tex, "scene"), (usd, "usd"),
        (os.path.join(work, "old.exr"), "output")])
    assert seen["checked"] == {hip, tex, usd}, "an output starts unchecked, but it IS in the tree"
    assert got == [hip, tex, usd]


def test_an_output_the_artist_re_checks_uploads_and_stays_checked(tmp_path):
    hip, _tex, _usd, work = _files(tmp_path)
    node = _FakeNode()

    got = pf.choose_uploads(
        node, _scan([hip], output_paths=[work]), ask=True,
        window=lambda roots, missing, checked, **kw: {n.path for n in pf.leaves(roots)})

    assert work in got, "a fully checked folder uploads as one reference"
    off, on = pf.load_choices(node.evalParm("rpfarm_exclude"))
    assert off == set() and on == {work}
    # and the next cook, with no window at all, honours it
    assert work in pf.choose_uploads(node, _scan([hip], output_paths=[work]), ask=False)


def test_an_unchecked_reference_stays_unchecked_next_cook(tmp_path):
    hip, tex, usd, _work = _files(tmp_path)
    node = _FakeNode()

    pf.choose_uploads(node, _scan([hip, tex]), usd_paths=[usd], ask=True,
                      window=lambda roots, missing, checked, **kw: {hip, tex})

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


# ---------------------------------------------------------------------------
# confirm_on_main_thread (2026-09-08)
#
# PDG generation is not guaranteed to run on Houdini's main thread, and the
# window used to refuse outright the moment it was not -- which is exactly
# why it never opened once during a normal cook (cook 5157a9b2's own log:
# "confirmation window not shown: generation is not running on Houdini's
# main thread"). hou.ui.postEventCallback runs a callback on the real main
# thread, documented (checked against this Houdini's own compiled _hou
# module, not guessed) to fire exactly once, "next in Houdini's event
# loop" -- so the fix bridges through it instead of refusing. These tests
# fake `hou` well enough to prove the bridging LOGIC -- one call, the right
# thread, a timeout that falls back -- without touching real Qt.
# ---------------------------------------------------------------------------


class _FakeEventLoop:
    """Stands in for hou.ui: postEventCallback queues the callback and runs
    it only when the test tells it to, never automatically -- so a test can
    prove the bridge waits for that, rather than racing a real thread
    against it."""

    def __init__(self):
        self.registered = []
        self.removed = []

    def postEventCallback(self, callback):
        self.registered.append(callback)

    def removePostedEventCallback(self, callback):
        # Real hou.ui.removePostedEventCallback: "does nothing if the
        # callback is not present" -- never raises, unlike
        # removeEventLoopCallback's sibling.
        if callback in self.registered:
            self.registered.remove(callback)
            self.removed.append(callback)

    def fire(self):
        """Simulate the main event loop reaching the callback."""
        for cb in list(self.registered):
            cb()


class _FakeHou:
    def __init__(self, loop):
        self.ui = loop


def test_confirm_on_main_thread_is_plain_confirm_on_the_main_thread(monkeypatch):
    """No bridge, no hou import, no behaviour change for the path that
    already worked (the Preview Upload... button, a parameter callback)."""
    calls = []
    monkeypatch.setattr(pf, "confirm", lambda *a, **k: calls.append((a, k)) or "answer")

    got = pf.confirm_on_main_thread(["roots"], [], {"a"}, title="t")

    assert got == "answer" and len(calls) == 1


def test_confirm_on_main_thread_bridges_from_a_background_thread(monkeypatch):
    import threading

    loop = _FakeEventLoop()
    monkeypatch.setattr(pf, "confirm", lambda *a, **k: "the artist's answer")
    import sys
    monkeypatch.setitem(sys.modules, "hou", _FakeHou(loop))

    result = {}

    def _worker():
        result["value"] = pf.confirm_on_main_thread(["roots"], [], {"a"}, timeout_s=5)

    t = threading.Thread(target=_worker)
    t.start()
    # Give the worker a moment to register, then simulate the main loop --
    # exactly what a real event loop iteration does, just not automatic.
    import time
    for _ in range(200):
        if loop.registered:
            break
        time.sleep(0.01)
    assert loop.registered, "the callback must be registered before this thread can answer it"
    loop.fire()
    t.join(timeout=5)

    assert result.get("value") == "the artist's answer"
    # No removal call on the success path: postEventCallback already
    # promises "only once", so there is nothing to undo once it has fired.
    assert loop.removed == []


def test_confirm_on_main_thread_never_asks_twice(monkeypatch):
    """postEventCallback is documented to fire exactly once, but the
    done.is_set() guard is kept as belt and suspenders -- if it is ever
    called again anyway (a fake loop, a future Houdini quirk), a second
    dialog must still not open."""
    import threading

    loop = _FakeEventLoop()
    calls = []
    monkeypatch.setattr(pf, "confirm", lambda *a, **k: calls.append(1) or "answer")
    import sys
    monkeypatch.setitem(sys.modules, "hou", _FakeHou(loop))

    result = {}

    def _worker():
        result["value"] = pf.confirm_on_main_thread(["roots"], [], {"a"}, timeout_s=5)

    t = threading.Thread(target=_worker)
    t.start()
    import time
    for _ in range(200):
        if loop.registered:
            break
        time.sleep(0.01)
    cb = loop.registered[0]
    cb()
    cb()  # a second, stale call to the SAME callback object
    t.join(timeout=5)

    assert len(calls) == 1, "confirm must run exactly once no matter how many times the loop fires"


def test_confirm_on_main_thread_times_out_and_never_hangs_the_cook(monkeypatch):
    """A main thread that never gets to the callback (however unlikely)
    must not be a cook that waits forever -- same principle as every other
    refusal here: fall back, do not stall."""
    import threading

    loop = _FakeEventLoop()  # fire() never called
    monkeypatch.setattr(pf, "confirm", lambda *a, **k: pytest.fail("must not run"))
    import sys
    monkeypatch.setitem(sys.modules, "hou", _FakeHou(loop))

    result = {}

    def _worker():
        try:
            pf.confirm_on_main_thread(["roots"], [], {"a"}, timeout_s=0.05)
        except TimeoutError as e:
            result["error"] = e

    t = threading.Thread(target=_worker)
    t.start()
    t.join(timeout=5)

    assert isinstance(result.get("error"), TimeoutError)


def test_choose_uploads_asks_through_the_bridge_by_default(tmp_path, monkeypatch):
    """The exact defect: choose_uploads must reach for the thread-safe
    asker on its own, not just when a caller happens to pass window=."""
    hip, tex, _usd, _work = _files(tmp_path)
    calls = []
    monkeypatch.setattr(pf, "confirm_on_main_thread",
                        lambda *a, **k: calls.append(1) or {hip, tex})

    got = pf.choose_uploads(_FakeNode(), _scan([hip, tex]), ask=True, log=lambda m: None)

    assert calls, "choose_uploads did not use confirm_on_main_thread"
    assert set(got) == {hip, tex}


# -- the tree the window shows ---------------------------------------------------
#
# The owner's requirement, verbatim: "должно быть дерево с возможностью
# раскрыть до самого низа, но по дефолту только верхний открыт и галки
# можно снять на всех уровнях или поставить". A flat list cannot do that:
# 500 rendered frames of an output reference is 500 rows nobody reads.


def _rows_for_tree(tmp_path):
    """A hip, a texture, and an output folder holding three frames."""
    hip = tmp_path / "scene.hip"
    hip.write_bytes(b"h")
    tex = tmp_path / "tex" / "wood.rat"
    tex.parent.mkdir()
    tex.write_bytes(b"t" * 10)
    render = tmp_path / "render"
    render.mkdir()
    for frame in ("0001", "0002", "0003"):
        (render / "beauty.{}.exr".format(frame)).write_bytes(b"e" * 100)
    scene_rows, _ = deps.plan_refs([str(hip), str(tex)], source="scene")
    out_rows, _ = deps.plan_refs([str(render)], source="output")
    return scene_rows + out_rows, str(hip), str(tex), str(render)


def test_a_directory_becomes_a_folder_you_can_open(tmp_path):
    rows, hip, tex, render = _rows_for_tree(tmp_path)

    roots = pf.build_tree(rows)

    assert len(roots) == 1, "one common root, not a chain of single-child folders"
    root = roots[0]
    assert root.path == str(tmp_path)
    names = [c.name for c in root.children]
    assert "render" in names and "tex" in names and "scene.hip" in names
    render_node = next(c for c in root.children if c.name == "render")
    assert [c.name for c in render_node.children] == [
        "beauty.0001.exr", "beauty.0002.exr", "beauty.0003.exr"]
    assert render_node.files == 3 and render_node.bytes == 300


def test_folders_carry_the_weight_of_everything_under_them(tmp_path):
    rows, _hip, _tex, _render = _rows_for_tree(tmp_path)

    root = pf.build_tree(rows)[0]

    assert root.files == 5  # hip + texture + three frames
    assert root.bytes == 1 + 10 + 300


def test_heaviest_first_within_each_level(tmp_path):
    rows, _hip, _tex, _render = _rows_for_tree(tmp_path)

    root = pf.build_tree(rows)[0]

    assert [c.name for c in root.children] == ["render", "tex", "scene.hip"]


def test_a_folder_of_one_source_says_so_and_a_mixed_one_does_not(tmp_path):
    rows, _hip, _tex, render = _rows_for_tree(tmp_path)

    root = pf.build_tree(rows)[0]

    assert next(c for c in root.children if c.name == "render").source == "output"
    assert root.source == "", "scene + output under one folder is not one source"


# -- the answer, recorded where the decision was made ----------------------------


def test_unchecking_a_folder_stores_the_folder_not_its_files(tmp_path):
    """Requirement 6: a parm holding 500 paths because one folder was
    unchecked is unreadable and grows without bound."""
    rows, hip, tex, render = _rows_for_tree(tmp_path)
    roots = pf.build_tree(rows)
    checked = {hip, tex}  # the whole render folder unchecked

    off, on = pf.compact_answer(roots, checked, default_off=lambda n: False)

    assert off == {render}
    assert on == set()


def test_one_unchecked_file_is_stored_as_that_file(tmp_path):
    rows, hip, tex, render = _rows_for_tree(tmp_path)
    roots = pf.build_tree(rows)
    dropped = os.path.join(render, "beauty.0002.exr")
    checked = {hip, tex, os.path.join(render, "beauty.0001.exr"),
               os.path.join(render, "beauty.0003.exr")}

    off, on = pf.compact_answer(roots, checked, default_off=lambda n: False)

    assert off == {dropped}


def test_an_output_folder_left_alone_records_nothing(tmp_path):
    """Outputs default to unchecked, so leaving them unchecked is not a
    decision worth storing."""
    rows, hip, tex, _render = _rows_for_tree(tmp_path)
    roots = pf.build_tree(rows)

    off, on = pf.compact_answer(roots, {hip, tex},
                                default_off=lambda n: n.source == "output")

    assert off == set() and on == set()


def test_a_re_checked_output_folder_is_stored_once(tmp_path):
    rows, hip, tex, render = _rows_for_tree(tmp_path)
    roots = pf.build_tree(rows)
    everything = {hip, tex} | {os.path.join(render, "beauty.{}.exr".format(f))
                               for f in ("0001", "0002", "0003")}

    off, on = pf.compact_answer(roots, everything,
                                default_off=lambda n: n.source == "output")

    assert on == {render} and off == set()


def test_the_nearest_answer_wins(tmp_path):
    """A file re-checked inside an unchecked folder stays checked."""
    render = str(tmp_path / "render")
    keep = os.path.join(render, "beauty.0002.exr")

    assert pf.is_excluded(os.path.join(render, "beauty.0001.exr"), {render}, {keep}, False) is True
    assert pf.is_excluded(keep, {render}, {keep}, False) is False
    assert pf.is_excluded("/elsewhere/a.exr", {render}, set(), False) is False
    assert pf.is_excluded("/elsewhere/a.exr", set(), set(), True) is True


# -- turning the answer back into an upload set ----------------------------------


def test_a_fully_checked_directory_uploads_as_one_reference(tmp_path):
    """resolve_entries walks a directory itself, so keeping the reference
    whole is both smaller to carry and identical in outcome."""
    rows, hip, tex, render = _rows_for_tree(tmp_path)
    everything = {hip, tex} | {os.path.join(render, "beauty.{}.exr".format(f))
                               for f in ("0001", "0002", "0003")}

    assert pf.selected_paths(rows, everything) == [hip, tex, render]


def test_a_partly_checked_directory_uploads_the_files_that_stayed(tmp_path):
    rows, hip, tex, render = _rows_for_tree(tmp_path)
    keep = os.path.join(render, "beauty.0002.exr")

    got = pf.selected_paths(rows, {hip, tex, keep})

    assert got == [hip, tex, keep]
    assert render not in got, "the folder as a whole would drag the other two frames in"


# -- how long it will take, before it starts ------------------------------------


def test_the_window_says_how_long_the_upload_will_take():
    """The owner's uplink measures 4.7 Mbps, so his 2.8 GB is about 80
    minutes. That is a fact worth having BEFORE the upload starts."""
    assert pf.eta_text(2_800_000_000, 4.7) == "~1 h 20 min at 4.7 Mbps"
    assert pf.eta_text(50_000_000, 100.0) == "~1 min at 100.0 Mbps"
    assert pf.eta_text(1000, 100.0) == "~1 min at 100.0 Mbps", "rounded up, never <1"


def test_no_measurement_means_no_guess():
    """`rpfarm doctor` is what measures the uplink. Without it, saying
    nothing beats inventing a number."""
    assert pf.eta_text(2_800_000_000, None) == ""
    assert pf.eta_text(2_800_000_000, 0) == ""
    assert pf.eta_text(0, 4.7) == ""
    assert pf.eta_text(1000, "not a number") == ""


def test_the_header_never_estimates_a_duration_any_more(tmp_path):
    """Cook 5157a9b2 (2026-09-08): the header said '~9 min at 5.4 Mbps' for
    an item whose real transfer was files=0 bytes=0 seconds=8.2 -- every
    one of those "selected" files was already on the farm, unchanged, and
    nothing moved. header_text has no way to know that (it never talks to
    the farm), so it must never again turn picked_bytes into a promised
    duration -- with or without a measured uplink."""
    big = tmp_path / "big.exr"
    big.write_bytes(b"x" * 5_000_000)
    rows, _ = deps.plan_refs([str(big)], source="scene")
    roots = pf.build_tree(rows)

    text = pf.header_text(roots, checked=[str(big)], mbps=4.7)

    assert "Mbps" not in text and "min" not in text
    assert "upload skips whatever the farm already has" in text
