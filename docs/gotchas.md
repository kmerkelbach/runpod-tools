# Runpod gotchas

Operational facts that shaped this tool. Each one cost real money or hours
the first time. Where `rpt` handles it, the heading says so.

## Connectivity

**A Secure Cloud pod is not guaranteed a public TCP endpoint.** Runpod's docs
say otherwise, but the fleet includes proxy-only hosts. A pod placed there has
no `22/tcp` mapping in `runtime.ports`, so rsync/ssh are impossible; only the
web terminal and the `ssh.runpod.io` proxy (terminal only, no file transfer)
work. `rpt start` sends `supportPublicIp: true` by default, which is a
*scheduling constraint*: only place me where a public port can be exposed.
`rpt pods` says "no TCP endpoint" when a pod has none.

**Host GPU drivers are mixed, and an image's CUDA wheels may be newer than the
host.** On one day the same account got hosts with driver 570 (CUDA 12.8) and
580 (CUDA 13.0). PyTorch built for CUDA 13 reports `cuda.is_available() ==
False` on the older driver ("The NVIDIA driver on your system is too old"), and
nothing else looks wrong. `allowedCudaVersions` on pod create is a scheduling
constraint like the public IP: `rpt start --cuda-version 13.0`, or
`[pod].allowed_cuda_versions` in the config. Check the GPU from your own code
right after start regardless.

**A detached job must release the session's streams, or ssh never returns.**
`mkdir -p d && nohup job > log 2>&1 & echo pid=$!` backgrounds the whole
`mkdir && nohup` list in a subshell that still holds the session's stdout, and
sshd waits for it. `rpt run --background` backgrounds only the job and
redirects stdin, stdout and stderr; do the same in your own launch lines
(`nohup job > log 2>&1 < /dev/null &`).

**Ports are discovered per call and change on restart.** Never cache an
`ip:port`; ask `rpt pods` (or `rpt wait`) again after a resume.

**Fresh pod, unknown host key.** Non-interactive ssh cannot answer the
"are you sure?" prompt, so the first rsync to a new pod dies with exit 255,
"Host key verification failed". `rpt` uses `StrictHostKeyChecking=accept-new`
and `rpt wait`/`rpt start --wait` run `ssh-keyscan` into `known_hosts` once
sshd answers. Runpod reuses `ip:port` pairs across pods, so a stale entry for
the same endpoint is removed first (otherwise the *new* key is refused as a
man-in-the-middle).

**sshd throttles you if you poll hard.** Several background monitors, each
opening a few ssh sessions every minute, trip `MaxStartups` and every new
connection is dropped during the banner exchange. It looks exactly like a
dead pod. The pod is fine. Kill your local hung ssh clients, back off, then
one patient probe a minute apart. Steady state: one consolidated ssh call per
check, no stacked watchers. `rpt wait` defaults to a 15 s poll for this
reason.

## Environment on the pod

**Template env vars and secrets live in PID 1 only.** sshd spawns fresh
shells that do not inherit them, so `ssh pod 'python job.py'` sees no
`HF_TOKEN`. Runpod's base images write `/etc/rp_environment` at start;
`rpt run` sources it before every command. A custom image must write the
file itself (`examples/pod-start.sh`).

**The pod's own `RUNPOD_API_KEY` cannot stop the pod.** Runpod injects
`RUNPOD_POD_ID` and a `RUNPOD_API_KEY` into every pod, but that key is scoped
to the pod: `POST /pods/{id}/stop` and `DELETE /pods/{id}` answer 403 with an
empty body. The injected key also lands in `/etc/rp_environment`, so any
script that falls back to `$RUNPOD_API_KEY` under `rpt run` or a login shell
picks up the wrong key. A pod-side killswitch needs the account key delivered
separately: over stdin into a root-only file under `/workspace` (see
`examples/killswitch.sh`, which reads only that file or `KILLSWITCH_API_KEY`,
never `RUNPOD_API_KEY`), or as a template env var under another name
referencing a secret that holds it. Never on a command line: it would sit in
`ps` output for the life of the process. One probe ran seven hours past its
cap this way; the script now logs the HTTP status and retries.

**The pod shell may be zsh.** Unquoted `$var` does not word-split there
(`ssh $OPTS ...` passes one argument). Write options inline or use arrays,
and put anything non-trivial in a script file you upload and run with
`bash file.sh`.

**A redirect into a missing directory fails silently under nohup.** `nohup
python x.py > results/x.log &` with no `results/` directory: the shell
fails the redirect, nothing starts, and `$!` prints a stale pid.
`rpt run --background` creates its log directory first; do the same in your
own launch lines.

## Disks and data

**Stop/resume recreates the container.** Only `/workspace` (the volume)
survives. `/root`, `/tmp`, installed packages outside the image, running
processes, killswitch scripts: gone. Keep anything that must survive under
`/workspace` and re-arm/relaunch after every resume.

**A network volume mounted at `/workspace` shadows anything the image bakes
there.** A venv or dataset built into the image under `/workspace/...` is
invisible on every pod that mounts a volume. Bake into `/opt` (or anywhere
outside the mount).

**Stopped pods are pinned to their host and may never resume** ("not enough
free GPUs on the host"). Terminate + start is the fallback. So: treat every
stop as if it were a terminate and fetch first.

**Fetch, verify locally, then destroy, in separate commands.** A quoted
brace-glob in `scp` fetches nothing remotely, and if the same compound
command then terminates the pod, the data is gone. Fetch whole directories
(`rpt fetch`), `ls` the local copy, then terminate.

**Large single-file transfers over rsync can stall**; `tar cf - dir | ssh pod
'tar xf -'` moved the same data reliably when rsync did not.

**Uplink from a laptop is slow; pod-to-pod is fast.** For N pods, push once
to one pod, then fan out pod-to-pod with agent forwarding
(`ssh -A pod1 "rsync -a -e 'ssh -p PORT2' /workspace/x/ root@IP2:/workspace/x/"`).

## Cost

**A cap enforced only by an external poller is not a cap.** A missed wakeup
or malformed command and the pods run on. Put the stop on the pod
(`examples/killswitch.sh`) or size the job with `timeout`.

**A stale killswitch kills the next run.** Before launching a new job on a
pod that ran one before, kill the previous killswitch (`pgrep -f
'[k]illswitch'`), or it fires on the new job's timeline.

**Driver pods do not need big GPUs.** A pod that only orchestrates a remote
service or runs CPU work on a default high-end GPU wastes the whole hourly
rate. Pass an explicit cheap `--gpu-type`.

**Stopped pods still bill for disk.** A stopped pod you would never resume
(old software stack, nothing on it you need) should be terminated.

## Monitoring

**`pgrep -f NAME` over ssh matches the ssh command itself** (the remote
`bash -c '... pgrep -f NAME ...'` contains NAME). Bracket the first
character: `pgrep -f '[N]AME'`. Same for `pkill`, which will otherwise kill
your own session (exit 255, indistinguishable from throttling).

**Grep markers with an anchored prefix.** Logs that echo model inputs or
outputs contain words like FAIL and DONE. Match your script's own tag
(`^\[job1\] DONE`), never a bare word. And `tail -3` is too shallow for a
completion marker that has scrolled past; use `tail -20` or grep the file.

**A process that died instantly still passes an immediate `pgrep`.** Verify
a launch after 15 to 20 s with real progress (log lines, files), not with a
1 s process check.

## API

**Pod listing comes from GraphQL, creation from REST.** The REST pod object
has no GPU display name and no runtime ports; GraphQL has both. REST is the
documented way to create/stop. When REST is timing out (it happens) while
GraphQL still answers, the GraphQL `podFindAndDeployOnDemand` mutation can
create a pod. See `docs/api-notes.md`.

**REST template reads resolve secret placeholders.** A read-modify-write of
a template through REST inlines the secret values into the template and
breaks the indirection. Read via GraphQL, write via REST PATCH. `rpt
template` does this.

**A timed-out create may still have created a pod.** After any timeout
storm, `rpt pods` and look for orphans.
