"""A short-lived pod listing shared by every ``rpt`` process of one account.

Runpod's API limit (180 requests/minute) is per account, and almost every
``rpt`` command starts with the GraphQL pod listing to turn ``--pod`` into an
ssh endpoint. A driver that probes twenty pods in parallel would list all pods
twenty times in a second. Commands that only need an endpoint (``run``,
``ssh``, ``push``, ``pull``, ``fetch``) read this cache instead; whoever
refreshes it holds a lock, so parallel callers wait for one listing rather than
each sending their own.

Anything that reports state or changes it (``pods``, ``start``, ``stop``,
``resume``, ``terminate``, ``wait``) still lists fresh and only writes the
cache. The listing holds pod ids, names, IPs and ports, no secrets.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TTL = 30.0
DEFAULT_STALE_LIMIT = 1800.0
TTL_ENV = "RPT_POD_CACHE_TTL"
STALE_LIMIT_ENV = "RPT_POD_CACHE_STALE"
DIR_ENV = "RPT_CACHE_DIR"
USAGE_LOG_ENV = "RPT_USAGE_LOG"


@dataclass
class PodCache:
    """The cached listing for one API key.

    ``ttl`` is how old a listing may be and still count as current;
    ``stale_limit`` is how old one may be when the API refuses a refresh
    (rate limit), for endpoint-only commands.
    """

    path: Path
    ttl: float = DEFAULT_TTL
    stale_limit: float = DEFAULT_STALE_LIMIT
    clock: Callable[[], float] = time.time

    @property
    def lock_path(self) -> Path:
        return self.path.with_suffix(".lock")

    def read(self, max_age: float) -> list[dict] | None:
        """The cached pods when the listing is at most ``max_age`` seconds old."""
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or not isinstance(data.get("pods"), list):
            return None
        written = data.get("written_at")
        if not isinstance(written, (int, float)) or self.clock() - written > max_age:
            return None
        return data["pods"]

    def write(self, pods: list[dict]) -> None:
        """Replace the listing atomically; a cache that cannot be written is skipped."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".pods-", suffix=".tmp")
            with os.fdopen(fd, "w") as handle:
                json.dump({"written_at": self.clock(), "pods": pods}, handle)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def clear(self) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass

    @contextmanager
    def refreshing(self) -> Iterator[None]:
        """Hold the refresh lock; without a usable lock file, proceed unlocked."""
        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            handle = open(self.lock_path, "a")
        except OSError:
            yield
            return
        with handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _seconds(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


def cache_dir(env: Mapping[str, str]) -> Path:
    return Path(env.get(DIR_ENV) or os.path.join(env.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"),
                                                 "runpod-tools"))


def account_tag(api_key: str) -> str:
    """A stable file-name tag per API key that does not reveal the key."""
    return hashlib.sha256(api_key.encode()).hexdigest()[:16]


def cache_from_env(api_key: str, env: Mapping[str, str]) -> PodCache | None:
    """The account's cache, or None when ``RPT_POD_CACHE_TTL=0`` turns it off."""
    ttl = _seconds(env, TTL_ENV, DEFAULT_TTL)
    if ttl <= 0:
        return None
    return PodCache(cache_dir(env) / f"pods-{account_tag(api_key)}.json", ttl=ttl,
                    stale_limit=max(ttl, _seconds(env, STALE_LIMIT_ENV, DEFAULT_STALE_LIMIT)))


def usage_log_from_env(api_key: str, env: Mapping[str, str]) -> Path | None:
    """Where each API request is logged (one line per request), or None when
    ``RPT_USAGE_LOG=0``. The log lets a shared account see who spends its
    rate limit."""
    if env.get(USAGE_LOG_ENV, "").strip() == "0":
        return None
    return cache_dir(env) / f"usage-{account_tag(api_key)}.log"


@dataclass
class Backoff:
    """Shared "refused until" marker per API, written when Runpod's Retry-After
    is longer than a caller would wait (an emptied hourly bucket). Other rpt
    processes then fail at once instead of each sending a doomed request."""

    directory: Path
    tag: str
    clock: Callable[[], float] = time.time

    def _path(self, api: str) -> Path:
        return self.directory / f"blocked-{api}-{self.tag}"

    def until(self, api: str) -> float | None:
        try:
            value = float(self._path(api).read_text().strip())
        except (OSError, ValueError):
            return None
        return value if value > self.clock() else None

    def block(self, api: str, seconds: float) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._path(api).write_text(f"{self.clock() + seconds:.0f}\n")
        except OSError:
            pass


def backoff_from_env(api_key: str, env: Mapping[str, str]) -> Backoff:
    return Backoff(cache_dir(env), account_tag(api_key))
