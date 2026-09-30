"""``rpt init`` — drop the annotated example config into the current directory."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from runpod_tools.cli import Context, UsageError
from runpod_tools.config import CONFIG_FILENAME

# Shipped inside the package so `rpt init` works from any install, not only an editable one.
# examples/runpod-tools.toml in the repo is a symlink to this file.
EXAMPLE_CONFIG = files("runpod_tools").joinpath("example-config.toml")


def register(sub) -> None:
    p = sub.add_parser("init", help=f"write an annotated {CONFIG_FILENAME} into the current directory",
                       description=f"Copy examples/{CONFIG_FILENAME} here. Edit [pod].template_id and [sync] afterwards.")
    p.add_argument("--force", action="store_true", help="overwrite an existing file")
    p.set_defaults(func=run)


def run(args, ctx: Context) -> int:
    target = Path.cwd() / CONFIG_FILENAME
    if target.exists() and not args.force:
        raise UsageError(f"{target} exists; pass --force to overwrite")
    target.write_text(EXAMPLE_CONFIG.read_text())
    ctx.print(f"wrote {target}")
    ctx.print("next: set [pod].template_id (see `rpt template show`) and [sync].remote_dest, then `rpt pods`.")
    return 0
