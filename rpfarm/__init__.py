"""The rpfarm package.

Two independent things guard against a Houdini that is holding an old copy
of this package in ``sys.modules`` while running a NEW asset -- the state
that cost the owner a failed cook on 2026-09-05.

``FINGERPRINT`` is the one that decides. It is taken when THIS process
imported the package: the size and content digest of every module file as
it was at that moment. Comparing it against the files on disk answers the
real question -- "has the code changed since this process read it?" --
without anyone having to remember anything. That is the point: the version
check below failed exactly because it depended on a human bumping a number,
and seven commits in a row went by without one.

``VERSION`` stays, for two smaller jobs: it goes out as the ``User-Agent``
on every RunPod and pod-worker request, and it makes the message a human
can act on ("2.2.0 in memory, 2.3.0 on disk"). It is no longer what the
guard decides on.
"""

import hashlib
import os

VERSION = "2.3.0"


def fingerprint(package_dir=None):
    """``{filename: (size, digest)}`` for every module file, as it is NOW.

    Content, not mtime: a ``git checkout`` that restores an identical file
    moves its mtime, and telling the artist to restart Houdini over a file
    that did not actually change is a false alarm they will learn to
    ignore. Reading ~20 small files costs microseconds, so there is no
    reason to accept a cheaper, wronger signal.
    """
    root = package_dir or os.path.dirname(os.path.abspath(__file__))
    out = {}
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".py"):
            continue
        try:
            with open(os.path.join(root, name), "rb") as handle:
                data = handle.read()
        except OSError:
            continue
        out[name] = (len(data), hashlib.sha256(data).hexdigest()[:16])
    return out


def changed_since_import(current=None):
    """Module files that differ from what this process imported.

    Empty means the code in memory is the code on disk. Anything else is
    "restart Houdini", whatever the version numbers say.
    """
    now = current if current is not None else fingerprint()
    return sorted(name for name in set(now) | set(FINGERPRINT)
                  if now.get(name) != FINGERPRINT.get(name))


#: Taken at import. Never recompute it -- that would erase the evidence.
FINGERPRINT = fingerprint()
