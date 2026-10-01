import json

import pytest

from runpod_tools.commands.template import merge_env, parse_secret_ref, redact
from runpod_tools.config import Config
from tests.fakes import FakeTransport, graphql_ok
from tests.test_cli import run

TPL = {
    "id": "tpl1", "name": "my template", "imageName": "example/image:1", "ports": "22/tcp,8888/http",
    "containerDiskInGb": 50, "volumeInGb": 100, "volumeMountPath": "/workspace", "dockerArgs": "",
    "env": [{"key": "HF_TOKEN", "value": "{{ RUNPOD_SECRET_HF }}"}, {"key": "DEBUG", "value": "1"},
            {"key": "OTHER_API_KEY", "value": "literal-secret-value"}],
}


def templates(*tpls):
    return graphql_ok({"myself": {"podTemplates": list(tpls)}})


def cfg(template_id="tpl1"):
    c = Config()
    c.pod.template_id = template_id
    return c


# --- pure helpers -------------------------------------------------------------

def test_redact_keeps_placeholders_and_plain_values():
    assert redact("HF_TOKEN", "{{ RUNPOD_SECRET_HF }}") == "{{ RUNPOD_SECRET_HF }}"
    assert redact("DEBUG", "1") == "1"


@pytest.mark.parametrize("key", ["OTHER_API_KEY", "MY_TOKEN", "DB_PASSWORD", "some_secret", "AWS_SECRET_ACCESS_KEY"])
def test_redact_hides_literal_secrets(key):
    assert redact(key, "hunter2hunter2") == "<literal, 14 chars>"


def test_parse_secret_ref():
    assert parse_secret_ref("HF_TOKEN") == ("HF_TOKEN", "{{ RUNPOD_SECRET_HF_TOKEN }}")
    assert parse_secret_ref("WANDB_API_KEY=WandbKey") == ("WANDB_API_KEY", "{{ RUNPOD_SECRET_WandbKey }}")


def test_merge_env_with_null_current():
    assert merge_env(None, {"A": "1"}) == {"A": "1"}
    assert merge_env({"A": "0", "B": "b"}, {"A": "1"}) == {"A": "1", "B": "b"}


# --- show ---------------------------------------------------------------------

def test_show_redacts_literals_and_keeps_placeholders(capsys):
    t = FakeTransport().queue(*templates(TPL))
    assert run(["template", "show"], t, config=cfg()) == 0
    out = capsys.readouterr().out
    assert "{{ RUNPOD_SECRET_HF }}" in out
    assert "literal-secret-value" not in out and "<literal, 20 chars>" in out
    assert "example/image:1" in out and "22/tcp,8888/http" in out


def test_show_json_is_redacted_too(capsys):
    t = FakeTransport().queue(*templates(TPL))
    assert run(["template", "show", "--json"], t, config=cfg()) == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["env"]["OTHER_API_KEY"] == "<literal, 20 chars>"


def test_show_lists_all_when_no_template_configured(capsys):
    t = FakeTransport().queue(*templates(TPL, {**TPL, "id": "tpl2", "name": "second", "env": None}))
    assert run(["template", "show"], t, config=cfg(template_id="")) == 0
    out = capsys.readouterr().out
    assert "tpl1" in out and "tpl2" in out


def test_show_unknown_template_exits_1(capsys):
    t = FakeTransport().queue(*templates(TPL))
    assert run(["template", "show", "--template-id", "nope"], t, config=cfg()) == 1
    assert "not found" in capsys.readouterr().err


# --- env ----------------------------------------------------------------------

def test_env_secret_ref_reads_graphql_and_patches_rest(capsys):
    t = FakeTransport().queue(*templates(TPL)).queue(200, {})
    assert run(["template", "env", "--secret-ref", "WANDB_API_KEY=WandbKey", "-y"], t, config=cfg()) == 0
    patch = t.last
    assert patch.method == "PATCH" and patch.url.endswith("/templates/tpl1")
    assert patch.body["env"] == {
        "HF_TOKEN": "{{ RUNPOD_SECRET_HF }}", "DEBUG": "1", "OTHER_API_KEY": "literal-secret-value",
        "WANDB_API_KEY": "{{ RUNPOD_SECRET_WandbKey }}",
    }
    assert "imageName" not in patch.body
    out = capsys.readouterr().out
    assert "+ WANDB_API_KEY" in out and "literal-secret-value" not in out


def test_env_with_null_env_on_template(capsys):
    t = FakeTransport().queue(*templates({**TPL, "env": None})).queue(200, {})
    assert run(["template", "env", "--env", "A=1", "-y"], t, config=cfg()) == 0
    assert t.last.body["env"] == {"A": "1"}


def test_env_image_only_patches_image():
    t = FakeTransport().queue(*templates(TPL)).queue(200, {})
    assert run(["template", "env", "--image", "example/image:2", "-y"], t, config=cfg()) == 0
    assert t.last.body == {"imageName": "example/image:2"}


def test_env_dry_run_patches_nothing(capsys):
    t = FakeTransport().queue(*templates(TPL))
    assert run(["template", "env", "--env", "A=1", "--dry-run"], t, config=cfg()) == 0
    assert len(t.calls) == 1
    assert "dry run" in capsys.readouterr().out.lower()


def test_env_no_change_patches_nothing(capsys):
    t = FakeTransport().queue(*templates(TPL))
    assert run(["template", "env", "--env", "DEBUG=1", "-y"], t, config=cfg()) == 0
    assert len(t.calls) == 1
    assert "no changes" in capsys.readouterr().out.lower()


def test_env_nothing_to_do_is_usage_error(capsys):
    assert run(["template", "env"], FakeTransport(), config=cfg()) == 2


def test_env_warns_on_literal_secret_value(capsys):
    t = FakeTransport().queue(*templates(TPL)).queue(200, {})
    assert run(["template", "env", "--env", "NEW_TOKEN=abc", "-y"], t, config=cfg()) == 0
    assert "secret" in capsys.readouterr().err.lower()


def test_env_off_tty_without_yes_exits_2(capsys):
    t = FakeTransport().queue(*templates(TPL))
    assert run(["template", "env", "--env", "A=1"], t, config=cfg(), isatty=False) == 2
    assert len(t.calls) == 1


# --- ports / volume (GraphQL saveTemplate round-trip) --------------------------

def test_ports_add_appends_and_round_trips_full_template():
    t = FakeTransport().queue(*templates(TPL)).queue(*graphql_ok({"saveTemplate": {"id": "tpl1", "ports": "22/tcp,8888/http,6006/http"}}))
    assert run(["template", "ports", "--add", "6006/http", "-y"], t, config=cfg()) == 0
    inp = t.last.body["variables"]["input"]
    assert inp["ports"] == "22/tcp,8888/http,6006/http"
    assert inp["id"] == "tpl1" and inp["name"] == "my template" and inp["imageName"] == "example/image:1"
    assert inp["containerDiskInGb"] == 50 and inp["volumeInGb"] == 100 and inp["volumeMountPath"] == "/workspace"
    assert inp["env"] == TPL["env"]  # placeholders intact, as a key/value list


def test_ports_already_present_is_a_noop(capsys):
    t = FakeTransport().queue(*templates(TPL))
    assert run(["template", "ports", "--add", "22/tcp", "-y"], t, config=cfg()) == 0
    assert len(t.calls) == 1
    assert "already" in capsys.readouterr().out


def test_ports_set_replaces():
    t = FakeTransport().queue(*templates(TPL)).queue(*graphql_ok({"saveTemplate": {"id": "tpl1", "ports": "22/tcp"}}))
    assert run(["template", "ports", "--set", "22/tcp", "-y"], t, config=cfg()) == 0
    assert t.last.body["variables"]["input"]["ports"] == "22/tcp"


def test_volume_sets_size():
    t = FakeTransport().queue(*templates(TPL)).queue(*graphql_ok({"saveTemplate": {"id": "tpl1", "volumeInGb": 500}}))
    assert run(["template", "volume", "--gb", "500", "-y"], t, config=cfg()) == 0
    assert t.last.body["variables"]["input"]["volumeInGb"] == 500


def test_volume_same_size_is_a_noop():
    t = FakeTransport().queue(*templates(TPL))
    assert run(["template", "volume", "--gb", "100", "-y"], t, config=cfg()) == 0
    assert len(t.calls) == 1


def test_template_commands_need_a_template_id(capsys):
    assert run(["template", "ports", "--add", "1/tcp", "-y"], FakeTransport(), config=cfg(template_id="")) == 2
    assert "--template-id" in capsys.readouterr().err


FULL = {**TPL, "containerRegistryAuthId": "reg1", "readme": "# hi", "isPublic": False,
        "startSsh": True, "startJupyter": False, "isServerless": False}


def test_show_json_is_always_a_list(capsys):
    t = FakeTransport().queue(*templates(TPL))
    assert run(["template", "show", "--json"], t, config=cfg()) == 0
    data = json.loads(capsys.readouterr().out)
    assert isinstance(data, list) and data[0]["id"] == "tpl1"


def test_save_template_round_trips_extra_fields():
    t = FakeTransport().queue(*templates(FULL)).queue(*graphql_ok({"saveTemplate": {"id": "tpl1", "volumeInGb": 5}}))
    assert run(["template", "volume", "--gb", "5", "-y"], t, config=cfg()) == 0
    inp = t.last.body["variables"]["input"]
    assert inp["containerRegistryAuthId"] == "reg1"
    assert inp["readme"] == "# hi"
    assert inp["isPublic"] is False and inp["startSsh"] is True and inp["startJupyter"] is False
    assert inp["isServerless"] is False


def test_save_template_omits_extra_fields_when_absent():
    t = FakeTransport().queue(*templates(TPL)).queue(*graphql_ok({"saveTemplate": {"id": "tpl1", "volumeInGb": 5}}))
    assert run(["template", "volume", "--gb", "5", "-y"], t, config=cfg()) == 0
    inp = t.last.body["variables"]["input"]
    assert "containerRegistryAuthId" not in inp and "readme" not in inp


def test_template_read_query_requests_extra_fields():
    t = FakeTransport().queue(*templates(FULL))
    assert run(["template", "show", "--json"], t, config=cfg()) == 0
    q = t.calls[0].body["query"]
    for field in ("containerRegistryAuthId", "readme", "isPublic", "startSsh", "startJupyter", "isServerless"):
        assert field in q
