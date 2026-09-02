#!/bin/bash
set -uo pipefail

echo "=== rpfarm pod (${RPFARM_ROLE:-?}) $(hostname) pod=${RUNPOD_POD_ID:-?} ==="

# 1. SSH: artist's key comes in via env (same convention as runpod/base images)
mkdir -p ~/.ssh /run/sshd && chmod 700 ~/.ssh
[ -n "${PUBLIC_KEY:-}" ] && echo "$PUBLIC_KEY" >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
/usr/sbin/sshd

# 2. Volume zones (shared network volume mounted at /workspace)
for d in houdini apps projects ledger/logs .rpfarm; do
  mkdir -p "/workspace/$d"
done

# 3. Houdini from the network volume (v1 used /workspace/houdini; v2 is
#    versioned: /workspace/houdini/<HOUDINI_VERSION>, so multiple Houdini
#    builds can live side by side on the same volume).
export HOUDINI_VERSION="${HOUDINI_VERSION:-22.0.393}"
export HFS="/workspace/houdini/${HOUDINI_VERSION}"
if [ -d "$HFS" ]; then
  cd "$HFS" && source houdini_setup_bash >/dev/null 2>&1; cd /

  if [ -n "${SESINETD_HOST:-}" ]; then
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
else
  echo "WARNING: $HFS not found on volume (install with runpodfarm_upload preset or 'rpfarm houdini install')"
fi

# 4. Worker: RPFARM_TOKEN / RPFARM_ROLE / RPFARM_SLOTS / RPFARM_PORT / HFS /
#    RUNPOD_POD_ID are all read directly from the environment by worker.py
#    (RunPod sets RUNPOD_POD_ID; RPFARM_PORT defaults to 8000 and
#    RPFARM_SLOTS defaults to 1 inside worker.py itself).
exec python3 /opt/rpfarm/worker.py
