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
from pathlib import Path

# ---------------------------------------------------------------------------
# Houdini installation discovery (ported from infrastructure/install_hda.py)
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
