#!/bin/bash
set -uo pipefail

# 0. Boot log on the network volume.
#    RunPod's own container logs are only visible in its web UI, which the
#    automation here cannot read -- so every boot tees its whole stdout+stderr
#    onto the shared volume, where any later pod (or an SSH session) can read
#    it. This is the ONLY way we get to see why a pod did or did not come up.
#    Everything below this point, including worker.py's own output after the
#    final `exec`, lands both on stdout and in $BOOTLOG.
#    If /workspace is missing or read-only (e.g. a plain `docker run` with no
#    volume), fall back to stdout only -- never let logging kill the boot.
BOOTLOG=""
if mkdir -p /workspace/ledger/logs 2>/dev/null && [ -w /workspace/ledger/logs ]; then
  BOOTLOG="/workspace/ledger/logs/boot-${RUNPOD_POD_ID:-unknown}-$(date +%s).log"
  # Process substitution keeps a `tee` child alive for the life of the
  # container; the final `exec python3 worker.py` still replaces this shell as
  # PID 1's process, and inherits these fds.
  exec > >(tee -a "$BOOTLOG") 2>&1
  echo "boot log: $BOOTLOG"
else
  echo "boot log: /workspace/ledger/logs not writable -- stdout only"
fi

echo "=== rpfarm pod (${RPFARM_ROLE:-?}) $(hostname) pod=${RUNPOD_POD_ID:-?} ==="
echo "boot: $(date -u +%Y-%m-%dT%H:%M:%SZ) image-entrypoint starting, uname=$(uname -a)"

# 1. SSH: artist's key comes in via env (same convention as runpod/base images)
mkdir -p ~/.ssh /run/sshd && chmod 700 ~/.ssh
[ -n "${PUBLIC_KEY:-}" ] && echo "$PUBLIC_KEY" >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
/usr/sbin/sshd
echo "sshd: started (rc=$?)"

# 2. Volume zones (shared network volume mounted at /workspace)
for d in houdini apps projects ledger/logs .rpfarm; do
  mkdir -p "/workspace/$d"
done

# 3. Houdini from the network volume (v1 used /workspace/houdini; v2 is
#    versioned: /workspace/houdini/<HOUDINI_VERSION>, so multiple Houdini
#    builds can live side by side on the same volume).
export HOUDINI_VERSION="${HOUDINI_VERSION:-22.0.393}"
export HFS="/workspace/houdini/${HOUDINI_VERSION}"
if [ -d "$HFS" ] && [ -f "$HFS/houdini_setup_bash" ]; then
  # `set -u` MUST be off while sourcing houdini_setup_bash. SideFX's script
  # reads unset variables (on Houdini 21.0.792 it is `SHFS` at line 30;
  # other builds trip on PYTHONPATH/LD_LIBRARY_PATH), and because it is
  # *sourced*, `set -u`'s unbound-variable abort kills THIS shell -- not a
  # subshell. That is exactly what happened on RunPod: the boot log stopped
  # dead after "sshd: started" and RunPod restarted the container roughly
  # every 17 seconds (52 boot logs in 15 minutes on the volume), which is
  # what produced the "ports appear then vanish / SSH refused / health 404"
  # symptom across rounds 1-4. Reproduced directly against a real
  # houdini_setup_bash: with `set -u` the shell dies at the source, without
  # it the script continues normally.
  echo "houdini: sourcing $HFS/houdini_setup_bash"
  set +u
  cd "$HFS" && source houdini_setup_bash >/tmp/houdini_setup.log 2>&1
  _setup_rc=$?
  cd /
  set -u
  echo "houdini: houdini_setup_bash rc=$_setup_rc, hython=$(command -v hython || echo 'not on PATH')"

  if [ -n "${SESINETD_HOST:-}" ] && ! command -v hserver >/dev/null 2>&1; then
    # `hserver` ships inside $HFS/bin and is only on PATH once
    # houdini_setup_bash above has actually set up a real Houdini install.
    # Guard it explicitly instead of letting each call fail with "command
    # not found" -- confirmed on mayfx02 (see task-4-report.md) that a
    # missing hserver does NOT crash the script under `set -uo pipefail`
    # (no `-e`), but the guard keeps the log clean and makes the "no
    # licensing attempted" case explicit rather than four separate
    # not-found lines.
    echo "License: hserver not found on PATH (HFS=$HFS has no working Houdini install) -- skipping license setup"
  elif [ -n "${SESINETD_HOST:-}" ]; then
    # v1 (docker/entrypoint.sh:34) pointed the local hserver at the remote
    # license server with `hserver --host "$SESINETD_HOST"`, silently
    # dropping SESINETD_PORT. That flag is documented (hserver --help,
    # "Client Options") as "Specify a remote host to query" for hserver's
    # own status/info API (default port 1714) -- NOT for selecting which
    # sesinetd license server to use, and it does not accept a "host:port"
    # value at all: appending ":<port>" makes hserver treat the whole
    # string as an IPv6 literal and fail with
    # "URL using bad/illegal format or missing URL" (verified against a
    # local Houdini 22.0.368 install).
    #
    # The documented client-side command for pointing at a remote sesinetd
    # is `hserver -S <server list>` (SideFX help, licensing/studio_licensing:
    # "Run `hserver -S <server list>` on the client machine to change the
    # server(s) used for licensing to the newly setup license server").
    # `-S host:port` was verified locally: it is accepted without a parse
    # error and `hserver -l` afterwards reports the exact host:port pair
    # back as "License Server: http://<host>:<port>".
    #
    # `-S` is a *client* option: it talks to an already-running local
    # hserver daemon. On a fresh pod there is no daemon yet, and relying on
    # `-S`'s own "Unable to connect to hserver. Attempting to restart
    # hserver..." auto-recovery was unreliable in testing (it sometimes
    # failed outright with "Failed to start hserver or connect to
    # hserver"). So start the daemon explicitly first (bare `hserver`, no
    # `-d`/`--run-in-foreground`, forks and returns -- per its own --help:
    # "With the following options (or without any options), this will
    # start a houdini server on the local host"), THEN point it at the
    # remote license server with `-S`, run synchronously (no trailing `&`
    # -- it's a quick client call, not the server process itself).
    hserver -q 2>/dev/null || true
    sleep 1
    hserver >/tmp/hserver_start.log 2>&1
    echo "hserver start: $(cat /tmp/hserver_start.log 2>/dev/null || echo '(no output)')"
    sleep 1
    hserver -S "${SESINETD_HOST}:${SESINETD_PORT:-1715}"
    sleep 2
    _hserver_l="$(hserver -l 2>&1)"
    if echo "$_hserver_l" | grep -q 'Connected To'; then
      echo "License: $(echo "$_hserver_l" | grep -m1 'Connected To')"
    else
      echo "License: not connected -- raw 'hserver -l' output:"
      echo "$_hserver_l"
    fi
  fi

  export HOUDINI_TEMP_DIR=/tmp/houdini_temp
  mkdir -p "$HOUDINI_TEMP_DIR"
  echo "Houdini: $(hython --version 2>/dev/null || echo 'hython failed')"

  # GPU rendering, in one line, at boot.
  #
  # Karma XPU needs three things the host's container runtime is supposed to
  # inject (libnvoptix, libnvidia-rtcore, nvoptix.bin) plus libEGL, which is a
  # plain package and was the piece actually missing -- eight render tasks died
  # identically with "Karma XPU delegate not supported on this machine" and the
  # only way to find out was reading a task log off the volume. On every pod
  # we have measured the three injected files were present, so the download-
  # the-driver-and-extract-them dance from v1 is deliberately NOT carried over:
  # this line is here to tell us if a host ever turns up that needs it.
  if [ -n "${NVIDIA_VISIBLE_DEVICES:-}" ] || command -v nvidia-smi >/dev/null 2>&1; then
    _gpu_bits=""
    for _f in libnvoptix.so.1 libnvidia-rtcore.so libEGL.so.1; do
      if ldconfig -p 2>/dev/null | grep -q "$_f"; then
        _gpu_bits="$_gpu_bits $_f=yes"
      else
        _gpu_bits="$_gpu_bits $_f=MISSING"
      fi
    done
    # Reported, not judged: XPU has worked on every pod we measured, so
    # whether this file is actually required here is unverified. yes/no rather
    # than MISSING so it does not read as an alarm we cannot justify.
    if [ -f /usr/share/nvidia/nvoptix.bin ]; then
      _gpu_bits="$_gpu_bits nvoptix.bin=yes"
    else
      _gpu_bits="$_gpu_bits nvoptix.bin=no"
    fi
    echo "GPU:$_gpu_bits driver=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1 || echo '?') caps=${NVIDIA_DRIVER_CAPABILITIES:-unset}"
  fi
else
  echo "WARNING: no Houdini at $HFS (need $HFS/houdini_setup_bash; install with the runpodfarm_upload preset or 'rpfarm houdini install')"
fi

# 4. Worker: RPFARM_TOKEN / RPFARM_ROLE / RPFARM_SLOTS / RPFARM_PORT / HFS /
#    RUNPOD_POD_ID are all read directly from the environment by worker.py
#    (RunPod sets RUNPOD_POD_ID; RPFARM_PORT defaults to 8000 and
#    RPFARM_SLOTS defaults to 1 inside worker.py itself).
echo "worker: exec python3 /opt/rpfarm/worker.py"
exec python3 /opt/rpfarm/worker.py
