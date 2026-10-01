"""Pure helpers over the pod dicts returned by ``RunpodClient.list_pods``."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class SshEndpoint:
    ip: str
    port: int


NO_ENDPOINT_NOTE = "no TCP endpoint (host has no public IP, or 22/tcp is not exposed by the template)"


def ssh_endpoint(pod: dict) -> SshEndpoint | None:
    """The public ``ip:port`` mapped to the pod's 22/tcp, if the host exposes one."""
    runtime = pod.get("runtime") or {}
    for port in runtime.get("ports") or []:
        if (isinstance(port, dict) and port.get("privatePort") == 22 and port.get("ip") and port.get("publicPort")
                and port.get("isIpPublic") is not False):
            return SshEndpoint(str(port["ip"]), int(port["publicPort"]))
    return None


def gpu_label(pod: dict) -> str:
    count = pod.get("gpuCount") or 1
    machine = pod.get("machine") or {}
    name = machine.get("gpuDisplayName") or "GPU"
    return name if (count == 1 and name != "GPU") else f"{count}x {name}"


def uptime_seconds(pod: dict) -> int | None:
    runtime = pod.get("runtime") or {}
    value = runtime.get("uptimeInSeconds")
    return int(value) if value is not None else None


def format_uptime(seconds: int | None) -> str:
    if seconds is None:
        return "-"
    hours, minutes = seconds // 3600, (seconds % 3600) // 60
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def cost_so_far(pod: dict) -> float | None:
    rate = pod.get("costPerHr")
    seconds = uptime_seconds(pod)
    if rate is None or seconds is None:
        return None
    return float(rate) * seconds / 3600


class SelectionError(Exception):
    """The caller's pod selection could not be resolved. Usage error: exit 2."""

    exit_code = 2


def describe(pod: dict) -> str:
    return f"{pod.get('id', '?')}  {pod.get('name', '?')}  [{gpu_label(pod)}, {pod.get('desiredStatus', '?')}]"


def select_pods(
    pods: Sequence[dict],
    *,
    ids: str | None,
    select_all: bool,
    statuses: Sequence[str],
    interactive: bool,
    multi: bool = False,
    prompt: Callable[[str], str] = input,
    name_prefix: str = "",
) -> list[dict]:
    """Resolve which pods a command acts on.

    ``ids`` is a comma-separated list of pod ids or unique name prefixes.
    With neither ``ids`` nor ``select_all``: one candidate is chosen
    automatically; several candidates prompt when ``interactive`` and raise
    ``SelectionError`` (listing them) otherwise, so an unattended agent never
    hangs on a prompt.

    When ``name_prefix`` is configured (shared accounts), ``select_all`` and
    automatic selection only consider pods carrying it; an explicit id in
    ``ids`` can still reach any pod.
    """
    in_status = [p for p in pods if p.get("desiredStatus") in statuses]
    candidates = [p for p in in_status if str(p.get("name", "")).startswith(name_prefix)] if name_prefix else in_status
    scope = f" named {name_prefix}*" if name_prefix else ""

    if ids:
        chosen: list[dict] = []
        for token in [t.strip() for t in ids.split(",") if t.strip()]:
            chosen.append(_match_one(token, in_status, pods, statuses))
        return chosen

    if not candidates:
        raise SelectionError(f"No pods with status {', '.join(statuses)}{scope}.")
    if select_all:
        return list(candidates)
    if len(candidates) == 1:
        return [candidates[0]]

    listing = "\n".join(f"  [{i}] {describe(p)}" for i, p in enumerate(candidates, 1))
    if not interactive:
        raise SelectionError(
            f"Several pods match; pass --pod <id|name-prefix>[,...] or --all:\n{listing}"
        )
    hint = "comma-separated numbers or 'all'" if multi else "a number"
    while True:
        try:
            raw = prompt(f"Select pod ({hint}) [1-{len(candidates)}]:\n{listing}\n> ").strip()
        except (EOFError, KeyboardInterrupt) as exc:
            raise SelectionError("No selection made.") from exc
        if multi and raw.lower() == "all":
            return list(candidates)
        try:
            indices = [int(x) - 1 for x in raw.split(",")]
        except ValueError:
            continue
        if (not multi and len(indices) != 1) or not all(0 <= i < len(candidates) for i in indices):
            continue
        return [candidates[i] for i in indices]


def _match_one(token: str, candidates: Sequence[dict], all_pods: Sequence[dict], statuses: Sequence[str]) -> dict:
    exact = [p for p in candidates if p.get("id") == token]
    if exact:
        return exact[0]
    by_prefix = [p for p in candidates if str(p.get("name", "")).startswith(token)]
    if len(by_prefix) == 1:
        return by_prefix[0]
    if len(by_prefix) > 1:
        listing = "\n".join(f"  {describe(p)}" for p in by_prefix)
        raise SelectionError(f"Name prefix {token!r} is ambiguous:\n{listing}")
    wrong_status = [p for p in all_pods if p.get("id") == token or str(p.get("name", "")).startswith(token)]
    if wrong_status:
        listing = "\n".join(f"  {describe(p)}" for p in wrong_status)
        raise SelectionError(
            f"{token!r} matches a pod but not with status {', '.join(statuses)}:\n{listing}"
        )
    raise SelectionError(f"No pod with id or name prefix {token!r}.")
