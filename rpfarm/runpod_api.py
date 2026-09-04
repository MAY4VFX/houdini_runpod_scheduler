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

from . import VERSION, tls

BASE = "https://rest.runpod.io/v1"
GRAPHQL_URL = "https://api.runpod.io/graphql"

# api.runpod.io sits behind Cloudflare, which rejects Python's default
# "Python-urllib/3.x" User-Agent with a 403 "error code: 1010" before the
# request ever reaches RunPod. Any explicit User-Agent gets through; this is the
# same header WorkerClient has to send to reach *.proxy.runpod.net. Verified
# against the live endpoint: no header -> 403, "rpfarm/2.0.0" -> 200.
USER_AGENT = "rpfarm/{}".format(VERSION)

# Fallback region for a caller that has no configured one (and the default
# for a brand-new volume). Every real caller passes ``cfg.datacenter``: a
# network volume exists in exactly one region, and a pod can only mount one
# that is in its own.
DEFAULT_DATACENTER = "EU-RO-1"

# RunPod's OpenAPI enum for `cloudType`. SECURE is vetted datacentre capacity;
# COMMUNITY is cheaper hosts and often has machines when SECURE is empty --
# which makes it the practical answer to a capacity shortage.
#
# Note what is deliberately absent: `interruptible`. Leaving it unset (RunPod
# defaults it false) is what guarantees a pod we already hold is never taken
# back, and that guarantee is the whole reason a shortage can be treated as
# waiting rather than as failure. Do not add it.
CLOUD_TYPE_SECURE = "SECURE"
CLOUD_TYPE_COMMUNITY = "COMMUNITY"
CLOUD_TYPES = (CLOUD_TYPE_SECURE, CLOUD_TYPE_COMMUNITY)


def is_capacity_error(exc) -> bool:
    """Is this RunPod refusal a temporary shortage of machines?

    Those are worth waiting out; a 4xx (bad key, bad template, malformed
    request) is not, and no amount of waiting fixes it. RunPod reports a
    shortage as a 500 whose body says "no longer any instances available with
    the requested specifications", so the status alone is not specific enough
    to key on -- but any 5xx is at least *plausibly* transient, and treating
    one as waiting costs a retry while treating a real shortage as fatal costs
    the artist the whole cook. Anything below 500 is taken at its word.
    """
    status = getattr(exc, "status", None)
    if status is None:
        return False
    if 400 <= int(status) < 500:
        return False
    if int(status) >= 500:
        return True
    return False

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
        with urllib.request.urlopen(req, timeout=60, context=tls.ssl_context()) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except OSError as e:
        # A transport-level failure -- DNS, TLS, connection reset, timeout.
        # Every caller in the scheduler knows how to handle RunPodError and
        # nothing handles a bare URLError, so a passing network blip would
        # otherwise take a whole cook down. status 0 means "never reached
        # RunPod", as it does in pod_public_endpoint.
        raise RunPodError(0, "request to {} failed: {}".format(url, e)) from e


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

    def _pod_body(self, name, template_id, volume_id, env, ports,
                  datacenter=DEFAULT_DATACENTER, cloud_type=CLOUD_TYPE_SECURE):
        """The fields every pod create shares.

        ``datacenter`` used to be the hardcoded :data:`DEFAULT_DATACENTER`
        while ``cfg.datacenter``, the scheduler's ``rpfarm_datacenter``
        parm, ``doctor``'s stock check and ``storage recreate`` all honoured
        the configured value (final-review finding 5). A network volume
        exists in exactly one region and a pod can only mount one that is in
        its own, so an artist whose account already held a volume elsewhere
        got pods created in EU-RO-1 against a volume that was not there, and
        every cook failed.
        """
        return {
            "name": name,
            "templateId": template_id,
            "networkVolumeId": volume_id,
            "volumeMountPath": "/workspace",
            "env": env,
            "ports": list(ports),
            "cloudType": cloud_type or CLOUD_TYPE_SECURE,
            "supportPublicIp": True,
            "dataCenterIds": [datacenter or DEFAULT_DATACENTER],
        }

    def create_gpu_pod(self, name, template_id, gpu_type_ids, volume_id, env, ports,
                       datacenter=DEFAULT_DATACENTER, cloud_type=CLOUD_TYPE_SECURE):
        """Create a GPU pod, trying ``gpu_type_ids`` in the order given.

        ``gpuTypePriority`` defaults to ``"availability"``, which picks
        whatever RunPod has most of and ignores the caller's order -- a live
        cook asked for an A4500 (~$0.25/h) first and got an RTX 4090 at
        $0.740/h. The openapi spec: "set to availability to respond to
        current GPU type availability. Set to custom to always try to rent
        GPU types in the order specified in gpuTypeIds." The artist's
        priority list is a cost decision, so it is ``"custom"`` here.
        """
        body = self._pod_body(name, template_id, volume_id, env, ports, datacenter,
                              cloud_type=cloud_type)
        body.update(
            {
                "computeType": "GPU",
                "gpuTypeIds": list(gpu_type_ids),
                "gpuTypePriority": "custom",
                "gpuCount": 1,
            }
        )
        return self._call("POST", "/pods", body)

    def create_cpu_pod(self, name, template_id, volume_id, env, ports, vcpu=2, flavors=("cpu3c", "cpu5c"),
                       datacenter=DEFAULT_DATACENTER, cloud_type=CLOUD_TYPE_SECURE):
        """Create a CPU pod, trying ``flavors`` in the order given.

        Same reasoning as :meth:`create_gpu_pod`. The openapi spec: "set to
        availability to respond to current CPU flavor availability. Set to
        custom to always try to rent CPU flavors in the order specified in
        cpuFlavorIds." The default flavours are cheapest-first ($0.06/h for
        cpu3c against $0.07/h for cpu5c), so the order is the point.
        """
        body = self._pod_body(name, template_id, volume_id, env, ports, datacenter,
                              cloud_type=cloud_type)
        body.update(
            {
                "computeType": "CPU",
                "cpuFlavorIds": list(flavors),
                "cpuFlavorPriority": "custom",
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

    def list_volumes(self):
        return self._call("GET", "/networkvolumes") or []

    def get_volume(self, vid):
        return self._call("GET", f"/networkvolumes/{vid}")

    def create_volume(self, name, size_gb, dc=DEFAULT_DATACENTER):
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

    def list_templates(self):
        return self._call("GET", "/templates") or []

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

    def gpu_types(self, dc=DEFAULT_DATACENTER, secure_cloud=True):
        """Every GPU type RunPod knows about, with ``lowestPrice`` for *dc*.

        REST has no per-datacenter stock signal (``GET /gputypes`` lists
        types with no ``lowestPrice``/``stockStatus`` field at all), so
        this is GraphQL, like :meth:`balance`. ``lowestPrice.stockStatus``
        is ``None``/``"Low"``/``"Medium"``/``"High"`` -- ``None`` means out
        of stock in *dc* right now, not a query failure (confirmed live
        2026-09-03: querying without an ``id`` filter returns every type
        RunPod offers, most with ``stockStatus: null`` for a niche
        datacenter). Filtering down to a caller's ``gpu_priority`` list is
        left to the caller -- this always returns the whole catalog so it
        stays useful outside ``doctor`` too (e.g. picking a first
        priority list at ``setup`` time).

        ``secure_cloud`` is not optional in practice: without it RunPod
        answers with the lowest price across ALL clouds, which is not what
        anybody is billed. It made the catalogue look wrong -- an RTX 4090
        priced at 0.34 while every cook was billed 0.740. Asked with
        ``secureCloud: true`` the same card prices at 0.74 and matches the
        ledger to the cent, as does the RTX PRO 4000 at 0.57. Pass the cloud
        the pods will actually be created in.
        """
        query = (
            "query { gpuTypes { id displayName "
            'lowestPrice(input: {gpuCount: 1, dataCenterId: "%s", secureCloud: %s}) '
            "{ stockStatus minimumBidPrice uninterruptablePrice } } }"
            % (dc, "true" if secure_cloud else "false")
        )
        status, raw = self._transport("POST", GRAPHQL_URL, {"query": query}, self._headers)
        if status >= 300:
            raise RunPodError(status, raw.decode(errors="replace"))
        data = json.loads(raw)
        return data["data"]["gpuTypes"]


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
