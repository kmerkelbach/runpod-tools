"""rpt init, plus checks that the shipped examples and docs stay honest."""

import subprocess
from pathlib import Path

from runpod_tools.cli import SUBCOMMANDS
from runpod_tools.commands.init import EXAMPLE_CONFIG
from runpod_tools.config import load_config
from tests.fakes import FakeTransport
from tests.test_cli import run

ROOT = Path(__file__).resolve().parents[1]


def test_init_writes_example_config(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert run(["init"], FakeTransport()) == 0
    written = tmp_path / "runpod-tools.toml"
    assert written.read_text() == EXAMPLE_CONFIG.read_text()
    assert "runpod-tools.toml" in capsys.readouterr().out


def test_init_refuses_to_overwrite_without_force(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runpod-tools.toml").write_text("[pod]\n")
    assert run(["init"], FakeTransport()) == 2
    assert "--force" in capsys.readouterr().err
    assert (tmp_path / "runpod-tools.toml").read_text() == "[pod]\n"
    assert run(["init", "--force"], FakeTransport()) == 0
    assert (tmp_path / "runpod-tools.toml").read_text() == EXAMPLE_CONFIG.read_text()


def test_example_config_loads_and_has_no_project_specifics(tmp_path):
    text = EXAMPLE_CONFIG.read_text()
    (tmp_path / "runpod-tools.toml").write_text(text)
    cfg = load_config(start=tmp_path, env={})
    assert cfg.pod.template_id == ""  # user must fill it in; nothing shipped
    assert cfg.pod.name_prefix == ""
    assert cfg.sync.remote_dest.startswith("/workspace/")


def test_readme_documents_every_subcommand():
    readme = (ROOT / "README.md").read_text()
    for name in SUBCOMMANDS:
        assert f"rpt {name}" in readme, f"README lacks `rpt {name}`"


def test_agents_md_exists_and_claude_md_points_to_it():
    assert (ROOT / "AGENTS.md").is_file()
    claude = ROOT / "CLAUDE.md"
    assert claude.is_symlink() and claude.resolve() == (ROOT / "AGENTS.md").resolve()


def test_shell_examples_parse():
    for script in ("examples/pod-start.sh", "examples/killswitch.sh"):
        subprocess.run(["bash", "-n", str(ROOT / script)], check=True)


def test_example_config_is_tracked_by_git_and_packaged():
    tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "--error-unmatch", "examples/runpod-tools.toml"],
                             capture_output=True, text=True)
    assert tracked.returncode == 0, tracked.stderr
    from importlib.resources import files
    assert files("runpod_tools").joinpath("example-config.toml").read_text() == EXAMPLE_CONFIG.read_text()


def test_agents_md_cites_only_real_flags():
    text = (ROOT / "AGENTS.md").read_text()
    assert "template show --dry-run" not in text


def _fake_environ(tmp_path, pairs):
    src = tmp_path / "environ"
    src.write_bytes(b"".join(f"{k}={v}".encode() + b"\0" for k, v in pairs))
    return src


def test_pod_start_env_writer_round_trips_awkward_values(tmp_path):
    src = _fake_environ(tmp_path, [
        ("PATH", "/usr/bin"), ("HOME", "/root"),
        ("PLAIN", "hello"),
        ("QUOTED", "it's a 'test'"),
        ("MULTI", "line1\nline2 && touch /tmp/should_not_run"),
        ("DOLLAR", "$(echo injected) `id`"),
        ("BAD NAME", "x"),
    ])
    out = tmp_path / "rp_environment"
    subprocess.run(["bash", str(ROOT / "examples/pod-start.sh"), "--env-only"], check=True, capture_output=True,
                   env={"PATH": "/usr/bin:/bin", "POD_START_ENVIRON": str(src), "POD_START_ENV_FILE": str(out)})
    text = out.read_text()
    assert "PATH=" not in text and "HOME=" not in text and "BAD NAME" not in text
    probe = subprocess.run(
        ["bash", "-c", f". {out}; printf '%s\\n' \"$PLAIN\" \"$QUOTED\" \"$DOLLAR\"; printf '%s' \"$MULTI\" | wc -l"],
        capture_output=True, text=True, check=True).stdout.splitlines()
    assert probe[0] == "hello"
    assert probe[1] == "it's a 'test'"
    assert probe[2] == "$(echo injected) `id`"
    assert probe[3].strip() == "1"
    assert not (tmp_path / "should_not_run").exists()


def test_killswitch_reads_key_from_file_and_calls_api(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake_curl = bindir / "curl"
    fake_curl.write_text('#!/bin/bash\nprintf "%s\\n" "$@" > "$CURL_LOG"\n')
    fake_curl.chmod(0o755)
    keyfile = tmp_path / ".rp_key"
    keyfile.write_text("file-key\n")
    log = tmp_path / "curl.log"
    subprocess.run(["bash", str(ROOT / "examples/killswitch.sh"), "podX", "0", "terminate"], check=True,
                   capture_output=True,
                   env={"PATH": f"{bindir}:/usr/bin:/bin", "CURL_LOG": str(log), "KILLSWITCH_KEY_FILE": str(keyfile)})
    args = log.read_text().splitlines()
    assert "DELETE" in args and any(a.endswith("/pods/podX") for a in args)
    assert "Authorization: Bearer file-key" in args


def test_docs_never_put_the_api_key_on_a_command_line():
    for rel in ("docs/fleet-pattern.md", "examples/killswitch.sh", "README.md", "AGENTS.md", "docs/gotchas.md"):
        assert "RUNPOD_API_KEY=$RUNPOD_API_KEY" not in (ROOT / rel).read_text(), rel
