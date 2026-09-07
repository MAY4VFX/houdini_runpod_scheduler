"""The farm volume, as a tree the artist can tick and delete from.

``rpfarm storage ls`` already knew everything here; it printed it as a table
in a terminal the owner does not use, so in practice nobody managed the
volume at all and it filled with other people's demos and our own test
leftovers. This turns the same listing into the same window the upload
already uses (Ruling R55: one tree, two jobs) with two differences -- the
rows come from the farm rather than from local disk, and the button deletes.

Deletion is the reason every rule here is a refusal rather than a warning:

* another artist's project is never deletable. The volume is shared and a
  window with a Check All button must not be able to end somebody else's
  day;
* the project cooking right now is never deletable;
* the ``houdini`` zone is not a project at all -- it is the Houdini install
  every pod runs from -- and neither it, ``ledger`` nor ``.rpfarm`` can be
  ticked;
* a project whose rendered frames nobody has downloaded is shown with
  OUTPUTS NOT FETCHED and is skipped by Check All. Deleting frames the
  artist has not got yet is the worst thing this window could do, so it
  takes an individual, deliberate tick.

The first three are enforced twice: here, so the box cannot be ticked, and
again on the pod in ``housekeeping.cmd_rm_paths``, which is the side that
actually unlinks anything.
"""

from __future__ import annotations

import os
import posixpath
from dataclasses import dataclass, field

from . import preflight

#: Where projects live on the volume.
PROJECTS_ROOT = "/workspace/projects"

#: Zones that are shown for their size but can never be ticked.
LOCKED_ZONES = ("houdini", "ledger", ".rpfarm")

#: Our own leavings, in the two shapes the real volume has. PDG scratch and
#: our test runs write under projects/ with their OWN name in the USER slot
#: (``projects/pdgtemp/41756``), so matching project names alone found none
#: of them -- and they then showed up in this window as another artist's
#: work, undeletable. Kept identical to housekeeping's copy by a test.
LITTER_USERS = ("pdgtemp", "test_render")
LITTER_PROJECT_PREFIXES = ("smoke-upload-", "rpfarm-smoke")


@dataclass
class Row:
    """A ``preflight.build_tree`` row. Same shape as ``deps.PlanRow``, which
    is the point -- the tree code cannot tell the two apart."""

    path: str
    kind: str = "dir"
    files: int = 1
    bytes: int = 0
    source: str = "mine"
    contents: list = field(default_factory=list)


# -- boundaries --------------------------------------------------------------


def project_of(path):
    """``(user, project)`` for a farm path, or ``(None, None)``."""
    path = posixpath.normpath(path or "")
    if not path.startswith(PROJECTS_ROOT + "/"):
        return None, None
    parts = path[len(PROJECTS_ROOT) + 1:].split("/")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return (parts[0] or None), None
    return parts[0], parts[1]


def is_locked(path, user, cooking=()):
    """Can this row never be ticked?

    Anything that is not inside one of THIS artist's project directories,
    plus whatever is cooking right now. Note the direction: the default is
    locked, and a path has to prove it is the artist's own project to
    become deletable. A rule written the other way round would let a new
    zone, or a path shape nobody thought about, fall through as deletable.
    """
    owner, project = project_of(path)
    if owner is None or project is None:
        return True
    if owner != user:
        return True
    return "{}/{}".format(owner, project) in set(cooking or ())


def is_litter(path):
    """Is this ours rather than the artist's? (Part 3.)"""
    owner, project = project_of(path)
    if owner in LITTER_USERS:
        return True
    if not project:
        return False
    return any(project.startswith(p) for p in LITTER_PROJECT_PREFIXES)


# -- the rows ----------------------------------------------------------------


def tree_rows(listing, user, cooking=(), contents=None):
    """``storage ls`` output -> rows for :func:`preflight.build_tree`.

    ``contents`` maps ``"user/project"`` to ``[(path, bytes), ...]`` from a
    ``du`` of that project, which is what lets the artist open a project and
    tick one folder inside it. A project with no listing stays a single
    aggregate row -- the same treatment ``deps`` gives a directory reference
    too big to enumerate, and for the same reason: an unopenable row that
    states its full weight is honest, an empty one that opens is not.
    """
    contents = contents or {}
    pending = {"{}/{}".format(p["user"], p["project"]) for p in listing.get("projects", [])
               if p.get("outputs_pending")}
    cooking = set(cooking or ())
    rows = []

    for project in listing.get("projects", []):
        key = "{}/{}".format(project["user"], project["project"])
        path = posixpath.join(PROJECTS_ROOT, project["user"], project["project"])
        if is_litter(path):
            # Ours. Swept before the window opens; never a decision for the
            # artist, and never a row that reads as somebody else's work.
            continue
        if project["user"] != user:
            source = "other"
        elif key in cooking:
            source = "cooking"
        elif key in pending:
            source = "pending"
        else:
            source = "mine"
        rows.append(Row(path=path, kind="dir", files=1,
                        bytes=int(project.get("bytes") or 0), source=source,
                        contents=list(contents.get(key, []))))

    for zone, size in sorted((listing.get("zones") or {}).items()):
        if zone == "projects":
            continue  # its projects are the rows above
        rows.append(Row(path=posixpath.join("/workspace", zone), kind="dir",
                        files=1, bytes=int(size or 0), source="zone"))
    return rows


def selected_bytes(roots, checked):
    """What ticking those paths would free."""
    picked = {preflight.normalise(p) for p in checked}
    return sum(n.bytes for n in preflight.leaves(roots)
               if preflight.normalise(n.path) in picked)


def selected_files(roots, checked):
    picked = {preflight.normalise(p) for p in checked}
    return sum(n.files for n in preflight.leaves(roots)
               if preflight.normalise(n.path) in picked)


def header_text(listing, roots, checked):
    """Used, total, and what the current ticks would give back.

    The one number an artist opening this window wants is "how much room
    would I have"; it is stated as the answer, not as an input to a sum
    they have to do themselves.
    """
    volume = listing.get("volume") or {}
    used = int(volume.get("used") or 0)
    total = int(volume.get("total") or 0)
    freed = selected_bytes(roots, checked)
    text = "{} of {} used".format(preflight.human_bytes(used),
                                  preflight.human_bytes(total) if total else "?")
    if total:
        text += " ({:.0f}%)".format(100.0 * used / total)
    if freed:
        text += "  --  deleting the ticked rows frees {} ({} item(s)), leaving {}".format(
            preflight.human_bytes(freed), selected_files(roots, checked),
            preflight.human_bytes(max(0, used - freed)))
    else:
        text += "  --  nothing ticked"
    protected = [p for p in listing.get("projects", []) if p.get("outputs_pending")]
    if protected:
        text += "  [{} project(s) still hold outputs nobody has downloaded]".format(
            len(protected))
    return text


# -- the window --------------------------------------------------------------


def to_local(path):
    """Farm path -> the separator ``preflight.build_tree`` splits on.

    A no-op everywhere but Windows, where the tree would otherwise see one
    long component and draw a single unopenable row.
    """
    return path.replace("/", os.sep) if os.sep != "/" else path


def to_farm(path):
    return path.replace(os.sep, "/") if os.sep != "/" else path


def browse(listing, user, cooking=(), contents=None, parent=None, confirm=None):
    """Open the volume window. Returns the farm paths to delete, or None.

    None is "the artist closed it" and must delete nothing -- the same
    contract the upload window has, for the same reason.
    """
    rows = tree_rows(listing, user, cooking=cooking, contents=contents)
    for row in rows:
        row.path = to_local(row.path)
        row.contents = [(to_local(p), b) for p, b in row.contents]
    roots = preflight.build_tree(rows)
    pending = {"{}/{}".format(p["user"], p["project"])
               for p in listing.get("projects", []) if p.get("outputs_pending")}

    def _locked(node):
        # Asked of the PATH, not of the row it came from: a folder invented
        # by the tree (a user directory, an intermediate) has no row of its
        # own, and "no row" must mean locked rather than unnoticed.
        return is_locked(to_farm(node.path), user, cooking)

    def _protected(node):
        owner, project = project_of(to_farm(node.path))
        # A file inside a pending project is pending too: the frames the
        # artist has not fetched are exactly the files in there.
        return bool(project) and "{}/{}".format(owner, project) in pending

    ask = confirm or preflight.confirm
    chosen = ask(
        roots, (), set(),
        title="RunPodFarm Volume",
        parent=parent,
        header=lambda r, c: header_text(listing, r, c),
        accept_label="Delete",
        columns=["On the farm", "Size", "Contains", "Note"],
        protected=_protected,
        locked=_locked,
    )
    if chosen is None:
        return None
    return sorted(to_farm(p) for p in chosen)


def confirm_text(paths, roots, listing):
    """The last sentence before anything is unlinked: size and count."""
    freed = selected_bytes(roots, paths)
    return "Delete {} item(s) from the farm, freeing {}? This cannot be undone.".format(
        len(paths), preflight.human_bytes(freed))
