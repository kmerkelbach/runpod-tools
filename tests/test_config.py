from pathlib import Path

import pytest

from runpod_tools.config import Config, ConfigError, find_config_file, load_config


def test_defaults_without_a_file(tmp_path):
    cfg = load_config(start=tmp_path, env={})
    assert cfg.source is None
    assert cfg.pod.template_id == ""
    assert cfg.pod.gpu_type == "NVIDIA H200"
    assert cfg.pod.gpu_count == 1
    assert cfg.pod.cloud_type == "SECURE"
    assert cfg.pod.support_public_ip is True
    assert cfg.pod.name_prefix == ""
    assert cfg.ssh.key == Path.home() / ".ssh" / "id_ed25519"
    assert cfg.ssh.user == "root"
    assert cfg.sync.remote_dest == ""
    assert cfg.sync.pull_max_size == "10m"
    assert cfg.sync.fetch_dirs == ["results", "logs"]


def test_discovery_walks_up_from_start(tmp_path):
    (tmp_path / "runpod-tools.toml").write_text('[pod]\ntemplate_id = "tpl"\n')
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert find_config_file(nested) == tmp_path / "runpod-tools.toml"
    cfg = load_config(start=nested, env={})
    assert cfg.pod.template_id == "tpl"
    assert cfg.source == tmp_path / "runpod-tools.toml"


def test_explicit_env_path_wins_over_discovery(tmp_path):
    (tmp_path / "runpod-tools.toml").write_text('[pod]\ntemplate_id = "near"\n')
    other = tmp_path / "elsewhere.toml"
    other.write_text('[pod]\ntemplate_id = "far"\n')
    cfg = load_config(start=tmp_path, env={"RUNPOD_TOOLS_CONFIG": str(other)})
    assert cfg.pod.template_id == "far"
    assert cfg.source == other


def test_env_overrides_file_values(tmp_path):
    (tmp_path / "runpod-tools.toml").write_text(
        '[pod]\ntemplate_id = "tpl"\nname_prefix = "file_"\n[ssh]\nkey = "/f/key"\n'
    )
    env = {
        "RUNPOD_TEMPLATE_ID": "envtpl",
        "RUNPOD_POD_NAME_PREFIX": "env_",
        "RUNPOD_SSH_KEY": "/e/key",
    }
    cfg = load_config(start=tmp_path, env=env)
    assert cfg.pod.template_id == "envtpl"
    assert cfg.pod.name_prefix == "env_"
    assert cfg.ssh.key == Path("/e/key")


def test_ssh_key_expands_tilde(tmp_path):
    (tmp_path / "runpod-tools.toml").write_text('[ssh]\nkey = "~/.ssh/other"\n')
    cfg = load_config(start=tmp_path, env={})
    assert cfg.ssh.key == Path.home() / ".ssh" / "other"


def test_sync_local_root_is_resolved_against_config_dir(tmp_path):
    (tmp_path / "runpod-tools.toml").write_text(
        '[sync]\nlocal_root = "src"\nremote_dest = "/workspace/p/"\n'
    )
    cfg = load_config(start=tmp_path, env={})
    assert cfg.sync.local_root == (tmp_path / "src").resolve()
    assert cfg.sync.remote_dest == "/workspace/p/"


def test_sync_fetch_excludes_table(tmp_path):
    (tmp_path / "runpod-tools.toml").write_text(
        '[sync]\nremote_dest = "/w/"\n[sync.fetch_excludes]\nresults = ["*.pt"]\n'
    )
    cfg = load_config(start=tmp_path, env={})
    assert cfg.sync.fetch_excludes == {"results": ["*.pt"]}


def test_unknown_table_is_an_error(tmp_path):
    (tmp_path / "runpod-tools.toml").write_text("[bogus]\nx = 1\n")
    with pytest.raises(ConfigError, match="bogus"):
        load_config(start=tmp_path, env={})


def test_unknown_key_is_an_error(tmp_path):
    (tmp_path / "runpod-tools.toml").write_text("[pod]\ngpu = 1\n")
    with pytest.raises(ConfigError, match="pod.gpu"):
        load_config(start=tmp_path, env={})


def test_wrong_type_is_an_error(tmp_path):
    (tmp_path / "runpod-tools.toml").write_text('[pod]\ngpu_count = "two"\n')
    with pytest.raises(ConfigError, match="gpu_count"):
        load_config(start=tmp_path, env={})


def test_config_is_a_plain_dataclass():
    assert Config.__dataclass_fields__.keys() >= {"pod", "ssh", "sync", "source"}
