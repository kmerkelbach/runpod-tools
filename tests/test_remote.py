"""rpt wait / ssh / run."""

import os
import subprocess
from pathlib import Path

from runpod_tools.config import Config
from tests.fakes import FakeTransport, graphql_ok, pod
from tests.test_cli import run

READY = pod(id="p1", name="a_pod")
NOT_READY = pod(id="p1", name="a_pod", runtime=None)


def listing(*pods):
    return graphql_ok({"myself": {"pods": list(pods)}})


class Recorder:
    def __init__(self, returncode=0, stdout=""):
        self.calls = []
        self.returncode = returncode
        self.stdout = stdout

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw))
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, "")


def cfg():
    c = Config()
    c.ssh.key = Path("/k/id")
    return c


def test_wait_prints_endpoint_when_ready(capsys, monkeypatch):
    monkeypatch.setattr("runpod_tools.commands.wait.keyscan", lambda ep, **kw: True)
    monkeypatch.setattr("time.sleep", lambda s: None)
    t = FakeTransport().queue(*listing(NOT_READY)).queue(*listing(NOT_READY)).queue(*listing(READY))
    assert run(["wait", "--pod", "p1"], t, config=cfg()) == 0
    assert capsys.readouterr().out.strip() == "1.2.3.4 40022"


def test_wait_timeout_exits_1(capsys, monkeypatch):
    monkeypatch.setattr("runpod_tools.commands.wait.keyscan", lambda ep, **kw: True)
    monkeypatch.setattr("time.sleep", lambda s: None)
    ticks = iter(range(0, 100000, 10))
    monkeypatch.setattr("time.monotonic", lambda: next(ticks))
    t = FakeTransport()
    for _ in range(20):
        t.queue(*listing(NOT_READY))
    assert run(["wait", "--pod", "p1", "--timeout", "30"], t, config=cfg()) == 1
    assert "no reachable SSH" in capsys.readouterr().err


def test_ssh_print_shows_command(capsys):
    t = FakeTransport().queue(*listing(READY))
    assert run(["ssh", "--print"], t, config=cfg()) == 0
    assert capsys.readouterr().out.strip() == "ssh root@1.2.3.4 -p 40022 -i /k/id"


def test_ssh_execs_interactive_session():
    execs = []
    t = FakeTransport().queue(*listing(READY))
    assert run(["ssh"], t, config=cfg(), isatty=True, execvp=lambda prog, argv: execs.append((prog, argv))) == 0
    prog, argv = execs[0]
    assert prog == "ssh"
    assert argv[:5] == ["ssh", "-p", "40022", "-i", "/k/id"]
    assert argv[-1] == "root@1.2.3.4"
    assert "BatchMode=yes" not in argv  # interactive: allow prompts


def test_ssh_no_endpoint_exits_1(capsys):
    t = FakeTransport().queue(*listing(NOT_READY))
    assert run(["ssh", "--print"], t, config=cfg()) == 1
    assert "no TCP endpoint" in capsys.readouterr().err


def test_run_sources_env_and_returns_remote_exit():
    rec = Recorder(returncode=3)
    t = FakeTransport().queue(*listing(READY))
    assert run(["run", "--", "nvidia-smi", "-L"], t, config=cfg(), runner=rec) == 3
    argv, kw = rec.calls[0]
    assert argv[:5] == ["ssh", "-p", "40022", "-i", "/k/id"]
    assert argv[-2] == "root@1.2.3.4"
    assert argv[-1].startswith("set -a; [ -f /etc/rp_environment ]")
    assert argv[-1].endswith("nvidia-smi -L")


def test_run_raw_skips_env_prefix():
    rec = Recorder()
    t = FakeTransport().queue(*listing(READY))
    assert run(["run", "--raw", "--", "echo", "hi"], t, config=cfg(), runner=rec) == 0
    assert rec.calls[0][0][-1] == "echo hi"


def test_run_background_wraps_with_nohup_and_log():
    rec = Recorder()
    t = FakeTransport().queue(*listing(READY))
    assert run(["run", "--background", "job1", "--", "python", "train.py", "--x", "1"], t, config=cfg(), runner=rec) == 0
    remote = rec.calls[0][0][-1]
    assert "mkdir -p /workspace/rpt" in remote
    assert "nohup bash -c 'python train.py --x 1' > /workspace/rpt/job1.log 2>&1 < /dev/null &" in remote
    assert "echo pid=$!" in remote


def test_run_background_wrapper_survives_a_real_shell(tmp_path, monkeypatch):
    """Quoting is verified by executing the wrapper in bash, not by eyeballing escapes."""
    import time
    from runpod_tools.commands.ssh import background_wrapper

    monkeypatch.setattr("runpod_tools.commands.ssh.BACKGROUND_DIR", str(tmp_path))
    wrapper = background_wrapper("echo \"it's\" 'a \"test\"' $((1+1))", "j")
    out = subprocess.run(["bash", "-c", wrapper], capture_output=True, text=True, check=True).stdout
    assert out.startswith("pid=")
    for _ in range(50):
        if (tmp_path / "j.log").exists() and (tmp_path / "j.log").read_text().strip():
            break
        time.sleep(0.05)
    assert (tmp_path / "j.log").read_text().strip() == "it's a \"test\" 2"


def test_run_background_wrapper_releases_the_session_streams(tmp_path, monkeypatch):
    """sshd keeps a session open while anything holds its stdout; the detached job must not."""
    from runpod_tools.commands.ssh import background_wrapper

    monkeypatch.setattr("runpod_tools.commands.ssh.BACKGROUND_DIR", str(tmp_path))
    wrapper = background_wrapper("sleep 20", "j")
    # With a pipe for stdout, run() returns only once every holder has closed it.
    out = subprocess.run(["bash", "-c", wrapper], capture_output=True, text=True, check=True, timeout=5).stdout
    pid = int(out.strip().removeprefix("pid="))
    os.kill(pid, 15)


def test_run_background_wrapper_does_not_launch_without_its_log_directory(tmp_path, monkeypatch):
    from runpod_tools.commands.ssh import background_wrapper

    blocker = tmp_path / "file"
    blocker.write_text("")
    monkeypatch.setattr("runpod_tools.commands.ssh.BACKGROUND_DIR", str(blocker / "sub"))
    result = subprocess.run(["bash", "-c", background_wrapper("echo hi", "j")], capture_output=True, text=True, timeout=5)
    assert result.returncode != 0 and "pid=" not in result.stdout


def test_run_all_iterates_pods_and_reports_worst_exit(capsys):
    codes = iter([0, 2])

    def runner(argv, **kw):
        return subprocess.CompletedProcess(argv, next(codes), "", "")

    t = FakeTransport().queue(*listing(READY, pod(id="p2", name="b_pod")))
    assert run(["run", "--all", "--", "true"], t, config=cfg(), runner=runner) == 2
    out = capsys.readouterr().out
    assert "a_pod" in out and "b_pod" in out


def test_run_requires_a_command(capsys):
    t = FakeTransport()
    assert run(["run"], t, config=cfg()) == 2


def test_wait_no_endpoint_fails_fast_with_reason(capsys, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    p = pod(id="p1", runtime={"uptimeInSeconds": 5, "ports": [{"ip": "1.2.3.4", "privatePort": 80, "publicPort": 1, "type": "http"}]})
    t = FakeTransport().queue(*listing(p)).queue(*listing(p))  # selection, then the first poll
    assert run(["wait", "--pod", "p1"], t, config=cfg()) == 1
    assert "no TCP endpoint" in capsys.readouterr().err


def test_run_all_skips_pod_without_endpoint_and_reports(capsys):
    rec = Recorder()
    no_ep = pod(id="p2", name="b_pod", runtime={"uptimeInSeconds": 1, "ports": None})
    t = FakeTransport().queue(*listing(READY, no_ep))
    assert run(["run", "--all", "--", "true"], t, config=cfg(), runner=rec) == 1
    assert len(rec.calls) == 1
    assert "b_pod" in capsys.readouterr().err


def test_ssh_interactive_off_tty_is_refused(capsys):
    t = FakeTransport().queue(*listing(READY))
    assert run(["ssh"], t, config=cfg(), isatty=False) == 2
    assert "--print" in capsys.readouterr().err


def test_ssh_interactive_on_tty_execs():
    execs = []
    t = FakeTransport().queue(*listing(READY))
    assert run(["ssh"], t, config=cfg(), isatty=True, execvp=lambda prog, argv: execs.append(prog)) == 0
    assert execs == ["ssh"]
