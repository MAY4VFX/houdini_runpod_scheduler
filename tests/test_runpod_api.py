import json
import ssl
import urllib.error

import pytest

from rpfarm import runpod_api as ra


class FakeTransport:
    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def __call__(self, method, url, body, headers):
        self.calls.append((method, url, body, headers))
        status, payload = self.responses.pop(0)
        return status, json.dumps(payload).encode()


def test_create_cpu_pod_builds_body():
    t = FakeTransport([(200, {"id": "p1"})])
    api = ra.RunPodAPI("k", transport=t)
    pod = api.create_cpu_pod("rpfarm-sync-may", "tpl", "vol", {"A": "1"}, ["22/tcp"])
    assert pod["id"] == "p1"
    m, url, body, headers = t.calls[0]
    assert (m, url) == ("POST", "https://api.runpod.io/v2/pods")
    # v2: exactly one of gpu/cpu, nested; no computeType, no *Priority lists,
    # no supportPublicIp (a 22/tcp pod gets a public address anyway).
    assert body["cpu"] == {"id": "cpu3c", "vcpuCount": 2}
    assert "gpu" not in body and "computeType" not in body and "supportPublicIp" not in body
    assert body["templateId"] == "tpl" and body["cloud"] == "SECURE"
    assert body["mounts"] == {"network": [{"volumeId": "vol", "path": "/workspace"}]}
    assert body["env"] == {"A": "1"}
    assert headers["Authorization"] == "Bearer k"


def test_create_gpu_pod_builds_body():
    t = FakeTransport([(200, {"id": "p1"})])
    api = ra.RunPodAPI("k", transport=t)
    pod = api.create_gpu_pod(
        "rpfarm-gpu", "tpl", ["NVIDIA RTX A4500", "NVIDIA A40"], "vol", {"A": "1"}, ["22/tcp"])
    assert pod["id"] == "p1"
    m, url, body, headers = t.calls[0]
    assert (m, url) == ("POST", "https://api.runpod.io/v2/pods")
    # The artist's GPU Priority list is a cost decision (ruling R18 -- a live
    # cook got a $0.740/h 4090 over an A4500). v2 takes one type per pod, so
    # the first type is the first request.
    assert body["gpu"] == {"id": "NVIDIA RTX A4500", "count": 1}
    assert len(t.calls) == 1
    assert body["mounts"] == {"network": [{"volumeId": "vol", "path": "/workspace"}]}


def test_pod_creates_carry_the_configured_datacenter():
    """Finding 5: the region used to be hardcoded to EU-RO-1 in _pod_body
    while cfg.datacenter, the scheduler's Datacenter parm, doctor's stock
    check and `storage recreate` all honoured the configured value. A
    network volume exists in exactly one region and a pod can only mount
    one in its own, so an artist whose account already held a volume
    elsewhere got pods that could not mount it and cooks that all failed.
    """
    t = FakeTransport([(200, {"id": "p1"}), (200, {"id": "p2"})])
    api = ra.RunPodAPI("k", transport=t)

    api.create_cpu_pod("rpfarm-sync-may", "tpl", "vol", {}, ["22/tcp"], datacenter="US-KS-2")
    api.create_gpu_pod("rpfarm-gpu", "tpl", ["NVIDIA A40"], "vol", {}, ["22/tcp"], datacenter="US-KS-2")

    assert [body["dataCenterIds"] for _m, _u, body, _h in t.calls] == [["US-KS-2"], ["US-KS-2"]]


def test_pod_creates_fall_back_to_the_default_datacenter():
    """A caller with nothing configured still gets a region, and it is the
    one literal every other default uses."""
    t = FakeTransport([(200, {"id": "p1"}), (200, {"id": "p2"})])
    api = ra.RunPodAPI("k", transport=t)
    api.create_cpu_pod("rpfarm-sync-may", "tpl", "vol", {}, [])
    api.create_cpu_pod("rpfarm-sync-may", "tpl", "vol", {}, [], datacenter="")
    for _m, _u, body, _h in t.calls:
        assert body["dataCenterIds"] == [ra.DEFAULT_DATACENTER]


def test_terminate_ignores_404():
    api = ra.RunPodAPI("k", transport=FakeTransport([(404, {"error": "no"})]))
    api.terminate_pod("gone")  # no raise


def test_error_raises_with_status():
    api = ra.RunPodAPI("k", transport=FakeTransport([(500, {"error": "boom"})]))
    with pytest.raises(ra.RunPodError) as e:
        api.get_pod("x")
    assert e.value.status == 500


def test_pod_public_endpoint():
    pod = {"publicIp": "1.2.3.4", "portMappings": {"22": 40022, "4440": 40440}}
    assert ra.pod_public_endpoint(pod, 4440) == ("1.2.3.4", 40440)


def test_list_pods_filters_by_name_prefix():
    t = FakeTransport([(200, {"pods": [{"id": "p1", "name": "rpfarm-a"}, {"id": "p2", "name": "other"}],
                              "pagination": {"hasNextPage": False, "nextCursor": None}})])
    api = ra.RunPodAPI("k", transport=t)
    pods = api.list_pods(name_prefix="rpfarm-")
    assert [p["id"] for p in pods] == ["p1"]
    m, url, body, headers = t.calls[0]
    assert (m, url) == ("GET", "https://api.runpod.io/v2/pods")


def test_get_volume_and_resize_and_create_and_delete():
    t = FakeTransport(
        [
            (200, {"id": "v1", "size": 20, "dataCenter": "EU-RO-1"}),
            (200, {"id": "v1", "size": 40}),
            (200, {"id": "v2"}),
            (204, {}),
        ]
    )
    api = ra.RunPodAPI("k", transport=t)
    v = api.get_volume("v1")
    assert v["id"] == "v1"
    assert v["dataCenterId"] == "EU-RO-1"   # the v1 key callers read
    assert t.calls[0][:2] == ("GET", "https://api.runpod.io/v2/network-volumes/v1")

    v = api.resize_volume("v1", 40)
    assert v["size"] == 40
    assert t.calls[1][:2] == ("PATCH", "https://api.runpod.io/v2/network-volumes/v1")
    assert t.calls[1][2] == {"size": 40}

    v = api.create_volume("rpfarm-vol", 50, dc="EU-RO-1")
    assert v["id"] == "v2"
    m, url, body, headers = t.calls[2]
    assert (m, url) == ("POST", "https://api.runpod.io/v2/network-volumes")
    assert body == {"name": "rpfarm-vol", "size": 50, "dataCenter": "EU-RO-1"}

    api.delete_volume("v1")
    assert t.calls[3][:2] == ("DELETE", "https://api.runpod.io/v2/network-volumes/v1")


def test_save_template_create_vs_update():
    t = FakeTransport([(200, {"id": "t1"}), (200, {"id": "t1"})])
    api = ra.RunPodAPI("k", transport=t)

    api.save_template("rpfarm-pod", "img:latest", ["22/tcp"], {"A": "1"})
    m, url, body, headers = t.calls[0]
    assert (m, url) == ("POST", "https://api.runpod.io/v2/templates")
    assert body["name"] == "rpfarm-pod" and body["image"] == "img:latest"
    assert body["disk"] == 10 and "imageName" not in body

    api.save_template("rpfarm-pod", "img:latest", ["22/tcp"], {"A": "1"}, template_id="t1")
    m, url, body, headers = t.calls[1]
    assert (m, url) == ("PATCH", "https://api.runpod.io/v2/templates/t1")


def test_billing_pods_and_volumes_query_params():
    t = FakeTransport([(200, {"metadata": {}, "records": [{"podId": "b1", "totalAmount": 0.5}]}),
                       (200, {"metadata": {}, "records": [{"networkVolumeId": "v", "totalAmount": 0.1}]})])
    api = ra.RunPodAPI("k", transport=t)

    pods = api.billing_pods("2026-01-01T00:00:00Z", "2026-01-31T23:59:59Z")
    assert [(b["podId"], b["amount"], b["totalAmount"]) for b in pods] == [("b1", 0.5, 0.5)]
    m, url, body, headers = t.calls[0]
    assert m == "GET"
    assert url.startswith("https://api.runpod.io/v2/billing/pods?")
    assert "startTime=2026-01-01T00%3A00%3A00Z" in url
    assert "endTime=2026-01-31T23%3A59%3A59Z" in url

    vols = api.billing_volumes("2026-01-01T00:00:00Z", "2026-01-31T23:59:59Z")
    assert [(b["networkVolumeId"], b["amount"]) for b in vols] == [("v", 0.1)]
    m, url, body, headers = t.calls[1]
    assert url.startswith("https://api.runpod.io/v2/billing/network-volumes?")


def test_balance_uses_graphql_endpoint():
    t = FakeTransport([(200, {"data": {"myself": {"clientBalance": 21.67}}})])
    api = ra.RunPodAPI("k", transport=t)
    bal = api.balance()
    assert bal == 21.67
    m, url, body, headers = t.calls[0]
    assert (m, url) == ("POST", "https://api.runpod.io/graphql")
    assert "clientBalance" in body["query"]
    assert headers["Authorization"] == "Bearer k"


def test_list_volumes_and_list_templates():
    t = FakeTransport([
        (200, {"networkVolumes": [{"id": "v1", "dataCenter": "EU-RO-1"}], "pagination": {"hasNextPage": False}}),
        (200, {"templates": [{"id": "t1", "name": "rpfarm-pod", "image": "img:1"}], "pagination": {"hasNextPage": False}}),
    ])
    api = ra.RunPodAPI("k", transport=t)

    vols = api.list_volumes()
    assert vols == [{"id": "v1", "dataCenter": "EU-RO-1", "dataCenterId": "EU-RO-1"}]
    assert t.calls[0][:2] == ("GET", "https://api.runpod.io/v2/network-volumes")

    templates = api.list_templates()
    assert templates == [{"id": "t1", "name": "rpfarm-pod", "image": "img:1", "imageName": "img:1"}]
    assert t.calls[1][:2] == ("GET", "https://api.runpod.io/v2/templates")


def test_gpu_types_uses_graphql_endpoint_with_dc():
    payload = {
        "data": {
            "gpuTypes": [
                {"id": "NVIDIA GeForce RTX 4090", "displayName": "RTX 4090",
                 "lowestPrice": {"stockStatus": "High", "minimumBidPrice": 0.34, "uninterruptablePrice": 0.34}},
                {"id": "NVIDIA RTX A4500", "displayName": "RTX A4500",
                 "lowestPrice": {"stockStatus": None, "minimumBidPrice": None, "uninterruptablePrice": None}},
            ]
        }
    }
    t = FakeTransport([(200, payload)])
    api = ra.RunPodAPI("k", transport=t)
    types = api.gpu_types(dc="EU-RO-1")
    assert types[0]["id"] == "NVIDIA GeForce RTX 4090"
    assert types[0]["lowestPrice"]["stockStatus"] == "High"
    assert types[1]["lowestPrice"]["stockStatus"] is None
    m, url, body, headers = t.calls[0]
    assert (m, url) == ("POST", "https://api.runpod.io/graphql")
    assert "EU-RO-1" in body["query"]


def test_every_request_carries_an_explicit_user_agent():
    """Cloudflare 403s Python's default User-Agent on api.runpod.io."""
    t = FakeTransport([(200, {"pods": []}), (200, {"data": {"myself": {"clientBalance": 1.0}}})])
    api = ra.RunPodAPI("k", transport=t)
    api.list_pods()
    api.balance()
    for _m, _url, _body, headers in t.calls:
        assert headers["User-Agent"] == ra.USER_AGENT
        assert "urllib" not in headers["User-Agent"]


def test_default_transport_uses_a_verifying_ssl_context(monkeypatch):
    """Houdini's bundled OpenSSL has no CA store; without an explicit context
    every RunPod call fails with CERTIFICATE_VERIFY_FAILED."""
    seen = {}

    class FakeResponse:
        status = 200

        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None, context=None):
        seen["context"] = context
        return FakeResponse()

    monkeypatch.setattr(ra.urllib.request, "urlopen", fake_urlopen)
    ra._urllib_transport("GET", "https://api.runpod.io/v2/pods", None, {})
    assert seen["context"] is not None
    assert seen["context"].verify_mode == ssl.CERT_REQUIRED


def test_default_transport_turns_network_errors_into_runpoderror(monkeypatch):
    def boom(req, timeout=None, context=None):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(ra.urllib.request, "urlopen", boom)
    with pytest.raises(ra.RunPodError) as excinfo:
        ra._urllib_transport("GET", "https://api.runpod.io/v2/pods", None, {})
    assert excinfo.value.status == 0


# ---------------------------------------------------------------------------
# cloud type + is_capacity_error
# ---------------------------------------------------------------------------


def test_cloud_type_reaches_the_request_body_for_both_pod_kinds():
    sent = {}

    def transport(method, url, body, headers):
        sent.clear()
        sent.update(body or {})
        return 200, b'{"id": "p"}'

    api = ra.RunPodAPI("key", transport=transport)

    api.create_gpu_pod("n", "tpl", ["A4500"], "vol", {}, [22],
                       cloud_type=ra.CLOUD_TYPE_COMMUNITY)
    assert sent["cloud"] == "COMMUNITY"

    api.create_cpu_pod("n", "tpl", "vol", {}, [22], cloud_type=ra.CLOUD_TYPE_COMMUNITY)
    assert sent["cloud"] == "COMMUNITY"


def test_cloud_type_defaults_to_secure():
    sent = {}

    def transport(method, url, body, headers):
        sent.update(body or {})
        return 200, b'{"id": "p"}'

    ra.RunPodAPI("key", transport=transport).create_gpu_pod(
        "n", "tpl", ["A4500"], "vol", {}, [22])

    assert sent["cloud"] == "SECURE"


def test_interruptible_is_never_sent():
    """Leaving it unset is what guarantees a pod we hold is not taken back --
    the premise the whole wait-for-capacity design rests on."""
    sent = {}

    def transport(method, url, body, headers):
        sent.update(body or {})
        return 200, b'{"id": "p"}'

    ra.RunPodAPI("key", transport=transport).create_gpu_pod(
        "n", "tpl", ["A4500"], "vol", {}, [22], cloud_type=ra.CLOUD_TYPE_COMMUNITY)

    assert "interruptible" not in sent


def test_is_capacity_error_waits_on_5xx_and_gives_up_on_4xx():
    body = ('{"error":"create pod: There are no longer any instances available '
            'with the requested specifications."}')

    assert ra.is_capacity_error(ra.RunPodError(500, body)) is True
    assert ra.is_capacity_error(ra.RunPodError(502, "bad gateway")) is True

    # No amount of waiting fixes a bad key or a bad request.
    assert ra.is_capacity_error(ra.RunPodError(401, "unauthorized")) is False
    assert ra.is_capacity_error(ra.RunPodError(403, "forbidden")) is False
    assert ra.is_capacity_error(ra.RunPodError(400, "malformed")) is False
    assert ra.is_capacity_error(ra.RunPodError(404, "no such template")) is False


def test_is_capacity_error_is_false_for_anything_without_a_status():
    assert ra.is_capacity_error(ValueError("not a runpod error")) is False


def test_pod_creates_can_ask_for_a_bigger_disk():
    """Render and host pods unpack Houdini onto their own disk; the
    template's 10 GB does not hold it, so those creates override it."""
    t = FakeTransport([(200, {"id": "p1"}), (200, {"id": "p2"}), (200, {"id": "p3"})])
    api = ra.RunPodAPI("k", transport=t)
    api.create_gpu_pod("g", "tpl", ["NVIDIA A40"], "vol", {}, ["22/tcp"], container_disk_gb=25)
    api.create_cpu_pod("h", "tpl", "vol", {}, ["22/tcp"], container_disk_gb=25)
    api.create_cpu_pod("rpfarm-sync-may", "tpl", "vol", {}, ["22/tcp"])
    assert t.calls[0][2]["disk"] == 25
    assert t.calls[1][2]["disk"] == 25
    # the sync pod keeps the template's disk
    assert "disk" not in t.calls[2][2]


# ---------------------------------------------------------------------------
# REST v2: shapes, pagination, fallback order, v1-shaped views
# ---------------------------------------------------------------------------

LIVE_POD = {
    # Shape of GET /v2/pods/{id} for a RUNNING CPU pod, checked live 2026-11-18.
    "id": "7ars8fv3gnyy6e", "name": "rpfarm-sync-may", "status": "RUNNING", "cost": 0.06,
    "image": "ghcr.io/may4vfx/rpfarm-pod:9016ed0", "cloud": "SECURE", "dataCenterId": "EU-RO-1",
    "createdAt": "2026-11-18T11:42:50.000Z", "startedAt": "2026-11-18T11:43:02.873Z",
    "mounts": {"network": [{"volumeId": "vol1", "path": "/workspace"}]},
    "cpu": {"id": "cpu3c", "vcpuCount": 2},
    "env": {"RPFARM_USER": "may"},
    "runtime": {"uptime": 16, "ports": [
        {"ip": "213.173.105.101", "private": 4440, "public": 26760, "type": "tcp"},
        {"ip": "100.65.35.124", "private": 8000, "public": 60784, "type": "http"},
        {"ip": "213.173.105.101", "private": 22, "public": 26759, "type": "tcp"},
    ]},
    "ssh": {"direct": {"host": "213.173.105.101", "port": 26759}},
}


def test_normalize_pod_adds_the_v1_keys_alongside_v2_ones():
    pod = ra.normalize_pod(LIVE_POD)
    assert pod["desiredStatus"] == "RUNNING" and pod["status"] == "RUNNING"
    assert pod["costPerHr"] == 0.06 and pod["cost"] == 0.06
    assert pod["imageName"] == pod["image"]
    assert pod["networkVolumeId"] == "vol1"
    assert pod["lastStatusChange"] == "2026-11-18T11:43:02.873Z"
    # only tcp ports have a public address; the http one is a private 100.x proxy hop
    assert pod["publicIp"] == "213.173.105.101"
    assert pod["portMappings"] == {"4440": 26760, "22": 26759}
    assert ra.pod_public_endpoint(pod, 22) == ("213.173.105.101", 26759)
    assert LIVE_POD.get("publicIp") is None   # the input is not mutated


def test_normalize_pod_gpu_machine_type():
    pod = ra.normalize_pod({"id": "g", "status": "RUNNING", "gpu": {"id": "NVIDIA GeForce RTX 4090", "count": 1}})
    assert pod["machine"] == {"gpuTypeId": "NVIDIA GeForce RTX 4090"}


@pytest.mark.parametrize("status,desired", [
    ("PROVISIONING", "RUNNING"),   # v1 said RUNNING from the moment of creation
    ("STARTING", "RUNNING"),
    ("RUNNING", "RUNNING"),
    ("EXITED", "EXITED"),
    ("ERROR", "EXITED"),           # a failed pod is not a live cook / usable sync pod
    ("TERMINATED", "TERMINATED"),
])
def test_normalize_pod_desired_status(status, desired):
    assert ra.normalize_pod({"id": "p", "status": status})["desiredStatus"] == desired


@pytest.mark.parametrize("status", ["EXITED", "STARTING", "PROVISIONING", "ERROR"])
def test_a_pod_that_is_not_running_has_no_address_even_if_runtime_still_lists_ports(status):
    """Seen live: just after a stop, ``EXITED`` still carried the old ports, and
    just after a start the old ports came back before the new ones."""
    pod = ra.normalize_pod(dict(LIVE_POD, status=status))
    assert "publicIp" not in pod and "portMappings" not in pod
    with pytest.raises(ra.RunPodError):
        ra.pod_public_endpoint(pod, 22)


def test_a_stopped_pod_has_no_last_status_change():
    """v2 has no stop time. startedAt is the last START, so using it would make a
    sync pod parked after a long run look stopped for hours -- and get deleted
    by the stopped-for-N-minutes policy. Unknown never triggers an action."""
    pod = ra.normalize_pod({"id": "p", "status": "EXITED", "startedAt": "2026-11-01T00:00:00Z",
                            "createdAt": "2026-10-01T00:00:00Z"})
    assert "lastStatusChange" not in pod


def test_a_list_entry_has_no_address_until_with_runtime():
    listed = dict(LIVE_POD, runtime=None)
    t = FakeTransport([(200, {"pods": [listed], "pagination": {"hasNextPage": False}}), (200, LIVE_POD)])
    api = ra.RunPodAPI("k", transport=t)
    (pod,) = api.list_pods("rpfarm-")
    assert "publicIp" not in pod
    full = api.with_runtime(pod)
    assert ra.pod_public_endpoint(full, 22) == ("213.173.105.101", 26759)
    assert t.calls[1][:2] == ("GET", "https://api.runpod.io/v2/pods/7ars8fv3gnyy6e")
    # and it costs nothing once the address is known
    assert api.with_runtime(full) is full
    assert len(t.calls) == 2


def test_with_runtime_survives_a_vanished_pod():
    t = FakeTransport([(404, {"error": "gone"})])
    api = ra.RunPodAPI("k", transport=t)
    pod = {"id": "p", "desiredStatus": "RUNNING"}
    assert api.with_runtime(pod) is pod
    # a stopped pod is never worth a request
    assert api.with_runtime({"id": "q", "desiredStatus": "EXITED"}) == {"id": "q", "desiredStatus": "EXITED"}
    assert len(t.calls) == 1


def test_lists_follow_the_pagination_cursor():
    t = FakeTransport([
        (200, {"pods": [{"id": "a", "name": "x1"}], "pagination": {"hasNextPage": True, "nextCursor": "c1"}}),
        (200, {"pods": [{"id": "b", "name": "x2"}], "pagination": {"hasNextPage": False, "nextCursor": None}}),
    ])
    pods = ra.RunPodAPI("k", transport=t).list_pods()
    assert [p["id"] for p in pods] == ["a", "b"]
    assert t.calls[0][1] == "https://api.runpod.io/v2/pods"
    assert t.calls[1][1] == "https://api.runpod.io/v2/pods?cursor=c1"


def test_gpu_priority_is_walked_one_request_per_type():
    t = FakeTransport([(400, {"detail": "no such GPU here"}), (500, {"detail": "no instances"}),
                       (201, {"id": "p9", "status": "RUNNING"})])
    api = ra.RunPodAPI("k", transport=t)
    pod = api.create_gpu_pod("g", "tpl", ["A", "B", "C"], "vol", {}, ["22/tcp"])
    assert pod["id"] == "p9" and pod["desiredStatus"] == "RUNNING"
    assert [c[2]["gpu"]["id"] for c in t.calls] == ["A", "B", "C"]


def test_every_type_refused_raises_the_capacity_error_if_there_was_one():
    t = FakeTransport([(500, {"detail": "no instances"}), (400, {"detail": "unknown"})])
    with pytest.raises(ra.RunPodError) as e:
        ra.RunPodAPI("k", transport=t).create_gpu_pod("g", "tpl", ["A", "B"], "vol", {}, [])
    assert ra.is_capacity_error(e.value)


@pytest.mark.parametrize("status", [401, 403, 422, 429])
def test_auth_schema_and_rate_errors_stop_the_walk(status):
    t = FakeTransport([(status, {"detail": "no"}), (201, {"id": "never"})])
    with pytest.raises(ra.RunPodError):
        ra.RunPodAPI("k", transport=t).create_gpu_pod("g", "tpl", ["A", "B"], "vol", {}, [])
    assert len(t.calls) == 1


def test_stop_start_use_the_action_endpoint():
    t = FakeTransport([(200, {}), (200, {})])
    api = ra.RunPodAPI("k", transport=t)
    api.stop_pod("p1")
    api.start_pod("p1")
    assert [(c[0], c[1], c[2]) for c in t.calls] == [
        ("POST", "https://api.runpod.io/v2/pods/p1/action", {"action": "stop"}),
        ("POST", "https://api.runpod.io/v2/pods/p1/action", {"action": "start"}),
    ]


def test_get_pod_still_raises_on_404_and_terminate_still_swallows_it():
    api = ra.RunPodAPI("k", transport=FakeTransport([(404, {}), (404, {})]))
    with pytest.raises(ra.RunPodError) as e:
        api.get_pod("gone")
    assert e.value.status == 404
    api.terminate_pod("gone")


def test_pod_billing_rows_keep_the_keys_the_ledger_reads():
    t = FakeTransport([(200, {"records": [{"podId": "p", "totalAmount": 0.25, "gpuAmount": 0.2,
                                           "startTime": "2026-11-12T00:00:00Z"}]})])
    (row,) = ra.RunPodAPI("k", transport=t).billing_pods("a", "b")
    assert row["amount"] == 0.25 and row["podId"] == "p"
    # v2 has no billed-time field: 0 means unknown to merge_billing
    assert row["timeBilledMs"] == 0
