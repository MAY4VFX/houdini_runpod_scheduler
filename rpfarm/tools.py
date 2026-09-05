"""Finding the external binaries a cook needs, without trusting ``PATH``.

The same lesson as :func:`rpfarm.houdini_local.resolve_package_python`, and
the same shape -- ordered candidates, each EXECUTED to prove it works,
result cached, always an absolute path -- applied to plain binaries rather
than to Python interpreters.

Why it exists a second time (2026-09-05): an upload item died with

    FileNotFoundError: [Errno 2] No such file or directory: 'zstd'

on a machine where zstd is installed. Houdini launched from the Dock has a
minimal ``PATH``::

    $HFS/bin:$HFS/toolkit/bin:/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin

Homebrew's ``/opt/homebrew/bin`` is not in it, so ``shutil.which("zstd")``
is None inside Houdini while the artist's shell finds it instantly. Every
external program this package runs has to be resolved the same way, and
every check of one has to be made in the environment the COOK gets, not the
one the developer's terminal has.
"""

from __future__ import annotations

import collections
import os
import platform
import shutil
import subprocess

#: Where binaries actually live, in the order worth trying. PATH comes
#: first (when it is sane it is right and free); these are the places a
#: Dock-launched Houdini cannot see.
CANDIDATE_DIRS = (
    "/opt/homebrew/bin",   # Homebrew on Apple silicon -- the one that bit us
    "/usr/local/bin",      # Homebrew on Intel, and most manual installs
    "/usr/bin",
    "/bin",
    "/opt/local/bin",      # MacPorts
    "/snap/bin",
    "/usr/sbin",
)

#: What ``PATH`` looks like inside a Dock-launched Houdini, minus the two
#: Houdini directories. Used to check a tool the way a cook will see it
#: instead of the way a developer's shell does.
HOUDINI_LIKE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin"

Tool = collections.namedtuple("Tool", "path how version")

_CACHE = {}


def clear_cache():
    """Forget resolved tools (tests, and after installing something)."""
    _CACHE.clear()


def _binary_name(name):
    return name + ".exe" if platform.system() == "Windows" else name


def _try(path, verify, run):
    """Run *path* to prove it works; ``(ok, version line)``."""
    runner = run or subprocess.run
    try:
        proc = runner([path, *verify], capture_output=True, text=True, timeout=15)
    except Exception:
        return False, ""   # missing, not executable, wrong architecture, hangs
    if getattr(proc, "returncode", 1) != 0:
        return False, ""
    out = (getattr(proc, "stdout", "") or getattr(proc, "stderr", "") or "").strip()
    return True, out.splitlines()[0] if out else ""


def resolve_tool(name, verify=("--version",), extra_dirs=(), which=None, run=None,
                 use_cache=True, path=None):
    """The absolute path to *name*, or None. Cached per name.

    ``path`` restricts the PATH lookup to a specific value -- pass
    :data:`HOUDINI_LIKE_PATH` to ask "would a cook find this?". The absolute
    directories are searched either way, so the answer for the cook is the
    same as the answer here; what changes is ``Tool.how``, which is how the
    doctor can say *why* it will be found.
    """
    binary = _binary_name(name)
    key = (binary, tuple(extra_dirs), path)
    if use_cache and key in _CACHE:
        return _CACHE[key]

    lookup = which or shutil.which
    found = None
    on_path = lookup(binary, path=path) if path is not None else lookup(binary)
    candidates = [(on_path, "on PATH")] if on_path else []
    for directory in tuple(extra_dirs) + CANDIDATE_DIRS:
        candidates.append((os.path.join(directory, binary), directory))

    seen = set()
    for candidate, how in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        if how != "on PATH" and not os.path.isfile(candidate):
            continue
        ok, version = _try(candidate, verify, run)
        if ok:
            found = Tool(path=candidate, how=how, version=version)
            break

    if use_cache:
        _CACHE[key] = found
    return found


def zstd(**kwargs):
    """The ``zstd`` binary, or None when this machine has none."""
    return resolve_tool("zstd", **kwargs)


def tar(**kwargs):
    """The ``tar`` binary, or None."""
    return resolve_tool("tar", **kwargs)
