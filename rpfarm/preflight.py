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


def houdini_dialog(rop, forced_unselected_patterns=(), project_dir_variable="HIP",
                   uploaded_files=()):
    """Houdini's own file-dependency dialog -- the one File > Pre-Flight Scene opens.

    Same call SideFX make from ``cloud.py`` (``_onShowFileReferenceDialog``)
    and ``hqrop.py`` (``copyProjectFilesToSharedFolder``), positionally, in
    that order. Returns ``(pressed_ok, ((parm, pattern), ...))``.

    ``forced_unselected_patterns`` is where the output references go: the
    artist sees them as unchecked rows and can put them back, instead of
    our code deciding for them out of sight. SideFX use it for exactly the
    same thing -- the ROP's own output pattern.
    """
    import hou

    return hou.ui.displayFileDependencyDialog(
        rop, tuple(uploaded_files), tuple(forced_unselected_patterns),
        project_dir_variable, True)


def choose_uploads(node, scan, usd_paths=(), ask=False, log=None, rop=None,
                   project_dir_variable="HIP", dialog=None, confirm_usd=None,
                   expand=None, hip_path=None):
    """The final list of local paths this cook uploads.

    Two halves, because Houdini's dialog can only show one of them:

    * **Parameter references** go through Houdini's own dependency dialog
      (:func:`houdini_dialog`). Its answer is taken as given -- no
      second-guessing, including references we would have dropped as
      outputs, because the artist can see and re-check those.
    * **USD references** have no parameter at all -- nothing in
      ``hou.fileReferences()`` points at a texture a USD layer names from
      inside itself, so Houdini's dialog has no row to show and no API to
      add one. They get :func:`confirm`, this package's own window, and the
      choice is remembered on the node (``rpfarm_exclude``). That parameter
      is now ONLY about USD files: for parameter references Houdini keeps
      its own selection state, and two sources of truth for one question is
      how they disagree.

    A dialog that FAILS (no Qt, no QApplication, generation off the main
    thread) is logged and the scan's own answer is used. Cancel raises
    :class:`UploadCancelled`. A window is never the reason a farm
    submission dies; a Cancel always is.

    ``dialog``/``confirm_usd`` are injection seams for the tests.
    """
    say = log if log is not None else (lambda _message: None)
    from . import deps as _deps

    parm_paths = list(scan.paths)
    if ask:
        show = dialog or houdini_dialog
        try:
            pressed_ok, selection = show(rop, scan.output_patterns, project_dir_variable)
        except Exception as exc:
            say("file dependency window unavailable ({}) -- using the scanned set".format(exc))
            pressed_ok, selection = True, None
        if not pressed_ok:
            raise UploadCancelled("upload cancelled in the file dependency window")
        if selection is not None:
            if expand is None or hip_path is None:
                import hou

                expand = expand or hou.text.expandString
                hip_path = hip_path or hou.hipFile.path()
            chosen, unresolved = _deps.expand_pairs(selection, expand)
            # The dialog answers about references; the hip file is not one
            # of them in every scene, and it is never optional.
            parm_paths = list(dict.fromkeys([hip_path] + chosen))
            say("file dependency window: {} reference(s) kept, {} forced unchecked".format(
                len(selection), len(scan.output_patterns)))
            if unresolved:
                say("{} selected reference(s) resolved to nothing on disk".format(len(unresolved)))

    excluded = load_exclusions(node.evalParm("rpfarm_exclude"))
    usd_paths = list(usd_paths)
    if usd_paths:
        rows, missing = _deps.plan_refs(usd_paths)
        if ask:
            asker = confirm_usd or confirm
            try:
                chosen = asker(rows, missing, excluded,
                               title="RunPodFarm -- USD dependencies (no parameter points at these)")
            except Exception as exc:
                say("USD window unavailable ({}) -- using the remembered selection".format(exc))
                chosen = excluded
            if chosen is None:
                raise UploadCancelled("upload cancelled in the USD dependency window")
            if chosen != excluded:
                node.parm("rpfarm_exclude").set(dump_exclusions(chosen))
            excluded = chosen
        else:
            blocked = {normalise(p) for p in excluded}
            for row in rows:
                if row.kind == "dir" and normalise(row.path) not in blocked:
                    say("directory reference {} -> {}, {}".format(
                        row_label(row), row_detail(row), human_bytes(row.bytes)))
        say("usd: " + header_text(rows, missing, excluded))
        usd_paths = apply_exclusions(usd_paths, excluded)

    return list(dict.fromkeys(parm_paths + usd_paths))
