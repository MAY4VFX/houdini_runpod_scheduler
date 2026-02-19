#!/usr/bin/env python3
"""
RunPodFarm HDA Installer

Installs the RunPodFarm Scheduler HDA into Houdini and installs
required Python packages (redis, psutil) into hython.

Cross-platform: macOS, Linux, Windows.
No external dependencies (stdlib only).

Usage:
    python3 install_hda.py
    python3 install_hda.py --non-interactive       # install to all found versions
    python3 install_hda.py --hfs /opt/hfs20.5      # install to specific HFS path
    python3 install_hda.py --skip-packages          # skip pip install step
    python3 install_hda.py --juicefs-path /project  # configure HOUDINI_PATH for JuiceFS
"""

import argparse
import glob
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Terminal colours (ANSI, disabled on Windows without VT support)
# ---------------------------------------------------------------------------

class _Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    DIM = "\033[2m"

    @classmethod
    def disable(cls):
        for attr in ("RESET", "BOLD", "RED", "GREEN", "YELLOW", "CYAN", "DIM"):
            setattr(cls, attr, "")


C = _Colors()

# Disable colours when output is not a terminal or on old Windows
if not sys.stdout.isatty():
    C.disable()
elif platform.system() == "Windows":
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        # Enable VT processing on Windows 10+
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        C.disable()


def info(msg: str) -> None:
    print(f"  {msg}")


def success(msg: str) -> None:
    print(f"  {C.GREEN}[OK]{C.RESET} {msg}")


def warn(msg: str) -> None:
    print(f"  {C.YELLOW}[!]{C.RESET}  {msg}")


def error(msg: str) -> None:
    print(f"  {C.RED}[ERR]{C.RESET} {msg}")


def header(msg: str) -> None:
    print(f"\n{C.BOLD}{C.CYAN}{msg}{C.RESET}")


# ---------------------------------------------------------------------------
# Locate this script / HDA source
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
HDA_SOURCE = REPO_ROOT / "hda" / "runpodfarm_scheduler.hda"

REQUIRED_PACKAGES = ["redis", "psutil"]


# ---------------------------------------------------------------------------
# Houdini installation discovery
# ---------------------------------------------------------------------------

class HoudiniInstall:
    """Represents a single Houdini installation."""

    def __init__(self, hfs: Path):
        self.hfs = hfs
        self.version = self._detect_version()
        self.major_minor = self._major_minor()
        self.hython = self._find_hython()
        self.user_pref_dir = self._find_user_pref_dir()

    # -- version detection ---------------------------------------------------

    def _detect_version(self) -> str:
        """Try to read version from SYS_Version.h, fall back to dir name."""
        version_header = self.hfs / "toolkit" / "include" / "SYS" / "SYS_Version.h"
        if version_header.is_file():
            ver = self._parse_version_header(version_header)
            if ver:
                return ver

        # Fallback: extract from directory name  (e.g. "Houdini 20.5.370", "hfs20.5.370")
        name = self.hfs.name
        m = re.search(r"(\d+\.\d+(?:\.\d+)?)", name)
        if m:
            return m.group(1)

        # Walk up one level for macOS framework layout
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
        """Return 'XX.X' portion used for user pref dir."""
        parts = self.version.split(".")
        if len(parts) >= 2:
            return f"{parts[0]}.{parts[1]}"
        return self.version

    # -- hython --------------------------------------------------------------

    def _find_hython(self) -> Path | None:
        system = platform.system()
        candidates: list[Path] = []

        if system == "Windows":
            candidates = [
                self.hfs / "bin" / "hython.exe",
                self.hfs / "bin" / "hython3.exe",
            ]
        elif system == "Darwin":
            candidates = [
                self.hfs / "bin" / "hython",
                self.hfs / "Frameworks" / "Houdini.framework" / "Versions" / "Current" / "Resources" / "bin" / "hython",
                # Sometimes HFS is already the Resources dir on macOS
                self.hfs / "bin" / "hython3",
            ]
        else:
            candidates = [
                self.hfs / "bin" / "hython",
                self.hfs / "bin" / "hython3",
            ]

        for c in candidates:
            if c.is_file():
                return c
        return None

    # -- user pref dir -------------------------------------------------------

    def _find_user_pref_dir(self) -> Path:
        system = platform.system()
        home = Path.home()

        if system == "Windows":
            # On Windows: ~/Documents/houdiniXX.X
            docs = Path(os.environ.get("USERPROFILE", home)) / "Documents"
            return docs / f"houdini{self.major_minor}"
        elif system == "Darwin":
            # macOS: ~/Library/Preferences/houdini/XX.X
            return home / "Library" / "Preferences" / "houdini" / self.major_minor
        else:
            # Linux: ~/houdiniXX.X
            return home / f"houdini{self.major_minor}"

    # -- display -------------------------------------------------------------

    def __repr__(self) -> str:
        return f"Houdini {self.version} ({self.hfs})"


def _glob_expand(patterns: list[str]) -> list[Path]:
    """Expand glob patterns and return existing directories."""
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

    # Deduplicate and validate
    seen: set[Path] = set()
    installs: list[HoudiniInstall] = []
    for d in candidate_dirs:
        if d in seen:
            continue
        seen.add(d)
        # Quick check: does it look like an HFS?
        if (d / "toolkit").is_dir() or (d / "bin").is_dir() or (d / "houdini").is_dir():
            try:
                installs.append(HoudiniInstall(d))
            except Exception:
                pass

    # Sort by version descending (newest first)
    installs.sort(key=lambda i: i.version, reverse=True)
    return installs


# ---------------------------------------------------------------------------
# Install operations
# ---------------------------------------------------------------------------

def install_hda(install: HoudiniInstall) -> bool:
    """Copy the HDA directory into the user pref otls directory."""
    if not HDA_SOURCE.is_dir():
        error(f"HDA source not found at {HDA_SOURCE}")
        return False

    otls_dir = install.user_pref_dir / "otls"
    target = otls_dir / "runpodfarm_scheduler.hda"

    try:
        otls_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        error(f"Cannot create directory {otls_dir}: {e}")
        return False

    # Remove previous installation if present
    if target.exists():
        try:
            shutil.rmtree(target)
        except OSError as e:
            error(f"Cannot remove existing HDA at {target}: {e}")
            return False

    try:
        shutil.copytree(HDA_SOURCE, target)
    except OSError as e:
        error(f"Failed to copy HDA: {e}")
        return False

    success(f"HDA installed to {target}")
    return True


def install_packages(install: HoudiniInstall) -> bool:
    """Install required Python packages into hython's environment."""
    if install.hython is None:
        warn(f"hython not found for {install}, skipping package install")
        return False

    hython = str(install.hython)

    # First, ensure pip is available
    if not _check_pip(hython):
        info("pip not found in hython, running ensurepip...")
        try:
            subprocess.run(
                [hython, "-m", "ensurepip", "--upgrade"],
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
            success("ensurepip completed")
        except subprocess.CalledProcessError as e:
            error(f"ensurepip failed: {e.stderr.strip() if e.stderr else e}")
            return False
        except FileNotFoundError:
            error(f"Cannot execute hython at {hython}")
            return False
        except subprocess.TimeoutExpired:
            error("ensurepip timed out (120s)")
            return False

    # Install each package
    all_ok = True
    for pkg in REQUIRED_PACKAGES:
        info(f"Installing {pkg} to hython...")
        try:
            result = subprocess.run(
                [hython, "-m", "pip", "install", "--upgrade", pkg],
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
            success(f"{pkg} installed")
        except subprocess.CalledProcessError as e:
            stderr = e.stderr.strip() if e.stderr else str(e)
            error(f"pip install {pkg} failed: {stderr}")
            all_ok = False
        except subprocess.TimeoutExpired:
            error(f"pip install {pkg} timed out (120s)")
            all_ok = False

    return all_ok


def _check_pip(hython: str) -> bool:
    """Return True if pip is available in hython."""
    try:
        result = subprocess.run(
            [hython, "-m", "pip", "--version"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode == 0
    except Exception:
        return False


def configure_houdini_env(install: HoudiniInstall, juicefs_path: str) -> bool:
    """Add HOUDINI_PATH to houdini.env for JuiceFS project paths."""
    env_file = install.user_pref_dir / "houdini.env"
    houdini_path_line = f'HOUDINI_PATH = "{juicefs_path}/hda:&"'
    marker = "# RunPodFarm JuiceFS path"

    existing_content = ""
    if env_file.is_file():
        try:
            existing_content = env_file.read_text(encoding="utf-8")
        except OSError as e:
            error(f"Cannot read {env_file}: {e}")
            return False

        # Check if already configured
        if marker in existing_content:
            warn(f"JuiceFS path already configured in {env_file}")
            return True

    # Append to file
    try:
        install.user_pref_dir.mkdir(parents=True, exist_ok=True)
        with open(env_file, "a", encoding="utf-8") as f:
            if existing_content and not existing_content.endswith("\n"):
                f.write("\n")
            f.write(f"\n{marker}\n")
            f.write(f"{houdini_path_line}\n")
        success(f"HOUDINI_PATH configured in {env_file}")
        return True
    except OSError as e:
        error(f"Cannot write {env_file}: {e}")
        return False


# ---------------------------------------------------------------------------
# Interactive flow
# ---------------------------------------------------------------------------

def prompt_choice(installs: list[HoudiniInstall]) -> list[HoudiniInstall]:
    """Prompt user to select which installations to target."""
    print()
    info(f"{C.BOLD}Found Houdini installations:{C.RESET}")
    for i, inst in enumerate(installs, 1):
        hython_status = f"{C.GREEN}hython found{C.RESET}" if inst.hython else f"{C.YELLOW}hython not found{C.RESET}"
        print(f"    [{i}] {C.BOLD}Houdini {inst.version}{C.RESET}  {C.DIM}({inst.hfs}){C.RESET}  {hython_status}")
    print()

    if len(installs) == 1:
        answer = input(f"  Install to Houdini {installs[0].version}? [Y/n] ").strip().lower()
        if answer in ("", "y", "yes"):
            return installs
        return []

    choices_hint = "/".join(str(i) for i in range(1, len(installs) + 1))
    answer = input(f"  Install to which version? [{choices_hint}/all] ").strip().lower()

    if answer == "all":
        return installs

    # Parse comma-separated or single number
    selected: list[HoudiniInstall] = []
    for part in answer.replace(",", " ").split():
        try:
            idx = int(part) - 1
            if 0 <= idx < len(installs):
                selected.append(installs[idx])
            else:
                warn(f"Invalid choice: {part}")
        except ValueError:
            warn(f"Invalid input: {part}")

    return selected


def run_install(install: HoudiniInstall, skip_packages: bool, juicefs_path: str | None) -> bool:
    """Run the full installation for a single Houdini install."""
    header(f"Installing for Houdini {install.version}")
    info(f"HFS:      {install.hfs}")
    info(f"hython:   {install.hython or 'not found'}")
    info(f"Prefs:    {install.user_pref_dir}")
    print()

    ok = True

    # Step 1: Install HDA
    info("Copying HDA...")
    if not install_hda(install):
        ok = False

    # Step 2: Install Python packages
    if not skip_packages:
        info("Installing Python packages to hython...")
        if not install_packages(install):
            ok = False
    else:
        info(f"{C.DIM}Skipping package installation (--skip-packages){C.RESET}")

    # Step 3: Configure houdini.env (optional)
    if juicefs_path:
        info("Configuring houdini.env for JuiceFS...")
        if not configure_houdini_env(install, juicefs_path):
            ok = False

    return ok


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install RunPodFarm Scheduler HDA into Houdini",
    )
    parser.add_argument(
        "--hfs",
        type=str,
        help="Path to a specific Houdini installation (HFS directory)",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Install to all found Houdini versions without prompting",
    )
    parser.add_argument(
        "--skip-packages",
        action="store_true",
        help="Skip installing Python packages (redis, psutil) to hython",
    )
    parser.add_argument(
        "--juicefs-path",
        type=str,
        default=None,
        help="Path to JuiceFS mount (e.g. /project). Configures HOUDINI_PATH in houdini.env",
    )
    args = parser.parse_args()

    # Banner
    print()
    print(f"  {C.BOLD}{C.CYAN}RunPodFarm HDA Installer{C.RESET}")
    print(f"  {C.CYAN}========================{C.RESET}")

    # Validate HDA source
    if not HDA_SOURCE.is_dir():
        print()
        error(f"HDA source directory not found: {HDA_SOURCE}")
        error("Make sure you are running this script from the repository.")
        return 1

    # Find installations
    if args.hfs:
        hfs_path = Path(args.hfs).resolve()
        if not hfs_path.is_dir():
            error(f"HFS path does not exist: {hfs_path}")
            return 1
        installs = [HoudiniInstall(hfs_path)]
    else:
        info("")
        info("Scanning for Houdini installations...")
        installs = find_houdini_installations()

    if not installs:
        print()
        error("No Houdini installations found.")
        print()
        info("You can specify a path manually:")
        info(f"  {C.BOLD}python3 {sys.argv[0]} --hfs /path/to/hfs{C.RESET}")
        print()
        info("Common locations:")
        if platform.system() == "Darwin":
            info("  /Applications/Side Effects Software/Houdini 20.5.370/Frameworks/Houdini.framework/Versions/Current/Resources")
        elif platform.system() == "Linux":
            info("  /opt/hfs20.5")
        else:
            info("  C:\\Program Files\\Side Effects Software\\Houdini 20.5.370")
        return 1

    # Select targets
    if args.non_interactive or args.hfs:
        selected = installs
    else:
        selected = prompt_choice(installs)

    if not selected:
        info("Nothing selected. Exiting.")
        return 0

    # Run installations
    all_ok = True
    for inst in selected:
        if not run_install(inst, args.skip_packages, args.juicefs_path):
            all_ok = False

    # Summary
    print()
    if all_ok:
        print(f"  {C.GREEN}{C.BOLD}Done!{C.RESET} Open Houdini -> TOP Network -> Tab -> RunPodFarm Scheduler")
    else:
        print(f"  {C.YELLOW}{C.BOLD}Completed with warnings.{C.RESET} Check the messages above for details.")

    print()
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
