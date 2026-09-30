from tests.fakes import FakeTransport, graphql_ok, pod
from tests.test_cli import run

RUNNING = pod(id="run1", name="a_run")
STOPPED = pod(id="stp1", name="a_stopped", desiredStatus="EXITED", gpuCount=2)


def listing(*pods):
    return graphql_ok({"myself": {"pods": list(pods)}})


def test_stop_without_tty_and_without_yes_exits_2_and_calls_nothing(capsys):
    t = FakeTransport().queue(*listing(RUNNING))
    assert run(["stop"], t, isatty=False) == 2
    assert "-y" in capsys.readouterr().err
    assert all(c.url.endswith("graphql") for c in t.calls)  # only the listing happened


def test_stop_with_yes_posts_stop(capsys):
    t = FakeTransport().queue(*listing(RUNNING)).queue(200, {"id": "run1"})
    assert run(["stop", "-y"], t) == 0
    assert t.last.method == "POST" and t.last.url.endswith("/pods/run1/stop")
    assert "stopped" in capsys.readouterr().out


def test_stop_selects_only_running(capsys):
    t = FakeTransport().queue(*listing(STOPPED))
    assert run(["stop", "-y"], t) == 2
    assert "No pods with status RUNNING" in capsys.readouterr().err


def test_resume_uses_pod_gpu_count_by_default():
    t = FakeTransport().queue(*listing(STOPPED)).queue(*graphql_ok({"podResume": {"id": "stp1"}}))
    assert run(["resume", "-y"], t) == 0
    assert t.last.body["variables"] == {"podId": "stp1", "gpuCount": 2}


def test_resume_gpu_count_override():
    t = FakeTransport().queue(*listing(STOPPED)).queue(*graphql_ok({"podResume": {"id": "stp1"}}))
    assert run(["resume", "-y", "--gpu-count", "1"], t) == 0
    assert t.last.body["variables"]["gpuCount"] == 1


def test_terminate_targets_running_and_stopped_and_warns(capsys):
    t = FakeTransport().queue(*listing(RUNNING, STOPPED)).queue(*graphql_ok({"podTerminate": None}))
    assert run(["terminate", "--pod", "stp1", "-y"], t) == 0
    err = capsys.readouterr().err
    assert "un-fetched" in err.lower() or "preserve" in err.lower()
    assert "podTerminate" in t.last.body["query"]


def test_terminate_ambiguous_without_pod_flag_exits_2(capsys):
    t = FakeTransport().queue(*listing(RUNNING, STOPPED))
    assert run(["terminate", "-y"], t) == 2
    assert "--pod" in capsys.readouterr().err


def test_terminate_all_hits_every_pod():
    t = (FakeTransport().queue(*listing(RUNNING, STOPPED))
         .queue(*graphql_ok({"podTerminate": None})).queue(*graphql_ok({"podTerminate": None})))
    assert run(["terminate", "--all", "-y"], t) == 0
    ids = [c.body["variables"]["podId"] for c in t.calls[1:]]
    assert sorted(ids) == ["run1", "stp1"]


def test_stop_declined_at_tty_prompt(capsys, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _q: "n")
    t = FakeTransport().queue(*listing(RUNNING))
    assert run(["stop"], t, isatty=True) == 0
    assert "cancelled" in capsys.readouterr().out
    assert len(t.calls) == 1


def test_stop_api_failure_exits_1(capsys):
    t = FakeTransport().queue(*listing(RUNNING)).queue(500, {"error": "boom"})
    assert run(["stop", "-y"], t) == 1
    assert "boom" in capsys.readouterr().err
