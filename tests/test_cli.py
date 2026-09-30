import json

import pytest

from runpod_tools.api import RunpodClient
from runpod_tools.cli import SUBCOMMANDS, main
from runpod_tools.config import Config
from tests.fakes import FakeTransport, graphql_ok, pod


def run(argv, transport=None, isatty=False, config=None, env=None, runner=None, execvp=None):
    """Invoke the CLI with a fake client; returns the exit code."""
    transport = transport or FakeTransport()

    def factory(**_):
        return RunpodClient("k", transport=transport)

    def forbidden(*a, **k):
        raise AssertionError(f"unexpected subprocess: {a} {k}")

    return main(
        argv,
        client_factory=factory,
        config_loader=lambda: config or Config(),
        stdin_isatty=lambda: isatty,
        env=env if env is not None else {"RUNPOD_API_KEY": "k"},
        runner=runner or forbidden,
        execvp=execvp or forbidden,
    )


@pytest.mark.parametrize("name", SUBCOMMANDS)
def test_every_subcommand_has_help(name, capsys):
    with pytest.raises(SystemExit) as exc:
        main([*name.split(), "--help"])
    assert exc.value.code == 0
    assert "usage" in capsys.readouterr().out.lower()


def test_top_level_help_lists_every_subcommand(capsys):
    with pytest.raises(SystemExit):
        main(["--help"])
    out = capsys.readouterr().out
    for name in SUBCOMMANDS:
        assert name.split()[0] in out


def test_missing_api_key_exits_2_with_hint(capsys):
    def factory(**_):
        from runpod_tools.api import client_from_env
        return client_from_env(env={})

    code = main(["pods"], client_factory=factory, config_loader=Config, stdin_isatty=lambda: False, env={})
    assert code == 2
    assert "RUNPOD_API_KEY" in capsys.readouterr().err


def test_pods_json_round_trips(capsys):
    t = FakeTransport().queue(*graphql_ok({"myself": {"pods": [pod()]}}))
    assert run(["pods", "--json"], t) == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["id"] == "abc123"
    assert data[0]["ssh"] == {"ip": "1.2.3.4", "port": 40022}


def test_pods_table_shows_ssh_and_rsync_lines(capsys):
    t = FakeTransport().queue(*graphql_ok({"myself": {"pods": [pod()]}}))
    assert run(["pods"], t) == 0
    out = capsys.readouterr().out
    assert "abc123" in out and "my_pod" in out and "H200 SXM" in out
    assert "ssh root@1.2.3.4 -p 40022" in out
    assert "rsync" in out
    assert "$/hr" in out


def test_pods_without_endpoint_shows_note(capsys):
    t = FakeTransport().queue(*graphql_ok({"myself": {"pods": [pod(runtime={"uptimeInSeconds": 3, "ports": None})]}}))
    assert run(["pods"], t) == 0
    assert "no TCP endpoint" in capsys.readouterr().out


def test_pods_empty(capsys):
    t = FakeTransport().queue(*graphql_ok({"myself": {"pods": []}}))
    assert run(["pods"], t) == 0
    assert "No pods" in capsys.readouterr().out


def test_pods_api_error_exits_1(capsys):
    t = FakeTransport().queue(200, {"errors": [{"message": "Unauthorized"}]})
    assert run(["pods"], t) == 1
    assert "Unauthorized" in capsys.readouterr().err


GPU = {"id": "NVIDIA H200", "displayName": "H200 SXM", "memoryInGb": 141, "secureCloud": True,
       "communityCloud": False, "lowestPrice": {"minimumBidPrice": 1.0, "uninterruptablePrice": 3.99}}
CHEAP = {"id": "NVIDIA RTX A4000", "displayName": "RTX A4000", "memoryInGb": 16, "secureCloud": False,
         "communityCloud": True, "lowestPrice": {"minimumBidPrice": 0.1, "uninterruptablePrice": None}}


def test_gpus_table_sorted_by_vram_with_price(capsys):
    t = FakeTransport().queue(*graphql_ok({"gpuTypes": [GPU, CHEAP]}))
    assert run(["gpus"], t) == 0
    out = capsys.readouterr().out
    assert out.index("RTX A4000") < out.index("H200 SXM")
    assert "$3.990/hr" in out and "spot" in out
    assert "NVIDIA H200" in out  # the id is what --gpu-type takes


def test_gpus_secure_filter_and_json(capsys):
    t = FakeTransport().queue(*graphql_ok({"gpuTypes": [GPU, CHEAP]}))
    assert run(["gpus", "--secure", "--json"], t) == 0
    data = json.loads(capsys.readouterr().out)
    assert [g["id"] for g in data] == ["NVIDIA H200"]


def test_volumes_table_and_json(capsys):
    vols = [{"id": "vol1", "name": "shared", "size": 4000, "dataCenterId": "US-NC-1"}]
    t = FakeTransport().queue(200, vols)
    assert run(["volumes"], t) == 0
    out = capsys.readouterr().out
    assert "vol1" in out and "shared" in out and "4000" in out and "US-NC-1" in out
    t = FakeTransport().queue(200, vols)
    assert run(["volumes", "--json"], t) == 0
    assert json.loads(capsys.readouterr().out)[0]["id"] == "vol1"


def test_no_volume_delete_command():
    assert not any("volume" in s and "delete" in s for s in SUBCOMMANDS)
    with pytest.raises(SystemExit) as exc:
        main(["volumes", "--delete", "vol1"])
    assert exc.value.code == 2
