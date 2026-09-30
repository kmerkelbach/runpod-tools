"""Builders for ssh / rsync argument vectors, host-key handling, and wait-for-SSH.

Pure functions build argv lists; the few functions that touch the system take
an injectable ``runner`` / ``sleep`` / ``clock`` so tests never shell out.

Flag choices carried over from production use:

* ``StrictHostKeyChecking=accept-new`` — a fresh pod has an unknown host key
  and a non-interactive ssh cannot answer the prompt; ``accept-new`` trusts
  the first key seen but still refuses a *changed* key.
* ``BatchMode=yes`` — never hang on a password prompt.
* push uses ``-a`` without ``-z`` and pins ``aes128-gcm`` with
  ``Compression=no``: the payloads are mostly already-compressed (wheels,
  parquet, weights), so gzip-on-the-wire burns CPU for nothing and AES-GCM
  is hardware-accelerated.
* pull uses ``-K`` (keep symlinked dirs) and a size cap so a stray checkpoint
  never floods the laptop; neither direction passes ``--delete`` unless the
  caller asks for it explicitly.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from runpod_tools.pods import SshEndpoint, ssh_endpoint

ENV_PREFIX = "set -a; [ -f /etc/rp_environment ] && . /etc/rp_environment; set +a; "


def ssh_base(ep: SshEndpoint, key: Path, *, fast: bool = False, extra: Sequence[str] = ()) -> list[str]:
    argv = [
        "ssh", "-p", str(ep.port), "-i", str(key),
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=15",
        "-o", "BatchMode=yes",
    ]
    if fast:
        argv += ["-c", "aes128-gcm@openssh.com", "-o", "Compression=no"]
    argv += list(extra)
    return argv


def ssh_target(ep: SshEndpoint, user: str) -> str:
    return f"{user}@{ep.ip}"


def remote_command(cmd: str, *, source_env: bool = True) -> str:
    """Prefix ``cmd`` so the pod's injected secrets (``/etc/rp_environment``) are visible.

    Runpod injects template env vars into PID 1 only; sshd-spawned shells do
    not inherit them. Sourcing the file first is what makes ``$HF_TOKEN`` etc.
    resolve in a non-interactive command.
    """
    return (ENV_PREFIX + cmd) if source_env else cmd


def _rsync_ssh_string(ep: SshEndpoint, key: Path, *, fast: bool) -> str:
    return " ".join(ssh_base(ep, key, fast=fast))


def rsync_push(
    src_dir: Path, ep: SshEndpoint, key: Path, dest: str, excludes: Sequence[str], *,
    user: str, delete: bool = False, dry_run: bool = False,
) -> list[str]:
    argv = ["rsync", "-a", "--no-owner", "--no-group", "--progress"]
    if dry_run:
        argv.append("--dry-run")
    if delete:
        argv.append("--delete")
    for pattern in excludes:
        argv += ["--exclude", pattern]
    argv += ["-e", _rsync_ssh_string(ep, key, fast=True)]
    argv += [f"{str(src_dir).rstrip('/')}/", f"{ssh_target(ep, user)}:{_slash(dest)}"]
    return argv


def rsync_pull(
    ep: SshEndpoint, key: Path, remote_dir: str, local_dir: Path, excludes: Sequence[str], *,
    user: str, max_size: str | None, dry_run: bool = False,
) -> list[str]:
    argv = ["rsync", "-azK", "--no-owner", "--no-group", "--progress"]
    if dry_run:
        argv.append("--dry-run")
    if max_size:
        argv.append(f"--max-size={max_size}")
    for pattern in excludes:
        argv += ["--exclude", pattern]
    argv += ["-e", _rsync_ssh_string(ep, key, fast=False)]
    argv += [f"{ssh_target(ep, user)}:{_slash(remote_dir)}", f"{str(local_dir).rstrip('/')}/"]
    return argv


def _slash(path: str) -> str:
    return path if path.endswith("/") else path + "/"


def ssh_command_string(ep: SshEndpoint, key: Path, user: str) -> str:
    return f"ssh {ssh_target(ep, user)} -p {ep.port} -i {key}"


def rsync_template_string(ep: SshEndpoint, key: Path, user: str, dest: str) -> str:
    return (
        f"rsync -a --no-owner --no-group -e 'ssh -p {ep.port} -i {key}' "
        f"<local_dir>/ {ssh_target(ep, user)}:{_slash(dest)}"
    )


Runner = Callable[..., subprocess.CompletedProcess]


def keyscan(
    ep: SshEndpoint, *, known_hosts: Path | None = None, runner: Runner = subprocess.run,
) -> bool:
    """Record the pod's host key in ``known_hosts``; False while sshd is not answering.

    Runpod reuses ``ip:port`` pairs across pods, so a stale entry for the same
    endpoint is removed first (otherwise ssh refuses the *new* key as a
    man-in-the-middle).
    """
    known_hosts = known_hosts or Path.home() / ".ssh" / "known_hosts"
    known_hosts.parent.mkdir(parents=True, exist_ok=True)
    host = f"[{ep.ip}]:{ep.port}"
    if known_hosts.exists() and host in known_hosts.read_text():
        runner(["ssh-keygen", "-R", host, "-f", str(known_hosts)], capture_output=True, text=True)
    result = runner(["ssh-keyscan", "-p", str(ep.port), "-T", "10", ep.ip], capture_output=True, text=True)
    if result.returncode != 0 or not result.stdout.strip():
        return False
    with known_hosts.open("a") as fh:
        fh.write(result.stdout if result.stdout.endswith("\n") else result.stdout + "\n")
    return True


def wait_for_ssh(
    client, pod_id: str, *, timeout: float = 900, poll: float = 15,
    sleep: Callable[[float], None] = time.sleep, keyscan: Callable[[SshEndpoint], bool] = keyscan,
    clock: Callable[[], float] = time.monotonic, log: Callable[[str], None] = lambda _m: None,
) -> SshEndpoint:
    """Poll until ``pod_id`` exposes 22/tcp *and* its sshd answers a keyscan."""
    deadline = clock() + timeout
    announced = False
    while True:
        pods = {p.get("id"): p for p in client.list_pods()}
        ep = ssh_endpoint(pods[pod_id]) if pod_id in pods else None
        if ep is not None:
            if not announced:
                log(f"pod {pod_id}: endpoint {ep.ip}:{ep.port}, waiting for sshd")
                announced = True
            if keyscan(ep):
                return ep
        if clock() >= deadline:
            raise TimeoutError(f"pod {pod_id}: no reachable SSH endpoint after {timeout:.0f}s")
        sleep(poll)
