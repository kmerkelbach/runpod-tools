"""``rpt start`` — create a pod from the configured template (or an image).

Defaults come from ``[pod]`` in runpod-tools.toml; every flag overrides one
value for this call. A configured ``name_prefix`` is applied to every name,
including explicit ``--name`` values, so shared-account naming conventions
hold no matter how the command is invoked.
"""

from __future__ import annotations

import random
import re
import shlex
import string
import time
from collections.abc import Callable
from datetime import datetime

from runpod_tools.api import RunpodError
from runpod_tools.cli import Context, OperationError, UsageError, add_json_flag
from runpod_tools.config import PodDefaults
from runpod_tools.sshutil import NoEndpoint, keyscan, wait_for_ssh

_DURATION = re.compile(r"^(\d+(?:\.\d+)?)([hms])$")
_UNIT = {"h": 3600, "m": 60, "s": 1}


def parse_duration(text: str | None) -> float:
    """'2h' | '30m' | '45s' | '1.5h' -> seconds. Empty/None -> 0."""
    if not text:
        return 0
    m = _DURATION.match(text.strip().lower())
    if not m:
        raise ValueError(f"invalid duration {text!r}: use e.g. 2h, 30m, 45s")
    return float(m.group(1)) * _UNIT[m.group(2)]


def _random_id() -> str:
    return "".join(random.choices(string.ascii_lowercase, k=6))


def default_pod_name(prefix: str, experiment: str | None, now: datetime | None = None,
                     rand: Callable[[], str] = _random_id) -> str:
    stamp = (now or datetime.now()).strftime("%Y%m%d")
    head = prefix or "pod_"
    if experiment:
        head = f"{head}{experiment}_"
    return f"{head}{stamp}_{rand()}"


def apply_prefix(name: str, prefix: str) -> str:
    return name if (not prefix or name.startswith(prefix)) else f"{prefix}{name}"


def parse_env(items: list[str] | None) -> dict[str, str]:
    env: dict[str, str] = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError(f"--env expects KEY=VALUE, got {item!r}")
        key, value = item.split("=", 1)
        env[key] = value
    return env


def build_payload(cfg: PodDefaults, args) -> dict:
    """The REST ``POST /pods`` body, from config defaults overridden by CLI args."""
    template_id = args.template_id if args.template_id is not None else cfg.template_id
    image = args.image if args.image is not None else ""
    payload: dict = {
        "name": args.name,
        "gpuTypeIds": [args.gpu_type or cfg.gpu_type],
        "gpuCount": args.gpu_count or cfg.gpu_count,
        "cloudType": args.cloud_type or cfg.cloud_type,
        "containerDiskInGb": args.container_disk_gb or cfg.container_disk_gb,
        "supportPublicIp": not args.no_public_ip and cfg.support_public_ip,
    }
    if template_id:
        payload["templateId"] = template_id
        if image and not args.image_override:
            raise ValueError("--image with a template needs --image-override (the template's image is used otherwise)")
        if image:
            payload["imageName"] = image
    else:
        image = image or cfg.image
        if not image:
            raise ValueError("no template or image: pass --template-id or --image, or set [pod].template_id in runpod-tools.toml")
        payload["imageName"] = image

    volume_gb = args.volume_gb if args.volume_gb is not None else cfg.volume_gb
    if volume_gb and volume_gb > 0:
        payload["volumeInGb"] = volume_gb
        payload["volumeMountPath"] = "/workspace"
    elif args.volume_gb == 0:
        payload["volumeInGb"] = 0  # explicit: no pod volume. Config 0 means "template/API default".
    network_volume = args.network_volume_id or cfg.network_volume_id
    if network_volume:
        payload["networkVolumeId"] = network_volume
    env = parse_env(args.env)
    if env:
        payload["env"] = env
    if args.docker_start_cmd:
        payload["dockerStartCmd"] = shlex.split(args.docker_start_cmd)
    ports = args.ports if args.ports is not None else cfg.ports
    if ports:
        payload["ports"] = [p.strip() for p in ports.split(",") if p.strip()]
    return payload


def register(sub) -> None:
    p = sub.add_parser(
        "start", help="create a pod (from the configured template or an image)",
        description="Create a pod. Prints `pod_id=<id>` as the last line so shells can capture it. "
        "Defaults come from [pod] in runpod-tools.toml; flags override for this call.",
        epilog="Secrets: reference Runpod secrets in --env as '{{ RUNPOD_SECRET_<Name> }}'; never paste literal tokens.",
    )
    p.add_argument("--name", help="pod name (configured name_prefix is always applied)")
    p.add_argument("--experiment", help="tag inserted into the default name: <prefix><experiment>_<date>_<id>")
    p.add_argument("--template-id", help="template supplying image, env, ports (default: config)")
    p.add_argument("--image", help="docker image; without a template it defines the pod")
    p.add_argument("--image-override", action="store_true",
                   help="use --image on top of the template (keeps its env/ports, swaps the image)")
    p.add_argument("--gpu-type", help="GPU type id, see `rpt gpus` (default: config)")
    p.add_argument("--gpu-count", type=int, help="number of GPUs (default: config)")
    p.add_argument("--cloud-type", choices=["SECURE", "COMMUNITY", "ALL"], help="(default: config)")
    p.add_argument("--container-disk-gb", type=int, help="(default: config)")
    p.add_argument("--volume-gb", type=int,
                   help="pod volume at /workspace. An explicit 0 asks for no volume; config 0 leaves the template/API default")
    p.add_argument("--network-volume-id", help="attach an existing network volume (default: config)")
    p.add_argument("--env", action="append", metavar="KEY=VALUE", help="extra env var; repeatable")
    p.add_argument("--docker-start-cmd", metavar="CMD", help="container start command override (REST dockerStartCmd)")
    p.add_argument("--ports", help="comma-separated ports, e.g. 22/tcp,8888/http (default: config)")
    p.add_argument("--no-public-ip", action="store_true",
                   help="drop the public-IP scheduling constraint (pod may get no SSH/rsync endpoint)")
    p.add_argument("--delay", help="wait before creating, e.g. 2h or 30m (Ctrl-C cancels)")
    p.add_argument("--retry", type=int, metavar="SECONDS", help="retry interval when no capacity is available")
    p.add_argument("--max-retries", type=int, default=10, help="attempts before giving up (default 10)")
    p.add_argument("--wait", action="store_true", help="block until SSH is reachable and record its host key")
    p.add_argument("--wait-timeout", type=int, default=900, help="seconds for --wait (default 900)")
    add_json_flag(p)
    p.set_defaults(func=run)


def run(args, ctx: Context) -> int:
    cfg = ctx.config.pod
    try:
        delay = parse_duration(args.delay)
    except ValueError as exc:
        raise UsageError(str(exc)) from exc
    args.name = apply_prefix(args.name or default_pod_name(cfg.name_prefix, args.experiment), cfg.name_prefix)
    try:
        payload = build_payload(cfg, args)
    except ValueError as exc:
        raise UsageError(str(exc)) from exc

    client = ctx.client()  # fail on a missing key now, not after a two-hour --delay
    if delay > 0:
        ctx.warn(f"waiting {args.delay} before creating pod {args.name!r} (Ctrl-C cancels)")
        _sleep_in_chunks(delay, ctx)

    attempts = args.max_retries if args.retry else 1
    result = None
    for attempt in range(1, attempts + 1):
        ctx.warn(f"creating {payload['gpuCount']}x {payload['gpuTypeIds'][0]} on {payload['cloudType']} as {args.name!r}"
                 + (f" (attempt {attempt}/{attempts})" if attempts > 1 else ""))
        try:
            result = client.create_pod(payload)
            break
        except RunpodError as exc:
            if not _retryable(exc) or attempt == attempts:
                if attempts > 1 and _retryable(exc):
                    raise OperationError(f"pod not created after {attempts} attempts: {exc}") from exc
                raise
            if exc.status >= 500:
                # a timed-out or failed create may still have created the pod server-side
                orphan = _find_by_name(client, args.name)
                if orphan:
                    ctx.warn(f"  {exc.message}; but pod {orphan['id']} named {args.name!r} exists; using it")
                    result = orphan
                    break
            ctx.warn(f"  {exc.message}; retrying in {args.retry}s")
            time.sleep(args.retry)

    assert result is not None
    pod_id = result.get("id")
    if args.json:
        ctx.print_json(_redacted(result))
    else:
        machine = result.get("machine") or {}
        ctx.print(f"created pod {pod_id}  name={result.get('name', args.name)}  "
                  f"gpu={machine.get('gpuTypeId', payload['gpuTypeIds'][0])}  "
                  f"dc={machine.get('dataCenterId', '?')}  cost={result.get('costPerHr', 0):.2f} $/hr")
    if args.wait:
        try:
            ep = wait_for_ssh(client, pod_id, timeout=args.wait_timeout,
                              keyscan=lambda e: keyscan(e, runner=ctx.runner), log=ctx.warn)
        except (TimeoutError, NoEndpoint) as exc:
            # the pod exists and is billing: hand back its id before failing
            if not args.json:
                ctx.print(f"pod_id={pod_id}")
            raise OperationError(f"{exc}. Pod {pod_id} is still running; `rpt pods` to inspect, `rpt stop --pod {pod_id} -y` to stop.") from exc
        if args.json:
            ctx.warn(f"ssh ready at {ep.ip}:{ep.port}")
        else:
            ctx.print(f"ssh ready at {ep.ip}:{ep.port}")
    if not args.json:
        ctx.print(f"pod_id={pod_id}")
    return 0


def _retryable(exc: RunpodError) -> bool:
    """Capacity-style 4xx and server-side 5xx are worth another try; auth and client bugs are not."""
    if exc.status is None:
        return False
    if exc.status in (400, 401, 403, 404, 422):
        return "available" in exc.message.lower() or "capacity" in exc.message.lower() or "stock" in exc.message.lower()
    return exc.status >= 500 or exc.status == 429


def _find_by_name(client, name: str) -> dict | None:
    try:
        return next((p for p in client.list_pods() if p.get("name") == name), None)
    except RunpodError:
        return None


def _redacted(result: dict) -> dict:
    from runpod_tools.commands.template import redact

    out = dict(result)
    env = result.get("env")
    if isinstance(env, dict):
        out["env"] = {k: redact(k, str(v)) for k, v in env.items()}
    elif isinstance(env, list):
        out["env"] = [{**e, "value": redact(str(e.get("key", "")), str(e.get("value", "")))} if isinstance(e, dict) else e
                      for e in env]
    return out


def _sleep_in_chunks(seconds: float, ctx: Context) -> None:
    remaining = seconds
    step = max(1.0, min(300.0, seconds / 10))
    while remaining > 0:
        chunk = min(step, remaining)
        time.sleep(chunk)
        remaining -= chunk
        if remaining > 0:
            ctx.warn(f"  {remaining / 60:.1f} min remaining")
