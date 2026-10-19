"""The same client interface over Runpod's REST API v2 (``https://api.runpod.io/v2``).

REST v1 stops serving on 2026-11-15 and GraphQL is retired in early 2027;
v2 covers everything ``rpt`` does (see docs/rest-v2-notes.md). Commands are
written against v1/GraphQL-shaped dicts, so this client translates at the
boundary: v2 pods, GPU types and templates come back in the shapes
``RunpodClient`` returns, and v1-shaped create/patch payloads are translated
to v2 bodies. Commands, tables and ``--json`` output stay as they are.

Differences that cannot be hidden:

* v2 has no "is the IP public" flag and no ``supportPublicIp`` on create; a
  pod's 22/tcp counts as reachable only when v2 reports ``ssh.direct``.
* v2 ``start`` takes no GPU count; ``resume_pod`` ignores ``gpu_count``.
* v2 creates one GPU type in one cloud; ``cloudType: ALL`` tries SECURE, then
  COMMUNITY.
* rpt's ``allowed_cuda_versions`` (hosts whose driver supports a version) is
  sent as v2's ``minCudaVersion``; v2's ``allowedCudaVersions`` is an exact
  match and would turn away newer drivers.
* v2 reports ``cost`` 0.0 for a stopped pod (v1/GraphQL reported the rate).
* the GPU display name is not on the pod; it comes from the GPU catalog,
  cached for a day.
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping, Sequence
from typing import Any

from runpod_tools.api import (
    API_KEY_HELP,
    V2_BASE,
    RunpodClient,
    RunpodError,
    rate_limit_wait_from_env,
)
from runpod_tools.podcache import (
    PodCache,
    account_tag,
    backoff_from_env,
    cache_dir,
    cache_from_env,
    usage_log_from_env,
)

PAGE_LIMIT = 1000
CATALOG_TTL = 24 * 3600.0

# v2 status -> the v1/GraphQL desiredStatus commands select on.
STATUS = {
    "PROVISIONING": "CREATED",
    "STARTING": "CREATED",
    "RUNNING": "RUNNING",
    "EXITED": "EXITED",
    "TERMINATED": "TERMINATED",
    "ERROR": "ERROR",
}

DEFAULT_MOUNT_PATH = "/workspace"


class RestV2Client(RunpodClient):
    rest_base = V2_BASE

    def __init__(self, *args, catalog_cache: PodCache | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.catalog_cache = catalog_cache

    # -- paging ----------------------------------------------------------------

    def _list(self, path: str, key: str) -> list[dict]:
        items: list[dict] = []
        cursor = None
        while True:
            query = f"?limit={PAGE_LIMIT}" + (f"&cursor={cursor}" if cursor else "")
            data = self.rest("GET", path + query) or {}
            items.extend(data.get(key) or [])
            page = data.get("pagination") or {}
            cursor = page.get("nextCursor")
            if not (page.get("hasNextPage") and cursor):
                return items

    # -- pods ------------------------------------------------------------------

    def list_pods(self) -> list[dict]:
        names = self._gpu_names()
        pods = [legacy_pod(p, names) for p in self._list("/pods", "pods")]
        if self.pod_cache is not None:
            self.pod_cache.write(pods)
        return pods

    def create_pod(self, payload: Mapping[str, Any]) -> dict:
        self.forget_pods()
        bodies = create_bodies(payload)
        for index, body in enumerate(bodies):
            try:
                pod = self.rest("POST", "/pods", body) or {}
            except RunpodError as exc:
                # cloudType ALL: a capacity refusal in one cloud moves on to the next.
                if exc.status == 400 and index + 1 < len(bodies):
                    continue
                raise
            return legacy_created(pod)
        raise RunpodError("create: no request body")  # unreachable: create_bodies is never empty

    def stop_pod(self, pod_id: str) -> None:
        self.forget_pods()
        self.rest("POST", f"/pods/{pod_id}/action", {"action": "stop"})

    def resume_pod(self, pod_id: str, gpu_count: int = 1) -> None:
        self.forget_pods()
        self.rest("POST", f"/pods/{pod_id}/action", {"action": "start"})

    def terminate_pod(self, pod_id: str) -> None:
        self.forget_pods()
        self.rest("DELETE", f"/pods/{pod_id}")

    # -- catalogue -------------------------------------------------------------

    def _catalog(self) -> list[dict]:
        gpus = (self.rest("GET", "/catalog/gpus") or {}).get("gpus") or []
        if self.catalog_cache is not None:
            self.catalog_cache.write(gpus)
        return gpus

    def _gpu_names(self) -> dict[str, str]:
        """GPU type id -> display name, from a catalog at most a day old. A
        catalog that cannot be read leaves the ids as names."""
        gpus = self.catalog_cache.read(self.catalog_cache.ttl) if self.catalog_cache is not None else None
        if gpus is None:
            try:
                gpus = self._catalog()
            except RunpodError:
                gpus = []
        return {g.get("id"): g.get("name") for g in gpus if g.get("id")}

    def list_gpu_types(self) -> list[dict]:
        return [legacy_gpu(g) for g in self._catalog()]

    def list_network_volumes(self) -> list[dict]:
        return [
            {"id": v.get("id"), "name": v.get("name"), "size": v.get("size"),
             "dataCenterId": v.get("dataCenter"), "type": v.get("type")}
            for v in self._list("/network-volumes", "networkVolumes")
        ]

    # -- templates -------------------------------------------------------------

    def list_templates(self) -> list[dict]:
        return [legacy_template(t) for t in self._list("/templates", "templates")]

    def get_template(self, template_id: str) -> dict:
        return legacy_template(self.rest("GET", f"/templates/{template_id}") or {})

    def patch_template(self, template_id: str, payload: Mapping[str, Any]) -> None:
        self.rest("PATCH", f"/templates/{template_id}", template_patch(payload))

    def save_template(self, template_input: Mapping[str, Any], changed: Sequence[str] = ()) -> dict:
        """v2 PATCHes in place: only the changed fields ``rpt`` saves through
        saveTemplate (ports, volume size) are sent, never the env."""
        template_id = template_input["id"]
        body: dict[str, Any] = {}
        if "ports" in changed:
            body["ports"] = split_ports(template_input["ports"])
        if "volumeInGb" in changed:
            body["mounts"] = {"persistent": {"size": template_input["volumeInGb"],
                                             "path": template_input.get("volumeMountPath") or DEFAULT_MOUNT_PATH}}
        if not body:
            raise RunpodError(f"save template {template_id}: no supported field changed ({', '.join(changed) or 'none'})")
        saved = legacy_template(self.rest("PATCH", f"/templates/{template_id}", body) or {})
        return {k: saved.get(k) for k in ("id", "name", "ports", "volumeInGb", "imageName")}


# -- translation ---------------------------------------------------------------


def _is_public(ip: Any) -> bool | None:
    try:
        return not ipaddress.ip_address(str(ip)).is_private
    except ValueError:
        return None


def legacy_ports(pod: dict) -> list[dict]:
    """GraphQL-shaped runtime ports. v2 has no ``isIpPublic``: 22/tcp is public
    only when v2 reports a direct ssh endpoint, and any other port is public
    when its IP is not a private address."""
    runtime = pod.get("runtime") or {}
    direct = (pod.get("ssh") or {}).get("direct") or None
    ports = []
    for port in runtime.get("ports") or []:
        if not isinstance(port, dict):
            continue
        if port.get("private") == 22 and port.get("type") == "tcp":
            continue
        ports.append({"ip": port.get("ip"), "isIpPublic": _is_public(port.get("ip")) if port.get("ip") else None,
                      "privatePort": port.get("private"), "publicPort": port.get("public"),
                      "type": port.get("type")})
    if direct and direct.get("host") and direct.get("port"):
        ports.insert(0, {"ip": direct["host"], "isIpPublic": True, "privatePort": 22,
                         "publicPort": int(direct["port"]), "type": "tcp"})
    return ports


def legacy_pod(pod: dict, gpu_names: Mapping[str, str]) -> dict:
    gpu = pod.get("gpu") or {}
    runtime = pod.get("runtime")
    proxy = (pod.get("ssh") or {}).get("proxy") or {}
    return {
        "id": pod.get("id"),
        "name": pod.get("name"),
        "desiredStatus": STATUS.get(pod.get("status"), pod.get("status")),
        "imageName": pod.get("image"),
        "costPerHr": pod.get("cost"),
        "gpuCount": gpu.get("count") or (1 if gpu else 0),
        "machine": {"gpuDisplayName": gpu_names.get(gpu.get("id"), gpu.get("id")) if gpu else "CPU",
                    "podHostId": proxy.get("username")},
        "runtime": None if runtime is None else {
            "uptimeInSeconds": runtime.get("uptime"),
            "ports": legacy_ports(pod),
        },
    }


def legacy_created(pod: dict) -> dict:
    """The fields ``rpt start`` reads from a v1 create response."""
    gpu = pod.get("gpu") or {}
    return {
        "id": pod.get("id"),
        "name": pod.get("name"),
        "costPerHr": pod.get("cost") or 0,
        "machine": {"gpuTypeId": gpu.get("id"), "dataCenterId": pod.get("dataCenterId")},
    }


def create_bodies(payload: Mapping[str, Any]) -> list[dict]:
    """v2 create bodies for a v1 ``POST /pods`` payload, one per cloud to try."""
    gpu: dict[str, Any] = {"id": (payload.get("gpuTypeIds") or [None])[0], "count": payload.get("gpuCount") or 1}
    if payload.get("allowedCudaVersions"):
        # rpt's setting means "the host driver supports this CUDA version"; v2's
        # allowedCudaVersions matches exactly (13.0 would exclude a 13.2 host),
        # and minCudaVersion is the floor that says what was meant.
        gpu["minCudaVersion"] = min(payload["allowedCudaVersions"], key=cuda_key)
    body: dict[str, Any] = {"name": payload["name"], "gpu": gpu}
    if payload.get("templateId"):
        body["templateId"] = payload["templateId"]
    if payload.get("imageName"):
        body["image"] = payload["imageName"]
    if payload.get("containerDiskInGb"):
        body["disk"] = payload["containerDiskInGb"]
    if payload.get("networkVolumeId"):
        body["mounts"] = {"network": [{"volumeId": payload["networkVolumeId"],
                                       "path": payload.get("volumeMountPath") or DEFAULT_MOUNT_PATH}]}
    elif payload.get("volumeInGb"):
        body["mounts"] = {"persistent": {"size": payload["volumeInGb"],
                                         "path": payload.get("volumeMountPath") or DEFAULT_MOUNT_PATH}}
    for key in ("env", "ports", "dataCenterIds"):
        if payload.get(key):
            body[key] = payload[key]
    if payload.get("dockerStartCmd"):
        body["cmd"] = list(payload["dockerStartCmd"])
    cloud = payload.get("cloudType") or "SECURE"
    clouds = ["SECURE", "COMMUNITY"] if cloud == "ALL" else [cloud]
    return [{**body, "cloud": c} for c in clouds]


def cuda_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in str(version).split(".") if part.isdigit())


def legacy_gpu(gpu: dict) -> dict:
    price = gpu.get("price") or {}
    on_demand = price.get("secure") if gpu.get("secure") else price.get("community")
    return {
        "id": gpu.get("id"),
        "displayName": gpu.get("name"),
        "memoryInGb": gpu.get("memory"),
        "secureCloud": gpu.get("secure"),
        "communityCloud": gpu.get("community"),
        "lowestPrice": {"uninterruptablePrice": on_demand or None, "minimumBidPrice": None},
    }


def split_ports(ports: Any) -> list[str]:
    if isinstance(ports, list):
        return [str(p).strip() for p in ports if str(p).strip()]
    return [p.strip() for p in str(ports or "").split(",") if p.strip()]


def legacy_template(template: dict) -> dict:
    persistent = (template.get("mounts") or {}).get("persistent") or {}
    return {
        "id": template.get("id"),
        "name": template.get("name"),
        "imageName": template.get("image"),
        "ports": ",".join(split_ports(template.get("ports"))),
        "containerDiskInGb": template.get("disk"),
        "volumeInGb": persistent.get("size") or 0,
        "volumeMountPath": persistent.get("path"),
        "dockerArgs": template.get("args") or "",
        "containerRegistryAuthId": template.get("registry"),
        "isPublic": template.get("public"),
        "isServerless": template.get("serverless"),
        "startSsh": template.get("startSsh"),
        "startJupyter": template.get("startJupyter"),
        "env": dict(template.get("env") or {}),
    }


def template_patch(payload: Mapping[str, Any]) -> dict:
    """A v1 template PATCH (env, imageName) as a v2 PATCH body."""
    body: dict[str, Any] = {}
    if "env" in payload:
        body["env"] = dict(payload["env"])
    if "imageName" in payload:
        body["image"] = payload["imageName"]
    if "containerDiskInGb" in payload:
        body["disk"] = payload["containerDiskInGb"]
    return body


def client_v2_from_env(env: Mapping[str, str] | None = None, transport=None) -> RestV2Client:
    env = os.environ if env is None else env
    key = env.get("RUNPOD_API_KEY", "").strip()
    if not key:
        raise RunpodError("RUNPOD_API_KEY is not set. " + API_KEY_HELP)
    pods = cache_from_env(key, env)

    # v2 listings carry other shapes than v1/GraphQL; keep their caches apart
    # so an rpt and an rpt2 on one account never read each other's file.
    if pods is not None:
        pods.path = pods.path.with_name(f"v2-{pods.path.name}")
    catalog = PodCache(cache_dir(env) / f"v2-gpus-{account_tag(key)}.json", ttl=CATALOG_TTL)
    return RestV2Client(key, transport=transport, rate_limit_wait=rate_limit_wait_from_env(env),
                        pod_cache=pods, usage_log=usage_log_from_env(key, env),
                        backoff=backoff_from_env(key, env), catalog_cache=catalog)


def selected_client_from_env(env: Mapping[str, str] | None = None, transport=None) -> RunpodClient:
    """The client ``rpt`` uses: REST v2 when ``RPT_API=v2``, else v1/GraphQL.
    For code that imports runpod_tools instead of running rpt."""
    from runpod_tools.api import client_from_env

    env = os.environ if env is None else env
    if env.get("RPT_API", "").strip().lower() == "v2":
        return client_v2_from_env(env, transport)
    return client_from_env(env, transport)
