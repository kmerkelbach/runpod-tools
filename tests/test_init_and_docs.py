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


KILLSWITCH = ROOT / "examples/killswitch.sh"


def fake_curl(tmp_path, status="200"):
    """A curl stand-in that records its arguments and answers `-w '%{http_code}'`
    with the given status."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    curl = bindir / "curl"
    curl.write_text('#!/bin/bash\nprintf "%s\\n" "$@" > "$CURL_LOG"\nprintf "%s" "$CURL_STATUS"\n')
    curl.chmod(0o755)
    keyfile = tmp_path / ".rp_key"
    keyfile.write_text("file-key\n")
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "CURL_LOG": str(tmp_path / "curl.log"),
        "CURL_STATUS": status,
        "KILLSWITCH_KEY_FILE": str(keyfile),
        "KILLSWITCH_RETRY_SECONDS": "0",
        "KILLSWITCH_RETRIES": "2",
    }
    return env


def run_killswitch(env, action="terminate"):
    return subprocess.run(["bash", str(KILLSWITCH), "podX", "0", action], capture_output=True, text=True, env=env)


def test_killswitch_reads_key_from_file_and_calls_api(tmp_path):
    env = fake_curl(tmp_path)
    result = run_killswitch(env)
    assert result.returncode == 0, result.stdout + result.stderr
    args = Path(env["CURL_LOG"]).read_text().splitlines()
    assert "DELETE" in args and any(a.endswith("/pods/podX") for a in args)
    assert "Authorization: Bearer file-key" in args
    assert "HTTP 200" in result.stdout and "[killswitch] done" in result.stdout


def test_killswitch_ignores_the_pods_own_runpod_api_key(tmp_path):
    # Runpod injects a pod-scoped RUNPOD_API_KEY that cannot stop the pod, and
    # `rpt run` sources it; the delivered key must win over it.
    env = fake_curl(tmp_path)
    env["RUNPOD_API_KEY"] = "injected-pod-key"
    result = run_killswitch(env, action="stop")
    assert result.returncode == 0, result.stdout + result.stderr
    args = Path(env["CURL_LOG"]).read_text().splitlines()
    assert "POST" in args and any(a.endswith("/pods/podX/stop") for a in args)
    assert "Authorization: Bearer file-key" in args
    assert "injected-pod-key" not in Path(env["CURL_LOG"]).read_text()


def test_killswitch_without_a_delivered_key_refuses_to_arm(tmp_path):
    env = fake_curl(tmp_path)
    env["KILLSWITCH_KEY_FILE"] = str(tmp_path / "missing")
    env["RUNPOD_API_KEY"] = "injected-pod-key"
    result = run_killswitch(env)
    assert result.returncode != 0
    assert "RUNPOD_API_KEY cannot stop the pod" in result.stderr
    assert not Path(env["CURL_LOG"]).exists()


def test_killswitch_retries_and_reports_a_failed_api_call(tmp_path):
    env = fake_curl(tmp_path, status="403")
    result = run_killswitch(env, action="stop")
    assert result.returncode == 1
    assert result.stdout.count("HTTP 403") == 2
    assert "FAILED after 2 attempts" in result.stdout


def test_docs_never_put_the_api_key_on_a_command_line():
    for rel in ("docs/fleet-pattern.md", "examples/killswitch.sh", "README.md", "AGENTS.md", "docs/gotchas.md"):
        assert "RUNPOD_API_KEY=$RUNPOD_API_KEY" not in (ROOT / rel).read_text(), rel
