"""Thin Runpod API client over the standard library.

Runpod has two APIs and each is the right tool for different things (see
docs/api-notes.md):

* REST  ``https://rest.runpod.io/v1``  — create/stop pods, list network
  volumes, PATCH templates.
* GraphQL ``https://api.runpod.io/graphql`` — list pods (the only place GPU
  display names and runtime ports appear), GPU types (no REST equivalent),
  resume/terminate, and *reading* templates (REST resolves
  ``{{ RUNPOD_SECRET_* }}`` placeholders to their secret values; GraphQL
  returns the placeholders verbatim, which is what a read-modify-write needs).

The ``transport`` seam lets tests inject canned responses.

Both APIs share one per-account rate limit (180 requests/minute, 3,600/hour,
43,200/day since 2026-10-19) and answer 429 above it. Every request retries a
429 with exponential backoff and jitter, honouring ``Retry-After``, within a
bounded wait (``RPT_RATE_LIMIT_WAIT`` seconds, default 90): many parallel
``rpt`` processes then spread out instead of failing together, and a caller
that has its own retry loop still gets a non-zero exit in bounded time.
"""

from __future__ import annotations

import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from runpod_tools.podcache import Backoff, PodCache, backoff_from_env, cache_from_env, usage_log_from_env

REST_BASE = "https://rest.runpod.io/v1"
GRAPHQL_URL = "https://api.runpod.io/graphql"
USER_AGENT = "runpod-tools/0.1"
API_KEY_HELP = "Set RUNPOD_API_KEY (create one at https://www.runpod.io/console/user/settings)."

DEFAULT_RATE_LIMIT_WAIT = 90.0
RATE_LIMIT_WAIT_ENV = "RPT_RATE_LIMIT_WAIT"
FIRST_BACKOFF = 2.0
MAX_BACKOFF = 30.0

# A transport returns (status, body) or (status, body, headers); headers are
# only needed for Retry-After, so test doubles may leave them out.
Transport = Callable[[str, str, dict[str, str], bytes | None], tuple]


class RunpodError(Exception):
    """An API call failed. ``status`` is the HTTP status when there was one."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.message = message
        self.status = status


def urllib_transport(timeout: float) -> Transport:
    def send(method: str, url: str, headers: dict[str, str], body: bytes | None) -> tuple[int, bytes]:
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers or {})

    return send


def retry_after_seconds(headers: Mapping[str, str]) -> float | None:
    """``Retry-After`` in seconds when the server sent a numeric one."""
    for key, value in headers.items():
        if key.lower() == "retry-after":
            try:
                return max(0.0, float(value))
            except (TypeError, ValueError):
                return None
    return None


def is_rate_limited(status: int, parsed: Any) -> bool:
    """HTTP 429, or a GraphQL 200 whose errors say the same thing."""
    if status == 429:
        return True
    if isinstance(parsed, dict) and parsed.get("errors"):
        text = json.dumps(parsed["errors"]).lower()
        return "too many requests" in text or "rate limit" in text
    return False


def rate_limit_wait_from_env(env: Mapping[str, str]) -> float:
    raw = env.get(RATE_LIMIT_WAIT_ENV, "").strip()
    if not raw:
        return DEFAULT_RATE_LIMIT_WAIT
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_RATE_LIMIT_WAIT


def _stderr(message: str) -> None:
    print(message, file=sys.stderr)


def _caller() -> str:
    """This rpt command and the process that ran it, for the usage log."""
    command = " ".join(sys.argv[1:2]) or "-"
    try:
        with open(f"/proc/{os.getppid()}/cmdline", "rb") as handle:
            parent = handle.read().replace(b"\0", b" ").decode(errors="replace").strip()
    except OSError:
        parent = "?"
    return f"{command}\t{parent[:160]}"


def append_usage(path: Path, label: str, status: int, headers: Mapping[str, str]) -> None:
    """One tab-separated line per request: time, pid, endpoint, status, the
    remaining hourly budget when the server reports it, command and parent."""
    remaining = next((v for k, v in headers.items() if k.lower() == "x-ratelimit-remaining-hour"), "")
    line = (f"{time.strftime('%Y-%m-%dT%H:%M:%S')}\t{os.getpid()}\t{label}\t{status}\t{remaining}"
            f"\t{_caller()}\n")
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(path, "a") as handle:
            handle.write(line)
    except OSError:
        pass


class RunpodClient:
    def __init__(
        self,
        api_key: str,
        transport: Transport | None = None,
        timeout: float = 30.0,
        *,
        rate_limit_wait: float = DEFAULT_RATE_LIMIT_WAIT,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        jitter: Callable[[], float] = random.random,
        log: Callable[[str], None] = _stderr,
        pod_cache: PodCache | None = None,
        usage_log: Path | None = None,
        backoff: Backoff | None = None,
    ):
        self._key = api_key
        self.pod_cache = pod_cache
        self._usage_log = usage_log
        self._backoff = backoff
        self._send = transport or urllib_transport(timeout)
        self._rate_limit_wait = rate_limit_wait
        self._sleep = sleep
        self._clock = clock
        self._jitter = jitter
        self._log = log

    def _request(self, method: str, url: str, body: bytes | None, label: str) -> tuple[int, bytes, Any]:
        """Send one request, waiting out rate limits; returns (status, raw, parsed)."""
        api = "graphql" if url == GRAPHQL_URL else "rest"
        if self._backoff is not None and (until := self._backoff.until(api)) is not None:
            raise RunpodError(
                f"{label}: rate limited (429) by Runpod until {time.strftime('%H:%M:%S', time.localtime(until))} "
                "(an earlier rpt call was told to retry then); not sending",
                429,
            )
        deadline = self._clock() + self._rate_limit_wait
        attempt = 0
        while True:
            try:
                response = self._send(method, url, self._headers(), body)
            except OSError as exc:
                raise RunpodError(f"{label}: {exc}") from exc
            status, data = response[0], response[1]
            headers = response[2] if len(response) > 2 else {}
            parsed = _parse_json(data)
            if self._usage_log is not None:
                append_usage(self._usage_log, label, status, headers)
            if not is_rate_limited(status, parsed):
                return status, data, parsed

            # Full jitter over an exponential ceiling, so parallel callers that
            # were refused together do not come back together.
            ceiling = min(MAX_BACKOFF, FIRST_BACKOFF * 2**attempt)
            delay = ceiling * (0.5 + 0.5 * self._jitter())
            server_delay = retry_after_seconds(headers)
            if server_delay is not None:
                delay = max(delay, server_delay)
            remaining = deadline - self._clock()
            if delay > remaining:
                if self._backoff is not None and server_delay is not None and server_delay > remaining:
                    self._backoff.block(api, server_delay)
                raise RunpodError(
                    f"{label}: rate limited (429) by Runpod; gave up after {attempt + 1} attempts "
                    f"within {self._rate_limit_wait:.0f}s ({RATE_LIMIT_WAIT_ENV} sets the wait)",
                    429,
                )
            attempt += 1
            self._log(f"rpt: {label} rate limited (429); retry {attempt} in {delay:.1f}s")
            self._sleep(delay)

    # -- low level -----------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        # Cloudflare in front of both APIs rejects urllib's default User-Agent
        # ("Python-urllib/3.x") with 403 "error code: 1010".
        return {
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }

    def rest(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> Any:
        raw = json.dumps(body).encode() if body is not None else None
        status, data, parsed = self._request(method, REST_BASE + path, raw, f"{method} {path}")
        if not 200 <= status < 300:
            message = parsed.get("error") if isinstance(parsed, dict) else None
            raise RunpodError(f"{method} {path} failed ({status}): {message or data.decode(errors='replace')}", status)
        return parsed

    def graphql(self, query: str, variables: Mapping[str, Any] | None = None) -> dict:
        payload = {"query": query, "variables": dict(variables or {})}
        status, data, parsed = self._request("POST", GRAPHQL_URL, json.dumps(payload).encode(), "graphql")
        if not 200 <= status < 300 or not isinstance(parsed, dict):
            raise RunpodError(f"graphql failed ({status}): {data.decode(errors='replace')}", status)
        if parsed.get("errors"):
            messages = "; ".join(str(e.get("message", e)) for e in parsed["errors"])
            raise RunpodError(f"graphql: {messages}", status)
        return parsed.get("data") or {}

    # -- pods ------------------------------------------------------------------

    _PODS_QUERY = """
    query Pods {
      myself {
        pods {
          id name desiredStatus imageName costPerHr gpuCount
          machine { gpuDisplayName podHostId }
          runtime {
            uptimeInSeconds
            ports { ip isIpPublic privatePort publicPort type }
          }
        }
      }
    }
    """

    def list_pods(self) -> list[dict]:
        """A fresh listing; it also refreshes the shared cache."""
        data = self.graphql(self._PODS_QUERY)
        myself = data.get("myself")
        if myself is None:
            raise RunpodError("graphql: no `myself` in response; check the API key's permissions")
        pods = myself.get("pods") or []
        if self.pod_cache is not None:
            self.pod_cache.write(pods)
        return pods

    def cached_pods(self) -> tuple[list[dict], bool]:
        """The pod listing for commands that only need an endpoint, and whether
        it came from the cache. A current cached listing is used as is; otherwise
        one process refreshes it while the others wait. When the API refuses the
        refresh with a rate limit, a listing up to ``stale_limit`` old is used."""
        cache = self.pod_cache
        if cache is None:
            return self.list_pods(), False
        pods = cache.read(cache.ttl)
        if pods is not None:
            return pods, True
        with cache.refreshing():
            pods = cache.read(cache.ttl)
            if pods is not None:
                return pods, True
            try:
                return self.list_pods(), False
            except RunpodError as exc:
                stale = cache.read(cache.stale_limit) if exc.status == 429 else None
                if stale is None:
                    raise
                self._log(f"rpt: pod listing rate limited; using the cached listing (<= {cache.stale_limit:.0f}s old)")
                return stale, True

    def forget_pods(self) -> None:
        """Drop the cached listing after a change to the account's pods."""
        if self.pod_cache is not None:
            self.pod_cache.clear()

    def create_pod(self, payload: Mapping[str, Any]) -> dict:
        self.forget_pods()
        return self.rest("POST", "/pods", payload)

    def stop_pod(self, pod_id: str) -> None:
        self.forget_pods()
        self.rest("POST", f"/pods/{pod_id}/stop")

    def resume_pod(self, pod_id: str, gpu_count: int = 1) -> None:
        mutation = """
        mutation ResumePod($podId: String!, $gpuCount: Int!) {
          podResume(input: {podId: $podId, gpuCount: $gpuCount}) { id }
        }
        """
        self.forget_pods()
        data = self.graphql(mutation, {"podId": pod_id, "gpuCount": gpu_count})
        if not data.get("podResume"):
            raise RunpodError(f"resume {pod_id}: empty response")

    def terminate_pod(self, pod_id: str) -> None:
        mutation = """
        mutation TerminatePod($podId: String!) { podTerminate(input: {podId: $podId}) }
        """
        self.forget_pods()
        self.graphql(mutation, {"podId": pod_id})

    # -- catalogue -------------------------------------------------------------

    def list_gpu_types(self) -> list[dict]:
        query = """
        query GpuTypes {
          gpuTypes {
            id displayName memoryInGb secureCloud communityCloud
            lowestPrice(input: {gpuCount: 1}) { minimumBidPrice uninterruptablePrice }
          }
        }
        """
        return self.graphql(query).get("gpuTypes") or []

    def list_network_volumes(self) -> list[dict]:
        return self.rest("GET", "/networkvolumes") or []

    # -- templates -------------------------------------------------------------

    _TEMPLATES_QUERY = """
    query Templates {
      myself {
        podTemplates {
          id name imageName ports containerDiskInGb volumeInGb volumeMountPath dockerArgs
          containerRegistryAuthId readme isPublic isServerless startSsh startJupyter
          env { key value }
        }
      }
    }
    """

    def list_templates(self) -> list[dict]:
        data = self.graphql(self._TEMPLATES_QUERY)
        templates = (data.get("myself") or {}).get("podTemplates") or []
        return [_normalise_template(t) for t in templates]

    def get_template(self, template_id: str) -> dict:
        for template in self.list_templates():
            if template.get("id") == template_id:
                return template
        raise RunpodError(f"template {template_id!r} not found among your templates", 404)

    def patch_template(self, template_id: str, payload: Mapping[str, Any]) -> None:
        self.rest("PATCH", f"/templates/{template_id}", payload)

    def save_template(self, template_input: Mapping[str, Any]) -> dict:
        mutation = """
        mutation SaveTemplate($input: SaveTemplateInput!) {
          saveTemplate(input: $input) { id name ports volumeInGb imageName }
        }
        """
        data = self.graphql(mutation, {"input": dict(template_input)})
        return data.get("saveTemplate") or {}


def _normalise_template(template: dict) -> dict:
    out = dict(template)
    env = template.get("env") or []
    out["env"] = {e["key"]: e["value"] for e in env}
    return out


def _parse_json(data: bytes) -> Any:
    if not data or not data.strip():
        return None
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        return None


def client_from_env(env: Mapping[str, str] | None = None, transport: Transport | None = None) -> RunpodClient:
    env = os.environ if env is None else env
    key = env.get("RUNPOD_API_KEY", "").strip()
    if not key:
        raise RunpodError("RUNPOD_API_KEY is not set. " + API_KEY_HELP)
    return RunpodClient(key, transport=transport, rate_limit_wait=rate_limit_wait_from_env(env),
                        pod_cache=cache_from_env(key, env), usage_log=usage_log_from_env(key, env),
                        backoff=backoff_from_env(key, env))
