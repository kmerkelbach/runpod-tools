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
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

REST_BASE = "https://rest.runpod.io/v1"
GRAPHQL_URL = "https://api.runpod.io/graphql"
USER_AGENT = "runpod-tools/0.1"
API_KEY_HELP = "Set RUNPOD_API_KEY (create one at https://www.runpod.io/console/user/settings)."

Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, bytes]]


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
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    return send


class RunpodClient:
    def __init__(self, api_key: str, transport: Transport | None = None, timeout: float = 30.0):
        self._key = api_key
        self._send = transport or urllib_transport(timeout)

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
        try:
            status, data = self._send(method, REST_BASE + path, self._headers(), raw)
        except OSError as exc:
            raise RunpodError(f"{method} {path}: {exc}") from exc
        parsed = _parse_json(data)
        if not 200 <= status < 300:
            message = parsed.get("error") if isinstance(parsed, dict) else None
            raise RunpodError(f"{method} {path} failed ({status}): {message or data.decode(errors='replace')}", status)
        return parsed

    def graphql(self, query: str, variables: Mapping[str, Any] | None = None) -> dict:
        payload = {"query": query, "variables": dict(variables or {})}
        try:
            status, data = self._send("POST", GRAPHQL_URL, self._headers(), json.dumps(payload).encode())
        except OSError as exc:
            raise RunpodError(f"graphql: {exc}") from exc
        parsed = _parse_json(data)
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
        data = self.graphql(self._PODS_QUERY)
        myself = data.get("myself")
        if myself is None:
            raise RunpodError("graphql: no `myself` in response; check the API key's permissions")
        return myself.get("pods") or []

    def create_pod(self, payload: Mapping[str, Any]) -> dict:
        return self.rest("POST", "/pods", payload)

    def stop_pod(self, pod_id: str) -> None:
        self.rest("POST", f"/pods/{pod_id}/stop")

    def resume_pod(self, pod_id: str, gpu_count: int = 1) -> None:
        mutation = """
        mutation ResumePod($podId: String!, $gpuCount: Int!) {
          podResume(input: {podId: $podId, gpuCount: $gpuCount}) { id }
        }
        """
        data = self.graphql(mutation, {"podId": pod_id, "gpuCount": gpu_count})
        if not data.get("podResume"):
            raise RunpodError(f"resume {pod_id}: empty response")

    def terminate_pod(self, pod_id: str) -> None:
        mutation = """
        mutation TerminatePod($podId: String!) { podTerminate(input: {podId: $podId}) }
        """
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
    return RunpodClient(key, transport=transport)
