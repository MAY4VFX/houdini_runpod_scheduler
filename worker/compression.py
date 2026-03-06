"""
File classification and compression for VFX data transfer.

Classifies VFX files by their compressibility and applies appropriate
compression strategies (zstd for compressible files, skip for already-compressed).
"""

import logging
import os
import shutil
import struct
import subprocess
from enum import Enum
from pathlib import Path

log = logging.getLogger(__name__)

try:
    import zstandard as _zstd
except ImportError:
    _zstd = None


class CompressionStrategy(Enum):
    SKIP = "skip"
    ZSTD = "zstd"
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

        if _zstd is not None:
            cctx = _zstd.ZstdCompressor(level=1)
            compressed = cctx.compress(sample)
            ratio = len(compressed) / len(sample)
        else:
            # Fallback to subprocess
            result = subprocess.run(
                ["zstd", "-1", "-c"],
                input=sample,
                capture_output=True,
                timeout=5,
            )
            if result.returncode != 0:
                return CompressionStrategy.SKIP
            ratio = len(result.stdout) / len(sample)

        if ratio < 0.95:
            log.debug("File %s compressible (ratio=%.2f)", path, ratio)
            return CompressionStrategy.ZSTD
        else:
            log.debug("File %s not compressible (ratio=%.2f)", path, ratio)
            return CompressionStrategy.SKIP

    except Exception as e:
        log.warning("Probe failed for %s: %s", path, e)
        return CompressionStrategy.SKIP


def compress_file(src: str, dst: str, strategy: CompressionStrategy, level: int = 3) -> bool:
    """
    Compress a single file according to strategy.
    Returns True on success, False if skipped.
    """
    if strategy == CompressionStrategy.SKIP:
        return False

    if strategy == CompressionStrategy.TAR_ZSTD:
        raise ValueError("TAR_ZSTD should not be used on single files; use compress_directory for batching")

    if strategy == CompressionStrategy.ZSTD:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        result = subprocess.run(
            ["zstd", f"-T0", f"--level={level}", "-f", src, "-o", dst],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            log.error("zstd compress failed for %s: %s", src, result.stderr)
            return False
        log.debug("Compressed %s -> %s", src, dst)
        return True

    return False


def decompress_file(src: str, dst: str, strategy: CompressionStrategy) -> bool:
    """
    Decompress a single file according to strategy.
    Returns True on success.
    """
    if strategy == CompressionStrategy.SKIP:
        return True

    os.makedirs(os.path.dirname(dst), exist_ok=True)

    if strategy == CompressionStrategy.ZSTD:
        result = subprocess.run(
            ["zstd", "-d", "-f", src, "-o", dst],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            log.error("zstd decompress failed for %s: %s", src, result.stderr)
            return False
        return True

    if strategy == CompressionStrategy.TAR_ZSTD:
        dst_dir = os.path.dirname(dst)
        os.makedirs(dst_dir, exist_ok=True)
        result = subprocess.run(
            ["tar", "--zstd", "-xf", src, "-C", dst_dir],
            capture_output=True,
            text=True,
        )
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

        result = subprocess.run(
            ["tar", "--zstd", "-cf", archive_path, "-C", src_dir] + file_args,
            capture_output=True,
            text=True,
        )

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
