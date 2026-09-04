"""Measure what Karma XPU's OptiX cache is worth, and whether we can move it.

READY TO FIRE, NOT FIRED. Raises exactly one GPU pod, runs a fixed script, and
terminates it in a `finally` -- run it when pods are allowed again.

    python3 scripts/measure_xpu_cache.py [--scene PATH] [--frames 6] [--gpu "NVIDIA GeForce RTX 4090"]

Three questions, one pod, because they need the same machine:

1. What does the cold frame actually cost? Already measured once on a 4090
   (frame 1 89.7s, warm 43.3s), but only once and only on that card.
2. Does OPTIX_CACHE_PATH move the cache? v1 set it and we inherited the belief
   without checking. It is an NVIDIA variable, not documented by SideFX, so it
   is checked here by pointing it somewhere empty and looking.
3. Does XPU run on Ampere at all? Asked with --gpu; the 3090 has had no stock
   in EU-RO-1 all day, so this may answer "could not rent one", which is itself
   the answer for that datacenter.

Two rules this script exists to obey, both learned the hard way:

* **Results stream out per frame.** The previous run buffered everything until
  the remote script finished, and when the session was cut two measurements
  that had already completed were lost with it. Every timing is printed the
  moment it lands.
* **Same pod, back to back** (R35). The same frame measured 35.7s on one pod
  and ~78s on another, both RTX 4090 -- cross-pod numbers are worthless, so
  every comparison here happens on one machine in one run.
"""

from __future__ import annotations

import argparse
import glob
import os
import subprocess
import sys
import time


def _bootstrap():
    root = os.environ.get("RPFARM_ROOT") or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)
    return root


REPO = _bootstrap()

from rpfarm import config as rpcfg  # noqa: E402
from rpfarm import pods as rppods  # noqa: E402
from rpfarm.runpod_api import RunPodAPI, pod_public_endpoint  # noqa: E402
from rpfarm.worker_client import WorkerClient  # noqa: E402

DEFAULT_SCENE = os.path.expanduser("~/Desktop/rpfarm_demo/rpfarm_demo.hip")
OTLS = os.path.expanduser("~/Library/Preferences/houdini/22.0/otls/runpodfarm_*.hda")


REMOTE = r'''
set -uo pipefail
HFS=$(ls -d /workspace/houdini/*/ 2>/dev/null | sort -V | tail -1); HFS=${HFS%/}
export HFS PATH="$HFS/bin:$PATH" HOUDINI_OTLSCAN_PATH="/tmp/otls;&"
echo "MEAS gpu $(nvidia-smi --query-gpu=name,driver_version --format=csv,noheader | head -1)"
echo "MEAS vcpu $(nproc)"
echo "MEAS xpu $(husk --list-renderers 2>&1 | grep KarmaXPU | head -1)"

# Every frame is its own hython, exactly as the farm dispatches them, so the
# only thing shared between them is the container's disk -- which is where the
# OptiX cache lives and the whole question.
render() {   # $1 engine  $2 frame  $3 tag
  hython -c "
import hou, time
hou.hipFile.load('/tmp/demo.hip', suppress_save_prompt=True)
k = hou.node('/out/karma_demo')
k.parm('engine').set('$1')
k.parm('picture').set('/tmp/m_$3_$1.\$F4.exr')
k.parm('trange').set(1)
for p, v in (('f1', $2), ('f2', $2), ('f3', 1)):
    pp = k.parm(p); pp.deleteAllKeyframes(); pp.set(v)
t = time.time(); k.render(verbose=False)
print('MEAS time $3 $1 frame $2 %.1f' % (time.time() - t))
" 2>&1 | grep -E "^MEAS|Error" | tail -2
}

echo "MEAS phase cold-cache-cleared"
rm -rf /var/tmp/OptixCache_* /tmp/oc_probe
for F in $(seq 1 __FRAMES__); do render xpu $F cold; done
echo "MEAS cache $(du -sh /var/tmp/OptixCache_* 2>/dev/null | head -1 || echo none)"
echo "MEAS cachefiles $(ls /var/tmp/OptixCache_*/ 2>/dev/null | tr '\n' ' ')"

echo "MEAS phase cpu-same-pod"
for F in $(seq 1 __FRAMES__); do render cpu $F cpu; done

# Does OPTIX_CACHE_PATH actually move it? v1 set it; SideFX does not document
# it. Point it at an empty directory with the default cache wiped, render, and
# look at which one filled up.
echo "MEAS phase optix-cache-path"
rm -rf /var/tmp/OptixCache_* /tmp/oc_probe; mkdir -p /tmp/oc_probe
OPTIX_CACHE_PATH=/tmp/oc_probe render xpu 1 probe
echo "MEAS probedir $(du -sh /tmp/oc_probe 2>/dev/null | head -1 || echo empty)"
echo "MEAS defaultdir $(du -sh /var/tmp/OptixCache_* 2>/dev/null | head -1 || echo empty)"
echo "MEAS done"
'''


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scene", default=DEFAULT_SCENE)
    ap.add_argument("--frames", type=int, default=6)
    ap.add_argument("--gpu", default=None,
                    help='force one card, e.g. "NVIDIA GeForce RTX 3090" to answer '
                         'the Ampere question; default uses the configured set')
    ap.add_argument("--timeout", type=int, default=2400)
    args = ap.parse_args(argv)

    scene = os.path.abspath(os.path.expanduser(args.scene))
    if not os.path.isfile(scene):
        print("error: no scene at {}".format(scene), file=sys.stderr)
        return 2

    cfg = rpcfg.load()
    api = RunPodAPI(cfg.api_key)
    token = rpcfg.session_token()
    with open(str(cfg.ssh_key_path) + ".pub") as f:
        pubkey = f.read().strip()
    wanted = [args.gpu] if args.gpu else list(cfg.gpu_priority)

    env = rppods.pod_env(cfg, "gpu", token, 1, pubkey,
                         cook="xpucache", project="xpucache")
    pod = None
    started = time.time()
    try:
        print("[meas] renting one pod ({})".format(", ".join(wanted)), flush=True)
        pod = api.create_gpu_pod("rpfarm-{}-xpucache-0".format(cfg.user),
                                 cfg.template_id, wanted, cfg.volume_id, env,
                                 rppods.PORTS, datacenter=cfg.datacenter,
                                 cloud_type=cfg.cloud_type)
        print("[meas] pod {}".format(pod["id"]), flush=True)
        ready = rppods.wait_ready(api, WorkerClient(pod["id"], token), pod["id"],
                                  timeout=600)
        ip, port = pod_public_endpoint(ready, 22)
        ssh = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
               "-o", "ConnectTimeout=20", "-i", str(cfg.ssh_key_path)]
        subprocess.run(["ssh", *ssh, "-p", str(port), "root@" + ip, "mkdir -p /tmp/otls"],
                       check=True, capture_output=True, timeout=120)
        subprocess.run(["scp", *ssh, "-P", str(port), scene, "root@{}:/tmp/demo.hip".format(ip)],
                       check=True, capture_output=True, timeout=600)
        otls = glob.glob(OTLS)
        if otls:
            subprocess.run(["scp", *ssh, "-P", str(port), *otls,
                            "root@{}:/tmp/otls/".format(ip)],
                           check=True, capture_output=True, timeout=600)

        script = REMOTE.replace("__FRAMES__", str(args.frames))
        # Streamed, not collected. The previous run buffered until the remote
        # script ended and lost two completed measurements when the session was
        # cut; anything that has been measured is printed immediately.
        proc = subprocess.Popen(
            ["ssh", *ssh, "-p", str(port), "root@" + ip, "bash -l -s"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1)
        proc.stdin.write(script)
        proc.stdin.close()
        deadline = started + args.timeout
        for line in proc.stdout:
            if line.startswith("MEAS ") or "Error" in line:
                print("[meas] " + line.rstrip(), flush=True)
            if time.time() > deadline:
                print("[meas] timeout -- stopping", flush=True)
                proc.kill()
                break
        proc.wait()
    finally:
        if pod:
            try:
                api.terminate_pod(pod["id"])
                print("[meas] terminated {}".format(pod["id"]), flush=True)
            except Exception as e:  # noqa: BLE001
                print("[meas] FAILED TO TERMINATE {}: {} -- IT MAY STILL BE "
                      "BILLING".format(pod["id"], e), file=sys.stderr, flush=True)
        left = [(p["id"], p.get("name")) for p in api.list_pods("rpfarm-")
                if "xpucache" in (p.get("name") or "")]
        print("[meas] measurement pods left: {}".format(left), flush=True)
        print("[meas] wall clock {:.0f}s".format(time.time() - started), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
