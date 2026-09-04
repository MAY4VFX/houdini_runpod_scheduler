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
    assert (m, url) == ("POST", "https://rest.runpod.io/v1/pods")
    assert body["computeType"] == "CPU" and body["networkVolumeId"] == "vol"
    # "custom" honours the order of cpuFlavorIds; the default "availability"
    # would ignore it and rent whatever RunPod has most of (ruling R18).
    assert body["cpuFlavorPriority"] == "custom" and body["cpuFlavorIds"] == ["cpu3c", "cpu5c"]
    assert body["volumeMountPath"] == "/workspace" and body["env"] == {"A": "1"}
    assert headers["Authorization"] == "Bearer k"


def test_create_gpu_pod_builds_body():
    t = FakeTransport([(200, {"id": "p1"})])
    api = ra.RunPodAPI("k", transport=t)
    pod = api.create_gpu_pod(
        "rpfarm-gpu", "tpl", ["NVIDIA RTX A4500", "NVIDIA A40"], "vol", {"A": "1"}, ["22/tcp"])
    assert pod["id"] == "p1"
    m, url, body, headers = t.calls[0]
    assert (m, url) == ("POST", "https://rest.runpod.io/v1/pods")
    assert body["computeType"] == "GPU"
    # The artist's GPU Priority list is a cost decision, so the order has to
    # be honoured: "availability" ignores it and picks whatever RunPod has
    # most of (ruling R18 -- a live cook got a $0.740/h 4090 over an A4500).
    assert body["gpuTypePriority"] == "custom"
    assert body["gpuTypeIds"] == ["NVIDIA RTX A4500", "NVIDIA A40"]
    assert body["networkVolumeId"] == "vol"
    assert body["volumeMountPath"] == "/workspace"


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
    t = FakeTransport([(200, [{"id": "p1", "name": "rpfarm-a"}, {"id": "p2", "name": "other"}])])
    api = ra.RunPodAPI("k", transport=t)
    pods = api.list_pods(name_prefix="rpfarm-")
    assert [p["id"] for p in pods] == ["p1"]
    m, url, body, headers = t.calls[0]
    assert (m, url) == ("GET", "https://rest.runpod.io/v1/pods")


def test_get_volume_and_resize_and_create_and_delete():
    t = FakeTransport(
        [
            (200, {"id": "v1", "size": 20}),
            (200, {"id": "v1", "size": 40}),
            (200, {"id": "v2"}),
            (204, {}),
        ]
    )
    api = ra.RunPodAPI("k", transport=t)
    v = api.get_volume("v1")
    assert v["id"] == "v1"
    assert t.calls[0][:2] == ("GET", "https://rest.runpod.io/v1/networkvolumes/v1")

    v = api.resize_volume("v1", 40)
    assert v["size"] == 40
    assert t.calls[1][:2] == ("PATCH", "https://rest.runpod.io/v1/networkvolumes/v1")
    assert t.calls[1][2] == {"size": 40}

    v = api.create_volume("rpfarm-vol", 50, dc="EU-RO-1")
    assert v["id"] == "v2"
    m, url, body, headers = t.calls[2]
    assert (m, url) == ("POST", "https://rest.runpod.io/v1/networkvolumes")
    assert body == {"name": "rpfarm-vol", "size": 50, "dataCenterId": "EU-RO-1"}

    api.delete_volume("v1")
    assert t.calls[3][:2] == ("DELETE", "https://rest.runpod.io/v1/networkvolumes/v1")


def test_save_template_create_vs_update():
    t = FakeTransport([(200, {"id": "t1"}), (200, {"id": "t1"})])
    api = ra.RunPodAPI("k", transport=t)

    api.save_template("rpfarm-pod", "img:latest", ["22/tcp"], {"A": "1"})
    m, url, body, headers = t.calls[0]
    assert (m, url) == ("POST", "https://rest.runpod.io/v1/templates")
    assert body["name"] == "rpfarm-pod" and body["imageName"] == "img:latest"

    api.save_template("rpfarm-pod", "img:latest", ["22/tcp"], {"A": "1"}, template_id="t1")
    m, url, body, headers = t.calls[1]
    assert (m, url) == ("PATCH", "https://rest.runpod.io/v1/templates/t1")


def test_billing_pods_and_volumes_query_params():
    t = FakeTransport([(200, [{"id": "b1"}]), (200, [{"id": "b2"}])])
    api = ra.RunPodAPI("k", transport=t)

    pods = api.billing_pods("2026-01-01T00:00:00Z", "2026-01-31T23:59:59Z")
    assert pods == [{"id": "b1"}]
    m, url, body, headers = t.calls[0]
    assert m == "GET"
    assert url.startswith("https://rest.runpod.io/v1/billing/pods?")
    assert "startTime=2026-01-01T00%3A00%3A00Z" in url
    assert "endTime=2026-01-31T23%3A59%3A59Z" in url

    vols = api.billing_volumes("2026-01-01T00:00:00Z", "2026-01-31T23:59:59Z")
    assert vols == [{"id": "b2"}]
    m, url, body, headers = t.calls[1]
    assert url.startswith("https://rest.runpod.io/v1/billing/networkvolumes?")


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
    t = FakeTransport([(200, [{"id": "v1"}]), (200, [{"id": "t1", "name": "rpfarm-pod"}])])
    api = ra.RunPodAPI("k", transport=t)

    vols = api.list_volumes()
    assert vols == [{"id": "v1"}]
    assert t.calls[0][:2] == ("GET", "https://rest.runpod.io/v1/networkvolumes")

    templates = api.list_templates()
    assert templates == [{"id": "t1", "name": "rpfarm-pod"}]
    assert t.calls[1][:2] == ("GET", "https://rest.runpod.io/v1/templates")


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
    t = FakeTransport([(200, []), (200, {"data": {"myself": {"clientBalance": 1.0}}})])
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
    ra._urllib_transport("GET", "https://rest.runpod.io/v1/pods", None, {})
    assert seen["context"] is not None
    assert seen["context"].verify_mode == ssl.CERT_REQUIRED


def test_default_transport_turns_network_errors_into_runpoderror(monkeypatch):
    def boom(req, timeout=None, context=None):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(ra.urllib.request, "urlopen", boom)
    with pytest.raises(ra.RunPodError) as excinfo:
        ra._urllib_transport("GET", "https://rest.runpod.io/v1/pods", None, {})
    assert excinfo.value.status == 0
