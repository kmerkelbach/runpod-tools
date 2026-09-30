from pathlib import Path

import pytest

from runpod_tools.pods import SshEndpoint
from runpod_tools.sshutil import (
    keyscan,
    remote_command,
    rsync_pull,
    rsync_push,
    rsync_template_string,
    ssh_base,
    ssh_command_string,
    ssh_target,
    wait_for_ssh,
)
from tests.fakes import pod

EP = SshEndpoint("1.2.3.4", 40022)
KEY = Path("/home/u/.ssh/id_ed25519")


def test_ssh_base_has_port_key_and_safe_host_key_policy():
    argv = ssh_base(EP, KEY)
    assert argv[:5] == ["ssh", "-p", "40022", "-i", str(KEY)]
    assert "StrictHostKeyChecking=accept-new" in argv
    assert "ConnectTimeout=15" in argv
    assert "BatchMode=yes" in argv


def test_ssh_base_fast_pins_cipher_and_disables_compression():
    argv = ssh_base(EP, KEY, fast=True)
    assert "-c" in argv and argv[argv.index("-c") + 1] == "aes128-gcm@openssh.com"
    assert "Compression=no" in argv


def test_ssh_base_extra_options_appended():
    argv = ssh_base(EP, KEY, extra=["-A"])
    assert argv[-1] == "-A"


def test_ssh_target():
    assert ssh_target(EP, "root") == "root@1.2.3.4"


def test_remote_command_sources_pod_environment_by_default():
    cmd = remote_command("nvidia-smi")
    assert cmd.startswith("set -a; [ -f /etc/rp_environment ] && . /etc/rp_environment; set +a; ")
    assert cmd.endswith("nvidia-smi")


def test_remote_command_raw():
    assert remote_command("nvidia-smi", source_env=False) == "nvidia-smi"


def test_rsync_push_shape_and_exclude_order():
    argv = rsync_push(Path("/local/proj"), EP, KEY, "/workspace/proj/", ["*.pyc", ".git"], user="root")
    assert argv[0] == "rsync"
    assert "-a" in argv and "--no-owner" in argv and "--no-group" in argv
    assert "-z" not in argv  # compressed payloads make -z a net loss
    ex = [argv[i + 1] for i, a in enumerate(argv) if a == "--exclude"]
    assert ex == ["*.pyc", ".git"]
    assert "--delete" not in argv
    e = argv[argv.index("-e") + 1]
    assert e.startswith("ssh -p 40022") and "aes128-gcm" in e and "Compression=no" in e
    assert argv[-2] == "/local/proj/"
    assert argv[-1] == "root@1.2.3.4:/workspace/proj/"


def test_rsync_push_delete_is_opt_in():
    argv = rsync_push(Path("/l"), EP, KEY, "/w/", [], user="root", delete=True)
    assert "--delete" in argv


def test_rsync_push_dry_run():
    argv = rsync_push(Path("/l"), EP, KEY, "/w/", [], user="root", dry_run=True)
    assert "--dry-run" in argv


def test_rsync_pull_never_deletes_and_caps_size():
    argv = rsync_pull(EP, KEY, "/workspace/proj/", Path("/local/proj"), ["wandb"], user="root", max_size="10m")
    assert "--delete" not in argv
    assert "--max-size=10m" in argv
    assert "-azK" in argv
    assert argv[-2] == "root@1.2.3.4:/workspace/proj/"
    assert argv[-1] == "/local/proj/"


def test_rsync_pull_no_max_size():
    argv = rsync_pull(EP, KEY, "/w/", Path("/l"), [], user="root", max_size=None)
    assert not any(a.startswith("--max-size") for a in argv)


def test_display_strings():
    assert ssh_command_string(EP, KEY, "root") == f"ssh root@1.2.3.4 -p 40022 -i {KEY}"
    assert "rsync -a --no-owner --no-group -e 'ssh -p 40022 -i" in rsync_template_string(EP, KEY, "root", "/workspace/")


def test_keyscan_appends_key_and_reports_success(tmp_path):
    known = tmp_path / "known_hosts"
    calls = []

    def runner(argv, **kw):
        calls.append(argv)

        class R:
            returncode = 0
            stdout = "[1.2.3.4]:40022 ssh-ed25519 AAAA\n"

        return R()

    assert keyscan(EP, known_hosts=known, runner=runner) is True
    assert calls[0][:3] == ["ssh-keyscan", "-p", "40022"]
    assert "[1.2.3.4]:40022" in known.read_text()


def test_keyscan_removes_stale_entry_first(tmp_path):
    known = tmp_path / "known_hosts"
    known.write_text("[1.2.3.4]:40022 ssh-ed25519 OLD\n")
    calls = []

    def runner(argv, **kw):
        calls.append(argv)

        class R:
            returncode = 0
            stdout = "[1.2.3.4]:40022 ssh-ed25519 NEW\n" if argv[0] == "ssh-keyscan" else ""

        return R()

    assert keyscan(EP, known_hosts=known, runner=runner)
    assert calls[0][:2] == ["ssh-keygen", "-R"]
    assert "NEW" in known.read_text()


def test_keyscan_reports_failure_when_sshd_not_up(tmp_path):
    def runner(argv, **kw):
        class R:
            returncode = 1
            stdout = ""

        return R()

    assert keyscan(EP, known_hosts=tmp_path / "kh", runner=runner) is False


class ClientStub:
    def __init__(self, sequence):
        self.sequence = list(sequence)

    def list_pods(self):
        return self.sequence.pop(0)


def test_wait_for_ssh_polls_until_endpoint_then_keyscans():
    no_ep = pod(id="p1", runtime=None)
    yes = pod(id="p1")
    client = ClientStub([[no_ep], [no_ep], [yes]])
    slept, scanned = [], []
    ep = wait_for_ssh(
        client, "p1", timeout=100, poll=5, sleep=slept.append,
        keyscan=lambda e: scanned.append(e) or True, clock=iter(range(0, 1000, 5)).__next__,
    )
    assert ep == EP
    assert slept == [5, 5]
    assert scanned == [EP]


def test_wait_for_ssh_times_out():
    client = ClientStub([[pod(id="p1", runtime=None)]] * 50)
    with pytest.raises(TimeoutError):
        wait_for_ssh(client, "p1", timeout=10, poll=5, sleep=lambda s: None,
                     keyscan=lambda e: True, clock=iter(range(0, 1000, 5)).__next__)


def test_wait_for_ssh_keeps_waiting_while_keyscan_fails():
    yes = pod(id="p1")
    client = ClientStub([[yes], [yes], [yes]])
    outcomes = iter([False, False, True])
    ep = wait_for_ssh(client, "p1", timeout=100, poll=1, sleep=lambda s: None,
                      keyscan=lambda e: next(outcomes), clock=iter(range(0, 1000)).__next__)
    assert ep == EP
