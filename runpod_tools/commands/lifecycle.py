"""``rpt stop`` / ``rpt resume`` / ``rpt terminate``.

Confirmation is required on a TTY unless ``-y``; off a TTY ``-y`` is
mandatory, so an unattended agent can never hang here and can never act
by accident either.

Runpod semantics worth knowing (details in docs/gotchas.md):

* stop keeps the pod's volume disk but *wipes the container disk* including
  ``/root``; anything not under ``/workspace`` is gone on resume.
* a stopped pod is pinned to its host and may fail to resume when that host
  is out of GPUs; the fallback is terminate + start.
* terminate destroys the pod's own disk. Network volumes are unaffected and
  this tool never deletes them.
"""

from __future__ import annotations

from runpod_tools.cli import Context, add_pod_selection, add_yes_flag
from runpod_tools.pods import cost_so_far, describe, format_uptime, select_pods, uptime_seconds

ACTIONS = {
    "stop": dict(statuses=["RUNNING"], past="stopped",
                 help="stop a running pod (GPU billing stops; only /workspace survives a later resume)"),
    "resume": dict(statuses=["EXITED"], past="resumed",
                   help="resume a stopped pod (may fail if its host is out of GPUs; then terminate + start)"),
    "terminate": dict(statuses=["RUNNING", "EXITED"], past="terminated",
                      help="delete a pod and its disk permanently (network volumes are untouched)"),
}


def register(sub) -> None:
    for name, meta in ACTIONS.items():
        p = sub.add_parser(name, help=meta["help"], description=meta["help"].capitalize() + ".")
        add_pod_selection(p, multi=True)
        if name == "resume":
            p.add_argument("--gpu-count", type=int, help="GPUs to resume with (default: the pod's own count)")
        add_yes_flag(p)
        p.set_defaults(func=run, action=name)


def _summary(pod: dict) -> str:
    cost = cost_so_far(pod)
    extra = f", up {format_uptime(uptime_seconds(pod))}" + (f", ${cost:.2f} so far" if cost is not None else "")
    return describe(pod) + (extra if uptime_seconds(pod) is not None else "")


def run(args, ctx: Context) -> int:
    action = args.action
    meta = ACTIONS[action]
    client = ctx.client()
    pods = select_pods(client.list_pods(), ids=args.pod, select_all=args.all, statuses=meta["statuses"],
                       interactive=ctx.isatty, multi=True)

    for p in pods:
        ctx.print(f"  {_summary(p)}")
    if action == "terminate":
        ctx.warn("terminate deletes the pod disk: preserve un-fetched artifacts first (rpt fetch / rpt pull).")
    question = f"{action} {len(pods)} pod(s)?"
    if not ctx.confirm(question, args.yes):
        ctx.print("cancelled; nothing changed.")
        return 0

    for p in pods:
        pod_id = p["id"]
        if action == "stop":
            client.stop_pod(pod_id)
        elif action == "resume":
            client.resume_pod(pod_id, gpu_count=args.gpu_count or p.get("gpuCount") or 1)
        else:
            client.terminate_pod(pod_id)
        ctx.print(f"{pod_id} {meta['past']}")
    if action == "resume":
        ctx.print("note: the container disk was recreated; only /workspace persisted. Re-run `rpt push` if needed.")
    return 0
