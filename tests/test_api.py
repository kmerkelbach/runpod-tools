import pytest

from runpod_tools.api import (
    DEFAULT_RATE_LIMIT_WAIT,
    GRAPHQL_URL,
    REST_BASE,
    RunpodClient,
    RunpodError,
    client_from_env,
    rate_limit_wait_from_env,
)
from tests.fakes import RATE_LIMITED, FakeClock, FakeTransport, graphql_ok


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
    assert call.headers["User-Agent"].startswith("runpod-tools/")
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


def throttled_client(transport: FakeTransport, clock: FakeClock, wait: float = 90.0, log=None) -> RunpodClient:
    return RunpodClient("k", transport=transport, rate_limit_wait=wait, sleep=clock.sleep, clock=clock,
                        jitter=lambda: 1.0, log=log or (lambda _m: None))


def test_rest_retries_429_with_exponential_backoff():
    t = FakeTransport().queue(*RATE_LIMITED).queue(*RATE_LIMITED).queue(200, [{"id": "v1"}])
    clock = FakeClock()
    assert throttled_client(t, clock).rest("GET", "/networkvolumes") == [{"id": "v1"}]
    assert len(t.calls) == 3
    assert clock.sleeps == [2.0, 4.0]


def test_graphql_retries_429_then_returns_data():
    t = FakeTransport().queue(*RATE_LIMITED).queue(*graphql_ok({"myself": {"pods": []}}))
    clock = FakeClock()
    assert throttled_client(t, clock).list_pods() == []
    assert clock.sleeps == [2.0]


def test_graphql_rate_limit_error_in_body_is_retried():
    t = (FakeTransport().queue(200, {"errors": [{"message": "Too many requests, slow down"}], "data": None})
         .queue(*graphql_ok({"gpuTypes": []})))
    clock = FakeClock()
    assert throttled_client(t, clock).list_gpu_types() == []
    assert len(t.calls) == 2


def test_retry_after_header_is_honoured():
    t = FakeTransport().queue(429, {"error": "slow down"}, {"Retry-After": "17"}).queue(204, b"")
    clock = FakeClock()
    throttled_client(t, clock).rest("POST", "/pods/abc/stop")
    assert clock.sleeps == [17.0]


def test_jitter_spreads_the_delay_between_half_and_full_ceiling():
    t = FakeTransport().queue(*RATE_LIMITED).queue(204, b"")
    clock = FakeClock()
    client = RunpodClient("k", transport=t, sleep=clock.sleep, clock=clock, jitter=lambda: 0.0, log=lambda _m: None)
    client.rest("POST", "/pods/abc/stop")
    assert clock.sleeps == [1.0]


def test_backoff_ceiling_is_capped():
    t = FakeTransport()
    for _ in range(7):
        t.queue(*RATE_LIMITED)
    t.queue(204, b"")
    clock = FakeClock()
    throttled_client(t, clock, wait=1000).rest("POST", "/pods/abc/stop")
    assert clock.sleeps == [2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0]


def test_rate_limit_gives_up_non_zero_within_the_wait_budget():
    t = FakeTransport()
    for _ in range(20):
        t.queue(*RATE_LIMITED)
    clock = FakeClock()
    with pytest.raises(RunpodError, match="rate limited") as exc:
        throttled_client(t, clock, wait=20).list_pods()
    assert exc.value.status == 429
    assert sum(clock.sleeps) <= 20
    assert clock.sleeps == [2.0, 4.0, 8.0]


def test_retry_after_beyond_the_budget_fails_at_once():
    t = FakeTransport().queue(429, {"error": "slow down"}, {"Retry-After": "600"})
    clock = FakeClock()
    with pytest.raises(RunpodError, match="rate limited"):
        throttled_client(t, clock, wait=90).rest("GET", "/pods")
    assert clock.sleeps == []


def test_zero_wait_disables_retries():
    t = FakeTransport().queue(*RATE_LIMITED)
    with pytest.raises(RunpodError) as exc:
        throttled_client(t, FakeClock(), wait=0).rest("GET", "/pods")
    assert exc.value.status == 429
    assert len(t.calls) == 1


def test_each_retry_is_logged():
    messages: list[str] = []
    t = FakeTransport().queue(*RATE_LIMITED).queue(204, b"")
    throttled_client(t, FakeClock(), log=messages.append).rest("POST", "/pods/abc/stop")
    assert messages == ["rpt: POST /pods/abc/stop rate limited (429); retry 1 in 2.0s"]


def test_other_errors_are_not_retried():
    t = FakeTransport().queue(400, {"error": "no GPUs available"})
    clock = FakeClock()
    with pytest.raises(RunpodError):
        throttled_client(t, clock).rest("POST", "/pods", {})
    assert clock.sleeps == []


def test_two_tuple_transport_still_works():
    def legacy(*_):
        return 200, b'{"data": {"gpuTypes": []}}'

    assert RunpodClient("k", transport=legacy).list_gpu_types() == []


def test_rate_limit_wait_comes_from_env():
    assert rate_limit_wait_from_env({}) == DEFAULT_RATE_LIMIT_WAIT
    assert rate_limit_wait_from_env({"RPT_RATE_LIMIT_WAIT": "15"}) == 15.0
    assert rate_limit_wait_from_env({"RPT_RATE_LIMIT_WAIT": "junk"}) == DEFAULT_RATE_LIMIT_WAIT


def test_usage_log_records_each_request(tmp_path):
    log = tmp_path / "usage.log"
    t = FakeTransport().queue(429, {"error": "x"}, {"X-RateLimit-Remaining-Hour": "0"}).queue(204, b"")
    clock = FakeClock()
    RunpodClient("k", transport=t, sleep=clock.sleep, clock=clock, log=lambda _m: None,
                 usage_log=log).rest("POST", "/pods/abc/stop")
    lines = [line.split("\t") for line in log.read_text().splitlines()]
    assert [(f[2], f[3], f[4]) for f in lines] == [("POST /pods/abc/stop", "429", "0"), ("POST /pods/abc/stop", "204", "")]


def test_usage_log_can_be_disabled(tmp_path):
    from runpod_tools.podcache import usage_log_from_env

    assert usage_log_from_env("k", {"RPT_CACHE_DIR": str(tmp_path), "RPT_USAGE_LOG": "0"}) is None
    assert usage_log_from_env("k", {"RPT_CACHE_DIR": str(tmp_path)}).parent == tmp_path


def test_a_long_retry_after_blocks_later_calls_without_sending(tmp_path):
    from runpod_tools.podcache import Backoff

    wall = [1000.0]
    backoff = Backoff(tmp_path, "acct", clock=lambda: wall[0])
    t = FakeTransport().queue(429, {"error": "x"}, {"Retry-After": "1200"})
    clock = FakeClock()
    c = RunpodClient("k", transport=t, sleep=clock.sleep, clock=clock, log=lambda _m: None, backoff=backoff)
    with pytest.raises(RunpodError):
        c.list_pods()
    with pytest.raises(RunpodError, match="not sending"):
        c.list_pods()
    assert len(t.calls) == 1
    # REST has its own bucket and is not blocked
    t.queue(200, [])
    assert c.list_network_volumes() == []
    wall[0] += 1201
    t.queue(*graphql_ok({"myself": {"pods": []}}))
    assert c.list_pods() == []
