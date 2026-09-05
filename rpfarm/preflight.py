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
from dataclasses import dataclass, field

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


def normalise(path):
    return os.path.normpath(path) if path else path


@dataclass
class TreeNode:
    """One row of the confirmation window: a file, or a folder holding files.

    Folders exist so the artist can answer at the level they think at --
    "not the whole render folder" is one decision, not five hundred. Weight
    and file count aggregate upward, so a folder's line says what saying no
    to it is worth.
    """

    path: str
    name: str
    kind: str = "file"
    files: int = 0
    bytes: int = 0
    source: str = ""
    children: list = field(default_factory=list)

    @property
    def is_leaf(self):
        return not self.children


def _insert(index, path, size, source):
    parts = [p for p in path.split(os.sep) if p]
    walked = ""
    node = index.setdefault("", TreeNode(path=os.sep, name=os.sep, kind="dir"))
    for i, part in enumerate(parts):
        walked = walked + os.sep + part
        child = index.get(walked)
        if child is None:
            leaf = i == len(parts) - 1
            child = TreeNode(path=walked, name=part, kind="file" if leaf else "dir",
                             files=1 if leaf else 0, bytes=size if leaf else 0,
                             source=source if leaf else "")
            index[walked] = child
            node.children.append(child)
        node = child
    return node


def _aggregate(node):
    """Weight, file count and a folder's common source, bottom-up."""
    if node.is_leaf:
        return node.files, node.bytes, {node.source}
    files = 0
    total = 0
    sources = set()
    for child in node.children:
        c_files, c_bytes, c_sources = _aggregate(child)
        files += c_files
        total += c_bytes
        sources |= c_sources
    node.files, node.bytes = files, total
    # One source, or none: a folder mixing scene and output files says
    # nothing useful in that column, and a guess there would be a lie.
    node.source = next(iter(sources)) if len(sources) == 1 else ""
    node.children.sort(key=lambda c: (-c.bytes, c.name))
    return files, total, sources


def _collapse(node):
    """Fold single-child folder chains: /Users/may/BS/airship, not /, Users, may.

    Without this the top level of the window is `/`, and "expand the top
    level only" shows the artist one useless row.
    """
    while node.kind == "dir" and len(node.children) == 1 and node.children[0].kind == "dir":
        only = node.children[0]
        node = TreeNode(path=only.path, name=node.name.rstrip(os.sep) + os.sep + only.name
                        if node.name != os.sep else os.sep + only.name,
                        kind="dir", files=only.files, bytes=only.bytes,
                        source=only.source, children=only.children)
    node.children = [_collapse(c) for c in node.children]
    return node


def build_tree(rows):
    """Plan rows -> the roots of a directory tree, heaviest child first.

    A directory row with a known file list (:attr:`PlanRow.contents`)
    becomes a real folder the artist can open all the way down; one without
    (a folder too large to list) stays a single aggregate leaf.
    """
    index = {}
    for row in rows:
        if row.kind == "dir" and row.contents:
            for path, size in row.contents:
                _insert(index, path, size, row.source)
        else:
            node = _insert(index, row.path, row.bytes, row.source)
            node.kind = row.kind
            node.files = row.files
    root = index.get("")
    if root is None:
        return []
    _aggregate(root)
    root = _collapse(root)
    return [root] if root.path != os.sep else root.children


def is_excluded(path, off, on, default_off):
    """Is *path* unchecked, given answers recorded at any level above it?

    Nearest answer wins, so a file re-checked inside an unchecked folder
    stays checked. ``default_off`` is what applies when nothing was ever
    said about this path or any of its parents.
    """
    walked = normalise(path)
    while True:
        if walked in off:
            return True
        if walked in on:
            return False
        parent = os.path.dirname(walked)
        if parent == walked:
            return bool(default_off)
        walked = parent


def compact_answer(roots, checked, default_off):
    """Record each decision at the highest level where it is uniform.

    An unchecked folder is one entry, not one per file under it. Anything
    that already matches the default is not recorded at all -- the
    parameter holds decisions, not a snapshot.
    """
    off = set()
    on = set()
    normalised = {normalise(p) for p in checked}

    def state(node):
        """(all children checked?, all children defaulting to off?) or None if mixed."""
        if node.is_leaf:
            return normalise(node.path) in normalised, bool(default_off(node))
        checks = set()
        defaults = set()
        for child in node.children:
            c_check, c_default = state(child)
            checks.add(c_check)
            defaults.add(c_default)
        return (checks.pop() if len(checks) == 1 else None,
                defaults.pop() if len(defaults) == 1 else None)

    def visit(node):
        is_checked, defaults_off = state(node)
        if is_checked is not None and defaults_off is not None:
            if is_checked == (not defaults_off):
                return  # matches the default: nothing to remember
            (on if is_checked else off).add(normalise(node.path))
            return
        for child in node.children:
            visit(child)

    for root in roots:
        visit(root)
    return off, on


def selected_paths(rows, checked):
    """The upload set: whole references where nothing under them was dropped.

    A directory nobody touched stays ONE reference -- ``resolve_entries``
    walks it itself, which is smaller to carry and identical in outcome.
    A directory with something unchecked inside is replaced by the files
    that stayed, because a reference cannot express "all but that one".
    """
    normalised = {normalise(p) for p in checked}
    out = []
    for row in rows:
        if row.kind == "dir" and row.contents:
            kept = [p for p, _size in row.contents if normalise(p) in normalised]
            if len(kept) == len(row.contents):
                out.append(row.path)
            else:
                out.extend(kept)
        elif normalise(row.path) in normalised:
            out.append(row.path)
    return list(dict.fromkeys(out))


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


def leaves(roots):
    """Every file row in the tree, in display order."""
    out = []
    for node in roots:
        if node.is_leaf:
            out.append(node)
        else:
            out.extend(leaves(node.children))
    return out


def header_text(roots, checked, missing=()):
    """The one line that has to be true before anyone reads the tree."""
    all_leaves = leaves(roots)
    picked = [n for n in all_leaves if normalise(n.path) in {normalise(p) for p in checked}]
    text = "{} of {} files -- {} of {}".format(
        len(picked), len(all_leaves),
        human_bytes(sum(n.bytes for n in picked)),
        human_bytes(sum(n.bytes for n in all_leaves)),
    )
    by_source = []
    for source in ("scene", "usd", "output"):
        count = len([n for n in all_leaves if n.source == source])
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


def build_dialog(roots, missing, checked, title="RunPodFarm", parent=None):
    """Construct (but do not run) the confirmation window.

    Split out from :func:`confirm` so the whole widget tree can be built
    and inspected head-less -- ``QT_QPA_PLATFORM=offscreen`` -- which is the
    only way any of this gets tested without a human at a screen.

    A folder is checkable like anything else, and Qt's ``ItemIsAutoTristate``
    does the rest: toggling a folder sets everything under it, and a folder
    with a mix shows the partially-checked box, which is the only way to see
    from a collapsed row that something inside was dropped.
    """
    from PySide6 import QtCore, QtWidgets

    picked = {normalise(p) for p in checked}

    dialog = QtWidgets.QDialog(parent)
    dialog.setWindowTitle(title)
    dialog.setMinimumSize(980, 620)

    layout = QtWidgets.QVBoxLayout(dialog)
    head = QtWidgets.QLabel(header_text(roots, checked, missing))
    head.setWordWrap(True)
    layout.addWidget(head)

    tree = QtWidgets.QTreeWidget()
    tree.setColumnCount(4)
    tree.setHeaderLabels(["Reference", "Size", "Contains", "Found by"])
    tree.setUniformRowHeights(True)
    tree.setAlternatingRowColors(True)
    tree.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)

    leaf_items = []

    def add(node, parent_item):
        detail = "1 file" if node.files == 1 else "{} files".format(node.files)
        item = QtWidgets.QTreeWidgetItem([
            node.name if node.is_leaf else node.name.rstrip(os.sep) + os.sep,
            human_bytes(node.bytes), detail, SOURCE_LABELS.get(node.source, "")])
        item.setData(0, QtCore.Qt.UserRole, node.path)
        item.setTextAlignment(1, QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        flags = item.flags() | QtCore.Qt.ItemIsUserCheckable
        if not node.is_leaf:
            flags |= QtCore.Qt.ItemIsAutoTristate
        item.setFlags(flags)
        if parent_item is None:
            tree.addTopLevelItem(item)
        else:
            parent_item.addChild(item)
        if node.is_leaf:
            leaf_items.append((item, node))
        for child in node.children:
            add(child, item)
        return item

    for root in roots:
        add(root, None).setExpanded(True)  # only the top level opens by default

    # States go on AFTER the tree is assembled: auto-tristate derives every
    # folder from its children, so a folder set before its children exist
    # would be overwritten by them.
    tree.blockSignals(True)
    for item, node in leaf_items:
        item.setCheckState(0, QtCore.Qt.Checked
                           if normalise(node.path) in picked else QtCore.Qt.Unchecked)
    tree.blockSignals(False)

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

    def _checked():
        return {node.path for item, node in leaf_items
                if item.checkState(0) == QtCore.Qt.Checked}

    def _refresh():
        chosen = _checked()
        head.setText(header_text(roots, chosen, missing))
        upload.setEnabled(bool(chosen))

    def _set_all(state):
        tree.blockSignals(True)
        for item, _node in leaf_items:
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
    dialog.rpfarm_checked = _checked
    return dialog


def confirm(roots, missing, checked, title="RunPodFarm", parent=None):
    """Show the plan. Returns the checked paths, or None if cancelled.

    None means the artist said no -- the caller must stop the cook, and must
    not fall back to uploading the previously remembered answer.
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
    dialog = build_dialog(roots, missing, checked, title=title, parent=parent)
    if not dialog.exec():
        return None
    return dialog.rpfarm_checked()


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

    One window, one tree, one stored answer. Every reference this cook could
    upload is in it, whatever found it:

    * ``scene`` -- a Houdini parameter holds the path.
    * ``USD`` -- a stage reads it and no parameter anywhere names it. There
      is no way to put these in Houdini's own dependency dialog: that dialog
      is fed entirely by ``hou.fileReferences()``, its rows are
      ``(hou.Parm, pattern)`` pairs, and it takes no argument for anything
      else. That is the whole reason this window exists.
    * ``output`` -- a parameter holds it, but it says where results GO
      (``pdg_workingdir``, ``outputimage``). Unchecked to start with, shown
      with its real weight, and overrulable: that is how one parameter's
      11.54 GB became visible instead of invisible.

    Directories are folders in the tree, opened all the way down to the
    file, so "not this one frame" and "not this whole folder" are both one
    click -- and the answer is stored at whichever level it was made.

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

    roots = build_tree(rows)
    off, on = load_choices(node.evalParm("rpfarm_exclude"))

    def default_off(leaf):
        return leaf.source == "output"

    checked = {leaf.path for leaf in leaves(roots)
               if not is_excluded(leaf.path, off, on, default_off(leaf))}

    if ask:
        asker = window or confirm
        try:
            chosen = asker(roots, missing, checked, title="RunPodFarm -- what will upload")
        except Exception as exc:
            say("confirmation window unavailable ({}) -- using the remembered answer".format(exc))
            chosen = checked
        if chosen is None:
            raise UploadCancelled("upload cancelled in the confirmation window")
        chosen = {normalise(p) for p in chosen}
        if chosen != {normalise(p) for p in checked}:
            node.parm("rpfarm_exclude").set(
                dump_choices(*compact_answer(roots, chosen, default_off)))
        checked = chosen
    else:
        for row in rows:
            if row.kind == "dir" and normalise(row.path) in {normalise(p) for p in checked} or (
                    row.kind == "dir" and any(normalise(p) in {normalise(c) for c in checked}
                                              for p, _s in row.contents)):
                say("directory reference {} -> {}, {}".format(
                    row_label(row), row_detail(row), human_bytes(row.bytes)))

    say(header_text(roots, checked, missing))
    return selected_paths(rows, checked)
