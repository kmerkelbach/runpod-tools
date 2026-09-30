# Rules for agents using runpod-tools

You are operating someone's cloud account. Pods bill by the hour whether or
not they do anything. These rules are the distilled cost of learning them the
hard way; follow them unless the person you work for says otherwise.

## Before you start

- Read `README.md` for the commands and `docs/gotchas.md` for why they behave
  as they do. `rpt <command> --help` is authoritative for flags.
- Confirm `RUNPOD_API_KEY` is set and `runpod-tools.toml` exists
  (`rpt init` if not). Never write the key into a file or a command line you
  print.
- Run `rpt pods` first. Know what is already running and what it costs.

## Naming and ownership

- If the account is shared, `[pod].name_prefix` must be set and every pod you
  create carries it. `rpt start` enforces this; if you ever create a pod
  another way, add the prefix yourself.
- **Only manage pods you launched in this session.** Other pods with the same
  prefix may belong to another person or another session. Do not stop,
  resume, or terminate them, even if they look idle. Mention it once if
  asked; otherwise leave them alone.

## Cost

- Pick the GPU for the work. A pod that only drives a remote service or runs
  CPU work gets the cheapest type `rpt gpus` lists, not the default.
- Do not leave a pod idle. When a job finishes and nothing is queued: fetch,
  verify, stop (or terminate). Stopped pods still bill for disk; a stopped
  pod you would never resume should be terminated.
- For an unattended long run, arm a pod-side cap
  (`examples/killswitch.sh`) so the pod stops itself even if you lose the
  session. Your own polling is a monitor, not an enforcement mechanism.
- Cap concurrency. Queue work onto existing pods before starting more.

## Data

- **Pod disks are ephemeral.** Stop/resume recreates the container: `/root`
  and everything outside `/workspace` is gone, running processes included.
  Terminate destroys `/workspace` too (unless it is a network volume).
- **Fetch, then verify locally, then destroy, as three separate steps.** Never
  put the fetch and the terminate in one command. Check the local files
  exist and are complete before you stop or terminate.
- **Never delete a network volume.** This tool has no command for it. If a
  task seems to need it, stop and ask a human, naming the volume.

## Running things

- Use `rpt run` for remote commands; it sources `/etc/rp_environment` so the
  pod's secrets resolve. `--raw` skips that.
- Long jobs: `rpt run --background NAME -- ...`, then poll the log
  **gently**. One consolidated `rpt run -- ...` per check, never a tight
  loop and never several parallel watchers: sshd rate-limits new connections
  and a throttled pod looks exactly like a dead one.
- Verify a launch after 15 to 20 seconds with real evidence (log lines
  advancing, output files appearing), not with an immediate `pgrep`. A
  process that died instantly still shows up for a second.
- `pgrep -f`/`pkill -f` over ssh match your own command line. Bracket the
  first character (`'[t]rain.py'`) and keep the target string out of the
  rest of the command.
- If the remote directory for a log does not exist, the redirect fails
  silently and nothing starts. `mkdir -p` first (`--background` does this
  for `/workspace/rpt`).

## Confirmation semantics

- Off a terminal, destructive commands need `-y` and otherwise exit 2. That
  is deliberate: pass `-y` only when you have already checked what the
  command will touch (`rpt pods`, `rpt template show --dry-run`).
- Ambiguous pod selection exits 2 with the candidates. Pass `--pod` with an
  id; do not pass `--all` to make an error go away.
