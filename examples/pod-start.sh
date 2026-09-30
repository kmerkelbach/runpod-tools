#!/bin/bash
# Container entrypoint for a custom Runpod image.
#
# Runpod's own base images do the equivalent of this; a custom image (say,
# one built FROM a vendor's inference image) has to do it itself, or the pod
# comes up without SSH and with its secrets invisible to login shells.
#
# What it does, in order:
#   1. installs $SSH_PUBLIC_KEY (a template env var) as root's authorized key
#   2. writes every env var Runpod injected into PID 1 to /etc/rp_environment
#      so non-interactive `ssh pod 'cmd'` sessions can source it
#      (`rpt run` does this for you)
#   3. moves ~/.cache onto /workspace so model downloads survive stop/resume
#   4. persists shell history on /workspace and marks the prompt as a pod
#   5. starts sshd and sleeps forever
#
# Use it with the Dockerfile lines in examples/Dockerfile.snippet.
#
# `pod-start.sh --env-only` just writes the env file and exits (used by the
# tests; POD_START_ENVIRON / POD_START_ENV_FILE override the paths).
set -e

POD_START_ENVIRON="${POD_START_ENVIRON:-/proc/1/environ}"
POD_START_ENV_FILE="${POD_START_ENV_FILE:-/etc/rp_environment}"

# Write PID 1's environment as `export NAME=<shell-quoted value>` lines.
# printf %q quotes each value, so values with quotes, newlines, $() or
# backticks round-trip as data instead of being executed when the file is
# sourced. Names that are not valid shell identifiers are dropped.
write_rp_environment() {
    [ -r "$POD_START_ENVIRON" ] || return 0
    local kv name
    : > "$POD_START_ENV_FILE"
    chmod 600 "$POD_START_ENV_FILE"
    while IFS= read -r -d '' kv; do
        name=${kv%%=*}
        [[ $name =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
        case "$name" in
            HOME|PATH|PWD|SHLVL|OLDPWD|TERM|HOSTNAME|_|LS_COLORS|SHELL|USER|LOGNAME) continue ;;
        esac
        printf 'export %s=%q\n' "$name" "${kv#*=}" >> "$POD_START_ENV_FILE"
    done < "$POD_START_ENVIRON"
    echo "[pod-start] wrote $POD_START_ENV_FILE ($(wc -l < "$POD_START_ENV_FILE" | tr -d ' ') vars)"
}

if [ "${1:-}" = "--env-only" ]; then
    write_rp_environment
    exit 0
fi

# 1. SSH key from the template env (appended: keys baked into the image stay)
if [ -n "${SSH_PUBLIC_KEY:-}" ]; then
    mkdir -p /root/.ssh
    touch /root/.ssh/authorized_keys
    grep -qxF "$SSH_PUBLIC_KEY" /root/.ssh/authorized_keys || echo "$SSH_PUBLIC_KEY" >> /root/.ssh/authorized_keys
    chmod 700 /root/.ssh && chmod 600 /root/.ssh/authorized_keys
    echo "[pod-start] SSH public key installed"
fi

# 2. Export the injected environment for ssh sessions.
#    Runpod sets template env vars (and resolved secrets) on PID 1 only; sshd
#    spawns fresh shells that do not inherit them. Everything except shell
#    bookkeeping is exported; the file is root-only.
write_rp_environment
for rc in /root/.bashrc /root/.zshrc; do
    if ! grep -q rp_environment "$rc" 2>/dev/null; then
        printf '\n# Runpod-injected env (secrets, tokens)\n[ -f /etc/rp_environment ] && . /etc/rp_environment\n' >> "$rc"
    fi
done

# 3. Persistent cache on the volume (only /workspace survives stop/resume)
if [ -d /workspace ]; then
    mkdir -p /workspace/.cache
    if [ ! -L /root/.cache ]; then
        [ -d /root/.cache ] && cp -a /root/.cache/. /workspace/.cache/ 2>/dev/null || true
        rm -rf /root/.cache
        ln -s /workspace/.cache /root/.cache
        echo "[pod-start] /root/.cache -> /workspace/.cache"
    fi

    # 4. Persistent shell history + a prompt that says "pod"
    mkdir -p /workspace/.shell
    for rc in /root/.bashrc /root/.zshrc; do
        if ! grep -q 'workspace/.shell' "$rc" 2>/dev/null; then
            cat >> "$rc" <<'RC'

# history on the persistent volume
HISTFILE=/workspace/.shell/history
HISTSIZE=50000
SAVEHIST=50000
RC
        fi
    done
    if ! grep -q 'POD PROMPT' /root/.bashrc 2>/dev/null; then
        printf '\n# POD PROMPT\nPS1="[pod:\\h] \\w \\$ "\n' >> /root/.bashrc
    fi
fi

# 5. sshd
mkdir -p /var/run/sshd
ssh-keygen -A >/dev/null 2>&1 || true
/usr/sbin/sshd
echo "[pod-start] sshd started"

sleep infinity
