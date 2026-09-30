# runpod-tools

A small command-line tool, `rpt`, for the everyday Runpod loop: create a pod,
wait for SSH, push code, run a job, fetch results, stop the pod. Plus the
template plumbing around it (secret references, exposed ports, volume size).

Written so that a coding agent or a person can read this one file and use
every command. Standard library only (Python ≥ 3.11); no SDK, no `requests`.

```
pip install -e .            # gives you `rpt` (or run `python -m runpod_tools`)
export RUNPOD_API_KEY=...   # https://www.runpod.io/console/user/settings
rpt init                    # writes runpod-tools.toml; fill in template_id + [sync]
rpt pods                    # see everything you have running, with ssh/rsync lines
```

Agents: read [AGENTS.md](AGENTS.md) too. It holds the rules (naming, cost,
what never to do). Operational lessons are in [docs/gotchas.md](docs/gotchas.md).

## Setup

1. **API key** in the environment: `RUNPOD_API_KEY`. It is never read from a
   file and never printed.
2. **SSH key**: `~/.ssh/id_ed25519` by default (`[ssh].key` to change). Its
   public half must be in the template's `SSH_PUBLIC_KEY` env var (or in the
   image) so pods accept it.
3. **Config file**: `rpt init` writes an annotated `runpod-tools.toml` next to
   your project. `rpt` finds it by walking up from the current directory, or
   via `RUNPOD_TOOLS_CONFIG=/path/to/file`. Every key has a default; the only
   commands that refuse to run without a config are `push`/`pull`/`fetch`
   (they need `[sync].remote_dest`). Commit the file: it holds ids and paths,
   never secrets.

   Per-shell overrides: `RUNPOD_TEMPLATE_ID`, `RUNPOD_POD_NAME_PREFIX`,
   `RUNPOD_SSH_KEY`.

## Commands

Every command has `--help`. Read commands take `--json` for machine-readable
output. Commands that act on a pod take `--pod <id|name-prefix>[,...]`; the
ones that can fan out also take `--all`. With neither, a single matching pod
is used automatically; several matches prompt on a terminal and **fail with
exit 2 listing the candidates when stdin is not a terminal**, so an unattended
run never hangs. Anything destructive needs `-y` off a terminal. When
`[pod].name_prefix` is set, `--all` and automatic selection only consider pods
carrying the prefix; an explicit `--pod <id>` can still reach any pod.

| Command | What it does |
|---|---|
| `rpt pods [--json] [--status S]` | List pods: GPU, status, uptime, $/hr, $ so far; per running pod an `ssh` command and an `rsync` template, or a note that the host has no TCP endpoint. |
| `rpt gpus [--secure] [--community] [--json]` | GPU types with VRAM, cloud availability, lowest price. The `id` column is what `--gpu-type` takes. |
| `rpt volumes [--json]` | Network volumes. Read-only: **this tool has no volume delete.** |
| `rpt start [...]` | Create a pod from `[pod]` defaults; every flag overrides one value. Prints `pod_id=<id>` last (also when `--wait` times out, since the pod exists and bills). `--wait` blocks until SSH answers and records the host key. `--delay 2h`, `--retry 60 --max-retries 20` for capacity waits (auth errors are never retried; after a 5xx the pod list is checked for an orphan before retrying). |
| `rpt stop [--pod\|--all] [-y]` | Stop running pod(s). Only `/workspace` survives; `/root` is wiped on resume. |
| `rpt resume [--pod\|--all] [--gpu-count N] [-y]` | Resume stopped pod(s). Can fail if the host is out of GPUs: then `terminate` + `start`. |
| `rpt terminate [--pod\|--all] [-y]` | Delete pod(s) and their disks. Fetch first. Network volumes are untouched. |
| `rpt wait [--pod ID] [--timeout S]` | Block until the pod exposes 22/tcp and sshd answers; prints `<ip> <port>`. |
| `rpt ssh [--pod ID] [--print]` | Interactive shell, or print the `ssh` command. |
| `rpt run [--pod\|--all] [--raw] [--background NAME] -- CMD...` | Run a command over ssh with the pod's injected secrets sourced (`/etc/rp_environment`). Exit code is the remote one (so a remote 2 is not an `rpt` usage error, and 255 means ssh itself failed). `--background` detaches under `nohup` with the log at `/workspace/rpt/NAME.log`. With `--all`, a pod without an endpoint is skipped and reported. |
| `rpt push [--pod\|--all] [--delete] [-n]` | rsync `[sync].local_root` to the pod(s) with `push_excludes`. `--delete` is opt-in and announced. |
| `rpt pull [--pod ID] [--allow-dirty] [-n]` | rsync the remote tree back. Never deletes, size-capped, refuses a dirty git checkout. |
| `rpt fetch [--pod\|--all] [--dir D]... [-n]` | Download each `fetch_dirs` entry (results, logs, ...) with per-dir excludes. A dir missing on the pod is skipped. |
| `rpt template show [--template-id T] [--json]` | Print a template with secret placeholders intact and literal secrets redacted. No id configured: list all. |
| `rpt template env --secret-ref ENV[=Secret] / --env K=V / --image IMG [--dry-run] [-y]` | Add or change env vars and/or the image. Read via GraphQL, PATCH via REST, so placeholders are preserved. |
| `rpt template ports --add 22/tcp \| --set LIST [-y]` | Expose ports on the template (22/tcp is what makes ssh/rsync possible). |
| `rpt template volume --gb N [-y]` | Set the template's default pod volume size. |
| `rpt init [--force]` | Write the annotated example config to `./runpod-tools.toml`. |

Exit codes: `0` ok, `1` the operation failed (API error, rsync error, remote
command failed), `2` usage or configuration error (bad flags, ambiguous pod,
missing key, confirmation needed off a TTY).

## A typical session

```bash
rpt gpus --secure                                  # pick a type id
rpt start --experiment eval --gpu-type "NVIDIA H200" --wait
#   ... created pod abc123 ...
#   pod_id=abc123
rpt push --pod abc123
rpt run --pod abc123 --background job1 -- python train.py --epochs 3
rpt run --pod abc123 -- tail -n 20 /workspace/rpt/job1.log     # poll gently
rpt fetch --pod abc123
ls results/                                        # verify locally BEFORE the next line
rpt terminate --pod abc123 -y
```

Capture the id in a script: `POD=$(rpt start --wait | tail -n1 | cut -d= -f2)`.

## Secrets

Pod secrets (HF tokens, API keys) belong in Runpod's secret store, referenced
from the template as `{{ RUNPOD_SECRET_<Name> }}`. Runpod substitutes them at
launch and writes them into the container's PID 1 environment. Two
consequences:

- `rpt template env --secret-ref HF_TOKEN` wires the env var to the secret of
  the same name (`--secret-ref HF_TOKEN=MyHfSecret` for a different name).
  Create the secret itself in the Runpod console; there is no API for that.
- Non-interactive ssh shells do **not** inherit PID 1's env. `rpt run` sources
  `/etc/rp_environment` first; a custom image has to write that file at
  startup (see `examples/pod-start.sh`).

`rpt template show` redacts literal values under secret-looking keys and
never touches the REST template read, which would resolve the placeholders.

## Custom images

`examples/pod-start.sh` is a container entrypoint that installs the template's
`SSH_PUBLIC_KEY`, writes `/etc/rp_environment`, moves `~/.cache` onto
`/workspace`, and starts sshd. `examples/Dockerfile.snippet` shows the lines
to add. `examples/killswitch.sh` is a pod-side time cap that stops or
terminates the pod it runs on, independent of whoever launched it.

## Docs

- [AGENTS.md](AGENTS.md): rules of engagement for agents (and humans).
- [docs/gotchas.md](docs/gotchas.md): the operational lessons this tool encodes, one heading each.
- [docs/api-notes.md](docs/api-notes.md): which Runpod endpoint does what, and why both APIs are used.
- [docs/fleet-pattern.md](docs/fleet-pattern.md): running one job across N pods with these primitives.

## Development

```bash
uv venv .venv && uv pip install -e '.[dev]'
.venv/bin/pytest -q
```

Tests never touch the network or spawn ssh: the client takes an injectable
transport and commands take an injectable subprocess runner. (Two tests run
real `bash` on purpose, to prove the shell quoting rather than eyeball it.)
