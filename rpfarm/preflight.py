"""The upload plan: weigh it, show it, remember what the artist unchecked.

``hou.fileReferences()`` is a generous list. Field case (2026-09-05,
``airship_v013.hip``): one scheduler's ``pdg_workingdir`` turned into 827
files and 11.54 GB of finished renders, old hip versions and a 1.47 GB
export zip -- a set no one chose and no one could see before it started
moving. :mod:`rpfarm.deps` narrows the set (scope + non-dependency
parameters); this module is the part that makes the remainder *visible*
and lets the artist say no to a line of it.

Layers, in order of how much they can break:

- pure helpers (:func:`human_bytes`, :func:`sort_rows`, the exclusion
  round-trip, :func:`header_text`) -- no Houdini, no Qt, unit-tested;
- :func:`build_dialog` -- PySide6 only, imported lazily, constructible
  head-less under ``QT_QPA_PLATFORM=offscreen``;
- :func:`confirm`/:func:`ui_available` -- the only parts that need a live
  Houdini UI.

Every caller must treat the window as optional: a missing UI, a cook
running off the main thread, or a Qt that raises must all degrade to "use
the remembered choices and upload", never to a stalled cook.
"""

from __future__ import annotations

import json
import os

_UNITS = ("B", "KB", "MB", "GB", "TB")


def human_bytes(size):
    """``1476395008`` -> ``"1.4 GB"``. Decimal-ish, one figure after the point.

    Matches how a file manager reads, not how a kernel counts: the artist
    is comparing a line against "how long will this take", not auditing
    block sizes.
    """
    value = float(max(0, int(size)))
    for unit in _UNITS:
        if value < 1024.0 or unit == _UNITS[-1]:
            if unit == "B":
                return "{:.0f} B".format(value)
            return "{:.1f} {}".format(value, unit)
        value /= 1024.0
    return "{:.1f} TB".format(value)


def sort_rows(rows):
    """Heaviest first, ties by path.

    The whole point of the window is that the 1.47 GB zip is the FIRST
    line, not the 431st: a list in reference order hides exactly the rows
    worth arguing with.
    """
    return sorted(rows, key=lambda r: (-r.bytes, r.path))


def normalise(path):
    return os.path.normpath(path) if path else path


SOURCE_LABELS = {"scene": "scene", "usd": "USD", "output": "output (not a dependency)"}


def load_choices(raw):
    """The node's remembered answer: ``(unchecked, re-checked)``.

    One parameter, one answer, two halves of it -- because two kinds of row
    have opposite defaults. A normal reference is checked until the artist
    says otherwise; an output reference (``pdg_workingdir``, ``outputimage``)
    is unchecked until the artist says otherwise. Keeping both in the same
    parameter, written by the same window in the same moment, is what stops
    this becoming two sources of truth that disagree.

    A bare JSON list (what this parameter held before outputs were shown at
    all) reads as the unchecked half, so an existing scene keeps its answer.
    """
    if not raw:
        return set(), set()
    text = raw.strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError:
            data = {}
        if isinstance(data, dict):
            return (
                {normalise(str(p)) for p in data.get("off") or () if p},
                {normalise(str(p)) for p in data.get("on") or () if p},
            )
    return load_exclusions(raw), set()


def dump_choices(off, on=()):
    """Render the answer for the node parameter: sorted JSON, stable in git."""
    return json.dumps({
        "off": sorted(normalise(p) for p in off if p),
        "on": sorted(normalise(p) for p in on if p),
    })


def default_excluded(rows, off, on):
    """Which rows start unchecked, given what the node remembers."""
    out = set()
    for row in rows:
        path = normalise(row.path)
        if path in off:
            out.add(path)
        elif row.source == "output" and path not in on:
            out.add(path)
    return out


def split_answer(rows, excluded):
    """The window's answer as ``(unchecked, re-checked outputs)`` to store."""
    blocked = {normalise(p) for p in excluded}
    on = {normalise(r.path) for r in rows
          if r.source == "output" and normalise(r.path) not in blocked}
    return blocked, on


def load_exclusions(raw):
    """Parse the node's remembered exclusions.

    Accepts the JSON list this module writes, and a plain newline-separated
    list too -- a parameter is a text field an artist can type into, and
    "I pasted a path in there" should work rather than silently reset every
    other choice.
    """
    if not raw:
        return set()
    text = raw.strip()
    if text.startswith("["):
        try:
            data = json.loads(text)
        except ValueError:
            data = None  # half-typed JSON: fall through, never silently reset
        if isinstance(data, list):
            return {normalise(str(p)) for p in data if p}
    return {normalise(line.strip()) for line in text.splitlines() if line.strip()}


def dump_exclusions(paths):
    """Render exclusions for the node parameter: sorted JSON, stable in git.

    Paths that no longer appear in the plan are kept, not pruned: a
    reference that disappears for a version and comes back must come back
    unchecked, or the choice quietly undoes itself.
    """
    return json.dumps(sorted(normalise(p) for p in paths if p))


def apply_exclusions(refs, excluded):
    """The references left after the artist's unchecked rows are removed."""
    blocked = {normalise(p) for p in excluded}
    return [r for r in refs if normalise(r) not in blocked]


def selected(rows, excluded):
    blocked = {normalise(p) for p in excluded}
    return [r for r in rows if normalise(r.path) not in blocked]


def totals(rows, excluded):
    """``(files, bytes)`` of the checked rows."""
    keep = selected(rows, excluded)
    return sum(r.files for r in keep), sum(r.bytes for r in keep)


def header_text(rows, missing, excluded):
    """The one line that has to be true before anyone reads the list."""
    files, size = totals(rows, excluded)
    total_bytes = sum(r.bytes for r in rows)
    text = "{} of {} references -- {} files, {} of {}".format(
        len(selected(rows, excluded)), len(rows), files,
        human_bytes(size), human_bytes(total_bytes),
    )
    by_source = []
    for source in ("scene", "usd", "output"):
        count = len([r for r in rows if r.source == source])
        if count:
            by_source.append("{} {}".format(count, SOURCE_LABELS.get(source, source)))
    if by_source:
        text += "  [" + " | ".join(by_source) + "]"
    if missing:
        text += "  ({} reference(s) name nothing on disk and are skipped)".format(len(missing))
    return text


def row_label(row):
    """A directory reads as a directory, or nobody asks what is inside it."""
    if row.kind == "dir":
        return row.path.rstrip(os.sep) + os.sep
    return row.path


def row_detail(row):
    if row.kind == "dir":
        return "{} files".format(row.files)
    return "1 file"


# -- the window ----------------------------------------------------------------


def ui_unavailable_reason():
    """None when a modal Qt dialog is safe to open here; else why it is not.

    Two conditions, both required, and the artist deserves to know which
    one refused. ``hou.isUIAvailable()`` is false under hython, so every
    headless cook skips the window with no special case. The main-thread
    check is the one that matters inside Houdini: PDG generation is not
    guaranteed to run on the main thread, and Qt from another thread does
    not raise politely -- it can take the session down. When that is what
    happened, the artist has a working alternative (the Preview button is a
    parameter callback, always on the main thread), so the reason says so
    instead of the cook going quiet.
    """
    try:
        import hou
    except ImportError:
        return "no Houdini in this process"
    try:
        if not hou.isUIAvailable():
            return "no UI (headless cook)"
    except Exception as exc:
        return "cannot tell whether a UI exists ({})".format(exc)
    import threading

    if threading.current_thread() is not threading.main_thread():
        return ("generation is not running on Houdini's main thread -- "
                "use the Preview Upload... button to choose")
    return None


def ui_available():
    return ui_unavailable_reason() is None


def build_dialog(rows, missing, excluded, title="RunPodFarm Upload", parent=None):
    """Construct (but do not run) the confirmation window.

    Split out from :func:`confirm` so the whole widget tree can be built
    and inspected head-less -- ``QT_QPA_PLATFORM=offscreen`` -- which is
    the only way any of this gets tested at all without a human at a
    screen.
    """
    from PySide6 import QtCore, QtWidgets

    ordered = sort_rows(rows)
    blocked = {normalise(p) for p in excluded}

    dialog = QtWidgets.QDialog(parent)
    dialog.setWindowTitle(title)
    dialog.setMinimumSize(940, 560)

    layout = QtWidgets.QVBoxLayout(dialog)

    head = QtWidgets.QLabel(header_text(rows, missing, excluded))
    head.setWordWrap(True)
    layout.addWidget(head)

    tree = QtWidgets.QTreeWidget()
    tree.setColumnCount(4)
    tree.setHeaderLabels(["Reference", "Size", "Contains", "Found by"])
    tree.setRootIsDecorated(False)
    tree.setUniformRowHeights(True)
    tree.setAlternatingRowColors(True)
    tree.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
    for row in ordered:
        item = QtWidgets.QTreeWidgetItem([
            row_label(row), human_bytes(row.bytes), row_detail(row),
            SOURCE_LABELS.get(row.source, row.source)])
        item.setFlags(item.flags() | QtCore.Qt.ItemIsUserCheckable)
        item.setCheckState(
            0,
            QtCore.Qt.Unchecked if normalise(row.path) in blocked else QtCore.Qt.Checked,
        )
        item.setData(0, QtCore.Qt.UserRole, row.path)
        item.setTextAlignment(1, QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        tree.addTopLevelItem(item)
    tree.setColumnWidth(0, 560)
    tree.setColumnWidth(1, 100)
    tree.setColumnWidth(2, 90)
    layout.addWidget(tree, 1)

    if missing:
        note = QtWidgets.QLabel("Not on disk, skipped:  " + "   ".join(missing[:3])
                                + ("   ..." if len(missing) > 3 else ""))
        note.setWordWrap(True)
        layout.addWidget(note)

    buttons = QtWidgets.QHBoxLayout()
    check_all = QtWidgets.QPushButton("Check All")
    check_none = QtWidgets.QPushButton("Uncheck All")
    buttons.addWidget(check_all)
    buttons.addWidget(check_none)
    buttons.addStretch(1)
    box = QtWidgets.QDialogButtonBox()
    upload = box.addButton("Upload", QtWidgets.QDialogButtonBox.AcceptRole)
    box.addButton("Cancel", QtWidgets.QDialogButtonBox.RejectRole)
    buttons.addWidget(box)
    layout.addLayout(buttons)

    def _items():
        return [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]

    def _current_exclusions():
        return {
            normalise(item.data(0, QtCore.Qt.UserRole))
            for item in _items()
            if item.checkState(0) != QtCore.Qt.Checked
        }

    def _refresh():
        head.setText(header_text(rows, missing, _current_exclusions()))
        upload.setEnabled(len(_current_exclusions()) < len(ordered))

    def _set_all(state):
        tree.blockSignals(True)
        for item in _items():
            item.setCheckState(0, state)
        tree.blockSignals(False)
        _refresh()

    tree.itemChanged.connect(lambda *_: _refresh())
    check_all.clicked.connect(lambda: _set_all(QtCore.Qt.Checked))
    check_none.clicked.connect(lambda: _set_all(QtCore.Qt.Unchecked))
    box.accepted.connect(dialog.accept)
    box.rejected.connect(dialog.reject)
    _refresh()

    # Read back by confirm() (and by the head-less test) rather than
    # returned through a signal: the dialog is modal, so the caller reads
    # the answer straight off the object after exec().
    dialog.rpfarm_exclusions = _current_exclusions
    return dialog


def confirm(rows, missing, excluded, title="RunPodFarm Upload", parent=None):
    """Show the plan. Returns the new exclusion set, or None if cancelled.

    None means the artist said no -- the caller must stop the cook, not
    quietly upload the previous selection.
    """
    from PySide6 import QtWidgets

    if parent is None:
        try:
            import hou

            parent = hou.qt.mainWindow()
        except Exception:
            parent = None
    if QtWidgets.QApplication.instance() is None:  # pragma: no cover - Houdini always has one
        QtWidgets.QApplication([])
    dialog = build_dialog(rows, missing, excluded, title=title, parent=parent)
    if not dialog.exec():
        return None
    return dialog.rpfarm_exclusions()


class UploadCancelled(Exception):
    """The artist pressed Cancel in a confirmation window.

    Distinct from any failure: the caller must stop the cook, and must not
    fall back to uploading the previously remembered selection.
    """


def wants_window(node, ask=None, log=None):
    """Whether to open a confirmation window, and say why when not.

    Decided before the scan, not after, because the answer changes what is
    scanned: with a window, Houdini's dialog is asked; without one, its
    remembered selection is read instead.
    """
    say = log if log is not None else (lambda _m: None)
    if ask is not None:
        return bool(ask)
    if not bool(node.evalParm("rpfarm_confirm")):
        return False
    reason = ui_unavailable_reason()
    if reason:
        say("confirmation window not shown: {}".format(reason))
        return False
    return True


def choose_uploads(node, scan, usd_paths=(), ask=False, log=None, window=None):
    """The final list of local paths this cook uploads.

    One window, one list, one stored answer. Every reference this cook could
    upload is a row in it, whatever found it:

    * ``scene`` -- a Houdini parameter holds the path.
    * ``USD`` -- a stage reads it and no parameter anywhere names it. There
      is no way to put these in Houdini's own dependency dialog: that dialog
      is fed entirely by ``hou.fileReferences()``, whose rows are
      ``(hou.Parm, pattern)`` pairs, and it takes no argument for anything
      else. That is the whole reason this window exists.
    * ``output`` -- a parameter holds it, but it says where results GO
      (``pdg_workingdir``, ``outputimage``). Unchecked by default, shown
      with its real weight, and the artist can overrule it: that is how one
      parameter's 11.54 GB became visible instead of invisible.

    A directory stays ONE row carrying its whole recursive weight, sorted
    heaviest first, because that is the row worth arguing with.

    Failure policy: Cancel raises :class:`UploadCancelled` and the cook
    stops. A window that FAILS (no Qt, no QApplication, generation off the
    main thread) is logged and the remembered answer is used instead -- a
    confirmation window must never be the reason a farm submission dies.

    ``window`` is an injection seam for the tests.
    """
    say = log if log is not None else (lambda _message: None)
    from . import deps as _deps

    rows = []
    missing = []
    seen = set()
    for source, paths in (("scene", scan.paths), ("usd", usd_paths),
                          ("output", scan.output_paths)):
        fresh = [p for p in paths if normalise(p) not in seen]
        seen.update(normalise(p) for p in fresh)
        found, gone = _deps.plan_refs(fresh, source=source)
        rows.extend(found)
        missing.extend(gone)

    off, on = load_choices(node.evalParm("rpfarm_exclude"))
    excluded = default_excluded(rows, off, on)

    if ask:
        asker = window or confirm
        try:
            chosen = asker(rows, missing, excluded, title="RunPodFarm -- what will upload")
        except Exception as exc:
            say("confirmation window unavailable ({}) -- using the remembered answer".format(exc))
            chosen = excluded
        if chosen is None:
            raise UploadCancelled("upload cancelled in the confirmation window")
        if chosen != excluded:
            node.parm("rpfarm_exclude").set(dump_choices(*split_answer(rows, chosen)))
        excluded = {normalise(p) for p in chosen}
    else:
        blocked = excluded
        for row in rows:
            if row.kind == "dir" and normalise(row.path) not in blocked:
                say("directory reference {} -> {}, {}".format(
                    row_label(row), row_detail(row), human_bytes(row.bytes)))

    say(header_text(rows, missing, excluded))
    return [r.path for r in rows if normalise(r.path) not in excluded]
