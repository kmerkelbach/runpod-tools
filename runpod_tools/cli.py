"""``rpt`` command-line entry point.

Every subcommand lives in ``runpod_tools.commands.<name>`` and exposes
``register(subparsers)`` and ``run(args, ctx) -> int``. Exit codes:
0 ok, 1 the operation failed, 2 usage or configuration error.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from runpod_tools import __version__
from runpod_tools.api import RunpodClient, RunpodError, client_from_env
from runpod_tools.config import Config, ConfigError, load_config
from runpod_tools.pods import SelectionError

# Every registered subcommand, in help order. Nested ones are "parent child".
SUBCOMMANDS: list[str] = [
    "pods", "gpus", "volumes",
    "start", "stop", "resume", "terminate",
    "wait", "ssh", "run",
    "push", "pull", "fetch",
    "template show", "template env", "template ports", "template volume",
]


class UsageError(Exception):
    """Bad arguments or configuration. Exit 2."""

    exit_code = 2


class OperationError(Exception):
    """The requested operation failed. Exit 1."""

    exit_code = 1


@dataclass
class Context:
    client_factory: Callable[..., RunpodClient]
    config: Config
    isatty: bool
    env: Mapping[str, str]
    out: Any = field(default_factory=lambda: sys.stdout)
    err: Any = field(default_factory=lambda: sys.stderr)
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run
    execvp: Callable[[str, Sequence[str]], Any] = os.execvp
    _client: RunpodClient | None = None

    def client(self) -> RunpodClient:
        if self._client is None:
            try:
                self._client = self.client_factory(env=self.env)
            except RunpodError as exc:  # no key: a setup problem, not an API failure
                raise UsageError(str(exc)) from exc
        return self._client

    def print(self, *parts: Any) -> None:
        print(*parts, file=self.out)

    def warn(self, *parts: Any) -> None:
        print(*parts, file=self.err)

    def print_json(self, data: Any) -> None:
        json.dump(data, self.out, indent=2, sort_keys=True)
        self.out.write("\n")

    def confirm(self, question: str, assume_yes: bool) -> bool:
        """True to proceed. Off a TTY, ``-y`` is the only way to say yes."""
        if assume_yes:
            return True
        if not self.isatty:
            raise UsageError(f"{question}\nNot a terminal: pass -y to confirm.")
        try:
            answer = input(f"{question} [y/N]: ")
        except (EOFError, KeyboardInterrupt):
            return False
        return answer.strip().lower() in {"y", "yes"}


def table(rows: Iterable[Sequence[Any]], headers: Sequence[str]) -> str:
    rows = [[str(c) for c in r] for r in rows]
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    lines = [fmt.format(*headers), fmt.format(*("-" * w for w in widths))]
    lines += [fmt.format(*row) for row in rows]
    return "\n".join(line.rstrip() for line in lines)


def add_json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON instead of a table")


def add_pod_selection(parser: argparse.ArgumentParser, *, multi: bool) -> None:
    parser.add_argument("--pod", metavar="ID[,ID]", help="pod id(s) or unique name prefix(es)")
    if multi:
        parser.add_argument("--all", action="store_true", help="every pod in the relevant state")


def add_yes_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation (required when stdin is not a TTY)")


def build_parser() -> argparse.ArgumentParser:
    from runpod_tools.commands import gpus, lifecycle, pods, ssh, start, sync, template, volumes, wait

    parser = argparse.ArgumentParser(
        prog="rpt",
        description="Manage Runpod pods, templates, and code sync over SSH. "
        "Needs RUNPOD_API_KEY; per-project settings come from runpod-tools.toml (see `rpt init`).",
        epilog="Exit codes: 0 ok, 1 operation failed, 2 usage/config error. Docs: README.md, docs/gotchas.md",
    )
    parser.add_argument("--version", action="version", version=f"rpt {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True
    for module in (pods, gpus, volumes, start, lifecycle, wait, ssh, sync, template):
        module.register(sub)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: Callable[..., RunpodClient] = client_from_env,
    config_loader: Callable[[], Config] = load_config,
    stdin_isatty: Callable[[], bool] = sys.stdin.isatty,
    env: Mapping[str, str] | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    execvp: Callable[[str, Sequence[str]], Any] = os.execvp,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    env = os.environ if env is None else env
    try:
        config = config_loader()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    ctx = Context(client_factory=client_factory, config=config, isatty=stdin_isatty(), env=env,
                  runner=runner, execvp=execvp)
    try:
        return int(args.func(args, ctx) or 0)
    except (UsageError, SelectionError, ConfigError) as exc:
        ctx.warn(f"error: {exc}")
        return 2
    except RunpodError as exc:
        ctx.warn(f"runpod api error: {exc}")
        return 1
    except OperationError as exc:
        ctx.warn(f"error: {exc}")
        return 1
    except KeyboardInterrupt:
        ctx.warn("interrupted")
        return 130
