"""``rpt wait`` — block until a pod's SSH endpoint exists and its sshd answers."""

from __future__ import annotations

from runpod_tools.cli import Context, OperationError, UsageError
from runpod_tools.pods import select_pods
from runpod_tools.sshutil import keyscan, wait_for_ssh


def register(sub) -> None:
    p = sub.add_parser("wait", help="wait until a pod is reachable over SSH (and record its host key)",
                       description="Poll until the pod exposes 22/tcp and sshd answers. Prints `<ip> <port>`.")
    p.add_argument("--pod", metavar="ID", help="pod id or unique name prefix (default: the only running/starting pod)")
    p.add_argument("--timeout", type=int, default=900, help="seconds before giving up (default 900)")
    p.add_argument("--poll", type=int, default=15, help="seconds between checks (default 15; keep it gentle)")
    p.set_defaults(func=run)


def run(args, ctx: Context) -> int:
    client = ctx.client()
    pods = select_pods(client.list_pods(), ids=args.pod, select_all=False,
                       statuses=["RUNNING", "CREATED"], interactive=ctx.isatty)
    if len(pods) != 1:
        raise UsageError("wait takes exactly one pod")
    pod_id = pods[0]["id"]
    try:
        ep = wait_for_ssh(client, pod_id, timeout=args.timeout, poll=args.poll,
                          keyscan=lambda e: keyscan(e, runner=ctx.runner), log=ctx.warn)
    except TimeoutError as exc:
        raise OperationError(str(exc)) from exc
    ctx.print(f"{ep.ip} {ep.port}")
    return 0
