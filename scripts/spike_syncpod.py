#!/usr/bin/env python3
"""Spike: CPU pod + network volume + public ports in EU-RO-1. Throwaway.

Confirmed against https://rest.runpod.io/v1/openapi.json (PodCreateInput / Pod
schemas) before running:
  - request fields: computeType, cpuFlavorIds, vcpuCount, cloudType,
    dataCenterIds, networkVolumeId, volumeMountPath, imageName,
    containerDiskInGb, ports, supportPublicIp, env, dockerStartCmd.
  - response fields: pod.publicIp (string, nullable), pod.portMappings
    (object mapping internal port string -> external port int, nullable),
    pod.costPerHr.

Creates the pod, waits for it to become reachable, prints the SSH command and
raw JSON, then ALWAYS terminates the pod (DELETE + GET verification) in a
finally block - on success, on error, on Ctrl-C, and on the ready-wait
timeout. To keep the pod alive for manual inspection (e.g. to run mqserver /
port checks by hand), set SPIKE_KEEP_POD=1; the script then skips the DELETE
and prints the exact curl command to terminate it yourself:
  curl -X DELETE -H "Authorization: Bearer $RUNPOD_API_KEY" https://rest.runpod.io/v1/pods/<id>
"""
import json, os, sys, time, urllib.request, urllib.error

API = "https://rest.runpod.io/v1"
KEY = os.environ["RUNPOD_API_KEY"]


def call(method, path, body=None):
    req = urllib.request.Request(
        API + path,
        method=method,
        data=json.dumps(body).encode() if body else None,
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        print(f"HTTP {e.code} on {method} {path}: {detail}", file=sys.stderr)
        raise


def terminate(pid):
    """DELETE the pod and verify it's gone. Never raises - this runs in
    finally and must not mask (or be skipped by) whatever else went wrong."""
    print(f"terminating pod {pid} ...")
    try:
        req = urllib.request.Request(
            f"{API}/pods/{pid}", method="DELETE",
            headers={"Authorization": f"Bearer {KEY}"},
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            print("DELETE status:", r.status)
    except urllib.error.HTTPError as e:
        print(f"DELETE HTTP {e.code}: {e.read().decode(errors='replace')}", file=sys.stderr)
    except Exception as e:
        print(f"DELETE failed: {e!r}", file=sys.stderr)
        return
    try:
        check = call("GET", f"/pods/{pid}")
        print("post-delete GET /pods/<id>:", json.dumps(check))
    except urllib.error.HTTPError as e:
        print(f"post-delete GET returned HTTP {e.code} (expected 404 once gone)")
    except Exception as e:
        print(f"post-delete GET failed: {e!r}", file=sys.stderr)


pubkey_path = os.environ.get("SPIKE_PUBKEY_PATH")
if not pubkey_path:
    sys.exit("SPIKE_PUBKEY_PATH must be set to a throwaway SSH public key path "
             "(do not default to the user's personal key).")
pubkey = open(pubkey_path).read().strip()

# Pod creation itself is NOT wrapped in try/finally: if this call fails, no
# pod exists yet and there is nothing to terminate.
pod = call(
    "POST",
    "/pods",
    {
        "name": "rpfarm-spike-sync",
        "computeType": "CPU",
        "cpuFlavorIds": ["cpu3c", "cpu5c"],
        "vcpuCount": 2,
        "cloudType": "SECURE",
        "dataCenterIds": ["EU-RO-1"],
        "networkVolumeId": "2ze7qdwkt3",
        "volumeMountPath": "/workspace",
        "imageName": "runpod/base:1.0.2-ubuntu2204",
        "containerDiskInGb": 10,
        "ports": ["22/tcp", "4440/tcp", "4442/tcp", "8000/http"],
        "supportPublicIp": True,
        "env": {"PUBLIC_KEY": pubkey},
        "dockerStartCmd": [
            "bash",
            "-c",
            'mkdir -p ~/.ssh && echo "$PUBLIC_KEY" >> ~/.ssh/authorized_keys && '
            "apt-get update -qq && apt-get install -y -qq openssh-server >/dev/null && "
            "mkdir -p /run/sshd && /usr/sbin/sshd && sleep infinity",
        ],
    },
)
pid = pod["id"]
print("pod", pid)

# From here on, the pod exists and must always be cleaned up - success,
# exception, or Ctrl-C (KeyboardInterrupt still runs `finally`).
try:
    t0 = time.time()
    p = pod
    ip = None
    ports = {}
    while True:
        p = call("GET", f"/pods/{pid}")
        ports = p.get("portMappings") or {}
        ip = p.get("publicIp")
        elapsed = time.time() - t0
        print(f"  t={elapsed:.0f}s desiredStatus={p.get('desiredStatus')} ip={ip} ports={ports}")
        if ip and ports.get("22"):
            break
        if elapsed > 180:
            print("TIMEOUT waiting for pod to become ready", file=sys.stderr)
            break
        time.sleep(5)

    print(f"ready in {time.time()-t0:.0f}s  ip={ip}  ports={ports}")
    if ip and ports.get("22"):
        print("ssh:", f"ssh -i {pubkey_path[:-4]} -p {ports['22']} root@{ip}")
    print("costPerHr:", p.get("costPerHr"))
    print(json.dumps(p, indent=1))
finally:
    if os.environ.get("SPIKE_KEEP_POD") == "1":
        print(f"SPIKE_KEEP_POD=1 set - leaving pod {pid} running. Terminate it yourself:")
        print(f'  curl -X DELETE -H "Authorization: Bearer $RUNPOD_API_KEY" {API}/pods/{pid}')
    else:
        terminate(pid)
