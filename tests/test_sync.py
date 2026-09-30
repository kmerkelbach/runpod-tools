"""rpt push / pull / fetch."""

import subprocess
from pathlib import Path

from runpod_tools.config import Config
from tests.fakes import FakeTransport, graphql_ok, pod
from tests.test_cli import run

READY = pod(id="p1", name="a_pod")
SECOND = pod(id="p2", name="b_pod", runtime={"uptimeInSeconds": 1, "ports": [
    {"ip": "5.6.7.8", "privatePort": 22, "publicPort": 50022, "type": "tcp"}]})


def listing(*pods):
    return graphql_ok({"myself": {"pods": list(pods)}})


class Recorder:
    """Records subprocess calls; per-argv[0] return codes and stdout."""

    def __init__(self, codes=None, git_status=""):
        self.calls = []
        self.codes = list(codes or [])
        self.git_status = git_status

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        if argv[0] == "git":
            return subprocess.CompletedProcess(argv, 0, self.git_status, "")
        code = self.codes.pop(0) if self.codes else 0
        return subprocess.CompletedProcess(argv, code, "", "")

    @property
    def rsyncs(self):
        return [c for c in self.calls if c[0] == "rsync"]


def cfg(tmp_path, remote="/workspace/proj/"):
    c = Config()
    c.ssh.key = Path("/k/id")
    c.sync.local_root = tmp_path
    c.sync.remote_dest = remote
    c.sync.push_excludes = [".git", "results"]
    c.sync.pull_excludes = ["wandb"]
    c.sync.fetch_dirs = ["results", "logs"]
    c.sync.fetch_excludes = {"results": ["*.pt"]}
    return c


def test_sync_commands_need_remote_dest(capsys, tmp_path):
    for cmd in (["push"], ["pull"], ["fetch"]):
        assert run(cmd, FakeTransport(), config=cfg(tmp_path, remote="")) == 2
        assert "rpt init" in capsys.readouterr().err


def test_push_builds_rsync_with_excludes(tmp_path):
    rec = Recorder()
    t = FakeTransport().queue(*listing(READY))
    assert run(["push"], t, config=cfg(tmp_path), runner=rec) == 0
    argv = rec.rsyncs[0]
    assert argv[0] == "rsync" and "-a" in argv and "--delete" not in argv
    assert [argv[i + 1] for i, a in enumerate(argv) if a == "--exclude"] == [".git", "results"]
    assert argv[-2] == f"{tmp_path}/"
    assert argv[-1] == "root@1.2.3.4:/workspace/proj/"


def test_push_delete_is_loud(tmp_path, capsys):
    rec = Recorder()
    t = FakeTransport().queue(*listing(READY))
    assert run(["push", "--delete"], t, config=cfg(tmp_path), runner=rec) == 0
    assert "--delete" in rec.rsyncs[0]
    assert "--delete" in capsys.readouterr().err


def test_push_all_targets_every_pod(tmp_path):
    rec = Recorder()
    t = FakeTransport().queue(*listing(READY, SECOND))
    assert run(["push", "--all"], t, config=cfg(tmp_path), runner=rec) == 0
    assert [c[-1] for c in rec.rsyncs] == ["root@1.2.3.4:/workspace/proj/", "root@5.6.7.8:/workspace/proj/"]


def test_push_dry_run(tmp_path):
    rec = Recorder()
    t = FakeTransport().queue(*listing(READY))
    assert run(["push", "--dry-run"], t, config=cfg(tmp_path), runner=rec) == 0
    assert "--dry-run" in rec.rsyncs[0]


def test_push_rsync_failure_exits_1(tmp_path, capsys):
    rec = Recorder(codes=[12])
    t = FakeTransport().queue(*listing(READY))
    assert run(["push"], t, config=cfg(tmp_path), runner=rec) == 1
    assert "rsync" in capsys.readouterr().err


def test_push_missing_local_root_exits_2(tmp_path, capsys):
    c = cfg(tmp_path)
    c.sync.local_root = tmp_path / "nope"
    assert run(["push"], FakeTransport().queue(*listing(READY)), config=c, runner=Recorder()) == 2
    assert "nope" in capsys.readouterr().err


def test_pull_refuses_dirty_git_tree(tmp_path, capsys):
    (tmp_path / ".git").mkdir()
    rec = Recorder(git_status=" M file.py\n")
    t = FakeTransport().queue(*listing(READY))
    assert run(["pull"], t, config=cfg(tmp_path), runner=rec) == 2
    assert "not clean" in capsys.readouterr().err
    assert rec.rsyncs == []


def test_pull_allow_dirty_overrides(tmp_path):
    (tmp_path / ".git").mkdir()
    rec = Recorder(git_status=" M file.py\n")
    t = FakeTransport().queue(*listing(READY))
    assert run(["pull", "--allow-dirty"], t, config=cfg(tmp_path), runner=rec) == 0
    assert len(rec.rsyncs) == 1


def test_pull_never_deletes_and_uses_combined_excludes_and_cap(tmp_path):
    rec = Recorder()
    t = FakeTransport().queue(*listing(READY))
    assert run(["pull"], t, config=cfg(tmp_path), runner=rec) == 0
    argv = rec.rsyncs[0]
    assert "--delete" not in argv
    assert "--max-size=10m" in argv
    assert [argv[i + 1] for i, a in enumerate(argv) if a == "--exclude"] == [".git", "results", "wandb"]
    assert argv[-2] == "root@1.2.3.4:/workspace/proj/"
    assert argv[-1] == f"{tmp_path}/"


def test_pull_without_git_dir_skips_check(tmp_path):
    rec = Recorder(git_status=" M x\n")
    t = FakeTransport().queue(*listing(READY))
    assert run(["pull"], t, config=cfg(tmp_path), runner=rec) == 0
    assert not any(c[0] == "git" for c in rec.calls)


def test_fetch_pulls_each_dir_with_its_excludes(tmp_path):
    rec = Recorder()
    t = FakeTransport().queue(*listing(READY))
    assert run(["fetch"], t, config=cfg(tmp_path), runner=rec) == 0
    assert len(rec.rsyncs) == 2
    results, logs = rec.rsyncs
    assert results[-2] == "root@1.2.3.4:/workspace/proj/results/" and results[-1] == f"{tmp_path / 'results'}/"
    assert [results[i + 1] for i, a in enumerate(results) if a == "--exclude"] == ["*.pt"]
    assert logs[-2].endswith("/logs/")
    assert "--max-size" not in " ".join(results)
    assert (tmp_path / "results").is_dir() and (tmp_path / "logs").is_dir()


def test_fetch_missing_remote_dir_is_skipped_not_fatal(tmp_path, capsys):
    rec = Recorder(codes=[23, 0])
    t = FakeTransport().queue(*listing(READY))
    assert run(["fetch"], t, config=cfg(tmp_path), runner=rec) == 0
    assert "skipp" in capsys.readouterr().out


def test_fetch_other_rsync_error_is_fatal(tmp_path):
    rec = Recorder(codes=[12])
    t = FakeTransport().queue(*listing(READY))
    assert run(["fetch"], t, config=cfg(tmp_path), runner=rec) == 1


def test_fetch_dir_override_and_dry_run(tmp_path):
    rec = Recorder()
    t = FakeTransport().queue(*listing(READY))
    assert run(["fetch", "--dir", "outputs", "--dry-run"], t, config=cfg(tmp_path), runner=rec) == 0
    assert len(rec.rsyncs) == 1 and rec.rsyncs[0][-2].endswith("/outputs/") and "--dry-run" in rec.rsyncs[0]


def test_sync_no_endpoint_exits_1(tmp_path, capsys):
    t = FakeTransport().queue(*listing(pod(id="p1", runtime=None)))
    assert run(["push"], t, config=cfg(tmp_path), runner=Recorder()) == 1
    assert "no TCP endpoint" in capsys.readouterr().err
