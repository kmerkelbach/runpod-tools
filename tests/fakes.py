"""Test doubles shared across the suite. No network, no subprocesses."""

from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass
class Call:
    method: str
    url: str
    headers: dict[str, str]
    body: dict | None


@dataclass
class FakeTransport:
    """Queue of (status, body) responses; records every request it receives."""

    responses: list[tuple[int, object, dict[str, str]]] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)

    def __call__(self, method: str, url: str, headers: dict[str, str], body: bytes | None):
        self.calls.append(Call(method, url, dict(headers), json.loads(body) if body else None))
        if not self.responses:
            raise AssertionError(f"unexpected request {method} {url}")
        status, payload, headers = self.responses.pop(0)
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return status, raw, headers

    def queue(self, status: int, payload: object, headers: dict[str, str] | None = None) -> "FakeTransport":
        self.responses.append((status, payload, headers or {}))
        return self

    @property
    def last(self) -> Call:
        return self.calls[-1]


@dataclass
class FakeClock:
    """A clock that only moves when the client sleeps; records every sleep."""

    now: float = 0.0
    sleeps: list[float] = field(default_factory=list)

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


RATE_LIMITED = (429, {"error": "Too Many Requests"})


def graphql_ok(data: dict) -> tuple[int, dict]:
    return 200, {"data": data}


def pod(**over) -> dict:
    """A pod dict shaped like the GraphQL ``myself.pods`` entries."""
    base = {
        "id": "abc123",
        "name": "my_pod",
        "desiredStatus": "RUNNING",
        "imageName": "example/image:tag",
        "costPerHr": 2.0,
        "gpuCount": 1,
        "machine": {"gpuDisplayName": "H200 SXM", "podHostId": "abc123-1234"},
        "runtime": {
            "uptimeInSeconds": 5400,
            "ports": [
                {"ip": "1.2.3.4", "isIpPublic": True, "privatePort": 22, "publicPort": 40022, "type": "tcp"}
            ],
        },
    }
    base.update(over)
    return base
