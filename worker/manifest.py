"""SHA-256 manifest operations for file sync between artist workstation and RunPod pods."""

import hashlib
import json
import logging
import os

from .compression import classify_file, CompressionStrategy

logger = logging.getLogger(__name__)

TEXTURE_EXTS = {'.exr', '.rat', '.tex', '.png', '.jpg', '.jpeg', '.tif', '.tiff', '.hdr', '.tga', '.bmp'}
GEO_EXTS = {'.bgeo', '.abc', '.obj', '.fbx', '.usd', '.usda', '.usdc', '.usdz', '.vdb'}
GEO_COMPOUND_EXTS = {'.bgeo.sc'}
CACHE_EXTS = {'.sim', '.simdata', '.gz'}
SCENE_EXTS = {'.hip', '.hipnc', '.hiplc', '.py', '.hda'}


def _classify_type(path: str) -> str:
    lower = path.lower()
    for ext in GEO_COMPOUND_EXTS:
        if lower.endswith(ext):
            return "geo"
    _, ext = os.path.splitext(lower)
    if ext in TEXTURE_EXTS:
        return "texture"
    if ext in GEO_EXTS:
        return "geo"
    if ext in CACHE_EXTS:
        return "cache"
    if ext in SCENE_EXTS:
        return "scene"
    return "other"


def compute_sha256(path: str, block_size: int = 1048576) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            block = f.read(block_size)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def build_manifest(base_dir: str, compression_enabled: bool = True) -> dict:
    manifest = {}
    base_dir = os.path.abspath(base_dir)
    for root, dirs, files in os.walk(base_dir):
        dirs[:] = [d for d in dirs if not d.startswith('.') and d != '__pycache__']
        for fname in files:
            if fname.startswith('.'):
                continue
            full_path = os.path.join(root, fname)
            rel_path = os.path.relpath(full_path, base_dir)
            try:
                sha = compute_sha256(full_path)
                size = os.path.getsize(full_path)
                file_type = _classify_type(fname)
                strategy = classify_file(full_path, compression_enabled)
                manifest[rel_path] = {
                    "sha256": sha,
                    "size": size,
                    "type": file_type,
                    "strategy": strategy.name,
                }
            except Exception as e:
                logger.warning("Failed to process %s: %s", rel_path, e)
    return manifest


def verify_manifest(base_dir: str, manifest: dict) -> tuple[list[str], list[str]]:
    valid = []
    invalid = []
    base_dir = os.path.abspath(base_dir)
    for rel_path, info in manifest.items():
        full_path = os.path.join(base_dir, rel_path)
        if not os.path.isfile(full_path):
            logger.debug("Missing file: %s", rel_path)
            invalid.append(rel_path)
            continue
        try:
            sha = compute_sha256(full_path)
            if sha == info["sha256"]:
                valid.append(rel_path)
            else:
                logger.debug("SHA mismatch for %s", rel_path)
                invalid.append(rel_path)
        except Exception as e:
            logger.warning("Error verifying %s: %s", rel_path, e)
            invalid.append(rel_path)
    return valid, invalid


def diff_manifests(local_manifest: dict, remote_manifest: dict) -> dict:
    local_keys = set(local_manifest.keys())
    remote_keys = set(remote_manifest.keys())
    new = sorted(local_keys - remote_keys)
    deleted = sorted(remote_keys - local_keys)
    modified = []
    unchanged = []
    for key in sorted(local_keys & remote_keys):
        if local_manifest[key]["sha256"] != remote_manifest[key]["sha256"]:
            modified.append(key)
        else:
            unchanged.append(key)
    return {"new": new, "modified": modified, "deleted": deleted, "unchanged": unchanged}


def save_manifest(manifest: dict, path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, 'w') as f:
        json.dump(manifest, f, indent=2)
    logger.info("Manifest saved to %s (%d entries)", path, len(manifest))


def load_manifest(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, 'r') as f:
        return json.load(f)
