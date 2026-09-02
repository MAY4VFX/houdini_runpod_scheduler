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

Creates the pod and waits for it to become reachable, then prints the SSH
command and raw JSON. Does NOT terminate the pod - that is a separate manual
step (see task-1-report.md) so mqserver / port checks can run first. ALWAYS
terminate the printed pod id when done:
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


pubkey_path = os.environ.get("SPIKE_PUBKEY_PATH", os.path.expanduser("~/.ssh/id_ed25519.pub"))
pubkey = open(pubkey_path).read().strip()

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
t0 = time.time()
p = pod
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
