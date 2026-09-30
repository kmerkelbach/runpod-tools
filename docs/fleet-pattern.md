# Running one job across N pods

`rpt` ships primitives, not an orchestrator. This is the pattern that worked
in practice for sharded evaluation and parameter sweeps, written with those
primitives. Adapt the shell; keep the safety steps.

## The shape

1. **Launch N pods, capture ids.** Retry on capacity; proceed with however
   many you got (a smaller fleet is the fallback, not a failure).
2. **Wait for SSH** on each; host keys get recorded.
3. **Push code once, fan out pod-to-pod** if the payload is large (laptop
   uplink is the bottleneck, datacenter links are 10 to 30 times faster).
4. **Launch each shard under nohup** with a completion marker file and a
   failure marker file, both under `/workspace`.
5. **Poll gently**: one ssh per pod per poll, minutes apart. Fetch a shard as
   soon as its marker appears; verify the local copy; stop *that* pod. No
   pod waits for the slowest.
6. **Merge** locally only when every shard is verified present.
7. **Cap the cost on the pod side** (`examples/killswitch.sh`) so a lost
   session cannot leave the fleet running.

## Sketch

```bash
#!/usr/bin/env bash
# needs bash >= 4 (macOS ships 3.2: `brew install bash`); no `set -e`/pipefail on purpose,
# a throttled ssh probe must not kill the watch loop
set -u
N=3; TAG=eval_$(date +%m%d_%H%M); PODS=()

# 1. launch
for i in $(seq 1 $N); do
  if out=$(rpt start --experiment "${TAG}_s$i" --retry 60 --max-retries 5 2>/dev/null); then
    PODS+=("$(echo "$out" | tail -n1 | cut -d= -f2)")
  else
    echo "shard $i: no capacity, continuing with fewer" >&2
  fi
done
N=${#PODS[@]}; [ "$N" -gt 0 ] || { echo "no pods"; exit 1; }

# 2. wait + 3. push
for p in "${PODS[@]}"; do rpt wait --pod "$p"; done
rpt push --pod "$(IFS=,; echo "${PODS[*]}")"

# 4. launch shards (the job must touch a DONE or FAIL marker itself)
for i in "${!PODS[@]}"; do
  s=$((i+1)); p=${PODS[$i]}
  rpt run --pod "$p" --background "shard$s" -- \
    "python job.py --shard $s/$N --out /workspace/project/results/${TAG}_shard$s \
       && touch /workspace/SHARD_${s}_DONE || touch /workspace/SHARD_${s}_FAIL"
  # 7. pod-side cap; the key goes over stdin into a root-only file, never onto argv
  rpt run --pod "$p" --raw -- 'cat > /workspace/killswitch.sh' < examples/killswitch.sh
  printf %s "$RUNPOD_API_KEY" | rpt run --pod "$p" --raw -- 'umask 077; cat > /workspace/.rp_key'
  rpt run --pod "$p" --background killswitch -- bash /workspace/killswitch.sh "$p" 8 stop
done

# 5. watch
pending=("${PODS[@]}")
while [ "${#pending[@]}" -gt 0 ]; do
  sleep 300
  still=()
  for i in "${!PODS[@]}"; do
    s=$((i+1)); p=${PODS[$i]}
    printf '%s\n' "${pending[@]}" | grep -qx "$p" || continue
    st=$(rpt run --pod "$p" --raw -- \
      "test -f /workspace/SHARD_${s}_DONE && echo DONE || (test -f /workspace/SHARD_${s}_FAIL && echo FAIL || echo RUN)" \
      2>/dev/null | tail -n1)
    case "$st" in
      DONE)
        rpt fetch --pod "$p" --dir "results/${TAG}_shard$s"
        if [ -s "results/${TAG}_shard$s/metrics.json" ]; then   # verify locally
          rpt stop --pod "$p" -y
        else
          echo "shard $s: DONE marker but fetch incomplete, retrying" >&2; still+=("$p")
        fi ;;
      FAIL) echo "shard $s FAILED on $p (pod left running for inspection)" >&2 ;;
      *)    still+=("$p") ;;
    esac
  done
  pending=("${still[@]+"${still[@]}"}")   # safe when still is empty
done
# 6. merge results/${TAG}_shard*/ locally
```

## Things this sketch gets right on purpose

- Marker files, not process greps, decide completion. If you must grep, use
  `pgrep -f '[j]ob.py'` and anchor log markers to your own tag.
- The fetch and the stop are separate commands with a local check between.
- Failed shards leave their pod running so you can look; the killswitch is
  the backstop.
- One ssh per pod per poll, five minutes apart. More is how you get
  throttled and misdiagnose a healthy pod as dead.
- `rpt push --pod a,b,c` uploads from the laptop N times. For big trees,
  push to one pod and fan out from there (see `docs/gotchas.md`).
