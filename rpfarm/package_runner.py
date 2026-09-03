"""CLI entry point for one out-of-process package upload.

Ruling R22: ``runpodfarm_upload`` must not block Houdini's UI while a
package uploads, and progress must be visible per package. PDG's
``pythonprocessor`` only dispatches a work item out of process when the
item carries a shell ``.command`` -- a callback-only item (neither
``inProcess`` nor a command) silently no-ops instead of cooking (verified
live; see the HDA's Help). So the node's ``onGenerate`` sets each item's
command to::

    python3 -m rpfarm.package_runner <path-to-item.json>

and PDG's local scheduler runs that as a genuine separate process, in
parallel across the scheduler's slots -- cost-free-verified locally
(scripts/build_runpodfarm_upload_hda.py's own dev history) at 4 items
sleeping 2s each finishing in ~1.3s total, not ~8s.

``<path-to-item.json>`` is a ``{"item": <build_upload_items entry>,
"compress": bool}`` file the node writes at generate time. This module
loads it, calls :func:`rpfarm.packages.run_upload_item`, and reports
progress/final attributes back onto the live PDG work item via ``pdgcmd``
-- the standard mechanism any out-of-process PDG command item uses to talk
back to the cook that spawned it (``PDG_SCRIPTDIR``/``PDG_RESULT_SERVER``/
``PDG_ITEM_ID`` are supplied automatically in the job environment, the
same way for the local scheduler as for any other PDG scheduler; nothing
node-specific is needed to enable it). The exit code -- 0 or non-zero --
is what the scheduler itself uses to mark the item cooked or failed;
``pdgcmd`` is only needed for the extra attributes (``bytes``, ``files``,
``seconds``, ``mbps``, ``progress``), not for basic success/failure.

Runs under a plain system ``python3``, not ``hython``: every module this
touches (``rpfarm.packages``, ``.sync``, ``.pods``, ``.config``,
``.worker_client``, ``.runpod_api``) is stdlib-only by design (see their
own module docstrings), so there is no reason to pay ``hython``'s startup
cost per package. ``sys.executable`` from the generating side would be
wrong here -- that process is ``hython`` -- so the node resolves a plain
``python3`` explicitly at generate time instead (see the HDA's
``onGenerate``/Help for exactly how and why).
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import time


def _bootstrap_rpfarm():
    """Put ``rpfarm`` on ``sys.path`` -- ``$RPFARM_ROOT`` if the node passed
    it through the command's environment, else ``~/.rpfarm/src`` (the
    checkout symlink ``rpfarm setup`` creates on the artist's machine)."""
    root = pathlib.Path(os.environ.get("RPFARM_ROOT", pathlib.Path.home() / ".rpfarm" / "src"))
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


def _pdgcmd():
    """Import ``pdgcmd`` from ``$PDG_SCRIPTDIR``, or return ``None`` if it
    isn't set (e.g. this module run by hand outside a PDG cook, as when
    debugging) -- attribute reporting is then skipped, but the upload
    itself still runs and the exit code still means what it means."""
    script_dir = os.environ.get("PDG_SCRIPTDIR")
    if not script_dir:
        return None
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    try:
        import pdgcmd  # type: ignore

        return pdgcmd
    except ImportError:
        return None


def main(argv):
    if len(argv) != 1:
        print("usage: python3 -m rpfarm.package_runner <item-json-path>", file=sys.stderr)
        return 2

    _bootstrap_rpfarm()

    from rpfarm import config as rpcfg
    from rpfarm import packages as rppkg
    from rpfarm import pods as rppods
    from rpfarm import sync as rpsync
    from rpfarm import worker_client as rpworker
    from rpfarm.runpod_api import RunPodAPI
    from rpfarm import runpod_api as rprunpod

    pdgcmd = _pdgcmd()

    try:
        with open(argv[0]) as f:
            payload = json.load(f)
        item = payload["item"]
        compress = bool(payload.get("compress"))

        def progress_cb(done, total, speed):
            msg = "{:.0f}/{:.0f} MB".format(done / 2**20, total / 2**20)
            print("[rpfarm-upload] progress {}".format(msg), flush=True)
            if pdgcmd is not None:
                try:
                    pdgcmd.setStringAttrib("progress", msg, 0)
                except Exception:
                    pass

        cfg = rpcfg.load()
        api = RunPodAPI(cfg.api_key)
        token = rpcfg.session_token()
        with open(cfg.ssh_key_path + ".pub") as f:
            pubkey = f.read()

        pod = rppods.ensure_sync_pod(api, cfg, token, pubkey)
        ip, port = rprunpod.pod_public_endpoint(pod, 22)
        sftp = rpsync.SftpTarget(host=ip, port=port, key_path=cfg.ssh_key_path)
        sync_client = rpworker.WorkerClient(pod["id"], token)

        t0 = time.time()
        stats = rppkg.run_upload_item(item, cfg, sftp, sync_client, compress, progress_cb)
        elapsed = time.time() - t0
        mbps = stats["bytes"] / 2**20 / max(1e-3, elapsed)

        print(
            "[rpfarm-upload] done bytes={} files={} seconds={:.2f} mbps={:.3f}".format(
                stats["bytes"], stats["files"], elapsed, mbps
            ),
            flush=True,
        )

        if pdgcmd is not None:
            try:
                pdgcmd.setIntAttrib("bytes", stats["bytes"], 0)
                pdgcmd.setIntAttrib("files", stats["files"], 0)
                pdgcmd.setFloatAttrib("seconds", elapsed, 0)
                pdgcmd.setFloatAttrib("mbps", mbps, 0)
            except Exception as e:
                print("[rpfarm-upload] pdgcmd attribute report failed (non-fatal): {}".format(e), file=sys.stderr)

        return 0
    except Exception:
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
