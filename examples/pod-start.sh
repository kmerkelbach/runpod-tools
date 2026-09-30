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
set -e

# 1. SSH key from the template env
if [ -n "${SSH_PUBLIC_KEY:-}" ]; then
    mkdir -p /root/.ssh
    echo "$SSH_PUBLIC_KEY" > /root/.ssh/authorized_keys
    chmod 700 /root/.ssh && chmod 600 /root/.ssh/authorized_keys
    echo "[pod-start] SSH public key installed"
fi

# 2. Export the injected environment for ssh sessions.
#    Runpod sets template env vars (and resolved secrets) on PID 1 only; sshd
#    spawns fresh shells that do not inherit them. Everything except shell
#    bookkeeping is exported; the file is root-only.
if [ -r /proc/1/environ ]; then
    tr '\0' '\n' < /proc/1/environ \
      | grep -Ev '^(HOME|PATH|PWD|SHLVL|OLDPWD|TERM|HOSTNAME|_|LS_COLORS|SHELL|USER|LOGNAME)=' \
      | sed -E "s/^([^=]+)=(.*)$/export \1='\2'/" > /etc/rp_environment
    chmod 600 /etc/rp_environment
    echo "[pod-start] wrote /etc/rp_environment ($(wc -l < /etc/rp_environment) vars)"
fi
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
