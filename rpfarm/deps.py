"""Hip-file dependency collection and PDG path mapping.

Ports the manifest-building half of ``_create_manifest`` from the v1 HDA
(``hda/runpodfarm_scheduler.hda/Top_1runpodfarmscheduler/PythonModule``,
roughly lines 965-1093): walk ``hou.fileReferences()``, expand ``$HIP``/
``$JOB``, and reduce sequence/UDIM references to their containing
directory. The SHA-256 content hashing that lived alongside that logic in
v1 is intentionally *not* ported here — dedup in this module is by
normalized local path only, not content hash.

Only :func:`collect_refs` may ``import hou``, and it does so lazily inside
the function body. Everything else (:func:`resolve_entries`,
:func:`pathmap_env`, and the small helpers) is pure stdlib and importable/
testable without Houdini installed.
"""

from __future__ import annotations

import json
import os
import re

from .sync import FileEntry

# $F, $F4, %04d, <UDIM>, ####, $(F4), ${F4} -- any of these inside a
# reference means the ref names a per-frame/per-tile sequence rather than
# one real file, so collect_refs wants the containing directory instead.
_SEQ = re.compile(r"\$F\d*|%0?\d*d|<UDIM>|<udim>|#{2,}|\$\(F\d*\)|\$\{F\d*\}")

# Directories never worth uploading: PDG's own scratch dir, hand-rolled
# backup folders, VCS metadata, bytecode cache. Pruned at any depth while
# walking a directory in resolve_entries.
_SKIP_DIR_NAMES = {"pdgtemp", "backup", ".git", "__pycache__"}

# Houdini's own autosave/backup files, plus the generic "~" editor-backup
# suffix. Skipped regardless of whether they're discovered via directory
# walk or passed directly.
_SKIP_FILE_SUFFIXES = (".hip.bak", ".hiplc.bak", ".hipnc.bak", "~")

# Windows drive letter at the start of a path, e.g. "C:\" or "C:/". Matched
# with a plain regex (not `ntpath`/`os.path.splitdrive`) so _ext_suffix
# behaves the same regardless of which OS is actually running the code.
_DRIVE_RE = re.compile(r"^([A-Za-z]):[\\/]")

# A bare drive root with nothing after it, e.g. "C:\" or "C:/" (or "C:"
# with no trailing separator, which os.path.dirname can produce). Used by
# _pathmap_key to detect the same degenerate-root problem POSIX "/" has.
_DRIVE_ROOT_RE = re.compile(r"^[A-Za-z]:[\\/]?$")


def collect_refs():
    """Collect this hip file's own path plus every file reference in the scene.

    The only function in this module allowed to ``import hou`` -- done
    lazily inside the function body so the rest of ``rpfarm.deps`` stays
    pure and importable/testable without Houdini installed.

    - The hip file itself (``hou.hipFile.path()``) is always first in the
      returned list.
    - Each ``(parm, path)`` pair from ``hou.fileReferences()`` is included
      unless ``path`` is empty or starts with ``op:``, ``opdef:`` or
      ``temp:`` (procedural/in-memory references, not real files).
    - A path containing ``$`` is expanded with ``hou.text.expandString``
      (this covers ``$HIP``/``$JOB`` and any other Houdini variable).
    - A path containing a sequence/UDIM token (``$F``, ``%04d``,
      ``<UDIM>``, ``####``, ...) is reduced to its containing directory,
      since the individual per-frame/per-tile files aren't resolvable from
      the reference itself.
    """
    import hou

    out = [hou.hipFile.path()]
    for _parm, path in hou.fileReferences():
        if not path or path.startswith(("op:", "opdef:", "temp:")):
            continue
        p = hou.text.expandString(path) if "$" in path else path
        if _SEQ.search(p):
            p = os.path.dirname(p)
        out.append(p)
    return out


def _ext_suffix(path):
    """POSIX-safe suffix appended after ``"_ext"`` for a path outside ``job_dir``.

    A plain POSIX-style absolute path is returned unchanged -- it already
    starts with ``/``, so ``remote_project + "/_ext" + suffix`` concatenates
    cleanly (e.g. ``/Users/may/lib/b.abc`` -> ``/Users/may/lib/b.abc``).

    A Windows-style absolute path loses its drive colon and gets its
    backslashes flipped to forward slashes, so the whole remote path stays
    a single valid POSIX-style string:
    ``C:\\lib\\b.abc`` -> ``/C/lib/b.abc``.
    """
    m = _DRIVE_RE.match(path)
    if not m:
        return path
    drive = m.group(1)
    rest = path[m.end():].replace("\\", "/")
    return f"/{drive}/{rest}"


def _is_skipped_file(path):
    return path.endswith(_SKIP_FILE_SUFFIXES)


def _pathmap_key(parent_dir, fp):
    """Pick the path-map key for an external file's parent directory.

    Normally the key is just ``parent_dir`` -- one path-map entry per
    external root, reused by every file discovered under that same
    directory. But pdgcmd.py's ``_applyPathMapForZone`` (pdgcmd.py:926)
    applies each path-map entry with an *unanchored* ``str.replace`` in a
    fixed-point loop: a rule keyed on ``/`` (or a bare drive root like
    ``C:\\``) would then rewrite the ``/`` inside every path the worker
    touches, not just this one file's prefix -- silently corrupting the
    whole path map. So when ``parent_dir`` is exactly ``/`` or a bare
    drive root, the key is the file's own full path instead, which keeps
    the rule specific to that single file.
    """
    if parent_dir == "/" or _DRIVE_ROOT_RE.match(parent_dir):
        return fp
    return parent_dir


def resolve_entries(paths, job_dir, remote_project):
    """Turn a list of local paths into upload entries and a PDG path map.

    Pure function (no ``hou``). Each item in ``paths`` may be a file or a
    directory; directories are walked recursively. Nonexistent paths are
    silently dropped (a reference to a file that hasn't been rendered yet
    is not an error here).

    Remote layout:

    - A file under ``job_dir`` maps to ``remote_project/<rel path>``.
    - A file outside ``job_dir`` ("external") maps to
      ``remote_project/_ext/<abs path>`` (drive letters lose their colon
      on Windows -- see :func:`_ext_suffix`).

    Skip rules (applied whether a path is discovered via directory walk or
    passed directly):

    - Directories named ``pdgtemp``, ``backup``, ``.git`` or
      ``__pycache__`` are pruned at any depth (their contents are never
      walked or uploaded).
    - Files ending in ``.hip.bak``, ``.hiplc.bak``, ``.hipnc.bak`` or ``~``
      are skipped.

    Symlinks: a file symlink is followed and uploaded like a normal file
    (``os.path.isfile`` follows symlinks). A directory symlink is never
    descended into (``os.walk(..., followlinks=False)``) -- this also
    guards against symlink loops, not just links that escape ``job_dir``.

    A file is classified "inside job_dir" by a plain literal prefix check
    first (so e.g. a file symlink that physically lives outside job_dir
    but is *referenced* through a job-local path -- a common
    linked-library pattern -- still uploads as a job-relative file, matching
    what its reference path says). Only when that literal check fails does
    it fall back to comparing ``os.path.realpath`` of both the file and
    ``job_dir`` -- this is what makes a symlinked ``$JOB`` itself resolve
    correctly regardless of whether a given path is expressed through the
    symlink or already through its real target. Either way,
    ``FileEntry.local`` keeps the original normalized (not realpath'd)
    path -- only the inside/outside decision and the resulting ``rel``
    path use the resolved form.

    Skip rules apply to a directory passed directly in ``paths`` too, not
    just to directories discovered while walking: ``resolve_entries(["/proj/backup"],
    ...)`` yields nothing.

    Dedup is by normalized local path: the same path passed twice (or
    reachable both directly and via a directory walk) produces one entry.

    Returns ``(entries, path_map)`` where ``path_map`` always contains
    ``{job_dir: remote_project}`` plus one entry per distinct external
    root directory (the immediate parent of each external file) -- except
    when that parent is ``/`` or a bare drive root, where the file's own
    full path is used as the key instead (see :func:`_pathmap_key`).
    """
    job_dir = os.path.normpath(job_dir)
    job_dir_real = os.path.realpath(job_dir)
    seen = set()
    entries = []
    pmap = {job_dir: remote_project}

    def add(raw_fp):
        fp = os.path.normpath(raw_fp)
        if fp in seen or _is_skipped_file(fp) or not os.path.isfile(fp):
            return
        seen.add(fp)

        if fp == job_dir or fp.startswith(job_dir + os.sep):
            rel = os.path.relpath(fp, job_dir)
        else:
            fp_real = os.path.realpath(fp)
            if fp_real == job_dir_real or fp_real.startswith(job_dir_real + os.sep):
                rel = os.path.relpath(fp_real, job_dir_real)
            else:
                rel = None

        if rel is not None:
            remote = f"{remote_project}/{rel.replace(os.sep, '/')}"
        else:
            remote = f"{remote_project}/_ext{_ext_suffix(fp)}"
            root = os.path.dirname(fp)
            key = _pathmap_key(root, fp)
            pmap.setdefault(key, f"{remote_project}/_ext{_ext_suffix(key)}")

        entries.append(FileEntry(local=fp, remote=remote, size=os.path.getsize(fp)))

    for p in paths:
        if os.path.isdir(p):
            if os.path.basename(os.path.normpath(p)) in _SKIP_DIR_NAMES:
                continue
            for root, dirs, files in os.walk(p, followlinks=False):
                dirs[:] = [d for d in dirs if d not in _SKIP_DIR_NAMES]
                for f in files:
                    add(os.path.join(root, f))
        else:
            add(p)
    return entries, pmap


def pathmap_env(path_map):
    """Render a ``{local_prefix: remote_prefix}`` dict as a ``$PDG_PATHMAP`` string.

    Format derived from the actual PDG_PATHMAP *consumer* in this Houdini
    install -- ``_buildPathMap()`` in
    ``$HFS/houdini/python3.13libs/pdgjob/pdgcmd.py`` (Houdini 22.0.368,
    lines 888-930), specifically the parse loop at lines 913-918::

        paths = pathmap['paths']
        for e in paths:
            for from_path, v in e.items():
                e_zone = v['zone']
                ...
                to_path  = v['path']

    So ``"paths"`` is a list where each element is a single-key dict:
    ``{from_path: {"zone": <zone>, "path": <to_path>}}`` -- NOT the flat
    ``{"path": from, "to": to, "zone": zone}`` shape one might guess.
    ``resolveEnvParams``/``resolvePathMapping`` in
    ``pdg/scheduler.py:590-608`` is what serializes ``_pdg.File.pathMapJSON()``
    into the ``PDG_PATHMAP`` job-env var in the first place.

    RunPod workers run Linux, so every mapping here targets the ``"LINUX"``
    zone -- this is what ``localizePath`` on the worker side (running under
    ``sys.platform == "linux"``) will match against.
    """
    return json.dumps({"paths": [{local: {"zone": "LINUX", "path": remote}} for local, remote in path_map.items()]})
