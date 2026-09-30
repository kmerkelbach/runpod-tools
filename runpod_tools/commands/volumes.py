"""``rpt volumes`` — list network volumes. Read-only by design: there is no delete."""

from __future__ import annotations

from runpod_tools.cli import Context, add_json_flag, table


def register(sub) -> None:
    p = sub.add_parser("volumes", help="list network volumes (read-only; volumes are never deleted by this tool)",
                       description="List network volumes. Attach one at pod creation with `rpt start --network-volume-id`.")
    add_json_flag(p)
    p.set_defaults(func=run)


def run(args, ctx: Context) -> int:
    volumes = ctx.client().list_network_volumes()
    if args.json:
        ctx.print_json(volumes)
        return 0
    if not volumes:
        ctx.print("No network volumes.")
        return 0
    volumes.sort(key=lambda v: v.get("name") or "")
    rows = [[v.get("id"), v.get("name"), f"{v.get('size') or 0} GB", v.get("dataCenterId")] for v in volumes]
    ctx.print(table(rows, ["id", "name", "size", "datacenter"]))
    return 0
