"""``rpt push`` / ``rpt pull`` / ``rpt fetch`` — rsync between the project tree and pods.

All three need ``[sync]`` in runpod-tools.toml (at least ``remote_dest``).

* push: local tree -> pod, with ``push_excludes``. ``--delete`` is opt-in and
  announced loudly, because pods often hold generated data under the tree.
* pull: pod -> local tree, never deletes, capped by ``pull_max_size``,
  refuses to overwrite an unclean git checkout.
* fetch: pod ``<remote_dest>/<dir>`` -> local ``<local_root>/<dir>`` for each
  ``fetch_dirs`` entry (results, logs, ...) with per-dir ``fetch_excludes``.
  A directory missing on the pod (rsync exit 23) is skipped, not fatal.
"""

from __future__ import annotations

from runpod_tools.cli import Context, OperationError, UsageError, add_pod_selection
from runpod_tools.config import SyncConfig
from runpod_tools.pods import NO_ENDPOINT_NOTE, select_pods, ssh_endpoint
from runpod_tools.sshutil import rsync_pull, rsync_push

RSYNC_PARTIAL = 23  # "some files/attrs were not transferred" — e.g. the remote dir does not exist


def register(sub) -> None:
    p = sub.add_parser("push", help="rsync the project tree to one or all running pods",
                       description="Upload [sync].local_root to <pod>:[sync].remote_dest, honouring push_excludes.")
    add_pod_selection(p, multi=True)
    p.add_argument("--delete", action="store_true", help="also delete remote files absent locally (dangerous)")
    p.add_argument("--dry-run", "-n", action="store_true")
    p.set_defaults(func=run_push)

    q = sub.add_parser("pull", help="rsync code back from a pod (never deletes; refuses a dirty git tree)",
                       description="Download <pod>:[sync].remote_dest into [sync].local_root. Adds/updates only.")
    add_pod_selection(q, multi=False)
    q.add_argument("--allow-dirty", action="store_true", help="pull even if the local git tree has changes")
    q.add_argument("--dry-run", "-n", action="store_true")
    q.set_defaults(func=run_pull)

    f = sub.add_parser("fetch", help="download output directories (results, logs, ...) from pods",
                       description="For each of [sync].fetch_dirs, rsync <remote_dest>/<dir>/ to <local_root>/<dir>/.")
    add_pod_selection(f, multi=True)
    f.add_argument("--dir", action="append", metavar="NAME", help="override fetch_dirs; repeatable")
    f.add_argument("--dry-run", "-n", action="store_true")
    f.set_defaults(func=run_fetch)


def _sync_config(ctx: Context) -> SyncConfig:
    sync = ctx.config.sync
    if not sync.remote_dest:
        raise UsageError("[sync].remote_dest is not configured. Run `rpt init` and fill in runpod-tools.toml.")
    if not sync.local_root.is_dir():
        raise UsageError(f"[sync].local_root does not exist: {sync.local_root}")
    return sync


def _endpoint(pod: dict):
    ep = ssh_endpoint(pod)
    if ep is None:
        raise OperationError(f"pod {pod.get('name')} ({pod.get('id')}): {NO_ENDPOINT_NOTE}")
    return ep


def _rsync(ctx: Context, argv: list[str], what: str, *, allow_partial: bool = False) -> int:
    result = ctx.runner(argv)
    if result.returncode == RSYNC_PARTIAL and not allow_partial:
        raise OperationError(f"rsync {what}: partial transfer (exit 23); some files were not transferred, see rsync output")
    if result.returncode not in (0, RSYNC_PARTIAL):
        raise OperationError(f"rsync failed ({what}) with exit code {result.returncode}")
    return result.returncode


def run_push(args, ctx: Context) -> int:
    sync = _sync_config(ctx)
    ssh = ctx.config.ssh
    if args.delete:
        ctx.warn("push --delete: files absent locally will be REMOVED on the pod under " + sync.remote_dest)
    pods = select_pods(ctx.client().list_pods(), ids=args.pod, select_all=args.all, statuses=["RUNNING"],
                       interactive=ctx.isatty, multi=True, name_prefix=ctx.config.pod.name_prefix)
    for pod in pods:
        ep = _endpoint(pod)
        ctx.print(f"push {sync.local_root} -> {pod.get('name')} ({ep.ip}:{ep.port}):{sync.remote_dest}")
        argv = rsync_push(sync.local_root, ep, ssh.key, sync.remote_dest, sync.push_excludes,
                          user=ssh.user, delete=args.delete, dry_run=args.dry_run)
        _rsync(ctx, argv, f"push to {pod.get('name')}")
    ctx.print(f"pushed to {len(pods)} pod(s)" + (" (dry run)" if args.dry_run else ""))
    return 0


def _git_is_clean(ctx: Context, root) -> bool:
    if not (root / ".git").exists():
        return True
    result = ctx.runner(["git", "-C", str(root), "status", "--porcelain"], capture_output=True, text=True)
    return result.returncode == 0 and not result.stdout.strip()


def run_pull(args, ctx: Context) -> int:
    sync = _sync_config(ctx)
    ssh = ctx.config.ssh
    if not args.allow_dirty and not _git_is_clean(ctx, sync.local_root):
        raise UsageError(f"{sync.local_root} is not clean; commit or stash first, or pass --allow-dirty")
    pods = select_pods(ctx.client().list_pods(), ids=args.pod, select_all=False, statuses=["RUNNING"],
                       interactive=ctx.isatty, name_prefix=ctx.config.pod.name_prefix)
    pod = pods[0]
    ep = _endpoint(pod)
    excludes = list(dict.fromkeys([*sync.push_excludes, *sync.pull_excludes]))
    ctx.print(f"pull {pod.get('name')}:{sync.remote_dest} -> {sync.local_root}")
    argv = rsync_pull(ep, ssh.key, sync.remote_dest, sync.local_root, excludes,
                      user=ssh.user, max_size=sync.pull_max_size or None, dry_run=args.dry_run)
    _rsync(ctx, argv, "pull")
    ctx.print("pulled" + (" (dry run)" if args.dry_run else ""))
    return 0


def run_fetch(args, ctx: Context) -> int:
    sync = _sync_config(ctx)
    ssh = ctx.config.ssh
    dirs = args.dir or sync.fetch_dirs
    pods = select_pods(ctx.client().list_pods(), ids=args.pod, select_all=args.all, statuses=["RUNNING"],
                       interactive=ctx.isatty, multi=True, name_prefix=ctx.config.pod.name_prefix)
    for pod in pods:
        ep = _endpoint(pod)
        for name in dirs:
            local_dir = sync.local_root / name
            local_dir.mkdir(parents=True, exist_ok=True)
            remote_dir = sync.remote_dest.rstrip("/") + "/" + name + "/"
            ctx.print(f"fetch {pod.get('name')}:{remote_dir} -> {local_dir}")
            argv = rsync_pull(ep, ssh.key, remote_dir, local_dir, sync.fetch_excludes.get(name, []),
                              user=ssh.user, max_size=None, dry_run=args.dry_run)
            if _rsync(ctx, argv, f"fetch {name}", allow_partial=True) == RSYNC_PARTIAL:
                ctx.print(f"  {name}/ missing or partial on {pod.get('name')}; skipped")
    ctx.print(f"fetched from {len(pods)} pod(s)" + (" (dry run)" if args.dry_run else ""))
    return 0
