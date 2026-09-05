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
    total_files = sum(r.files for r in rows)
    total_bytes = sum(r.bytes for r in rows)
    text = "{} of {} references -- {} files, {} of {}".format(
        len(selected(rows, excluded)), len(rows), files,
        human_bytes(size), human_bytes(total_bytes),
    )
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
    tree.setColumnCount(3)
    tree.setHeaderLabels(["Reference", "Size", "Contains"])
    tree.setRootIsDecorated(False)
    tree.setUniformRowHeights(True)
    tree.setAlternatingRowColors(True)
    tree.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
    for row in ordered:
        item = QtWidgets.QTreeWidgetItem([row_label(row), human_bytes(row.bytes), row_detail(row)])
        item.setFlags(item.flags() | QtCore.Qt.ItemIsUserCheckable)
        item.setCheckState(
            0,
            QtCore.Qt.Unchecked if normalise(row.path) in blocked else QtCore.Qt.Checked,
        )
        item.setData(0, QtCore.Qt.UserRole, row.path)
        item.setTextAlignment(1, QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        tree.addTopLevelItem(item)
    tree.setColumnWidth(0, 620)
    tree.setColumnWidth(1, 110)
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
    """The artist pressed Cancel in the confirmation window.

    Distinct from any failure: the caller must stop the cook, and must not
    fall back to uploading the previously remembered selection.
    """


def resolve_upload_set(node, refs, ask=None, log=None):
    """Decide what this node uploads, asking the artist when there is a UI.

    Returns ``(kept_refs, rows, missing, excluded)``. ``node`` is read and
    written through ``evalParm``/``parm(...).set`` only -- no ``hou``
    import here, which is what makes the whole flow testable with a fake
    node.

    ``ask`` overrides the decision to show the window: ``None`` (default)
    means "when the node's Confirm toggle is on and a UI is actually
    available", ``True`` forces it (the Preview button), ``False``
    suppresses it.

    Failure policy, in order of importance:

    - Cancel raises :class:`UploadCancelled` -- the cook stops.
    - A window that *fails* (no Qt, no QApplication, a cook generating off
      the main thread) is logged and the upload proceeds with what the node
      remembers. A confirmation dialog must never be the reason a farm
      submission dies.
    - With no window at all, every directory reference is logged with its
      full weight. "Do not expand a directory silently" is the rule; when
      no one is there to be asked, the log is where it is not silent.
    """
    say = log if log is not None else (lambda _message: None)
    from . import deps as _deps

    rows, missing = _deps.plan_refs(refs)
    excluded = load_exclusions(node.evalParm("rpfarm_exclude"))
    if ask is None:
        wanted = bool(node.evalParm("rpfarm_confirm"))
        reason = ui_unavailable_reason() if wanted else None
        if reason:
            say("confirmation window not shown: {}".format(reason))
        ask = wanted and reason is None

    if ask:
        try:
            chosen = confirm(rows, missing, excluded)
        except Exception as exc:  # Qt is optional; the upload is not
            say("confirmation window unavailable ({}) -- using the remembered selection".format(exc))
            chosen = excluded
        if chosen is None:
            raise UploadCancelled("upload cancelled in the confirmation window")
        if chosen != excluded:
            node.parm("rpfarm_exclude").set(dump_exclusions(chosen))
        excluded = chosen
    else:
        blocked = {normalise(p) for p in excluded}
        for row in rows:
            if row.kind == "dir" and normalise(row.path) not in blocked:
                say("directory reference {} -> {}, {}".format(
                    row_label(row), row_detail(row), human_bytes(row.bytes)))

    say(header_text(rows, missing, excluded))
    return apply_exclusions(refs, excluded), rows, missing, excluded
