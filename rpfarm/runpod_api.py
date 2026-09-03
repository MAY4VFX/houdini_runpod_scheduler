"""Stdlib-only REST (+ one GraphQL call) client for the RunPod API.

This module is the single place the codebase talks to RunPod. It must stay
stdlib-only: it also runs inside Houdini's bundled Python (3.13), where
third-party packages such as ``requests`` are not available. All network I/O
goes through an injectable ``transport`` callable so tests can run without
any real HTTP calls.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from . import VERSION

BASE = "https://rest.runpod.io/v1"
GRAPHQL_URL = "https://api.runpod.io/graphql"

# api.runpod.io sits behind Cloudflare, which rejects Python's default
# "Python-urllib/3.x" User-Agent with a 403 "error code: 1010" before the
# request ever reaches RunPod. Any explicit User-Agent gets through; this is the
# same header WorkerClient has to send to reach *.proxy.runpod.net. Verified
# against the live endpoint: no header -> 403, "rpfarm/2.0.0" -> 200.
USER_AGENT = "rpfarm/{}".format(VERSION)

_BALANCE_QUERY = "{ myself { clientBalance } }"


class RunPodError(Exception):
    """Raised for any non-2xx RunPod response (except the 404s we swallow)."""

    def __init__(self, status, body):
        super().__init__(f"RunPod {status}: {body[:300]}")
        self.status = status
        self.body = body


def _urllib_transport(method, url, body, headers):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, method=method, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class RunPodAPI:
    def __init__(self, api_key, base_url=BASE, transport=_urllib_transport):
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        self.base = base_url.rstrip("/")
        self._transport = transport

    # -- low-level -----------------------------------------------------

    def _call(self, method, path, body=None, ok404=False, query=None):
        url = self.base + path
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        status, raw = self._transport(method, url, body, self._headers)
        if status == 404 and ok404:
            return None
        if status >= 300:
            raise RunPodError(status, raw.decode(errors="replace"))
        return json.loads(raw) if raw else None

    # -- pods ------------------------------------------------------------

    def _pod_body(self, name, template_id, volume_id, env, ports):
        return {
            "name": name,
            "templateId": template_id,
            "networkVolumeId": volume_id,
            "volumeMountPath": "/workspace",
            "env": env,
            "ports": list(ports),
            "cloudType": "SECURE",
            "supportPublicIp": True,
            "dataCenterIds": ["EU-RO-1"],
        }

    def create_gpu_pod(self, name, template_id, gpu_type_ids, volume_id, env, ports):
        body = self._pod_body(name, template_id, volume_id, env, ports)
        body.update(
            {
                "computeType": "GPU",
                "gpuTypeIds": list(gpu_type_ids),
                "gpuTypePriority": "availability",
                "gpuCount": 1,
            }
        )
        return self._call("POST", "/pods", body)

    def create_cpu_pod(self, name, template_id, volume_id, env, ports, vcpu=2, flavors=("cpu3c", "cpu5c")):
        body = self._pod_body(name, template_id, volume_id, env, ports)
        body.update(
            {
                "computeType": "CPU",
                "cpuFlavorIds": list(flavors),
                "cpuFlavorPriority": "availability",
                "vcpuCount": vcpu,
            }
        )
        return self._call("POST", "/pods", body)

    def get_pod(self, pod_id):
        return self._call("GET", f"/pods/{pod_id}")

    def terminate_pod(self, pod_id):
        self._call("DELETE", f"/pods/{pod_id}", ok404=True)

    def list_pods(self, name_prefix=""):
        pods = self._call("GET", "/pods") or []
        return [p for p in pods if p.get("name", "").startswith(name_prefix)]

    # -- network volumes --------------------------------------------------

    def get_volume(self, vid):
        return self._call("GET", f"/networkvolumes/{vid}")

    def create_volume(self, name, size_gb, dc="EU-RO-1"):
        return self._call(
            "POST",
            "/networkvolumes",
            {"name": name, "size": size_gb, "dataCenterId": dc},
        )

    def resize_volume(self, vid, size_gb):
        return self._call("PATCH", f"/networkvolumes/{vid}", {"size": size_gb})

    def delete_volume(self, vid):
        self._call("DELETE", f"/networkvolumes/{vid}", ok404=True)

    # -- templates ---------------------------------------------------------

    def save_template(
        self,
        name,
        image,
        ports,
        env,
        container_disk_gb=10,
        registry_auth_id=None,
        template_id=None,
    ):
        body = {
            "name": name,
            "imageName": image,
            "ports": list(ports),
            "env": env,
            "containerDiskInGb": container_disk_gb,
            "volumeInGb": 0,
            "isPublic": False,
        }
        if registry_auth_id:
            body["containerRegistryAuthId"] = registry_auth_id
        if template_id:
            return self._call("PATCH", f"/templates/{template_id}", body)
        return self._call("POST", "/templates", body)

    # -- billing -------------------------------------------------------------

    def billing_pods(self, since_iso, until_iso):
        return self._call(
            "GET",
            "/billing/pods",
            query={"startTime": since_iso, "endTime": until_iso},
        ) or []

    def billing_volumes(self, since_iso, until_iso):
        return self._call(
            "GET",
            "/billing/networkvolumes",
            query={"startTime": since_iso, "endTime": until_iso},
        ) or []

    # -- account -------------------------------------------------------------

    def balance(self) -> float:
        status, raw = self._transport(
            "POST", GRAPHQL_URL, {"query": _BALANCE_QUERY}, self._headers
        )
        if status >= 300:
            raise RunPodError(status, raw.decode(errors="replace"))
        data = json.loads(raw)
        return float(data["data"]["myself"]["clientBalance"])


def pod_public_endpoint(pod, private_port):
    """Return (public_ip, public_port) for a private tcp port on a pod.

    Only tcp ports show up in ``portMappings``; http ports are reached via
    the ``https://<podId>-<port>.proxy.runpod.net/`` proxy instead and are
    not covered by this helper.
    """
    ip = pod.get("publicIp")
    mapping = pod.get("portMappings") or {}
    public = mapping.get(str(private_port))
    if not ip or not public:
        raise RunPodError(0, f"pod {pod.get('id')} has no public endpoint for {private_port}")
    return ip, int(public)
