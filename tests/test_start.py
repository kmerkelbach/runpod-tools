import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from runpod_tools.commands.start import (
    apply_prefix,
    build_payload,
    default_pod_name,
    parse_duration,
    parse_env,
)
from runpod_tools.config import Config, PodDefaults
from tests.fakes import FakeTransport, graphql_ok, pod
from tests.test_cli import run


def test_parse_duration_units():
    assert parse_duration("2h") == 7200
    assert parse_duration("30m") == 1800
    assert parse_duration("45s") == 45
    assert parse_duration("1.5h") == 5400
    assert parse_duration(None) == 0


@pytest.mark.parametrize("bad", ["2", "2d", "h", "abc", "-1h"])
def test_parse_duration_rejects(bad):
    with pytest.raises(ValueError):
        parse_duration(bad)


def test_default_pod_name_layout():
    now = datetime(2026, 9, 30, 12, 0)
    assert default_pod_name("alice_", "eval", now, lambda: "abcdef") == "alice_eval_20260930_abcdef"
    assert default_pod_name("alice_", None, now, lambda: "abcdef") == "alice_20260930_abcdef"
    assert default_pod_name("", None, now, lambda: "abcdef") == "pod_20260930_abcdef"


def test_apply_prefix_is_idempotent():
    assert apply_prefix("job1", "alice_") == "alice_job1"
    assert apply_prefix("alice_job1", "alice_") == "alice_job1"
    assert apply_prefix("job1", "") == "job1"


def test_parse_env():
    assert parse_env(["A=1", "B=x=y", "T={{ RUNPOD_SECRET_HF }}"]) == {"A": "1", "B": "x=y", "T": "{{ RUNPOD_SECRET_HF }}"}
    with pytest.raises(ValueError, match="KEY=VALUE"):
        parse_env(["NOEQUALS"])


def args(**over):
    base = dict(
        name="n", template_id=None, image=None, image_override=False, gpu_type=None, gpu_count=None,
        cloud_type=None, container_disk_gb=None, volume_gb=None, network_volume_id=None, env=None,
        docker_start_cmd=None, ports=None, no_public_ip=False,
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_payload_with_template_only():
    cfg = PodDefaults(template_id="tpl", image="img", gpu_type="NVIDIA H200", container_disk_gb=50)
    p = build_payload(cfg, args())
    assert p["templateId"] == "tpl"
    assert "imageName" not in p
    assert p["gpuTypeIds"] == ["NVIDIA H200"]
    assert p["gpuCount"] == 1
    assert p["cloudType"] == "SECURE"
    assert p["containerDiskInGb"] == 50
    assert p["supportPublicIp"] is True
    assert "volumeInGb" not in p and "networkVolumeId" not in p and "env" not in p


def test_payload_with_image_only():
    cfg = PodDefaults(template_id="", image="img:tag")
    p = build_payload(cfg, args())
    assert p["imageName"] == "img:tag" and "templateId" not in p


def test_payload_needs_template_or_image():
    with pytest.raises(ValueError, match="--template-id or --image"):
        build_payload(PodDefaults(), args())


def test_payload_image_override_sends_both():
    cfg = PodDefaults(template_id="tpl", image="")
    p = build_payload(cfg, args(image="other:tag", image_override=True))
    assert p["templateId"] == "tpl" and p["imageName"] == "other:tag"


def test_payload_image_with_template_but_no_override_is_an_error():
    cfg = PodDefaults(template_id="tpl")
    with pytest.raises(ValueError, match="--image-override"):
        build_payload(cfg, args(image="other:tag"))


def test_payload_cli_overrides_config_and_optional_fields():
    cfg = PodDefaults(template_id="tpl", volume_gb=100, network_volume_id="vol", ports="22/tcp,8888/http")
    p = build_payload(cfg, args(gpu_type="NVIDIA RTX A4000", gpu_count=2, cloud_type="COMMUNITY",
                                container_disk_gb=20, env=["A=1"], docker_start_cmd="bash -c sleep", no_public_ip=True))
    assert p["gpuTypeIds"] == ["NVIDIA RTX A4000"] and p["gpuCount"] == 2 and p["cloudType"] == "COMMUNITY"
    assert p["containerDiskInGb"] == 20
    assert p["volumeInGb"] == 100 and p["volumeMountPath"] == "/workspace"
    assert p["networkVolumeId"] == "vol"
    assert p["env"] == {"A": "1"}
    assert p["dockerStartCmd"] == ["bash", "-c", "sleep"]
    assert p["ports"] == ["22/tcp", "8888/http"]
    assert p["supportPublicIp"] is False


# --- CLI ---------------------------------------------------------------------

CREATED = {"id": "newpod", "name": "alice_x", "costPerHr": 3.99, "machine": {"gpuTypeId": "NVIDIA H200", "dataCenterId": "EU-RO-1"}}


def cfg_with_template(prefix="alice_"):
    c = Config()
    c.pod.template_id = "tpl"
    c.pod.name_prefix = prefix
    return c


def test_start_creates_pod_and_prints_id(capsys):
    t = FakeTransport().queue(201, CREATED)
    assert run(["start", "--name", "x"], t, config=cfg_with_template()) == 0
    out = capsys.readouterr().out
    assert out.rstrip().splitlines()[-1] == "pod_id=newpod"
    assert t.last.body["name"] == "alice_x"
    assert t.last.body["templateId"] == "tpl"


def test_start_json_prints_response(capsys):
    t = FakeTransport().queue(201, CREATED)
    assert run(["start", "--json"], t, config=cfg_with_template()) == 0
    assert json.loads(capsys.readouterr().out)["id"] == "newpod"


def test_start_without_template_or_image_is_usage_error(capsys):
    assert run(["start"], FakeTransport(), config=Config()) == 2
    assert "--template-id or --image" in capsys.readouterr().err


def test_start_retries_until_capacity(capsys, monkeypatch):
    slept = []
    monkeypatch.setattr("runpod_tools.commands.start.time.sleep", slept.append)
    t = (FakeTransport()
         .queue(400, {"error": "no instances available"})
         .queue(400, {"error": "no instances available"})
         .queue(201, CREATED))
    assert run(["start", "--retry", "7", "--max-retries", "5"], t, config=cfg_with_template()) == 0
    assert slept == [7, 7]
    assert "pod_id=newpod" in capsys.readouterr().out


def test_start_gives_up_after_max_retries(capsys, monkeypatch):
    monkeypatch.setattr("runpod_tools.commands.start.time.sleep", lambda s: None)
    t = (FakeTransport().queue(400, {"error": "no instances available"})
         .queue(400, {"error": "no instances available"}))
    assert run(["start", "--retry", "1", "--max-retries", "2"], t, config=cfg_with_template()) == 1
    assert "after 2 attempts" in capsys.readouterr().err


def test_start_delay_then_create(capsys, monkeypatch):
    slept = []
    monkeypatch.setattr("runpod_tools.commands.start.time.sleep", slept.append)
    t = FakeTransport().queue(201, CREATED)
    assert run(["start", "--delay", "10s"], t, config=cfg_with_template()) == 0
    assert sum(slept) == pytest.approx(10)


def test_start_delay_interrupted_creates_nothing(capsys, monkeypatch):
    def interrupted(_s):
        raise KeyboardInterrupt

    monkeypatch.setattr("runpod_tools.commands.start.time.sleep", interrupted)
    t = FakeTransport().queue(201, CREATED)
    assert run(["start", "--delay", "1h"], t, config=cfg_with_template()) == 130
    assert t.calls == []


def test_start_wait_blocks_until_ssh(capsys, monkeypatch):
    monkeypatch.setattr("runpod_tools.commands.start.time.sleep", lambda s: None)
    monkeypatch.setattr("runpod_tools.commands.start.keyscan", lambda ep, **kw: True)
    t = (FakeTransport().queue(201, CREATED)
         .queue(*graphql_ok({"myself": {"pods": [pod(id="newpod", runtime=None)]}}))
         .queue(*graphql_ok({"myself": {"pods": [pod(id="newpod")]}})))
    assert run(["start", "--wait"], t, config=cfg_with_template()) == 0
    out = capsys.readouterr().out
    assert "1.2.3.4:40022" in out
    assert out.rstrip().splitlines()[-1] == "pod_id=newpod"


# --- review fixes ---------------------------------------------------------------

def test_start_wait_timeout_still_prints_pod_id_and_exits_1(capsys, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    ticks = iter(range(0, 100000, 100))
    monkeypatch.setattr("time.monotonic", lambda: next(ticks))
    monkeypatch.setattr("runpod_tools.commands.start.keyscan", lambda ep, **kw: True)
    t = FakeTransport().queue(201, CREATED)
    for _ in range(30):
        t.queue(*graphql_ok({"myself": {"pods": [pod(id="newpod", runtime=None)]}}))
    assert run(["start", "--wait", "--wait-timeout", "300"], t, config=cfg_with_template()) == 1
    out, err = capsys.readouterr()
    assert out.rstrip().splitlines()[-1] == "pod_id=newpod"
    assert "newpod" in err and "no reachable SSH" in err


def test_start_checks_api_key_before_delay(capsys, monkeypatch):
    slept = []
    monkeypatch.setattr("runpod_tools.commands.start.time.sleep", slept.append)

    def factory(**_):
        from runpod_tools.api import client_from_env
        return client_from_env(env={})

    from runpod_tools.cli import main
    code = main(["start", "--delay", "1h"], client_factory=factory, config_loader=cfg_with_template,
                stdin_isatty=lambda: False, env={})
    assert code == 2
    assert slept == []


def test_start_does_not_retry_auth_errors(capsys, monkeypatch):
    monkeypatch.setattr("runpod_tools.commands.start.time.sleep", lambda s: None)
    t = FakeTransport().queue(401, {"error": "Unauthorized"})
    assert run(["start", "--retry", "1", "--max-retries", "5"], t, config=cfg_with_template()) == 1
    assert len(t.calls) == 1


def test_start_5xx_checks_for_orphan_before_retrying(capsys, monkeypatch):
    monkeypatch.setattr("runpod_tools.commands.start.time.sleep", lambda s: None)
    t = (FakeTransport().queue(504, {"error": "gateway timeout"})
         .queue(*graphql_ok({"myself": {"pods": [pod(id="orphan", name="alice_x")]}})))
    assert run(["start", "--name", "x", "--retry", "1", "--max-retries", "5"], t, config=cfg_with_template()) == 0
    out = capsys.readouterr().out
    assert out.rstrip().splitlines()[-1] == "pod_id=orphan"
    assert len(t.calls) == 2  # create, list; no second create


def test_start_json_redacts_env_in_response(capsys):
    t = FakeTransport().queue(201, {**CREATED, "env": {"HF_TOKEN": "resolved-secret-value", "DEBUG": "1"}})
    assert run(["start", "--json"], t, config=cfg_with_template()) == 0
    out = capsys.readouterr().out
    assert "resolved-secret-value" not in out
    assert json.loads(out)["env"]["DEBUG"] == "1"


def test_docker_start_cmd_is_sent_as_list():
    cfg = PodDefaults(template_id="tpl")
    p = build_payload(cfg, args(docker_start_cmd="bash -c 'sleep infinity'"))
    assert p["dockerStartCmd"] == ["bash", "-c", "sleep infinity"]
    assert "dockerArgs" not in p
