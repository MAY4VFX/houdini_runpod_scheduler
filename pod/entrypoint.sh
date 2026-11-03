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
# Where Houdini lives on the volume. Scheduler-side task commands name this
# path; worker.py rewrites it to $HFS when the pod runs a local copy.
export RPFARM_VOLUME_HFS="$HFS"
# --- local-hfs begin (tests/test_entrypoint_local_hfs.py runs this block) ---

# 3a. Local copy of Houdini (render and host pods).
#     Houdini is ~150k files. Read one by one off the MooseFS network volume
#     that is latency-bound: hython took 122-145 s just to start on every task
#     (live 2026-11-01). One SquashFS image of the same tree is read
#     sequentially and unpacked onto the pod's own disk in well under a
#     minute, once per pod. RunPod pods have no /dev/fuse and no loop devices,
#     so the image is unpacked, never mounted (agent-board DECISIONS 40).
#     The image sits inside the version directory, so `houdini rm` removes it
#     with the install. No image, no tool, too little disk or a failed unpack
#     -> Houdini runs from the volume exactly as before.
#     The sync pod does not run Houdini tasks and keeps the volume copy.
_HFS_IMG="$HFS/.rpfarm-hfs.sqfs"
_HFS_LOCAL_ROOT="${RPFARM_LOCAL_HFS_ROOT:-/opt/rpfarm-hfs}"
_HFS_LOCAL="$_HFS_LOCAL_ROOT/${HOUDINI_VERSION}"
_HFS_MIN_FREE_KB="${RPFARM_LOCAL_HFS_MIN_FREE_KB:-$((12 * 1024 * 1024))}"
case "${RPFARM_ROLE:-}" in
  gpu|host)
    if [ -f "$_HFS_LOCAL/.rpfarm-ok" ]; then
      export HFS="$_HFS_LOCAL"
      echo "houdini: local copy already at $HFS"
    elif [ ! -f "$_HFS_IMG" ]; then
      echo "houdini: no image at $_HFS_IMG -- running from the volume (pack it: rpfarm houdini pack)"
    elif ! command -v unsquashfs >/dev/null 2>&1; then
      echo "houdini: unsquashfs missing from the pod image -- running from the volume"
    else
      mkdir -p "$_HFS_LOCAL_ROOT"
      _free_kb=$(df -Pk "$_HFS_LOCAL_ROOT" | awk 'NR==2 {print $4}')
      if [ "${_free_kb:-0}" -lt "$_HFS_MIN_FREE_KB" ]; then
        echo "houdini: only $((_free_kb / 1024 / 1024)) GB free on the pod disk -- running from the volume"
      else
        # In the BACKGROUND: the worker must answer /health within the
        # scheduler's 300 s or the pod is given up on. Live 2026-11-03 four
        # pods in a row sat in a foreground unpack past that and were killed
        # before printing a single line. Until the copy is complete, tasks
        # run Houdini from the volume as before; worker.py switches each new
        # task to the copy once .rpfarm-ok exists (RPFARM_LOCAL_HFS below).
        export RPFARM_LOCAL_HFS="$_HFS_LOCAL"
        (
          _t0=$(date +%s)
          # The pack stored files the volume failed to read as EMPTY and listed
          # them in .rpfarm-hfs.repair. Live 2026-11-03 21 of those 22 do not
          # read on the volume at all -- a read blocks in the kernel (state D,
          # minutes, immune to kill) -- so the volume install is just as
          # broken there and an empty file is no worse. Best effort: each copy
          # gets a few seconds, in the background so a blocked read cannot
          # hold the unpack hostage; what does not arrive stays empty.
          _hfs_repair() {
            _list="$RPFARM_VOLUME_HFS/.rpfarm-hfs.repair"
            [ -f "$_list" ] || return 0
            # all at once: one shared wait instead of one per unreadable file
            while IFS= read -r _rel; do
              [ -n "$_rel" ] || continue
              # own fds: a copy blocked for minutes must not hold the boot log open
              { cp -p "$RPFARM_VOLUME_HFS/$_rel" "$_HFS_LOCAL/$_rel.rpfarm-tmp" \
                  && mv -f "$_HFS_LOCAL/$_rel.rpfarm-tmp" "$_HFS_LOCAL/$_rel"; } </dev/null >/dev/null 2>&1 &
            done < "$_list"
            _w=0
            while [ -n "$(jobs -r)" ] && [ "$_w" -lt "${RPFARM_REPAIR_WAIT_S:-10}" ]; do
              sleep 1; _w=$((_w + 1))
            done
            _left=0
            while IFS= read -r _rel; do
              [ -n "$_rel" ] || continue
              if [ ! -s "$_HFS_LOCAL/$_rel" ]; then
                _left=$((_left + 1))
                echo "houdini: $_rel does not read on the volume either -- left empty"
              fi
            done < "$_list"
            echo "houdini: repair done, $_left file(s) left empty"
          }
          # Unpacked as `nobody`: as root, unsquashfs restores each file's
          # owner from the image, and the volume's uids (232072) do not exist
          # in the pod's user namespace -- chown fails with EINVAL and the whole
          # unpack reports failure (live 2026-11-03). Unprivileged it does not
          # try. Tasks run as root and only read these files.
          _unsq=(unsquashfs)
          if [ "$(id -u)" = 0 ] && command -v setpriv >/dev/null 2>&1; then
            chown nobody "$_HFS_LOCAL_ROOT"
            _unsq=(setpriv --reuid=nobody --regid=nogroup --clear-groups unsquashfs)
          fi
          # nproc in a container can report the whole machine
          _p=$(nproc); [ "$_p" -gt 8 ] && _p=8
          "${_unsq[@]}" -q -n -f -p "$_p" -d "$_HFS_LOCAL" "$_HFS_IMG" >/tmp/unsquashfs.log 2>&1 &
          _pid=$!
          while kill -0 "$_pid" 2>/dev/null; do
            sleep "${RPFARM_UNPACK_REPORT_S:-30}"
            kill -0 "$_pid" 2>/dev/null && echo "houdini: unpacking... $(du -sm "$_HFS_LOCAL" 2>/dev/null | cut -f1) MB after $(( $(date +%s) - _t0 ))s"
          done
          if wait "$_pid" && [ -f "$_HFS_LOCAL/houdini_setup_bash" ]; then
            _hfs_repair
            touch "$_HFS_LOCAL/.rpfarm-ok"
            echo "houdini: unpacked $(du -sh "$_HFS_IMG" | cut -f1) image to $_HFS_LOCAL in $(( $(date +%s) - _t0 ))s -- new tasks use it"
          else
            echo "houdini: unpack failed after $(( $(date +%s) - _t0 ))s ($(tail -1 /tmp/unsquashfs.log)) -- tasks keep running from the volume"
            rm -rf "$_HFS_LOCAL"
          fi
        ) &
        echo "houdini: unpacking $_HFS_IMG to $_HFS_LOCAL in the background; tasks use the volume until it is done"
      fi
    fi
    ;;
esac
# Houdini 22 shared files: houdini_setup_bash looks for them at $HFS/../shfs.
# On the volume that is /workspace/houdini/shfs; a local copy has no sibling,
# so name the volume one explicitly -- and as a normalized path, never
# "$HFS/../shfs": worker.py rewrites the volume HFS prefix to the local copy
# in task environments, which would turn that into a folder that is not there.
if [ -z "${SHFS:-}" ] && [ -d "$RPFARM_VOLUME_HFS/../shfs" ]; then
  export SHFS="$(cd "$RPFARM_VOLUME_HFS/../shfs" && pwd)"
  export HOUDINI_SHFS_PATH="$SHFS"
fi
# --- local-hfs end ---
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

  if { [ -n "${SESINETD_URL:-}" ] || [ -n "${SESINETD_HOST:-}" ]; } && ! command -v hserver >/dev/null 2>&1; then
    # `hserver` ships inside $HFS/bin and is only on PATH once
    # houdini_setup_bash above has actually set up a real Houdini install.
    # Guard it explicitly instead of letting each call fail with "command
    # not found" -- confirmed on a Linux host (see task-4-report.md) that a
    # missing hserver does NOT crash the script under `set -uo pipefail`
    # (no `-e`), but the guard keeps the log clean and makes the "no
    # licensing attempted" case explicit rather than four separate
    # not-found lines.
    echo "License: hserver not found on PATH (HFS=$HFS has no working Houdini install) -- skipping license setup"
  elif [ -n "${SESINETD_URL:-}" ] || [ -n "${SESINETD_HOST:-}" ]; then
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
    # `-S` also takes a full connection URL -- the same form
    # `.sesi_licenses.pref` itself stores -- which matters now that
    # sesinetd sits behind an nginx reverse proxy carrying a secret path
    # (Ruling R69, 2026-09-09: sesinetd has no client authentication of
    # its own, only IP masks, so the raw port cannot be exposed to the
    # internet again). SESINETD_URL, when set, is used INSTEAD of
    # SESINETD_HOST/SESINETD_PORT -- never both, so there is exactly one
    # answer to "which server did this pod use" in the log.
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
    if [ -n "${SESINETD_URL:-}" ]; then
      echo "License: connecting via SESINETD_URL"
      hserver -S "${SESINETD_URL}"
    else
      echo "License: connecting via SESINETD_HOST ${SESINETD_HOST}:${SESINETD_PORT:-1715}"
      hserver -S "${SESINETD_HOST}:${SESINETD_PORT:-1715}"
    fi
    sleep 2
    # hserver -l echoes back whatever server string it was given, secret
    # path included when SESINETD_URL was used -- masked here UNCONDITIONALLY,
    # before either branch below prints anything, so there is one place
    # this can leak from instead of one per branch. Never skip this: the
    # owner-infra guard test catches a real hostname landing in a tracked
    # file, it does not and cannot catch a secret in a runtime log line.
    _hserver_l_raw="$(hserver -l 2>&1)"
    _hserver_l="$(printf '%s' "$_hserver_l_raw" | sed -E 's#(https?://[^/[:space:]]+)/[^[:space:]]*#\1/***#g')"
    if echo "$_hserver_l" | grep -q 'Connected To'; then
      echo "License: $(echo "$_hserver_l" | grep -m1 'Connected To')"
    else
      echo "License: not connected -- raw 'hserver -l' output (masked):"
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

# 4. Worker, or host-cook: RPFARM_ROLE selects which.
#
#    RPFARM_ROLE=host (Submit As Job, Ruling R71) is not a task an
#    external scheduler submits to worker.py -- this pod IS the scheduler
#    for its own cook, driving $HHP/pdgjob/topcook.py directly and
#    terminating itself when done (host_cook.py's own docstring has the
#    detail, including why it -- not this script -- owns the watchdog and
#    the self-terminate). No worker.py HTTP server is started for it:
#    nothing outside this pod is meant to talk to it.
#
#    Every other role: RPFARM_TOKEN / RPFARM_ROLE / RPFARM_SLOTS /
#    RPFARM_PORT / HFS / RUNPOD_POD_ID are all read directly from the
#    environment by worker.py (RunPod sets RUNPOD_POD_ID; RPFARM_PORT
#    defaults to 8000 and RPFARM_SLOTS defaults to 1 inside worker.py
#    itself).
if [ "${RPFARM_ROLE:-}" = "host" ]; then
  echo "host-cook: exec python3 /opt/rpfarm/host_cook.py"
  exec python3 /opt/rpfarm/host_cook.py
else
  echo "worker: exec python3 /opt/rpfarm/worker.py"
  exec python3 /opt/rpfarm/worker.py
fi
