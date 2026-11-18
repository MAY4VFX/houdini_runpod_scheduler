"""Stdlib-only REST v2 (+ two GraphQL calls) client for the RunPod API.

This module is the single place the codebase talks to RunPod. It must stay
stdlib-only: it also runs inside Houdini's bundled Python (3.13), where
third-party packages such as ``requests`` are not available. All network I/O
goes through an injectable ``transport`` callable so tests can run without
any real HTTP calls.

REST v1 (``rest.runpod.io/v1``) was retired on 2026-11-15 and its host no longer
resolves; this module speaks v2 (``api.runpod.io/v2``). What v2 changed:

* create bodies are nested and reject unknown fields (``image``/``templateId``,
  ``disk``, ``cloud``, ``mounts.network``, exactly one of ``gpu{id,count}`` /
  ``cpu{id,vcpuCount}``);
* a pod takes ONE GPU type / CPU flavour -- the priority list is our loop;
* lists are wrapped (``{"pods": [...], "pagination": {...}}``) and paginated;
* a pod's ``status`` is its actual state, ``runtime`` (live ports) exists only
  on ``GET /pods/{id}``, and ``publicIp`` / ``portMappings`` are gone.

The rest of the codebase (15 modules, the HDAs, ~1200 tests) was written
against the v1 shape. Rather than touch all of it, every record leaves this
module through :func:`normalize_pod` / :func:`normalize_volume` /
:func:`normalize_template` / the billing normalisers, which return the v2
fields AND the v1 keys callers read. The mapping lives in exactly one place.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from . import VERSION, tls

BASE = "https://api.runpod.io/v2"
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

# RunPod's OpenAPI enum for `cloud`. SECURE is vetted datacentre capacity;
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


# -- v1-shaped views of v2 records ------------------------------------------

# v1 reported the *requested* state in ``desiredStatus`` and the code was written
# around that: a pod is "RUNNING" from the moment it is created (the v1 create
# response said so) and stays so until somebody stops it. v2's ``status`` is the
# ACTUAL state and passes through PROVISIONING / STARTING first, which would make
# every freshly created pod look "not running" to the callers that filter on it
# (orphan sweeps, the sync-pod lookup, the cost report) -- and make them miss a
# pod that is already billing. So the in-flight states map to RUNNING.
#
# ERROR maps to EXITED, not RUNNING. An ERROR pod is not serving anything, so it
# must not count as a live cook or as a usable sync pod. Mapped to EXITED the
# sync-pod lookup offers it to ``start_pod`` and, if it will not come back,
# terminates and rebuilds it -- exactly the recovery we want. The cost of the
# choice: ``find_orphans`` will not list an ERROR pod, so a failed GPU pod needs
# the account page (or ``rpfarm farm status``, which shows it as EXITED) to spot.
_DESIRED_BY_STATUS = {
    "PROVISIONING": "RUNNING",
    "STARTING": "RUNNING",
    "RUNNING": "RUNNING",
    "EXITED": "EXITED",
    "ERROR": "EXITED",
    "TERMINATED": "TERMINATED",
}
_LIVE_STATUSES = ("PROVISIONING", "STARTING", "RUNNING")


def normalize_pod(pod):
    """A v2 pod record plus the v1 keys the rest of the code reads.

    Existing keys are never overwritten. ``publicIp`` / ``portMappings`` need
    ``runtime``, which only ``GET /pods/{id}`` returns: on a list entry they are
    simply absent (see :meth:`RunPodAPI.with_runtime`).

    ``lastStatusChange`` is set only for a live pod (from ``startedAt``). v2 has
    no stop time, and the scheduler's "delete a sync pod that has been stopped
    for N minutes" policy measures from ``lastStatusChange``: feeding it the
    last START time would make a pod parked after a long run look stopped for
    hours and get deleted at once, taking its container disk with it. Absent
    means unknown, and unknown never triggers an action.
    """
    if not isinstance(pod, dict):
        return pod
    out = dict(pod)
    status = out.get("status")
    if status and "desiredStatus" not in out:
        out["desiredStatus"] = _DESIRED_BY_STATUS.get(status, "EXITED")
    if "cost" in out:
        out.setdefault("costPerHr", out["cost"])
    if "image" in out:
        out.setdefault("imageName", out["image"])
    network = ((out.get("mounts") or {}).get("network")) or []
    if network and network[0].get("volumeId"):
        out.setdefault("networkVolumeId", network[0]["volumeId"])
    if status in _LIVE_STATUSES:
        stamp = out.get("startedAt") or out.get("createdAt")
        if stamp:
            out.setdefault("lastStatusChange", stamp)
    gpu = out.get("gpu") or {}
    if gpu.get("id") and "machine" not in out:
        out["machine"] = {"gpuTypeId": gpu["id"]}
    # Only tcp ports have a public address (http ports are reached through the
    # proxy and carry a private 100.x address): same as v1's portMappings.
    #
    # Only while the pod is RUNNING. Observed live (2026-11-18): right after a
    # stop the pod reported EXITED with its old ports still in ``runtime``, and
    # after a start the old ports came back before the new ones -- an address
    # that connects to nothing (ConnectionRefused). v1 gave no address for a pod
    # that was not up; keeping that makes "has an endpoint" mean "is running".
    tcp = []
    if status == "RUNNING":
        tcp = [p for p in ((out.get("runtime") or {}).get("ports") or []) if p.get("type") == "tcp"]
    if tcp:
        out.setdefault("publicIp", tcp[0].get("ip"))
        out.setdefault("portMappings", {str(p["private"]): p["public"] for p in tcp})
    return out


def normalize_volume(volume):
    if not isinstance(volume, dict):
        return volume
    out = dict(volume)
    if "dataCenter" in out:
        out.setdefault("dataCenterId", out["dataCenter"])
    return out


def normalize_template(template):
    if not isinstance(template, dict):
        return template
    out = dict(template)
    if "image" in out:
        out.setdefault("imageName", out["image"])
    return out


def _normalize_billing(records, extra=None):
    out = []
    for rec in records or []:
        rec = dict(rec)
        rec.setdefault("amount", rec.get("totalAmount"))
        if extra:
            extra(rec)
        out.append(rec)
    return out


def _pod_billing_extra(rec):
    # v1 rows carried ``timeBilledMs``; v2 has no billed-time field at all, and a
    # bucket's span is not billed time. 0 = "unknown": ``ledger.merge_billing``
    # then splits the pod's amount across its tasks by duration and reports no
    # idle remainder, so totals still reconcile with the bill.
    rec.setdefault("timeBilledMs", 0)


def _try_next_type(exc) -> bool:
    """Creating a pod with one GPU/CPU type failed: is the next type worth a try?

    Bad credentials, a rejected body (422) and rate limits fail identically for
    every type. Anything else -- including the 400 an unknown type gets and
    whatever way a missing stock is reported -- costs at most one more request.
    """
    return getattr(exc, "status", None) not in (401, 403, 422, 429)


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

    def _list(self, path, key, query=None):
        """Every item of a v2 list endpoint: follow ``pagination.nextCursor`` to the end."""
        items, cursor = [], None
        while True:
            q = dict(query or {})
            if cursor:
                q["cursor"] = cursor
            page = self._call("GET", path, query=q or None) or {}
            items.extend(page.get(key) or [])
            pg = page.get("pagination") or {}
            cursor = pg.get("nextCursor")
            if not (pg.get("hasNextPage") and cursor):
                return items

    # -- pods ------------------------------------------------------------

    def _pod_body(self, name, template_id, volume_id, env, ports,
                  datacenter=DEFAULT_DATACENTER, cloud_type=CLOUD_TYPE_SECURE,
                  container_disk_gb=None):
        """The fields every pod create shares.

        ``datacenter`` used to be the hardcoded :data:`DEFAULT_DATACENTER`
        while ``cfg.datacenter``, the scheduler's ``rpfarm_datacenter``
        parm, ``doctor``'s stock check and ``storage recreate`` all honoured
        the configured value (final-review finding 5). A network volume
        exists in exactly one region and a pod can only mount one that is in
        its own, so an artist whose account already held a volume elsewhere
        got pods created in EU-RO-1 against a volume that was not there, and
        every cook failed.

        There is no ``supportPublicIp`` in v2. Checked live 2026-11-18: a pod
        whose ``ports`` include ``22/tcp`` gets a public address anyway --
        ``runtime.ports`` lists ``{ip, private, public, type: "tcp"}`` for each
        tcp port, and ``ssh.direct`` repeats the one for 22.
        """
        body = {
            "name": name,
            "templateId": template_id,
            "env": env,
            "ports": list(ports),
            "cloud": cloud_type or CLOUD_TYPE_SECURE,
            "dataCenterIds": [datacenter or DEFAULT_DATACENTER],
        }
        if volume_id:
            body["mounts"] = {"network": [{"volumeId": volume_id, "path": "/workspace"}]}
        # Overrides the template's disk for this one pod: render and host
        # pods unpack a local copy of Houdini there (pod/entrypoint.sh), the
        # sync pod does not and keeps the template's small disk.
        if container_disk_gb:
            body["disk"] = int(container_disk_gb)
        return body

    def _create_first(self, body, key, choices):
        """POST /pods once per choice, in order, until one is placed.

        v1 took a list and a ``*Priority: "custom"`` switch; v2 takes one type
        per pod, so the order the artist configured (cheapest first -- a cook
        once asked for an A4500 at ~$0.25/h and got a 4090 at $0.74/h when the
        order was ignored) is walked here, one request per type.

        If every type is refused, the error raised is the first 5xx seen (that
        is what :func:`is_capacity_error` treats as "wait for stock"), else the
        last one.
        """
        if not choices:
            raise RunPodError(0, f"no {key} types given")
        errors = []
        for choice in choices:
            try:
                return normalize_pod(self._call("POST", "/pods", {**body, key: choice}))
            except RunPodError as e:
                if not _try_next_type(e):
                    raise
                errors.append(e)
        raise next((e for e in errors if is_capacity_error(e)), errors[-1])

    def create_gpu_pod(self, name, template_id, gpu_type_ids, volume_id, env, ports,
                       datacenter=DEFAULT_DATACENTER, cloud_type=CLOUD_TYPE_SECURE,
                       container_disk_gb=None):
        """Create a GPU pod, trying ``gpu_type_ids`` in the order given."""
        body = self._pod_body(name, template_id, volume_id, env, ports, datacenter,
                              cloud_type=cloud_type, container_disk_gb=container_disk_gb)
        return self._create_first(body, "gpu", [{"id": g, "count": 1} for g in gpu_type_ids])

    def create_cpu_pod(self, name, template_id, volume_id, env, ports, vcpu=2, flavors=("cpu3c", "cpu5c"),
                       datacenter=DEFAULT_DATACENTER, cloud_type=CLOUD_TYPE_SECURE,
                       container_disk_gb=None):
        """Create a CPU pod, trying ``flavors`` in the order given.

        The default flavours are cheapest-first ($0.06/h for cpu3c against
        $0.07/h for cpu5c), so the order is the point. ``vcpu`` must be a
        power of two.
        """
        body = self._pod_body(name, template_id, volume_id, env, ports, datacenter,
                              cloud_type=cloud_type, container_disk_gb=container_disk_gb)
        return self._create_first(body, "cpu", [{"id": f, "vcpuCount": vcpu} for f in flavors])

    def get_pod(self, pod_id):
        return normalize_pod(self._call("GET", f"/pods/{pod_id}"))

    def terminate_pod(self, pod_id):
        self._call("DELETE", f"/pods/{pod_id}", ok404=True)

    def _action(self, pod_id, action, ok404=False):
        return self._call("POST", f"/pods/{pod_id}/action", {"action": action}, ok404=ok404)

    def stop_pod(self, pod_id):
        """Stop a pod without deleting it. Its container disk survives.

        Stopping is not free -- RunPod keeps charging for the container disk,
        and at DOUBLE the running rate ($0.20/GB/month against $0.10). For the
        sync pod (10 GB, no volume) that is roughly $2/month stopped against
        $43/month running: about twenty times cheaper, and emphatically not
        zero. The pair with :meth:`start_pod` is what lets the sync pod be
        parked between cooks instead of rebuilt, which is the whole point --
        a stopped pod starts in seconds and keeps whatever it had.
        """
        return self._action(pod_id, "stop", ok404=True)

    def start_pod(self, pod_id):
        """Bring a stopped pod back. Raises if it cannot be resumed."""
        return self._action(pod_id, "start")

    def list_pods(self, name_prefix=""):
        """Pods whose name starts with ``name_prefix`` (all pages).

        v2's list has ``runtime: null`` for every pod, so entries have no
        ``publicIp`` / ``portMappings``. A caller that needs the address of a
        pod it found here goes through :meth:`with_runtime` (one extra
        request, for the one pod it cares about) -- never a get per listed
        pod: the limit is 180 requests a minute and the status poll lists on
        every tick.
        """
        pods = self._list("/pods", "pods")
        return [normalize_pod(p) for p in pods if (p.get("name") or "").startswith(name_prefix)]

    def with_runtime(self, pod):
        """``pod`` (a list entry) with ``publicIp`` / ``portMappings`` filled in.

        Fetches ``GET /pods/{id}`` only when the entry has no address yet and
        looks live. If that fails (pod gone, network blip) the entry is
        returned untouched: callers already treat a missing endpoint as
        "not reachable" via :func:`pod_public_endpoint`.
        """
        if not pod or pod.get("publicIp") or pod.get("desiredStatus") != "RUNNING" or not pod.get("id"):
            return pod
        try:
            full = self.get_pod(pod["id"])
        except RunPodError:
            return pod
        return {**pod, **{k: v for k, v in (full or {}).items() if v is not None}}

    # -- network volumes --------------------------------------------------

    def list_volumes(self):
        return [normalize_volume(v) for v in self._list("/network-volumes", "networkVolumes")]

    def get_volume(self, vid):
        return normalize_volume(self._call("GET", f"/network-volumes/{vid}"))

    def create_volume(self, name, size_gb, dc=DEFAULT_DATACENTER):
        return normalize_volume(self._call(
            "POST",
            "/network-volumes",
            {"name": name, "size": size_gb, "dataCenter": dc},
        ))

    def resize_volume(self, vid, size_gb):
        return normalize_volume(self._call("PATCH", f"/network-volumes/{vid}", {"size": size_gb}))

    def delete_volume(self, vid):
        self._call("DELETE", f"/network-volumes/{vid}", ok404=True)

    # -- templates ---------------------------------------------------------

    def list_templates(self):
        return [normalize_template(t) for t in self._list("/templates", "templates")]

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
            "image": image,
            "ports": list(ports),
            "env": env,
            "disk": container_disk_gb,
            "public": False,
        }
        if registry_auth_id:
            body["registry"] = registry_auth_id
        if template_id:
            return normalize_template(self._call("PATCH", f"/templates/{template_id}", body))
        return normalize_template(self._call("POST", "/templates", body))

    # -- billing -------------------------------------------------------------

    def _billing(self, path, since_iso, until_iso):
        page = self._call("GET", path, query={"startTime": since_iso, "endTime": until_iso}) or {}
        return page.get("records") or []

    def billing_pods(self, since_iso, until_iso):
        """Pod billing rows (one per pod per day bucket), v1 keys added.

        v2 rows: ``{podId, startTime, endTime, cpuAmount, gpuAmount,
        diskAmount, totalAmount}``; ``amount`` is ``totalAmount``.
        """
        return _normalize_billing(self._billing("/billing/pods", since_iso, until_iso),
                                  _pod_billing_extra)

    def billing_volumes(self, since_iso, until_iso):
        """Network-volume billing rows; ``amount`` is ``totalAmount``."""
        return _normalize_billing(self._billing("/billing/network-volumes", since_iso, until_iso))

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

        REST has no per-datacenter stock signal (the GPU catalogue lists
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

    Only tcp ports show up in ``portMappings`` (built from v2's
    ``runtime.ports`` by :func:`normalize_pod`); http ports are reached via
    the ``https://<podId>-<port>.proxy.runpod.net/`` proxy instead and are
    not covered by this helper.
    """
    ip = pod.get("publicIp")
    mapping = pod.get("portMappings") or {}
    public = mapping.get(str(private_port))
    if not ip or not public:
        raise RunPodError(0, f"pod {pod.get('id')} has no public endpoint for {private_port}")
    return ip, int(public)
