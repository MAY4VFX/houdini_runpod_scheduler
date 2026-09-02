"""RunPodFarm pod housekeeping CLI -- stub.

Only the ``du`` command is implemented here. The full housekeeping module
(retention, ledger pruning, workspace GC, ...) is Task 12.

stdlib only -- deployed as-is to Ubuntu 22.04 / python3.10 pods alongside
``worker.py``, so no syntax newer than 3.10 and no third-party imports.

Usage::

    python3 housekeeping.py du <path>

Prints a JSON array of ``{"path": str, "bytes": int}`` for each first-level
child of ``<path>`` (files and directories alike; directory sizes are the
recursive total of their contents). Entries that raise on stat (e.g. broken
symlinks, permission errors) are skipped rather than aborting the whole
listing.
"""

from __future__ import annotations

import json
import os
import sys


def _size(path: str) -> int:
    """Total size in bytes of a file, or recursively of a directory."""
    if os.path.isdir(path) and not os.path.islink(path):
        total = 0
        for dirpath, _dirnames, filenames in os.walk(path, onerror=lambda e: None):
            for name in filenames:
                fp = os.path.join(dirpath, name)
                try:
                    total += os.lstat(fp).st_size
                except OSError:
                    continue
        return total
    try:
        return os.lstat(path).st_size
    except OSError:
        return 0


def du(path: str) -> list[dict]:
    """Sizes of the first-level children of ``path``."""
    entries = []
    try:
        names = sorted(os.listdir(path))
    except OSError:
        return entries
    for name in names:
        child = os.path.join(path, name)
        try:
            entries.append({"path": child, "bytes": _size(child)})
        except OSError:
            continue
    return entries


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] != "du":
        print("usage: housekeeping.py du <path>", file=sys.stderr)
        return 2
    print(json.dumps(du(argv[2])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
