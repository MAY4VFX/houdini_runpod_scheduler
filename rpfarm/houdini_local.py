"""Local-machine Houdini discovery and HDA install for ``rpfarm setup``/``doctor``.

Ported from v1's ``infrastructure/install_hda.py`` (``HoudiniInstall``,
``find_houdini_installations``) -- cross-platform (macOS/Linux/Windows),
stdlib only, no dependency on ``hou`` (this runs from a plain system
``python3``, before Houdini is even known to be installed). The JuiceFS
``HOUDINI_PATH``/``install_packages`` branches from v1 are dropped: v2 has
no JuiceFS and no per-hython pip install step (the four HDAs are the only
thing that has to land in Houdini's own directories; everything else
``rpfarm`` needs runs as a plain system ``python3`` subprocess, see
``rpfarm/package_runner.py``'s own docstring for why).

This module adds one thing v1's installer didn't need: collapsing this
repo's git-tracked, VCS-friendly-expanded HDA directories
(``hda/*.hda/``, built with ``hotl -t`` -- see any
``scripts/build_runpodfarm_*_hda.py`` docstring) into installable ``.hda``
files with ``hotl -l`` (confirmed against a real ``hotl --help`` and a real
round-trip on this machine 2026-09-03: ``-l`` is the counterpart of ``-t``,
not ``-c``/``-C``, which pair with ``-x``/``-X`` expanded form instead --
collapsing a ``-t`` directory with ``-C`` silently produces a corrupt or
empty archive).
"""

from __future__ import annotations

import ast
import glob
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

# ---------------------------------------------------------------------------
# Houdini installation discovery (ported from v1's infrastructure/install_hda.py)
# ---------------------------------------------------------------------------


class HoudiniInstall:
    """Represents a single Houdini installation."""

    def __init__(self, hfs: Path):
        self.hfs = hfs
        self.version = self._detect_version()
        self.major_minor = self._major_minor()
        self.hython = self._find_hython()
        self.hotl = self._find_hotl()
        self.user_pref_dir = self._find_user_pref_dir()

    # -- version detection ---------------------------------------------------

    def _detect_version(self) -> str:
        version_header = self.hfs / "toolkit" / "include" / "SYS" / "SYS_Version.h"
        if version_header.is_file():
            ver = self._parse_version_header(version_header)
            if ver:
                return ver

        name = self.hfs.name
        m = re.search(r"(\d+\.\d+(?:\.\d+)?)", name)
        if m:
            return m.group(1)

        for part in self.hfs.parts:
            m = re.search(r"(\d+\.\d+(?:\.\d+)?)", part)
            if m:
                return m.group(1)

        return "unknown"

    @staticmethod
    def _parse_version_header(path: Path) -> str | None:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        major = minor = build = None
        for line in text.splitlines():
            if "#define SYS_VERSION_MAJOR" in line:
                m = re.search(r"(\d+)", line.split("SYS_VERSION_MAJOR")[-1])
                if m:
                    major = m.group(1)
            elif "#define SYS_VERSION_MINOR" in line:
                m = re.search(r"(\d+)", line.split("SYS_VERSION_MINOR")[-1])
                if m:
                    minor = m.group(1)
            elif "#define SYS_VERSION_BUILD" in line:
                m = re.search(r"(\d+)", line.split("SYS_VERSION_BUILD")[-1])
                if m:
                    build = m.group(1)
        if major and minor:
            ver = f"{major}.{minor}"
            if build:
                ver += f".{build}"
            return ver
        return None

    def _major_minor(self) -> str:
        parts = self.version.split(".")
        if len(parts) >= 2:
            return f"{parts[0]}.{parts[1]}"
        return self.version

    # -- hython / hotl ---------------------------------------------------------

    def _bin_candidates(self, names: list[str]) -> list[Path]:
        system = platform.system()
        if system == "Darwin":
            return [self.hfs / "bin" / n for n in names] + [
                self.hfs / "Frameworks" / "Houdini.framework" / "Versions" / "Current" / "Resources" / "bin" / n
                for n in names
            ]
        return [self.hfs / "bin" / n for n in names]

    def _find_hython(self) -> Path | None:
        names = ["hython.exe", "hython3.exe"] if platform.system() == "Windows" else ["hython", "hython3"]
        for c in self._bin_candidates(names):
            if c.is_file():
                return c
        return None

    def _find_hotl(self) -> Path | None:
        """``hotl`` lives next to ``hython`` in every layout ``_find_hython``
        checks -- look there directly instead of re-deriving ``bin/``."""
        if self.hython is None:
            return None
        name = "hotl.exe" if platform.system() == "Windows" else "hotl"
        candidate = self.hython.parent / name
        return candidate if candidate.is_file() else None

    # -- user pref dir -------------------------------------------------------

    def _find_user_pref_dir(self) -> Path:
        """Houdini's per-version user preference directory.

        ``HOUDINI_USER_PREF_DIR`` wins when set, exactly as Houdini itself
        treats it, including the ``__HVER__`` placeholder it documents for
        the ``major.minor`` version. Honouring it is not just courtesy to
        artists with a relocated pref dir: without it this function reads
        ``Path.home()`` unconditionally, so anything constructing a
        ``HoudiniInstall`` over a *fake* HFS -- the test suite did exactly
        this -- resolves to the machine's REAL pref dir and
        ``build_and_install_hdas`` overwrites the artist's installed HDAs.
        That happened during Task 14: a plain ``pytest`` run replaced all
        four real ``runpodfarm_*.hda`` with 17-byte fixtures and the next
        smoke run died with "Invalid node type name".
        """
        override = os.environ.get("HOUDINI_USER_PREF_DIR")
        if override:
            return Path(override.replace("__HVER__", self.major_minor))

        system = platform.system()
        home = Path.home()

        if system == "Windows":
            docs = Path(os.environ.get("USERPROFILE", home)) / "Documents"
            return docs / f"houdini{self.major_minor}"
        elif system == "Darwin":
            return home / "Library" / "Preferences" / "houdini" / self.major_minor
        else:
            return home / f"houdini{self.major_minor}"

    def __repr__(self) -> str:
        return f"Houdini {self.version} ({self.hfs})"


def _glob_expand(patterns: list[str]) -> list[Path]:
    results: list[Path] = []
    for pat in patterns:
        for p in glob.glob(pat):
            pp = Path(p)
            if pp.is_dir():
                results.append(pp.resolve())
    return results


def find_houdini_installations() -> list[HoudiniInstall]:
    """Scan common paths for Houdini installations."""
    system = platform.system()
    candidate_dirs: list[Path] = []

    if system == "Darwin":
        candidate_dirs.extend(_glob_expand([
            "/Applications/Houdini/Houdini*/Frameworks/Houdini.framework/Versions/Current/Resources",
            "/Applications/Side Effects Software/Houdini */Frameworks/Houdini.framework/Versions/Current/Resources",
            "/Applications/Houdini/Houdini*",
            "/Applications/Side Effects Software/Houdini *",
        ]))
    elif system == "Linux":
        candidate_dirs.extend(_glob_expand([
            "/opt/hfs*",
            "/opt/sidefx/hfs*",
        ]))
    elif system == "Windows":
        candidate_dirs.extend(_glob_expand([
            "C:\\Program Files\\Side Effects Software\\Houdini *",
        ]))

    seen: set[Path] = set()
    installs: list[HoudiniInstall] = []
    for d in candidate_dirs:
        if d in seen:
            continue
        seen.add(d)
        if (d / "toolkit").is_dir() or (d / "bin").is_dir() or (d / "houdini").is_dir():
            try:
                installs.append(HoudiniInstall(d))
            except Exception:
                pass

    installs.sort(key=lambda i: i.version, reverse=True)
    return installs


# ---------------------------------------------------------------------------
# HDA build (hotl -l) + install
# ---------------------------------------------------------------------------

# The four HDAs this repo ships, in the order they're most useful to see
# in setup's checklist output.
HDA_NAMES = ["runpodfarm_scheduler", "runpodfarm_upload", "runpodfarm_download", "runpodfarm_stats"]


def repo_root() -> Path:
    """This checkout's root -- ``rpfarm/houdini_local.py``'s own grandparent."""
    return Path(__file__).resolve().parent.parent


def hda_source_dir(name: str, root: Path | None = None) -> Path:
    return (root or repo_root()) / "hda" / f"{name}.hda"


# ---------------------------------------------------------------------------
# The stable package copy (2026-09-08)
#
# An artist's Houdini used to read rpfarm/*.py straight out of a live git
# checkout (RPFARM_ROOT/~/.rpfarm/src pointed at repo_root()) -- fine while
# one person edits and tests sequentially, not while an agent is mid-edit on
# the same files a running session imports live. A guard reinstall mid-edit
# cost the owner three blocked dialogs in one session (2026-09-08) and,
# separately, cost a rendered frame when a rebuild landed mid-cook.
#
# The fix mirrors how the .hda assets already work: a STABLE, COPIED
# location (~/.rpfarm/pkg) an artist's Houdini points at by default, kept
# apart from whatever a developer's checkout is doing. Updating it is then
# a conscious act (`rpfarm setup`, or a future dedicated "publish" step) --
# not a side effect of every asset rebuild during ordinary iteration.
# ---------------------------------------------------------------------------


def stable_package_root(home: Path) -> Path:
    """Where the artist-facing copy of ``rpfarm/`` lives -- the value
    ``RPFARM_ROOT``/the ``~/.rpfarm/src`` symlink point at by default.
    Its own ``rpfarm/`` subdirectory is what actually gets imported, same
    shape as a checkout (``repo_root()`` also directly contains ``rpfarm/``).
    """
    return home / "pkg"


def install_package_copy(home: Path, root: Path | None = None, log=None) -> Path:
    """Copy this checkout's ``rpfarm/`` package into :func:`stable_package_root`.

    Atomic from the reader's side: staged in a sibling temp directory under
    the same parent (so the swap-in rename is on one filesystem), then
    swapped into place with plain renames -- the old copy is renamed out of
    the way first, the new one renamed in, and only THEN deleted. A Houdini
    process that imports mid-swap sees either the complete old copy or the
    complete new one, never a partially written directory; that is the
    entire point (the incident this fixes was exactly a half-updated
    package caught live).

    Rolls back (puts the old copy back) rather than leave the artist with
    no package at all if the final rename fails.
    """
    say = log if log is not None else (lambda _m: None)
    root = root or repo_root()
    stable_root = stable_package_root(home)
    stable_root.mkdir(parents=True, exist_ok=True)
    dest = stable_root / "rpfarm"

    tag = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    staging = stable_root / f".rpfarm.new-{tag}"
    if staging.exists():
        shutil.rmtree(staging)
    shutil.copytree(root / "rpfarm", staging)

    old = None
    if dest.exists() or dest.is_symlink():
        old = stable_root / f".rpfarm.old-{tag}"
        os.replace(dest, old)
    try:
        os.replace(staging, dest)
    except OSError:
        if old is not None:
            os.replace(old, dest)  # put it back -- never leave dest missing
        raise
    if old is not None:
        shutil.rmtree(old, ignore_errors=True)

    say(f"installed rpfarm package -> {dest}")
    return stable_root


#: Same path convention the scheduler's PythonModule holds an OS-level lock
#: on for a cook's whole duration (see its _acquireCookLock/_releaseCookLock) --
#: duplicated here rather than imported, since this setup-side module has no
#: other reason to depend on the runtime scheduler code, and the path itself
#: is the only thing the two sides need to agree on.
def cook_lock_path(home: Path) -> Path:
    return home / "locks" / "cook.lock"


def cook_is_running(home: Path) -> bool:
    """Best-effort: is some Houdini on this machine mid-cook right now?

    A non-blocking probe of the scheduler's own cook lock -- held from
    onStartCook to onStopCook, an OS-level flock that releases itself if
    that Houdini crashes, so there is no staleness to reason about the way
    a plain marker file would have. 2026-09-08: a package swap landing
    mid-cook is exactly what made a live session's onTick fall over on a
    NameError, and cost a rendered frame -- this is the installer's side of
    not doing that again.

    "Unknown" answers False (no cook detected) rather than True: the lock
    file cannot be opened, or this platform has neither ``fcntl`` nor
    ``msvcrt``. Refusing to install over a cook only helps when a cook is
    actually known to be running; guessing wrong the other way would block
    every install, forever, on a machine where the probe itself is broken.
    """
    path = cook_lock_path(home)
    if not path.exists():
        return False
    try:
        fh = open(path, "a+")
    except OSError:
        return False
    try:
        try:
            if platform.system() == "Windows":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            return True  # someone else holds it -- a cook is running
        return False
    finally:
        fh.close()


def collapse_hda(hotl_bin: Path, source_dir: Path, dest_file: Path, runner=subprocess.run) -> None:
    """Collapse a VCS-friendly expanded HDA directory (``hotl -t`` form,
    what's checked into ``hda/*.hda/``) into one installable ``.hda`` file
    with ``hotl -l`` (its own documented counterpart -- see this module's
    docstring for why not ``-c``/``-C``).

    Raises :class:`subprocess.CalledProcessError` on a non-zero ``hotl``
    exit; ``dest_file``'s parent directory is created first.
    """
    dest_file.parent.mkdir(parents=True, exist_ok=True)
    if dest_file.exists():
        dest_file.unlink()
    runner([str(hotl_bin), "-l", str(source_dir), str(dest_file)], check=True, capture_output=True, text=True)


def install_hda_file(install: HoudiniInstall, hda_file: Path, name: str) -> Path:
    """Copy an already-collapsed ``.hda`` file into ``<prefs>/otls/<name>.hda``."""
    otls_dir = install.user_pref_dir / "otls"
    otls_dir.mkdir(parents=True, exist_ok=True)
    target = otls_dir / f"{name}.hda"
    shutil.copyfile(hda_file, target)
    return target


def build_and_install_hdas(install: HoudiniInstall, otls_cache_dir: Path, root: Path | None = None, runner=subprocess.run) -> list[dict]:
    """Collapse and install all four :data:`HDA_NAMES` for one Houdini
    installation. Returns one status dict per HDA::

        {"name": str, "ok": bool, "installed_to": str | None,
         "error": str | None, "stale": bool}

    If ``install.hotl`` is missing, every HDA is reported ``ok: False``
    with an explanatory error rather than raising -- ``setup``/``doctor``
    are expected to surface that per-item, not abort the whole run over
    one missing tool.

    ``stale`` says the SOURCE in this checkout was built against a
    different package than the one installing it, i.e. somebody changed
    ``rpfarm/`` and did not rerun ``scripts/rebuild_assets.py``. Installing
    it anyway is right -- a stale asset beats yesterday's asset -- but
    saying nothing is how "rebuilt" and "installed" came apart in the first
    place.
    """
    import rpfarm

    package_fingerprint = rpfarm.fingerprint(str((root or repo_root()) / "rpfarm"))
    results = []
    for name in HDA_NAMES:
        source = hda_source_dir(name, root)
        if install.hotl is None:
            results.append({
                "name": name, "ok": False, "installed_to": None, "stale": False,
                "error": f"hotl not found next to hython ({install.hython})",
            })
            continue
        if not source.is_dir():
            results.append({
                "name": name, "ok": False, "installed_to": None, "stale": False,
                "error": f"HDA source not found at {source}",
            })
            continue
        collapsed = otls_cache_dir / f"{name}.hda"
        stale = asset_state(source, package_fingerprint)["stale"]
        if stale:
            results.append({'name': name, 'ok': False, 'installed_to': None, 'stale': True,
                            'error': 'HDA and Python differ; rebuild the bundle before installing.'})
            continue
        try:
            collapse_hda(install.hotl, source, collapsed, runner=runner)
            target = install_hda_file(install, collapsed, name)
            results.append({"name": name, "ok": True, "installed_to": str(target),
                            "error": None, "stale": stale})
        except (subprocess.CalledProcessError, OSError) as e:
            results.append({"name": name, "ok": False, "installed_to": None,
                            "error": str(e), "stale": stale})
    return results


# ---------------------------------------------------------------------------
# what an installed asset was built against
# ---------------------------------------------------------------------------
#
# tests/test_hda_assets.py already refuses to let the REPOSITORY's assets
# drift from the package. It cannot see the copy that is actually installed
# in somebody's Houdini, and on 2026-09-07 that was the whole failure: the
# assets were rebuilt in the checkout, the tests were green, and the artist
# went on cooking with a build from the day before, because "rebuilt" and
# "installed" are two steps and only the first one happened.
#
# So the same measurement is read back out of the installed file. The baked
# block survives ``hotl -l`` as plain text inside the collapsed .hda
# (verified), which makes this a fact about what the artist has rather than
# a date on a file.

#: Must stay identical to scripts/hda_guard.py's -- asserted by a test.
BAKE_BEGIN = "# BEGIN baked by scripts/bake_asset_fingerprint.py -- do not edit"
BAKE_END = "# END baked"

_BAKED_RE = re.compile(
    re.escape(BAKE_BEGIN) + r"(?P<body>.*?)" + re.escape(BAKE_END), re.S)


def baked_fingerprint(data) -> dict | None:
    """``{"version": str, "fingerprint": {...}}`` baked into an asset, or None.

    Takes the bytes of a collapsed ``.hda`` or the text of an expanded
    section -- both carry the block verbatim. Never raises: an asset from
    before the block existed, or one this cannot parse, is "I cannot tell",
    which the callers report as a warning rather than a failure.
    """
    if isinstance(data, bytes):
        data = data.decode("utf-8", "replace")
    match = _BAKED_RE.search(data or "")
    if match is None:
        return None
    body = match.group("body")
    version = re.search(r"_ASSET_BUILT_AGAINST_VERSION\s*=\s*['\"]([^'\"]*)['\"]", body)
    fp = re.search(r"_ASSET_FINGERPRINT\s*=\s*(\{.*?\n\})", body, re.S)
    if fp is None:
        return None
    try:
        parsed = ast.literal_eval(fp.group(1))
    except (ValueError, SyntaxError):
        return None
    if not isinstance(parsed, dict):
        return None
    return {"version": version.group(1) if version else "", "fingerprint": parsed}


def fingerprint_mismatch(package_fingerprint, baked) -> list[str]:
    """Module files whose current content is not what the asset was built
    against. Empty means they agree."""
    if not baked:
        return []
    off = []
    for name in sorted(set(package_fingerprint) | set(baked)):
        if package_fingerprint.get(name) != baked.get(name):
            off.append(name)
    return off


def asset_state(path: Path, package_fingerprint) -> dict:
    """One asset (source directory or installed .hda) against the package::

        {"present": bool, "baked": bool, "stale": bool, "off": [names]}

    ``stale`` is only ever True when the asset actually carries a baked
    block AND it disagrees -- "cannot tell" is never reported as "wrong".
    """
    state = {"present": False, "baked": False, "stale": False, "off": []}
    try:
        if path.is_dir():
            data = "".join(
                f.read_text(encoding="utf-8", errors="replace")
                for f in sorted(path.rglob("PythonModule")))
        else:
            data = path.read_bytes()
    except OSError:
        return state
    state["present"] = True
    baked = baked_fingerprint(data)
    if baked is None:
        return state
    state["baked"] = True
    off = fingerprint_mismatch(package_fingerprint, baked["fingerprint"])
    state["off"] = off
    state["stale"] = bool(off)
    return state


def installed_asset_states(install: HoudiniInstall, package_fingerprint) -> dict:
    """:func:`asset_state` for every HDA as it is INSTALLED for this artist."""
    otls = install.user_pref_dir / "otls"
    managed = install.user_pref_dir / 'packages' / 'runpodfarm-release.json'
    if managed.is_file():
        try:
            settings = json.loads(managed.read_text())
            root = next(Path(entry['RPFARM_ROOT']) for entry in settings.get('env', []) if 'RPFARM_ROOT' in entry)
            if (root / 'houdini' / 'otls').is_dir():
                import rpfarm
                otls = root / 'houdini' / 'otls'
                package_fingerprint = rpfarm.fingerprint(str(root / 'rpfarm'))
        except (OSError, ValueError, KeyError, StopIteration, TypeError):
            pass
    return {name: asset_state(otls / f"{name}.hda", package_fingerprint)
            for name in HDA_NAMES}


# ---------------------------------------------------------------------------
# node shape
# ---------------------------------------------------------------------------

# All four HDAs draw themselves with this custom network-editor shape (a
# chamfered rectangle -- see hda/nodeshapes/rpfarm.json). Houdini only knows
# a shape by name, and only finds it under ``config/NodeShapes`` somewhere on
# HOUDINI_PATH, so the file has to be copied next to the HDAs at setup time
# or every farm node falls back to a plain rectangle. Unlike the icons
# (which live inside each HDA as an ``IconSVG`` section and need no
# installing) a shape cannot travel inside an asset.
NODE_SHAPE_NAME = "rpfarm"


def node_shape_source(root: Path | None = None) -> Path:
    return (root or repo_root()) / "hda" / "nodeshapes" / f"{NODE_SHAPE_NAME}.json"


def node_shape_target(install: HoudiniInstall) -> Path:
    return install.user_pref_dir / "config" / "NodeShapes" / f"{NODE_SHAPE_NAME}.json"


def install_node_shape(install: HoudiniInstall, root: Path | None = None) -> dict:
    """Copy the shape into ``<prefs>/config/NodeShapes/``.

    Same never-raise contract as :func:`build_and_install_hdas`: returns
    ``{"ok", "installed_to", "error"}`` so ``setup`` can report it beside
    the HDAs rather than aborting the whole run over a cosmetic file.
    """
    source = node_shape_source(root)
    if not source.is_file():
        return {"ok": False, "installed_to": None, "error": f"node shape not found at {source}"}
    try:
        target = node_shape_target(install)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    except OSError as e:
        return {"ok": False, "installed_to": None, "error": str(e)}
    return {"ok": True, "installed_to": str(target), "error": None}


# ---------------------------------------------------------------------------
# TAB-menu tool
# ---------------------------------------------------------------------------

#: Our own shelf file, never the artist's ``default.shelf``. Houdini loads
#: every ``*.shelf`` under ``<prefs>/toolbar`` at startup (that is the
#: default ``HOUDINI_TOOLBAR_PATH``), so one file of our own is enough and
#: nothing of theirs is touched.
SHELF_FILENAME = "rpfarm.shelf"
SHELF_TOOL_NAME = "rpfarm_farm_setup"
SHELF_TOOL_LABEL = "RunPod Farm Setup"
SHELF_TOOL_ICON = "TOP/scheduler"

#: THE TAB submenu. One name for the four assets and for the setup tool,
#: defined once here and asserted by a test, because it was two: the assets'
#: Tools.shelf said "RunPodFarm" and this tool said "RunPod Farm", so the
#: artist got two sections in the TAB menu and neither held everything.
#: The spelling follows the node labels ("RunPodFarm Upload").
TAB_SUBMENU = "RunPodFarm"
#: Kept as the name the installer code reads.
SHELF_TOOL_SUBMENU = TAB_SUBMENU

#: The Tools.shelf every one of the four assets carries, so all four appear
#: in that one submenu. Houdini expands the $HDA_* placeholders per asset,
#: which is why one document serves all of them.
ASSET_TOOLS_SHELF = """<?xml version="1.0" encoding="UTF-8"?>
<shelfDocument>
  <!-- This file contains definitions of shelves, toolbars, and tools.
 It should not be hand-edited when it is being used by the application.
 Note, that two definitions of the same element are not allowed in
 a single file. -->

  <tool name="$HDA_DEFAULT_TOOL" label="$HDA_LABEL" icon="$HDA_ICON">
    <toolMenuContext name="viewer">
      <contextNetType>TOP</contextNetType>
    </toolMenuContext>
    <toolMenuContext name="network">
      <contextOpType>$HDA_TABLE_AND_NAME</contextOpType>
    </toolMenuContext>
    <toolSubmenu>{submenu}</toolSubmenu>
    <script scriptType="python"><![CDATA[import toptoolutils

toptoolutils.genericTool(kwargs, '$HDA_NAME')]]></script>
  </tool>
</shelfDocument>""".format(submenu=TAB_SUBMENU)


def asset_tools_shelf() -> str:
    """The Tools.shelf document an asset ships so it lands in the TAB menu."""
    return ASSET_TOOLS_SHELF

#: The tool's script. Two jobs: put this checkout on sys.path the same way
#: the HDAs do (RPFARM_ROOT, else the ~/.rpfarm/src symlink), and hand the
#: TAB's own kwargs to the builder. Everything else lives in
#: rpfarm.scene_setup, where it can be read and tested; a shelf file is a
#: bad place to keep logic, because nothing recompiles or reviews it.
SHELF_TOOL_SCRIPT = """\
# RunPod Farm Setup -- written by `rpfarm setup`. Do not edit here; edit
# rpfarm/scene_setup.py and rerun setup.
import os
import pathlib
import sys

_root = os.environ.get("RPFARM_ROOT") or str(pathlib.Path.home() / ".rpfarm" / "src")
if _root not in sys.path:
    sys.path.insert(0, _root)

try:
    from rpfarm import scene_setup
except ImportError as exc:
    raise ImportError(
        "RunPod Farm Setup cannot import rpfarm from {}. Run "
        "`python3 -m rpfarm setup` and restart Houdini.".format(_root)) from exc

scene_setup.run(kwargs)
"""

SHELF_TOOL_HELP = (
    "Build the RunPod farm graph in this TOP network: scheduler (set as the "
    "network's TOP Scheduler), upload, a Wait For All gate and a ROP Fetch, "
    "preconfigured from this scene. Creating the nodes rather than copying "
    "them from another scene is the point -- a copied node brings the other "
    "scene's cached work items and its old definition with it. Safe to run "
    "twice: it adopts what is already there instead of duplicating it."
)

_SHELF_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<shelfDocument>
  <!-- This file contains definitions of shelves, toolbars, and tools.
 It should not be hand-edited when it is being used by the application.
 Note, that two definitions of the same element are not allowed in
 a single file. -->

  <tool name="{name}" label="{label}" icon="{icon}">
    <helpText><![CDATA[{help}]]></helpText>
    <toolMenuContext name="network">
      <contextNetType>TOP</contextNetType>
    </toolMenuContext>
    <toolSubmenu>{submenu}</toolSubmenu>
    <script scriptType="python"><![CDATA[{script}]]></script>
  </tool>
</shelfDocument>
"""


def shelf_tool_source() -> str:
    """The ``.shelf`` document, in the shape Houdini's own
    ``hou.shelves.newTool`` writes.

    Generated as text rather than through ``hou.shelves`` because
    ``rpfarm setup`` runs on a plain ``python3`` with no Houdini in the
    process -- launching hython just to write nine lines of XML would make
    installation slower and more fragile than the thing being installed.
    The format was taken from a file Houdini wrote, not invented.
    """
    for field in (SHELF_TOOL_HELP, SHELF_TOOL_SCRIPT):
        # A CDATA section ends at the first "]]>", so one inside the payload
        # would truncate the tool silently.
        assert "]]>" not in field, "a shelf payload must not contain ']]>'"
    return _SHELF_TEMPLATE.format(
        name=SHELF_TOOL_NAME,
        label=SHELF_TOOL_LABEL,
        icon=SHELF_TOOL_ICON,
        help=SHELF_TOOL_HELP,
        submenu=SHELF_TOOL_SUBMENU,
        script=SHELF_TOOL_SCRIPT,
    )


def shelf_tool_target(install: HoudiniInstall) -> Path:
    return install.user_pref_dir / "toolbar" / SHELF_FILENAME


def install_shelf_tool(install: HoudiniInstall) -> dict:
    """Write the TAB-menu tool into ``<prefs>/toolbar/rpfarm.shelf``.

    Same never-raise contract as the HDAs and the node shape: returns
    ``{"ok", "installed_to", "error"}`` so a read-only prefs directory
    costs a warning rather than the whole setup.
    """
    try:
        target = shelf_tool_target(install)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(shelf_tool_source(), encoding="utf-8")
    except OSError as e:
        return {"ok": False, "installed_to": None, "error": str(e)}
    return {"ok": True, "installed_to": str(target), "error": None}


# ---------------------------------------------------------------------------
# houdini.env
# ---------------------------------------------------------------------------

_RPFARM_ROOT_MARKER = "# rpfarm setup"
_RPFARM_ROOT_LINE_RE = re.compile(r'^\s*RPFARM_ROOT\s*=\s*"(.*)"\s*$')


def _existing_rpfarm_root(text: str) -> str | None:
    """The value of an ``RPFARM_ROOT = "..."`` line already in ``text``, if
    any -- with or without this project's own marker comment above it, so a
    value someone set by hand is recognised exactly like one this function
    wrote itself."""
    for ln in text.splitlines():
        m = _RPFARM_ROOT_LINE_RE.match(ln)
        if m:
            return m.group(1)
    return None


def write_rpfarm_root_env(install: HoudiniInstall, root: Path | None = None,
                          home: Path | None = None, log=None) -> Path:
    """Write/replace the ``RPFARM_ROOT`` line in ``<prefs>/houdini.env`` so
    HDAs (and out-of-process ``rpfarm.package_runner`` calls they spawn,
    via the job environment) can find rpfarm without depending on the
    ``~/.rpfarm/src`` symlink alone.

    Idempotent: a previous ``rpfarm setup``'s marker+line pair is replaced
    in place rather than appended again.

    ``root=None`` -- the default -- now points at :func:`stable_package_root`
    (``~/.rpfarm/pkg``), NOT this checkout. Pointing an artist's session at
    a live git checkout was the root cause of 2026-09-08's incidents: an
    agent's mid-edit files, imported straight off disk, blocked his cook
    with a stale-code guard three times in one session. ``rpfarm setup`` is
    expected to have already run :func:`install_package_copy` so that
    location actually has something in it.

    On top of that, this no longer overwrites a DIFFERENT value the file
    already has, whatever the default is: an artist's `houdini.env` pointed
    somewhere else on purpose is left alone, because the SAME incident also
    happened the other way -- a `rebuild_assets.py` run for an unrelated
    fix silently switched a deliberately-set value back to this function's
    own default, mid-session. An explicit ``root=`` (a developer choosing a
    specific checkout) is a deliberate instruction and always wins, exactly
    as before; it is only the *default* that now defers to an existing,
    different, presumably intentional value instead of clobbering it.
    """
    say = log if log is not None else (lambda _m: None)
    explicit_root = root is not None
    root = root or stable_package_root(home or (Path.home() / ".rpfarm"))
    env_file = install.user_pref_dir / "houdini.env"
    line = f'RPFARM_ROOT = "{root}"'

    existing = ""
    if env_file.is_file():
        try:
            existing = env_file.read_text(encoding="utf-8")
        except OSError:
            existing = ""

    if not explicit_root:
        current = _existing_rpfarm_root(existing)
        if current is not None and current != str(root):
            say(f"RPFARM_ROOT already set to {current!r} in {env_file} -- "
                f"leaving it (pass root= to override deliberately)")
            return env_file

    lines = existing.splitlines()
    out = []
    skip_next = False
    for ln in lines:
        if skip_next:
            skip_next = False
            continue
        if ln.strip() == _RPFARM_ROOT_MARKER:
            skip_next = True  # drop the line that follows the marker too
            continue
        if _RPFARM_ROOT_LINE_RE.match(ln):
            # A bare RPFARM_ROOT line with no marker above it -- someone's
            # hand-written one, or a copy of a real houdini.env that had
            # one (isolated_child_env does exactly this). Dropped here too:
            # leaving it behind would put TWO RPFARM_ROOT lines in the file,
            # and which one Houdini honours is not a question worth having
            # an answer to.
            continue
        out.append(ln)

    out.append(_RPFARM_ROOT_MARKER)
    out.append(line)

    install.user_pref_dir.mkdir(parents=True, exist_ok=True)
    env_file.write_text("\n".join(out) + "\n", encoding="utf-8")
    return env_file


# ---------------------------------------------------------------------------
# Forcing a child hython to read a SPECIFIC rpfarm package (Ruling R63)
#
# Passing RPFARM_ROOT through a child process's env= is not enough: Houdini
# applies its own houdini.env to the environment AFTER the process starts,
# and houdini.env is documented to override existing entries -- so on any
# machine whose houdini.env sets RPFARM_ROOT (every artist's, after Ruling
# R62), a plain env= override is silently discarded. Measured live,
# 2026-09-08: rpfarm.smoke's own end-to-end proof, and
# tests/test_node_creation.py, were both affected.
#
# The only thing that stops houdini.env from overriding anything is there
# being no houdini.env to apply -- which means pointing
# HOUDINI_USER_PREF_DIR somewhere else, which is ALSO where Houdini looks
# for installed HDAs by default. Two live findings, not assumed:
#   * a HOUDINI_USER_PREF_DIR value that does not carry Houdini's own
#     "__HVER__" placeholder is silently ignored -- logged as "EnvControl:
#     HOUDINI_USER_PREF_DIR missing __HVER__, ignored" -- and Houdini falls
#     straight back to the real prefs dir, i.e. exactly the bug this exists
#     to avoid;
#   * licensing does not depend on HOUDINI_USER_PREF_DIR at all (verified:
#     hou.licenseCategory() still reports Commercial under a brand new,
#     otherwise empty prefs dir) -- so isolating this does not cost a
#     licence.
# ---------------------------------------------------------------------------


def isolated_child_env(root: Path, scratch_dir: Path, install: HoudiniInstall,
                       extra_env=None, keep_real_otls=True) -> dict:
    """The env= to hand a child hython so it reads ``root``'s rpfarm package
    no matter what any real ``houdini.env`` on this machine says.

    Builds a fresh ``HOUDINI_USER_PREF_DIR`` under ``scratch_dir``:

    * ``houdini.env`` is a COPY of ``install``'s real one (license server,
      any HOUDINI_PATH extras, everything an artist's setup depends on),
      with its ``RPFARM_ROOT`` line forced to ``root`` afterwards
      (:func:`write_rpfarm_root_env` with an explicit ``root=`` -- always
      wins, see that function's own docstring). No real one to copy is not
      an error, just a fresh file with only the forced line.
    * ``otls/`` is SYMLINKED to the real one when ``keep_real_otls`` (the
      default): the assets a live caller like `rpfarm smoke` already
      checked are installed (see its own ``_check_hdas``) stay found,
      rather than silently vanishing because the prefs dir moved. Pass
      ``keep_real_otls=False`` for a caller that installs (or has already
      installed) its OWN copy into ``scratch_dir`` instead -- a fully
      isolated unit test wants the checkout's own freshly built HDAs, not
      whatever happens to be on this machine.

    Returns the env dict (a copy of ``os.environ`` plus ``extra_env`` plus
    the ``HOUDINI_USER_PREF_DIR`` override, in the ``__HVER__`` placeholder
    form Houdini itself requires).
    """
    root = Path(root)
    scratch_dir = Path(scratch_dir)
    real_pref_dir = Path(install.user_pref_dir)
    placeholder = str(scratch_dir / "prefs_v__HVER__")
    user_pref_dir = Path(placeholder.replace("__HVER__", install.major_minor))
    user_pref_dir.mkdir(parents=True, exist_ok=True)

    if keep_real_otls:
        real_otls = real_pref_dir / "otls"
        scratch_otls = user_pref_dir / "otls"
        if real_otls.is_dir() and not scratch_otls.exists():
            scratch_otls.symlink_to(real_otls)

    real_env_file = real_pref_dir / "houdini.env"
    if real_env_file.is_file():
        shutil.copyfile(real_env_file, user_pref_dir / "houdini.env")
    fake_install = HoudiniInstall.__new__(HoudiniInstall)
    fake_install.user_pref_dir = user_pref_dir
    write_rpfarm_root_env(fake_install, root=root)  # explicit root always wins

    env = dict(os.environ, **(extra_env or {}))
    env["HOUDINI_USER_PREF_DIR"] = placeholder
    return env


# ---------------------------------------------------------------------------
# interpreter for out-of-process package work items
# ---------------------------------------------------------------------------

# `rpfarm.config` reads config.toml with `tomllib`, which is 3.11+. An older
# interpreter cannot import `rpfarm` at all, so handing one to a work item is
# the same as not running the item.
PACKAGE_PYTHON_MIN = (3, 11)

# Newest first. Used only after the bundled interpreter and a plain `python3`
# have been ruled out, so the list being finite costs nothing in practice.
_NAMED_PYTHONS = tuple("python3.{}".format(m) for m in range(20, 10, -1))


class NoUsablePythonError(RuntimeError):
    """No interpreter on this machine can import ``rpfarm``.

    Raised rather than falling back to a bare ``python3``: writing an
    interpreter into a work item's command without knowing it can import
    ``rpfarm`` is what shipped the tomllib defect, and it fails at the far
    end of a cook instead of here, where the reason is still obvious.
    """


_VERSION_CACHE = {}


def python_version(exe, run=None):
    """``(major, minor)`` of the interpreter at ``exe``, or ``None``.

    Actually runs it -- a path can exist, be the right name, and still be a
    3.9. Cached per path, because the resolver is called once per generate but
    a generate can be called repeatedly, and because a subprocess per candidate
    per call would be a silly price for a constant. Never raises: a candidate
    that cannot be executed is simply not a candidate.
    """
    if exe in _VERSION_CACHE:
        return _VERSION_CACHE[exe]
    run = run or subprocess.run
    result = None
    try:
        proc = run(
            [exe, "-c", "import sys; print(sys.version_info[0], sys.version_info[1])"],
            capture_output=True, text=True, timeout=30)
        if proc.returncode == 0:
            major, minor = (int(x) for x in proc.stdout.split()[:2])
            result = (major, minor)
    except Exception:  # noqa: BLE001 - unrunnable candidate, not an error
        result = None
    _VERSION_CACHE[exe] = result
    return result


def houdini_bundled_python(hfs, version=None, platform_name=None, exists=None,
                           listdir=None):
    """Absolute path to the **plain** Python that ships inside a Houdini install.

    ``hfs`` is ``$HFS`` (what ``hou.getenv("HFS")`` returns) and ``version`` is
    the ``(major, minor)`` of the Python that Houdini is running -- taken from
    the caller's own ``sys.version_info`` rather than hardcoded, because it
    moves with the Houdini version (22.0 ships 3.13, older ones 3.11/3.10).

    Deliberately **not** ``hython``: ``hython`` initialises the Houdini
    environment and checks out a licence, and an upload that splits into eight
    packages would try to take eight. The plain interpreter beside it takes
    none, and everything ``rpfarm.package_runner`` touches is stdlib-only.

    Layouts:

    - macOS: ``Python.framework`` is a *sibling* of ``Houdini.framework``, so
      the path is found by walking up from ``$HFS``
      (``.../Frameworks/Houdini.framework/Versions/<ver>/Resources``) until a
      directory containing ``Python.framework`` appears. Searched rather than
      counted: it is four levels here, and counting two -- the obvious guess --
      lands inside ``Houdini.framework/Versions`` and resolves nothing.
    - Linux: ``$HFS/python/bin/python3``.
    - Windows: ``$HFS/python/python.exe``.

    Returns the path only if the file exists, else ``None``.
    """
    if not hfs:
        return None
    platform_name = platform_name or sys.platform
    exists = exists or os.path.exists
    listdir = listdir or _safe_listdir
    root = Path(hfs)
    version = version or sys.version_info[:2]
    tag = "{}.{}".format(version[0], version[1])

    if platform_name == "darwin":
        for parent in [root] + list(root.parents):
            versions = parent / "Python.framework" / "Versions"
            candidate = versions / tag / "bin" / ("python" + tag)
            if exists(str(candidate)):
                return str(candidate)
            # `version` is only a hint. Its default is the *calling* process's
            # sys.version_info, which is right when the caller is Houdini (the
            # generate script) and wrong for anything else -- the CLI, a test,
            # a plain python3 -- and being wrong there meant silently falling
            # through to PATH, which is the very thing this function exists to
            # avoid. So when the hint misses, ask the install which Pythons it
            # actually has and take the newest.
            for found in sorted(_framework_versions(versions, listdir), reverse=True):
                found_tag = "{}.{}".format(*found)
                candidate = versions / found_tag / "bin" / ("python" + found_tag)
                if exists(str(candidate)):
                    return str(candidate)
        return None

    if platform_name.startswith("win"):
        candidate = root / "python" / "python.exe"
    else:
        candidate = root / "python" / "bin" / "python3"
    return str(candidate) if exists(str(candidate)) else None


def _framework_versions(versions_dir, listdir=None):
    """``(major, minor)`` of every ``Versions/<M.m>`` a Python.framework has."""
    listdir = listdir or _safe_listdir
    out = []
    for name in listdir(str(versions_dir)):
        match = re.fullmatch(r"(\d+)\.(\d+)", name)
        if match:
            out.append((int(match.group(1)), int(match.group(2))))
    return out


def discover_python_on_disk(search_dirs=None, exists=None, listdir=None):
    """Newest ``python3.<minor>`` on disk that *names* itself >= the minimum.

    The name is a filter, not proof -- the caller still runs it. Scans explicit
    directories rather than ``PATH``, because ``PATH`` is exactly what cannot
    be trusted here.
    """
    exists = exists or os.path.exists
    listdir = listdir or _safe_listdir
    if search_dirs is None:
        search_dirs = ["/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin", "/usr/bin"]
    best = None
    for directory in search_dirs:
        for name in listdir(directory):
            match = re.fullmatch(r"python3\.(\d+)", name)
            if not match:
                continue
            minor = int(match.group(1))
            if (3, minor) < PACKAGE_PYTHON_MIN:
                continue
            path = os.path.join(directory, name)
            if not exists(path):
                continue
            if best is None or minor > best[0]:
                best = (minor, path)
    return best[1] if best else None


def _safe_listdir(directory):
    try:
        return sorted(os.listdir(directory))
    except OSError:
        return []


def resolve_package_python(hfs=None, version=None, platform_name=None,
                           exists=None, which=None, run=None, search_dirs=None):
    """``(interpreter, reason)`` for the out-of-process package runner.

    The command written into a work item must name an absolute interpreter that
    is *known* to be able to import ``rpfarm``. It used to be
    ``shutil.which("python3") or "python3"``, and that shipped a real defect:
    ``PATH`` inside a Dock-launched Houdini is minimal, so ``python3`` resolved
    to Xcode's 3.9, which has no ``tomllib``, so every upload item died on
    ``import rpfarm.config`` before doing any work. Every headless run went
    through a shell whose ``PATH`` started with a modern python, so it passed
    the smoke for the wrong reason -- three times.

    Order. Every candidate is **executed** to read its real version before it
    is accepted; existing at the right path under the right name is not proof.

    1. The plain Python bundled inside the running Houdini, from ``$HFS``.
       Guaranteed present wherever this tool can run at all, modern, and takes
       no licence.
    2. ``which("python3")`` -- accepted only if it is new enough.
    3. Named ``python3.<minor>`` interpreters, newest first, from ``PATH`` and
       then from known install directories.
    4. Nothing. Raises :class:`NoUsablePythonError` naming what was tried,
       rather than handing a work item an interpreter that cannot import
       ``rpfarm`` and letting it fail a cook later.
    """
    version = version or sys.version_info[:2]
    which = which or shutil.which
    tried = []

    def accept(path, how):
        found = python_version(path, run=run)
        tried.append("{} -> {}".format(path, "{}.{}".format(*found) if found else "unusable"))
        if found and found >= PACKAGE_PYTHON_MIN:
            return path, "{} (python{}.{})".format(how, *found)
        return None

    bundled = houdini_bundled_python(hfs, version, platform_name=platform_name, exists=exists)
    if bundled:
        got = accept(bundled, "Houdini's own bundled python from $HFS, no licence taken")
        if got:
            return got
    else:
        tried.append("no bundled python under $HFS={!r}".format(hfs))

    on_path = which("python3")
    if on_path:
        got = accept(on_path, "python3 from PATH")
        if got:
            return got
    else:
        tried.append("no python3 on PATH")

    seen = {bundled, on_path}
    for name in _NAMED_PYTHONS:
        candidate = which(name)
        if candidate and candidate not in seen:
            seen.add(candidate)
            got = accept(candidate, "{} from PATH".format(name))
            if got:
                return got
    found_on_disk = discover_python_on_disk(search_dirs=search_dirs, exists=exists)
    if found_on_disk and found_on_disk not in seen:
        got = accept(found_on_disk, "found on disk")
        if got:
            return got

    raise NoUsablePythonError(
        "No Python >= {}.{} found to run rpfarm's package work items, so the "
        "cook would fail on 'import tomllib' on every package. Tried:\n  {}\n"
        "Expected Houdini's own bundled interpreter beside $HFS={!r}. If this "
        "Houdini really has none, put a python3.11+ on PATH.".format(
            PACKAGE_PYTHON_MIN[0], PACKAGE_PYTHON_MIN[1], "\n  ".join(tried) or "nothing", hfs))
