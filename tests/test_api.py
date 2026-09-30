import pytest

from runpod_tools.api import GRAPHQL_URL, REST_BASE, RunpodClient, RunpodError, client_from_env
from tests.fakes import FakeTransport, graphql_ok


def make(transport: FakeTransport) -> RunpodClient:
    return RunpodClient("sekret", transport=transport)


def test_rest_sends_bearer_and_json():
    t = FakeTransport().queue(200, {"ok": True})
    assert make(t).rest("POST", "/pods", {"name": "x"}) == {"ok": True}
    call = t.last
    assert call.method == "POST"
    assert call.url == REST_BASE + "/pods"
    assert call.headers["Authorization"] == "Bearer sekret"
    assert call.headers["Content-Type"] == "application/json"
    assert call.body == {"name": "x"}


def test_rest_empty_body_returns_none():
    t = FakeTransport().queue(204, b"")
    assert make(t).rest("POST", "/pods/abc/stop") is None


def test_rest_non_2xx_raises_with_status_and_message():
    t = FakeTransport().queue(400, {"error": "no GPUs available"})
    with pytest.raises(RunpodError) as exc:
        make(t).rest("POST", "/pods", {})
    assert exc.value.status == 400
    assert "no GPUs available" in str(exc.value)


def test_graphql_errors_array_raises():
    t = FakeTransport().queue(200, {"errors": [{"message": "Unauthorized"}], "data": None})
    with pytest.raises(RunpodError, match="Unauthorized"):
        make(t).graphql("{ myself { id } }")


def test_graphql_returns_data_and_sends_auth():
    t = FakeTransport().queue(*graphql_ok({"myself": {"pods": []}}))
    assert make(t).graphql("{ myself { pods { id } } }") == {"myself": {"pods": []}}
    assert t.last.url == GRAPHQL_URL
    assert t.last.headers["Authorization"] == "Bearer sekret"
    assert t.last.body["query"].startswith("{ myself")


def test_list_pods_unwraps_myself_pods():
    t = FakeTransport().queue(*graphql_ok({"myself": {"pods": [{"id": "a"}]}}))
    assert make(t).list_pods() == [{"id": "a"}]


def test_create_pod_posts_payload():
    t = FakeTransport().queue(201, {"id": "newpod"})
    assert make(t).create_pod({"name": "n"}) == {"id": "newpod"}
    assert t.last.method == "POST" and t.last.url.endswith("/pods")


def test_stop_pod_uses_rest():
    t = FakeTransport().queue(200, {"id": "abc"})
    make(t).stop_pod("abc")
    assert t.last.method == "POST" and t.last.url.endswith("/pods/abc/stop")


def test_resume_pod_sends_gpu_count():
    t = FakeTransport().queue(*graphql_ok({"podResume": {"id": "abc"}}))
    make(t).resume_pod("abc", gpu_count=2)
    assert t.last.body["variables"] == {"podId": "abc", "gpuCount": 2}


def test_terminate_pod_is_graphql_mutation():
    t = FakeTransport().queue(*graphql_ok({"podTerminate": None}))
    make(t).terminate_pod("abc")
    assert "podTerminate" in t.last.body["query"]
    assert t.last.body["variables"] == {"podId": "abc"}


def test_list_gpu_types_is_graphql():
    t = FakeTransport().queue(*graphql_ok({"gpuTypes": [{"id": "NVIDIA H200"}]}))
    assert make(t).list_gpu_types() == [{"id": "NVIDIA H200"}]


def test_list_network_volumes_is_rest_get():
    t = FakeTransport().queue(200, [{"id": "vol"}])
    assert make(t).list_network_volumes() == [{"id": "vol"}]
    assert t.last.method == "GET" and t.last.url.endswith("/networkvolumes")


def test_get_template_keeps_secret_placeholders():
    tpl = {
        "id": "tpl1",
        "name": "t",
        "imageName": "img",
        "ports": "22/tcp",
        "containerDiskInGb": 50,
        "volumeInGb": 0,
        "volumeMountPath": "/workspace",
        "dockerArgs": "",
        "env": [{"key": "HF_TOKEN", "value": "{{ RUNPOD_SECRET_HF }}"}],
    }
    t = FakeTransport().queue(*graphql_ok({"myself": {"podTemplates": [tpl]}}))
    got = make(t).get_template("tpl1")
    assert got["env"] == {"HF_TOKEN": "{{ RUNPOD_SECRET_HF }}"}
    assert got["imageName"] == "img"


def test_get_template_null_env_becomes_empty_dict():
    tpl = {"id": "tpl1", "name": "t", "imageName": "img", "env": None}
    t = FakeTransport().queue(*graphql_ok({"myself": {"podTemplates": [tpl]}}))
    assert make(t).get_template("tpl1")["env"] == {}


def test_get_template_missing_raises_404():
    t = FakeTransport().queue(*graphql_ok({"myself": {"podTemplates": []}}))
    with pytest.raises(RunpodError) as exc:
        make(t).get_template("nope")
    assert exc.value.status == 404


def test_patch_template_uses_rest_patch():
    t = FakeTransport().queue(200, {})
    make(t).patch_template("tpl1", {"env": {"A": "b"}})
    assert t.last.method == "PATCH" and t.last.url.endswith("/templates/tpl1")
    assert t.last.body == {"env": {"A": "b"}}


def test_save_template_is_graphql_mutation():
    t = FakeTransport().queue(*graphql_ok({"saveTemplate": {"id": "tpl1", "ports": "22/tcp"}}))
    got = make(t).save_template({"id": "tpl1", "ports": "22/tcp"})
    assert got == {"id": "tpl1", "ports": "22/tcp"}
    assert "saveTemplate" in t.last.body["query"]


def test_client_from_env_requires_key():
    with pytest.raises(RunpodError, match="RUNPOD_API_KEY"):
        client_from_env(env={})


def test_client_from_env_uses_key():
    c = client_from_env(env={"RUNPOD_API_KEY": "k"})
    assert isinstance(c, RunpodClient)


def test_transport_error_is_runpod_error():
    def boom(*_):
        raise OSError("connection refused")

    with pytest.raises(RunpodError, match="connection refused"):
        RunpodClient("k", transport=boom).rest("GET", "/pods")
