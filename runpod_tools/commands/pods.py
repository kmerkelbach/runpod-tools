"""``rpt pods`` — list pods with GPU, uptime, cost, and how to reach them."""

from __future__ import annotations

from runpod_tools.cli import Context, add_json_flag, table
from runpod_tools.pods import NO_ENDPOINT_NOTE, cost_so_far, format_uptime, gpu_label, ssh_endpoint, uptime_seconds
from runpod_tools.sshutil import rsync_template_string, ssh_command_string

STATUS_LABEL = {"RUNNING": "running", "EXITED": "stopped", "CREATED": "starting", "TERMINATED": "terminated"}


def register(sub) -> None:
    p = sub.add_parser("pods", help="list your pods with GPU, uptime, cost, and SSH endpoints",
                       description="List pods. Running pods come first with an ssh command and an rsync template each.")
    add_json_flag(p)
    p.add_argument("--status", help="only pods in this state (RUNNING, EXITED, ...)")
    p.set_defaults(func=run)


def pod_record(pod: dict) -> dict:
    ep = ssh_endpoint(pod)
    return {
        "id": pod.get("id"),
        "name": pod.get("name"),
        "status": pod.get("desiredStatus"),
        "gpu": gpu_label(pod),
        "gpu_count": pod.get("gpuCount"),
        "image": pod.get("imageName"),
        "uptime_seconds": uptime_seconds(pod),
        "cost_per_hr": pod.get("costPerHr"),
        "cost_so_far": cost_so_far(pod),
        "ssh": {"ip": ep.ip, "port": ep.port} if ep else None,
        "proxy_ssh_host": (pod.get("machine") or {}).get("podHostId"),
    }


def run(args, ctx: Context) -> int:
    pods = ctx.client().list_pods()
    if args.status:
        pods = [p for p in pods if p.get("desiredStatus") == args.status.upper()]
    if args.json:
        ctx.print_json([pod_record(p) for p in pods])
        return 0
    if not pods:
        ctx.print("No pods." if not args.status else f"No pods with status {args.status.upper()}.")
        return 0

    running = sorted((p for p in pods if p.get("desiredStatus") == "RUNNING"), key=lambda p: p.get("name") or "")
    others = sorted((p for p in pods if p.get("desiredStatus") != "RUNNING"), key=lambda p: p.get("name") or "")
    cfg = ctx.config

    if running:
        rows = []
        for p in running:
            cost = cost_so_far(p)
            rows.append([
                p.get("id"), p.get("name"), gpu_label(p), STATUS_LABEL.get(p.get("desiredStatus"), p.get("desiredStatus")),
                format_uptime(uptime_seconds(p)), f"{p.get('costPerHr') or 0:.3f}", f"{cost:.2f}" if cost is not None else "-",
            ])
        ctx.print(table(rows, ["id", "name", "gpu", "status", "uptime", "$/hr", "$ so far"]))
        total_rate = sum(p.get("costPerHr") or 0 for p in running)
        total_spent = sum(cost_so_far(p) or 0 for p in running)
        ctx.print(f"\nrunning: {len(running)} pod(s), {total_rate:.3f} $/hr, {total_spent:.2f} $ so far\n")
        ctx.print("connect:")
        for p in running:
            ep = ssh_endpoint(p)
            ctx.print(f"  {p.get('name')}")
            if ep is None:
                ctx.print(f"    {NO_ENDPOINT_NOTE}")
                host = (p.get("machine") or {}).get("podHostId")
                if host:
                    ctx.print(f"    proxy (terminal only, no rsync): ssh {host}@ssh.runpod.io -i {cfg.ssh.key}")
                continue
            ctx.print(f"    {ssh_command_string(ep, cfg.ssh.key, cfg.ssh.user)}")
            dest = cfg.sync.remote_dest or "/workspace/"
            ctx.print(f"    {rsync_template_string(ep, cfg.ssh.key, cfg.ssh.user, dest)}")
        ctx.print()
    else:
        ctx.print("No running pods.\n")

    if others:
        rows = [[p.get("id"), p.get("name"), gpu_label(p), STATUS_LABEL.get(p.get("desiredStatus"), p.get("desiredStatus"))]
                for p in others]
        ctx.print("not running:")
        ctx.print(table(rows, ["id", "name", "gpu", "status"]))
    return 0
