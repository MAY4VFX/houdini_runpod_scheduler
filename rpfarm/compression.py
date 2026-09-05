"""
File classification and compression for VFX data transfer.

Classifies VFX files by their compressibility and applies appropriate
compression strategies (zstd for compressible files, skip for already-compressed).
"""

import logging
import os
import shutil
import struct
import collections
import gzip
import shlex
import subprocess
import zlib
from enum import Enum
from pathlib import Path
from types import SimpleNamespace

from . import tools as _tools

log = logging.getLogger(__name__)

try:
    import lzma as _lzma
except ImportError:  # a Python built without liblzma; zlib is always there
    _lzma = None

try:
    import zstandard as _zstd
except ImportError:
    _zstd = None


class CompressionStrategy(Enum):
    SKIP = "skip"
    #: Compress this file. The VALUE stays "zstd" because it is written into
    #: package manifests, but the name no longer claims to know which codec
    #: does the work -- see select_codec.
    COMPRESS = "zstd"
    ZSTD = COMPRESS  # the old name, kept so existing callers and manifests read
    TAR_ZSTD = "tar_zstd"


# EXR compression types that are already well-compressed
_EXR_COMPRESSED = {2, 3, 4, 5, 6, 7, 8, 9}  # ZIPS, ZIP, PIZ, PXR24, B44, B44A, DWAA, DWAB
_EXR_MAGIC = 0x01312F76  # little-endian magic bytes 76 2f 31 01


def classify_file(path: str) -> CompressionStrategy:
    """Classify a file and return the appropriate compression strategy."""
    lower = path.lower()

    # Already blosc-compressed
    if lower.endswith(".bgeo.sc"):
        return CompressionStrategy.SKIP

    # EXR: check if already compressed
    if lower.endswith(".exr"):
        if _check_exr_compression(path):
            return CompressionStrategy.SKIP
        return CompressionStrategy.ZSTD

    # Houdini mipmapped textures — already compressed
    if lower.endswith((".rat", ".tex")):
        return CompressionStrategy.SKIP

    # VDB: batch with tar+zstd
    if lower.endswith(".vdb"):
        return CompressionStrategy.TAR_ZSTD

    # Compressible scene/geo formats
    if lower.endswith((".abc", ".usd", ".usdc")):
        return CompressionStrategy.ZSTD

    if lower.endswith((".bgeo", ".geo")):
        return CompressionStrategy.ZSTD

    if lower.endswith((".hip", ".hipnc", ".hda")):
        return CompressionStrategy.ZSTD

    # Text files
    if lower.endswith((".py", ".json", ".txt")):
        return CompressionStrategy.ZSTD

    # Unknown: probe compressibility
    return _probe_compressibility(path)


def _check_exr_compression(path: str) -> bool:
    """
    Parse EXR header to determine if the file is already compressed.
    Returns True if already compressed, False if NONE/RLE (benefits from zstd).
    On error, returns True (assume compressed to avoid risk).
    """
    try:
        with open(path, "rb") as f:
            magic = struct.unpack("<I", f.read(4))[0]
            if magic != _EXR_MAGIC:
                log.warning("Not a valid EXR file: %s", path)
                return True

            # Skip version (4 bytes)
            f.read(4)

            # Scan attributes for "compression"
            while True:
                # Read attribute name (null-terminated)
                name_bytes = b""
                while True:
                    ch = f.read(1)
                    if not ch or ch == b"\x00":
                        break
                    name_bytes += ch

                # Empty name marks end of header
                if not name_bytes:
                    break

                attr_name = name_bytes.decode("ascii", errors="replace")

                # Read attribute type (null-terminated)
                type_bytes = b""
                while True:
                    ch = f.read(1)
                    if not ch or ch == b"\x00":
                        break
                    type_bytes += ch

                # Read attribute size
                attr_size = struct.unpack("<I", f.read(4))[0]

                if attr_name == "compression" and attr_size >= 1:
                    comp_type = struct.unpack("B", f.read(1))[0]
                    log.debug("EXR %s compression type: %d", path, comp_type)
                    return comp_type in _EXR_COMPRESSED
                else:
                    # Skip attribute data
                    f.read(attr_size)

        # No compression attribute found
        return True
    except Exception as e:
        log.warning("Failed to read EXR header for %s: %s", path, e)
        return True


def _probe_compressibility(path: str, sample_size: int = 65536) -> CompressionStrategy:
    """
    Read first 64KB and try compressing to determine if file benefits from zstd.
    Returns ZSTD if ratio < 0.95, else SKIP.
    """
    try:
        with open(path, "rb") as f:
            sample = f.read(sample_size)

        if not sample:
            return CompressionStrategy.SKIP

        # zlib at level 1 on a 64 KB sample: microseconds, no subprocess, no
        # external program. This used to shell out to zstd, which meant that
        # on a machine without it EVERY file classified SKIP -- compression
        # silently off, with nothing said.
        ratio = len(zlib.compress(sample, 1)) / len(sample)

        if ratio < 0.95:
            log.debug("File %s compressible (ratio=%.2f)", path, ratio)
            return CompressionStrategy.ZSTD
        else:
            log.debug("File %s not compressible (ratio=%.2f)", path, ratio)
            return CompressionStrategy.SKIP

    except Exception as e:
        log.warning("Probe failed for %s: %s", path, e)
        return CompressionStrategy.SKIP


#: What lzma costs and buys, measured on the owner's own files against his
#: own uplink (4.7 Mbps = 0.59 MB/s, `rpfarm doctor`):
#:
#:     Zeppelin_Balon_Test.usdc, 29 MB   raw upload 51.6 s
#:       lzma preset 1   ratio 0.341   10.1 MB/s   total 20.4 s
#:       lzma preset 3   ratio 0.340    6.0 MB/s   total 22.4 s
#:       lzma preset 6   ratio 0.323    2.9 MB/s   total 26.8 s
#:       zlib level 6    ratio 0.474   26.0 MB/s   total 25.6 s
#:
#: Preset 1 wins the only race that matters -- compress time PLUS transfer
#: time -- and every higher preset loses ground it cannot make back on a
#: slow link. zlib is faster per byte and still slower overall, because at
#: 0.59 MB/s the bytes you did not send are worth more than the CPU you did
#: not spend. Already-compressed files (EXR, most of the weight) never get
#: here: classify_file sends them straight to SKIP, and the same measurement
#: shows why -- every codec made them bigger in total time.
LZMA_PRESET = 1
ZLIB_LEVEL = 6

Codec = collections.namedtuple("Codec", "name ext binary")

#: The stdlib codecs, in order of preference. No downloads, no PATH, no
#: execute bit, identical on macOS, Windows and Linux -- which is the whole
#: requirement: a repository someone can clone, type their RunPod keys into,
#: and use.
CODEC_XZ = Codec("xz", ".xz", None)
CODEC_GZ = Codec("gz", ".gz", None)


def select_codec(allow_external: bool = True, resolve=None) -> Codec:
    """Which codec this machine will use for this package.

    ``zstd`` is an OPTIONAL accelerator, taken only when a real binary is
    found by absolute path (never by bare name on PATH -- that is what died
    on 2026-09-05). Its absence is a normal mode, not a problem: lzma is in
    the standard library of every Python this tool runs on, including the
    one bundled with Houdini and the one on the pod (verified: 3.10.12 there,
    lzma/gzip/zlib all importable).
    """
    if allow_external:
        found = (resolve or _tools.resolve_tool)("zstd")
        if found is not None:
            return Codec("zstd", ".zst", found.path)
    if _lzma is not None:
        return CODEC_XZ
    return CODEC_GZ


def codec_for(path: str) -> Codec:
    """The codec a staged file's extension names."""
    for codec in (CODEC_XZ, CODEC_GZ, Codec("zstd", ".zst", None)):
        if path.endswith(codec.ext):
            return codec
    return CODEC_XZ


# The pod has python3 and lzma but NO xz binary (checked on the live sync
# pod: python 3.10.12, lzma/gzip/zlib importable, `which xz` empty, zstd at
# /usr/bin/zstd). So a .xz/.gz package is unpacked by python, not by a
# command-line tool that may not be installed.
_PY_DECOMPRESS = (
    "import gzip,lzma,os,shutil,sys\n"
    "for p in sys.argv[1:]:\n"
    "    o = gzip.open if p.endswith('.gz') else lzma.open\n"
    "    with o(p,'rb') as s, open(p[:p.rindex('.')],'wb') as d: shutil.copyfileobj(s,d)\n"
    "    os.remove(p)\n"
)


def decompress_command(remote_root: str, rel_paths, codec: Codec) -> str:
    """The shell command that unpacks a staged package on the sync pod."""
    if not rel_paths:
        return ""
    quoted = " ".join(shlex.quote(r) for r in rel_paths)
    if codec.name == "zstd":
        return "cd {} && zstd -d --rm {}".format(shlex.quote(remote_root), quoted)
    return "cd {} && python3 -c {} {}".format(
        shlex.quote(remote_root), shlex.quote(_PY_DECOMPRESS), quoted)


def archiver(name: str = "zstd"):
    """The absolute path to *name*, or None on a machine that has none.

    Never a bare ``"zstd"``: Houdini launched from the Dock has a PATH
    without ``/opt/homebrew/bin``, so the name alone raised FileNotFoundError
    on a machine where zstd was installed the whole time (2026-09-05, an
    upload item died on it). :mod:`rpfarm.tools` resolves and verifies it by
    absolute path instead.

    Absence is silent on purpose. zstd is an accelerator here, not a
    dependency: without it the standard library does the work, which is the
    normal case on a fresh clone and not worth alarming anyone about.
    """
    found = _tools.resolve_tool(name)
    return found.path if found else None


def compress_file(src: str, dst: str, strategy: CompressionStrategy, level: int = 3,
                  codec: Codec = None) -> bool:
    """Compress one file into *dst*. Returns True if it was compressed.

    False is a normal outcome, never an exception: a file that will not
    compress, or a machine with no codec at all, means the file uploads as
    it is. Losing compression is slower; raising fails the work item, which
    is what a bare ``"zstd"`` did to the owner's cook.
    """
    if strategy == CompressionStrategy.SKIP:
        return False

    if strategy == CompressionStrategy.TAR_ZSTD:
        raise ValueError("TAR_ZSTD should not be used on single files; use compress_directory for batching")

    if strategy != CompressionStrategy.COMPRESS:
        return False

    codec = codec or select_codec()
    os.makedirs(os.path.dirname(dst), exist_ok=True)

    if codec.binary:
        try:
            result = subprocess.run(
                [codec.binary, "-T0", f"-{level}", "-f", src, "-o", dst],
                capture_output=True,
                text=True,
            )
        except OSError as e:
            # The binary was resolved and verified once, and is gone or
            # unrunnable now. This is the 2026-09-05 failure in its purest
            # form: an upload item died on FileNotFoundError instead of
            # uploading the file uncompressed.
            log.warning("zstd at %s did not run (%s) -- uploading uncompressed",
                        codec.binary, e)
            return False
        if result.returncode != 0:
            log.error("zstd compress failed for %s: %s", src, result.stderr)
            return False
        log.debug("Compressed %s -> %s", src, dst)
        return True

    opener = _stdlib_opener(codec)
    if opener is None:
        return False
    try:
        with open(src, "rb") as source, opener(dst) as target:
            shutil.copyfileobj(source, target, length=1024 * 1024)
    except OSError as e:
        log.error("%s compress failed for %s: %s", codec.name, src, e)
        return False
    log.debug("Compressed %s -> %s (%s)", src, dst, codec.name)
    return True


def _stdlib_opener(codec: Codec):
    """A ``open(dst) -> writable file`` for a standard-library codec."""
    if codec.name == "xz" and _lzma is not None:
        return lambda dst: _lzma.open(dst, "wb", preset=LZMA_PRESET)
    if codec.name == "gz":
        return lambda dst: gzip.open(dst, "wb", compresslevel=ZLIB_LEVEL)
    return None


def decompress_file(src: str, dst: str, strategy: CompressionStrategy) -> bool:
    """
    Decompress a single file according to strategy.
    Returns True on success.
    """
    if strategy == CompressionStrategy.SKIP:
        return True

    os.makedirs(os.path.dirname(dst), exist_ok=True)

    if strategy == CompressionStrategy.COMPRESS:
        # Which codec is a fact about the FILE, not about this machine: a
        # package staged as .xz on a Mac must unpack as .xz wherever it lands.
        codec = codec_for(src)
        if codec.name != "zstd":
            opener = _stdlib_opener(codec)
            if opener is None:
                log.error("cannot decompress %s: no %s codec here", src, codec.name)
                return False
            reader = _lzma.open if codec.name == "xz" else gzip.open
            try:
                with reader(src, "rb") as source, open(dst, "wb") as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
            except OSError as e:
                log.error("%s decompress failed for %s: %s", codec.name, src, e)
                return False
            return True

        binary = archiver("zstd")
        if binary is None:
            log.error("cannot decompress %s: it is zstd and this machine has none", src)
            return False
        try:
            result = subprocess.run(
                [binary, "-d", "-f", src, "-o", dst],
                capture_output=True,
                text=True,
            )
        except OSError as e:
            log.error("zstd at %s did not run (%s)", binary, e)
            return False
        if result.returncode != 0:
            log.error("zstd decompress failed for %s: %s", src, result.stderr)
            return False
        return True

    if strategy == CompressionStrategy.TAR_ZSTD:
        dst_dir = os.path.dirname(dst)
        os.makedirs(dst_dir, exist_ok=True)
        binary = archiver("tar")
        if binary is None:
            log.error("cannot unpack %s: no tar on this machine", src)
            return False
        try:
            result = subprocess.run(
                [binary, "--zstd", "-xf", src, "-C", dst_dir],
                capture_output=True,
                text=True,
            )
        except OSError as e:
            log.error("tar at %s did not run (%s)", binary, e)
            return False
        if result.returncode != 0:
            log.error("tar+zstd decompress failed for %s: %s", src, result.stderr)
            return False
        return True

    return False


def compress_directory(src_dir: str, staging_dir: str, enabled: bool = True) -> dict:
    """
    Walk src_dir, classify and compress files into staging_dir.
    Returns manifest: {rel_path: {strategy, original_size, compressed_size, compressed_path}}
    """
    if not enabled:
        return {}

    manifest = {}
    vdb_batches = {}  # parent_dir -> [file_paths]

    src_dir = os.path.abspath(src_dir)
    staging_dir = os.path.abspath(staging_dir)
    os.makedirs(staging_dir, exist_ok=True)

    # First pass: classify all files
    for root, _dirs, files in os.walk(src_dir):
        for fname in files:
            full_path = os.path.join(root, fname)
            rel_path = os.path.relpath(full_path, src_dir)
            strategy = classify_file(full_path)
            original_size = os.path.getsize(full_path)

            if strategy == CompressionStrategy.TAR_ZSTD:
                parent = os.path.dirname(rel_path) or "."
                vdb_batches.setdefault(parent, []).append((rel_path, full_path, original_size))
                continue

            staged_path = os.path.join(staging_dir, rel_path)

            if strategy == CompressionStrategy.SKIP:
                os.makedirs(os.path.dirname(staged_path), exist_ok=True)
                shutil.copy2(full_path, staged_path)
                manifest[rel_path] = {
                    "strategy": CompressionStrategy.SKIP.value,
                    "original_size": original_size,
                    "compressed_size": original_size,
                    "compressed_path": staged_path,
                }
            elif strategy == CompressionStrategy.ZSTD:
                zst_path = staged_path + ".zst"
                if compress_file(full_path, zst_path, CompressionStrategy.ZSTD):
                    compressed_size = os.path.getsize(zst_path)
                    manifest[rel_path] = {
                        "strategy": CompressionStrategy.ZSTD.value,
                        "original_size": original_size,
                        "compressed_size": compressed_size,
                        "compressed_path": zst_path,
                    }
                else:
                    # Fallback: copy as-is
                    os.makedirs(os.path.dirname(staged_path), exist_ok=True)
                    shutil.copy2(full_path, staged_path)
                    manifest[rel_path] = {
                        "strategy": CompressionStrategy.SKIP.value,
                        "original_size": original_size,
                        "compressed_size": original_size,
                        "compressed_path": staged_path,
                    }

    # Second pass: batch VDB files by directory
    for parent_dir, vdb_files in vdb_batches.items():
        archive_name = parent_dir.replace(os.sep, "_") if parent_dir != "." else "root"
        archive_name = f"vdb_{archive_name}.tar.zst"
        archive_path = os.path.join(staging_dir, archive_name)

        # Build list of files relative to src_dir
        file_args = [rel for rel, _, _ in vdb_files]
        total_original = sum(sz for _, _, sz in vdb_files)

        binary = archiver("tar")
        if binary is None:
            # Same outcome as a failed tar: the files below are copied
            # individually. No archiver is slower, never broken.
            result = SimpleNamespace(returncode=1, stderr="no tar on this machine")
        else:
            try:
                result = subprocess.run(
                    [binary, "--zstd", "-cf", archive_path, "-C", src_dir] + file_args,
                    capture_output=True,
                    text=True,
                )
            except OSError as e:
                result = SimpleNamespace(returncode=1, stderr=str(e))

        if result.returncode != 0:
            log.error("VDB batch tar failed for %s: %s", parent_dir, result.stderr)
            # Fallback: copy individually
            for rel_path, full_path, original_size in vdb_files:
                staged_path = os.path.join(staging_dir, rel_path)
                os.makedirs(os.path.dirname(staged_path), exist_ok=True)
                shutil.copy2(full_path, staged_path)
                manifest[rel_path] = {
                    "strategy": CompressionStrategy.SKIP.value,
                    "original_size": original_size,
                    "compressed_size": original_size,
                    "compressed_path": staged_path,
                }
            continue

        compressed_size = os.path.getsize(archive_path)

        # All VDB files in this batch share the same archive
        for rel_path, _full_path, original_size in vdb_files:
            manifest[rel_path] = {
                "strategy": CompressionStrategy.TAR_ZSTD.value,
                "original_size": original_size,
                "compressed_size": compressed_size,
                "compressed_path": archive_path,
                "archive": archive_name,
            }

        log.info(
            "VDB batch %s: %d files, %d -> %d bytes (%.1f%%)",
            parent_dir,
            len(vdb_files),
            total_original,
            compressed_size,
            (compressed_size / total_original * 100) if total_original else 0,
        )

    return manifest


def decompress_directory(staging_dir: str, dst_dir: str, manifest: dict) -> bool:
    """
    Decompress files from staging_dir to dst_dir according to manifest.
    Returns True if all succeeded.
    """
    os.makedirs(dst_dir, exist_ok=True)
    all_ok = True
    extracted_archives = set()

    for rel_path, entry in manifest.items():
        strategy = CompressionStrategy(entry["strategy"])
        dst_path = os.path.join(dst_dir, rel_path)

        if strategy == CompressionStrategy.SKIP:
            os.makedirs(os.path.dirname(dst_path), exist_ok=True)
            src_path = entry["compressed_path"]
            if os.path.exists(src_path):
                shutil.copy2(src_path, dst_path)
            else:
                log.error("Source file missing: %s", src_path)
                all_ok = False

        elif strategy == CompressionStrategy.ZSTD:
            src_path = entry["compressed_path"]
            if not decompress_file(src_path, dst_path, CompressionStrategy.ZSTD):
                all_ok = False

        elif strategy == CompressionStrategy.TAR_ZSTD:
            archive_path = entry["compressed_path"]
            if archive_path not in extracted_archives:
                if not decompress_file(archive_path, dst_path, CompressionStrategy.TAR_ZSTD):
                    all_ok = False
                else:
                    extracted_archives.add(archive_path)

    return all_ok
