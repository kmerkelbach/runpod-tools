"""``rpt template show|env|ports|volume`` — inspect and edit a pod template.

Why two APIs: REST ``GET /templates/{id}`` *resolves* ``{{ RUNPOD_SECRET_* }}``
placeholders to their secret values, so a read-modify-write through REST
would inline every secret into the template and break the indirection. All
reads here go through GraphQL, which returns placeholders verbatim. Env and
image changes are then PATCHed via REST; ports and volume size are not
PATCHable and go through the GraphQL ``saveTemplate`` mutation, which needs
the full template object round-tripped.

Nothing here prints a literal value under a secret-looking key.
"""

from __future__ import annotations

import re

from runpod_tools.cli import Context, UsageError, add_json_flag, add_yes_flag, table
from runpod_tools.commands.start import parse_env

PLACEHOLDER = re.compile(r"^\{\{\s*RUNPOD_SECRET_\S+\s*\}\}$")
SENSITIVE = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL")


def redact(key: str, value: str) -> str:
    if PLACEHOLDER.match(value or ""):
        return value
    if any(part in key.upper() for part in SENSITIVE):
        return f"<literal, {len(value)} chars>"
    return value


def parse_secret_ref(item: str) -> tuple[str, str]:
    env_name, _, secret = item.partition("=")
    secret = secret or env_name
    return env_name, f"{{{{ RUNPOD_SECRET_{secret} }}}}"


def merge_env(current: dict[str, str] | None, updates: dict[str, str]) -> dict[str, str]:
    merged = dict(current or {})
    merged.update(updates)
    return merged


def register(sub) -> None:
    t = sub.add_parser("template", help="inspect or edit a pod template (env, image, ports, volume)",
                       description="Template operations. Reads go through GraphQL so secret placeholders stay placeholders.")
    ts = t.add_subparsers(dest="template_command", metavar="<subcommand>")
    ts.required = True

    show = ts.add_parser("show", help="print a template (literal secrets redacted); all templates if none configured")
    show.add_argument("--template-id", help="(default: [pod].template_id)")
    add_json_flag(show)
    show.set_defaults(func=run_show)

    env = ts.add_parser("env", help="set env vars (preferably as secret references) and/or the image",
                        description="Merge env changes into the template and PATCH it. Prefer --secret-ref over --env "
                        "for anything sensitive: the value stored is '{{ RUNPOD_SECRET_<Name> }}' and Runpod "
                        "substitutes the secret at pod launch.")
    env.add_argument("--template-id")
    env.add_argument("--secret-ref", action="append", default=[], metavar="ENV[=SECRET_NAME]",
                     help="ENV = '{{ RUNPOD_SECRET_<SECRET_NAME> }}' (name defaults to ENV); repeatable")
    env.add_argument("--env", action="append", default=[], metavar="KEY=VALUE", help="literal value; repeatable")
    env.add_argument("--image", metavar="IMAGE:TAG", help="set the template's docker image")
    env.add_argument("--dry-run", action="store_true", help="show the diff, change nothing")
    add_yes_flag(env)
    env.set_defaults(func=run_env)

    ports = ts.add_parser("ports", help="expose ports on the template (e.g. --add 22/tcp for SSH/rsync)")
    ports.add_argument("--template-id")
    g = ports.add_mutually_exclusive_group(required=True)
    g.add_argument("--add", metavar="PORT/PROTO", help="append one port spec, e.g. 22/tcp or 8888/http")
    g.add_argument("--set", metavar="LIST", help="replace the whole comma-separated list")
    add_yes_flag(ports)
    ports.set_defaults(func=run_ports)

    vol = ts.add_parser("volume", help="set the template's default pod volume size")
    vol.add_argument("--template-id")
    vol.add_argument("--gb", type=int, required=True)
    add_yes_flag(vol)
    vol.set_defaults(func=run_volume)


def _template_id(args, ctx: Context) -> str:
    template_id = args.template_id or ctx.config.pod.template_id
    if not template_id:
        raise UsageError("no template: pass --template-id or set [pod].template_id in runpod-tools.toml")
    return template_id


def _redacted(template: dict) -> dict:
    out = dict(template)
    out["env"] = {k: redact(k, v) for k, v in (template.get("env") or {}).items()}
    return out


def _print_template(ctx: Context, template: dict) -> None:
    ctx.print(f"template {template.get('id')}  {template.get('name')}")
    for key in ("imageName", "ports", "containerDiskInGb", "volumeInGb", "volumeMountPath", "dockerArgs"):
        ctx.print(f"  {key:18s} {template.get(key)}")
    env = template.get("env") or {}
    ctx.print("  env:")
    for key in sorted(env):
        ctx.print(f"    {key:24s} = {redact(key, env[key])}")


def run_show(args, ctx: Context) -> int:
    client = ctx.client()
    template_id = args.template_id or ctx.config.pod.template_id
    if template_id:
        templates = [client.get_template(template_id)]
    else:
        templates = client.list_templates()
    if args.json:
        ctx.print_json([_redacted(t) for t in templates] if len(templates) != 1 else _redacted(templates[0]))
        return 0
    if len(templates) == 1:
        _print_template(ctx, templates[0])
        return 0
    rows = [[t.get("id"), t.get("name"), t.get("imageName"), t.get("ports"), len(t.get("env") or {})] for t in templates]
    ctx.print(table(rows, ["id", "name", "image", "ports", "env vars"]))
    ctx.print("\n(`rpt template show --template-id <id>` for details)")
    return 0


def run_env(args, ctx: Context) -> int:
    updates: dict[str, str] = {}
    for item in args.secret_ref:
        key, value = parse_secret_ref(item)
        updates[key] = value
    try:
        literals = parse_env(args.env)
    except ValueError as exc:
        raise UsageError(str(exc)) from exc
    for key, value in literals.items():
        if not PLACEHOLDER.match(value) and any(p in key.upper() for p in SENSITIVE):
            ctx.warn(f"warning: {key} looks like a secret but is being stored as a literal; prefer --secret-ref {key}")
    updates.update(literals)
    if not updates and args.image is None:
        raise UsageError("nothing to do: pass --secret-ref, --env, or --image")

    template_id = _template_id(args, ctx)
    template = ctx.client().get_template(template_id)
    current = template.get("env") or {}
    payload: dict = {}

    if updates:
        merged = merge_env(current, updates)
        changed = False
        ctx.print(f"template {template_id} env:")
        for key, new in updates.items():
            old = current.get(key)
            if old is None:
                ctx.print(f"  + {key:24s} = {redact(key, new)}")
                changed = True
            elif old != new:
                ctx.print(f"  ~ {key:24s} = {redact(key, new)}   (was {redact(key, old)})")
                changed = True
            else:
                ctx.print(f"  = {key:24s} unchanged")
        if changed:
            payload["env"] = merged
    if args.image is not None:
        if args.image == template.get("imageName"):
            ctx.print(f"image unchanged: {args.image}")
        else:
            ctx.print(f"image: {template.get('imageName')} -> {args.image}")
            payload["imageName"] = args.image

    if not payload:
        ctx.print("no changes.")
        return 0
    if args.dry_run:
        ctx.print("dry run: nothing patched.")
        return 0
    if not ctx.confirm(f"patch template {template_id}?", args.yes):
        ctx.print("cancelled.")
        return 0
    ctx.client().patch_template(template_id, payload)
    ctx.print("template updated.")
    return 0


def _save_input(template: dict, **changes) -> dict:
    """The full SaveTemplateInput (GraphQL needs every field, or it resets them)."""
    inp = {
        "id": template["id"],
        "name": template.get("name"),
        "imageName": template.get("imageName"),
        "ports": template.get("ports") or "",
        "containerDiskInGb": template.get("containerDiskInGb") or 20,
        "volumeInGb": template.get("volumeInGb") or 0,
        "dockerArgs": template.get("dockerArgs") or "",
        "env": [{"key": k, "value": v} for k, v in (template.get("env") or {}).items()],
    }
    if template.get("volumeMountPath"):
        inp["volumeMountPath"] = template["volumeMountPath"]
    inp.update(changes)
    return inp


def run_ports(args, ctx: Context) -> int:
    template_id = _template_id(args, ctx)
    template = ctx.client().get_template(template_id)
    current = [p for p in (template.get("ports") or "").split(",") if p]
    if args.add:
        if args.add in current:
            ctx.print(f"{args.add} already exposed on {template_id}: {','.join(current)}")
            return 0
        new = ",".join([*current, args.add])
    else:
        new = ",".join(p.strip() for p in args.set.split(",") if p.strip())
        if new == ",".join(current):
            ctx.print(f"ports already {new!r}")
            return 0
    ctx.print(f"ports: {','.join(current) or '(none)'} -> {new}")
    if not ctx.confirm(f"save template {template_id}?", args.yes):
        ctx.print("cancelled.")
        return 0
    saved = ctx.client().save_template(_save_input(template, ports=new))
    ctx.print(f"saved; ports now {saved.get('ports')}")
    return 0


def run_volume(args, ctx: Context) -> int:
    template_id = _template_id(args, ctx)
    template = ctx.client().get_template(template_id)
    if template.get("volumeInGb") == args.gb:
        ctx.print(f"volume already {args.gb} GB")
        return 0
    ctx.print(f"volume: {template.get('volumeInGb')} GB -> {args.gb} GB")
    if not ctx.confirm(f"save template {template_id}?", args.yes):
        ctx.print("cancelled.")
        return 0
    saved = ctx.client().save_template(_save_input(template, volumeInGb=args.gb))
    ctx.print(f"saved; volume now {saved.get('volumeInGb')} GB")
    return 0
