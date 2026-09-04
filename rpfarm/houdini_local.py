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

import glob
import os
import platform
import re
import shutil
import subprocess
import sys
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

        {"name": str, "ok": bool, "installed_to": str | None, "error": str | None}

    If ``install.hotl`` is missing, every HDA is reported ``ok: False``
    with an explanatory error rather than raising -- ``setup``/``doctor``
    are expected to surface that per-item, not abort the whole run over
    one missing tool.
    """
    results = []
    for name in HDA_NAMES:
        source = hda_source_dir(name, root)
        if install.hotl is None:
            results.append({
                "name": name, "ok": False, "installed_to": None,
                "error": f"hotl not found next to hython ({install.hython})",
            })
            continue
        if not source.is_dir():
            results.append({
                "name": name, "ok": False, "installed_to": None,
                "error": f"HDA source not found at {source}",
            })
            continue
        collapsed = otls_cache_dir / f"{name}.hda"
        try:
            collapse_hda(install.hotl, source, collapsed, runner=runner)
            target = install_hda_file(install, collapsed, name)
            results.append({"name": name, "ok": True, "installed_to": str(target), "error": None})
        except (subprocess.CalledProcessError, OSError) as e:
            results.append({"name": name, "ok": False, "installed_to": None, "error": str(e)})
    return results


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
# houdini.env
# ---------------------------------------------------------------------------

_RPFARM_ROOT_MARKER = "# rpfarm setup"


def write_rpfarm_root_env(install: HoudiniInstall, root: Path | None = None) -> Path:
    """Write/replace the ``RPFARM_ROOT`` line in ``<prefs>/houdini.env`` so
    HDAs (and out-of-process ``rpfarm.package_runner`` calls they spawn,
    via the job environment) can find this checkout without depending on
    the ``~/.rpfarm/src`` symlink alone.

    Idempotent: a previous ``rpfarm setup``'s marker+line pair is replaced
    in place rather than appended again.
    """
    root = root or repo_root()
    env_file = install.user_pref_dir / "houdini.env"
    line = f'RPFARM_ROOT = "{root}"'

    existing = ""
    if env_file.is_file():
        try:
            existing = env_file.read_text(encoding="utf-8")
        except OSError:
            existing = ""

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
        out.append(ln)

    out.append(_RPFARM_ROOT_MARKER)
    out.append(line)

    install.user_pref_dir.mkdir(parents=True, exist_ok=True)
    env_file.write_text("\n".join(out) + "\n", encoding="utf-8")
    return env_file


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
