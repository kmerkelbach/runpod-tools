"""Project configuration: built-in defaults, then ``runpod-tools.toml``, then environment.

Discovery walks up from the current directory (or ``start``) looking for
``runpod-tools.toml``; ``RUNPOD_TOOLS_CONFIG`` names a file explicitly and
wins. Every value has a default, so the tool works without a file for
everything except the sync commands, which need ``[sync].remote_dest``.

Environment overrides (for values an agent may set per shell):
  RUNPOD_TEMPLATE_ID       -> pod.template_id
  RUNPOD_POD_NAME_PREFIX   -> pod.name_prefix
  RUNPOD_SSH_KEY           -> ssh.key
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

CONFIG_FILENAME = "runpod-tools.toml"
ENV_CONFIG_PATH = "RUNPOD_TOOLS_CONFIG"


class ConfigError(Exception):
    """Malformed configuration file."""


@dataclass
class PodDefaults:
    template_id: str = ""
    image: str = ""
    gpu_type: str = "NVIDIA H200"
    gpu_count: int = 1
    cloud_type: str = "SECURE"
    container_disk_gb: int = 50
    volume_gb: int = 0
    network_volume_id: str = ""
    name_prefix: str = ""
    support_public_ip: bool = True
    allowed_cuda_versions: list[str] = field(default_factory=list)
    ports: str = ""


@dataclass
class SshConfig:
    key: Path = field(default_factory=lambda: Path.home() / ".ssh" / "id_ed25519")
    user: str = "root"


@dataclass
class SyncConfig:
    local_root: Path = field(default_factory=lambda: Path.cwd().resolve())
    remote_dest: str = ""
    push_excludes: list[str] = field(
        default_factory=lambda: [".git", ".venv", "__pycache__", "*.pyc", "results", "logs"]
    )
    pull_excludes: list[str] = field(default_factory=lambda: ["checkpoints", "*.pt", "wandb"])
    pull_max_size: str = "10m"
    fetch_dirs: list[str] = field(default_factory=lambda: ["results", "logs"])
    fetch_excludes: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class Config:
    pod: PodDefaults = field(default_factory=PodDefaults)
    ssh: SshConfig = field(default_factory=SshConfig)
    sync: SyncConfig = field(default_factory=SyncConfig)
    source: Path | None = None


def find_config_file(start: Path | None = None) -> Path | None:
    """Walk up from ``start`` (default: cwd) and return the first runpod-tools.toml."""
    here = (start or Path.cwd()).resolve()
    for directory in (here, *here.parents):
        candidate = directory / CONFIG_FILENAME
        if candidate.is_file():
            return candidate
    return None


_ENV_OVERRIDES = {
    "RUNPOD_TEMPLATE_ID": ("pod", "template_id"),
    "RUNPOD_POD_NAME_PREFIX": ("pod", "name_prefix"),
    "RUNPOD_SSH_KEY": ("ssh", "key"),
}


def load_config(start: Path | None = None, env: Mapping[str, str] | None = None) -> Config:
    """Return the effective configuration (defaults < file < environment)."""
    env = os.environ if env is None else env
    cfg = Config()

    explicit = env.get(ENV_CONFIG_PATH)
    path = Path(explicit).expanduser() if explicit else find_config_file(start)
    if path is not None:
        if not path.is_file():
            raise ConfigError(f"config file not found: {path}")
        _apply_file(cfg, path)

    for var, (section, key) in _ENV_OVERRIDES.items():
        value = env.get(var)
        if value:
            setattr(getattr(cfg, section), key, _coerce(section, key, value))
    return cfg


def _apply_file(cfg: Config, path: Path) -> None:
    try:
        raw = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    cfg.source = path
    known = {"pod": cfg.pod, "ssh": cfg.ssh, "sync": cfg.sync}
    for table, values in raw.items():
        if table not in known:
            raise ConfigError(f"{path}: unknown table [{table}] (expected one of {sorted(known)})")
        if not isinstance(values, dict):
            raise ConfigError(f"{path}: [{table}] must be a table")
        target = known[table]
        allowed = {f.name for f in fields(target)}
        for key, value in values.items():
            if key not in allowed:
                raise ConfigError(f"{path}: unknown key {table}.{key} (expected one of {sorted(allowed)})")
            setattr(target, key, _coerce(table, key, value, path))
    if "sync" in raw and "local_root" in raw["sync"]:
        cfg.sync.local_root = (path.parent / raw["sync"]["local_root"]).resolve()
    elif "sync" in raw:
        cfg.sync.local_root = path.parent.resolve()


def _coerce(section: str, key: str, value: Any, path: Path | None = None) -> Any:
    where = f"{path}: " if path else ""
    if (section, key) in {("ssh", "key"), ("sync", "local_root")}:
        if not isinstance(value, str):
            raise ConfigError(f"{where}{section}.{key} must be a string path")
        return Path(value).expanduser()
    template = {"pod": PodDefaults(), "ssh": SshConfig(), "sync": SyncConfig()}[section]
    expected = type(getattr(template, key))
    if expected is bool:
        if not isinstance(value, bool):
            raise ConfigError(f"{where}{section}.{key} must be true or false")
    elif expected is int:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ConfigError(f"{where}{section}.{key} must be an integer")
    elif expected is str:
        if not isinstance(value, str):
            raise ConfigError(f"{where}{section}.{key} must be a string")
    elif expected is list:
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigError(f"{where}{section}.{key} must be a list of strings")
    elif expected is dict:
        if not isinstance(value, dict) or not all(
            isinstance(v, list) and all(isinstance(s, str) for s in v) for v in value.values()
        ):
            raise ConfigError(f"{where}{section}.{key} must map names to lists of strings")
    return value
