"""``rpt ssh`` (interactive shell or printed command) and ``rpt run`` (remote command)."""

from __future__ import annotations

import shlex

from runpod_tools.cli import Context, OperationError, UsageError, add_pod_selection
from runpod_tools.pods import NO_ENDPOINT_NOTE, select_pods, ssh_endpoint
from runpod_tools.sshutil import remote_command, ssh_base, ssh_command_string, ssh_target

BACKGROUND_DIR = "/workspace/rpt"


def register(sub) -> None:
    p = sub.add_parser("ssh", help="open a shell on a pod, or print the ssh command",
                       description="Open an interactive shell on the selected pod. --print shows the command instead.")
    add_pod_selection(p, multi=False)
    p.add_argument("--print", action="store_true", help="print the ssh command instead of connecting")
    p.set_defaults(func=run_ssh)

    r = sub.add_parser(
        "run", help="run a command on one or all pods (pod secrets sourced, output streamed)",
        description="Run COMMAND over ssh. /etc/rp_environment (the template's env vars and secrets) is sourced "
        "first unless --raw. Exit code is the remote command's; with --all it is the worst one.",
        epilog="Long jobs: --background NAME runs under nohup with output in /workspace/rpt/NAME.log and prints the pid. "
        "Poll gently afterwards (one `rpt run -- tail -n 20 /workspace/rpt/NAME.log` per check, not a tight loop).",
    )
    add_pod_selection(r, multi=True)
    r.add_argument("--raw", action="store_true", help="do not source /etc/rp_environment first")
    r.add_argument("--background", metavar="NAME", help="detach under nohup; log at /workspace/rpt/NAME.log")
    r.add_argument("command", nargs="*", help="command and arguments (put `--` before them)")
    r.set_defaults(func=run_run)


def _endpoint_or_fail(pod: dict):
    ep = ssh_endpoint(pod)
    if ep is None:
        raise OperationError(f"pod {pod.get('name')} ({pod.get('id')}): {NO_ENDPOINT_NOTE}")
    return ep


def run_ssh(args, ctx: Context) -> int:
    cfg = ctx.config.ssh
    pods = select_pods(ctx.client().list_pods(), ids=args.pod, select_all=False, statuses=["RUNNING"],
                       interactive=ctx.isatty, name_prefix=ctx.config.pod.name_prefix)
    ep = _endpoint_or_fail(pods[0])
    if args.print:
        ctx.print(ssh_command_string(ep, cfg.key, cfg.user))
        return 0
    if not ctx.isatty:
        raise UsageError("stdin is not a terminal; use `rpt ssh --print` for the command or `rpt run -- <cmd>` to execute")
    argv = ["ssh", "-p", str(ep.port), "-i", str(cfg.key), "-o", "StrictHostKeyChecking=accept-new",
            ssh_target(ep, cfg.user)]
    ctx.execvp("ssh", argv)
    return 0


def background_wrapper(command: str, name: str) -> str:
    # Only the job is backgrounded, with all three streams redirected. Without the
    # braces `mkdir && nohup ... &` backgrounds the whole list in a subshell that
    # keeps the session's stdout, and sshd holds the session open until it exits.
    return (f"mkdir -p {BACKGROUND_DIR} && {{ nohup bash -c {shlex.quote(command)} "
            f"> {BACKGROUND_DIR}/{name}.log 2>&1 < /dev/null & echo pid=$!; }}")


def run_run(args, ctx: Context) -> int:
    if not args.command:
        raise UsageError("run needs a command: rpt run [--pod ID] -- <command...>")
    cfg = ctx.config.ssh
    command = shlex.join(args.command) if len(args.command) > 1 else args.command[0]
    if args.background:
        if not args.background.replace("_", "").replace("-", "").isalnum():
            raise UsageError("--background NAME must be alphanumeric (plus - and _)")
        command = background_wrapper(command, args.background)
    remote = remote_command(command, source_env=not args.raw)

    pods = select_pods(ctx.client().list_pods(), ids=args.pod, select_all=args.all, statuses=["RUNNING"],
                       interactive=ctx.isatty, multi=True, name_prefix=ctx.config.pod.name_prefix)
    worst = 0
    for pod in pods:
        try:
            ep = _endpoint_or_fail(pod)
        except OperationError as exc:
            if len(pods) == 1:
                raise
            ctx.warn(f"skipped: {exc}")
            worst = max(worst, 1)
            continue
        if len(pods) > 1:
            ctx.print(f"== {pod.get('name')} ({pod.get('id')}) ==")
        argv = ssh_base(ep, cfg.key) + [ssh_target(ep, cfg.user), remote]
        result = ctx.runner(argv)
        worst = max(worst, result.returncode)
    return worst
