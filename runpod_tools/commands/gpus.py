"""``rpt gpus`` — GPU types, availability, and lowest on-demand price."""

from __future__ import annotations

from runpod_tools.cli import Context, add_json_flag, table


def register(sub) -> None:
    p = sub.add_parser("gpus", help="list GPU types with VRAM, cloud availability, and price",
                       description="List GPU types. The `id` column is the value `rpt start --gpu-type` expects.")
    add_json_flag(p)
    p.add_argument("--secure", action="store_true", help="only types available on Secure Cloud")
    p.add_argument("--community", action="store_true", help="only types available on Community Cloud")
    p.set_defaults(func=run)


def price_label(price: dict | None) -> str:
    if not price:
        return "-"
    if price.get("uninterruptablePrice"):
        return f"${price['uninterruptablePrice']:.3f}/hr"
    if price.get("minimumBidPrice"):
        return f"${price['minimumBidPrice']:.3f}/hr (spot)"
    return "-"


def run(args, ctx: Context) -> int:
    gpus = ctx.client().list_gpu_types()
    if args.secure:
        gpus = [g for g in gpus if g.get("secureCloud")]
    if args.community:
        gpus = [g for g in gpus if g.get("communityCloud")]
    gpus.sort(key=lambda g: (g.get("memoryInGb") or 0, g.get("displayName") or ""))
    if args.json:
        ctx.print_json(gpus)
        return 0
    rows = []
    for g in gpus:
        clouds = [name for name, key in (("secure", "secureCloud"), ("community", "communityCloud")) if g.get(key)]
        rows.append([g.get("id"), g.get("displayName"), f"{g.get('memoryInGb') or 0} GB",
                     ",".join(clouds) or "unavailable", price_label(g.get("lowestPrice"))])
    ctx.print(table(rows, ["id (use with --gpu-type)", "name", "vram", "clouds", "lowest price"]))
    return 0
