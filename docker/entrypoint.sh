#!/bin/bash
set -uo pipefail

echo "=== RunPodFarm Worker Starting ==="
echo "Pod ID: ${RUNPOD_POD_ID:-unknown}"
echo "GPU: $(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null || echo 'N/A')"

# Normalize env var names (support both JUICEFS_META_URL and JUICEFS_REDIS_URL)
JUICEFS_META="${JUICEFS_META_URL:-${JUICEFS_REDIS_URL:-}}"
JUICEFS_STORAGE="${JUICEFS_BUCKET_URL:-${JUICEFS_BUCKET:-}}"

# --- Step 1: Mount JuiceFS ---
JUICEFS_MOUNTED=false
if [ -n "${JUICEFS_META}" ]; then
    echo "Mounting JuiceFS at /project..."
    mkdir -p /project

    # Ensure FUSE is available
    if [ ! -e /dev/fuse ]; then
        echo "Creating /dev/fuse..."
        mknod /dev/fuse c 10 229 2>/dev/null || true
    fi
    modprobe fuse 2>/dev/null || true

    # Write RSA key if provided
    if [ -n "${JUICEFS_RSA_KEY:-}" ]; then
        echo "$JUICEFS_RSA_KEY" > /tmp/juicefs-rsa.pem
        chmod 600 /tmp/juicefs-rsa.pem
    fi

    juicefs mount \
        "${JUICEFS_META}" \
        /project \
        --background \
        --cache-dir /tmp/jfs-cache \
        --cache-size 51200 \
        --no-bgjob \
        ${JUICEFS_CACHE_GROUP:+--cache-group "$JUICEFS_CACHE_GROUP"} \
        -o allow_other 2>&1 || true

    # Wait for mount
    for i in $(seq 1 30); do
        if mountpoint -q /project; then
            echo "JuiceFS mounted successfully"
            JUICEFS_MOUNTED=true
            break
        fi
        sleep 1
    done

    if [ "$JUICEFS_MOUNTED" = "false" ]; then
        echo "WARNING: JuiceFS mount failed, continuing without shared filesystem"
        echo "Check /var/log/juicefs.log for details"
        cat /var/log/juicefs.log 2>/dev/null | tail -20 || true
    fi
else
    echo "WARNING: JuiceFS not configured (JUICEFS_META_URL not set), skipping mount"
fi

export JUICEFS_MOUNTED

# --- Step 2: Setup Houdini environment ---
if [ -d "/workspace/houdini" ]; then
    echo "Setting up Houdini from Network Volume..."
    export HFS="/workspace/houdini"
    source "$HFS/houdini_setup_bash" 2>/dev/null || true

    # Houdini licensing via sesinetd license server
    if [ -n "${SESINETD_HOST:-}" ]; then
        SESINETD_PORT="${SESINETD_PORT:-1715}"
        echo "Configuring license server: ${SESINETD_HOST}:${SESINETD_PORT}"
        mkdir -p /root/.sesi_licenses.d
        echo "serverhost=${SESINETD_HOST}" > /root/.sesi_licenses.d/sesinetd_licenses.pref
        echo "serverport=${SESINETD_PORT}" >> /root/.sesi_licenses.d/sesinetd_licenses.pref
        # Also set via environment for hserver
        export SESI_LMHOST="${SESINETD_HOST}"
        export SESI_LMPORT="${SESINETD_PORT}"
    fi

    # Set project-specific Houdini env
    export HOUDINI_PATH="/project/hda:&"
    export HOUDINI_TEMP_DIR="/tmp/houdini_temp"
    mkdir -p "$HOUDINI_TEMP_DIR"

    echo "Houdini ready: $(hython --version 2>/dev/null || echo 'version check failed')"
else
    echo "WARNING: Houdini not found at /workspace/houdini"
fi

# --- Step 3: Start worker daemon ---
echo "Starting RunPodFarm Worker daemon..."
exec python3 -m worker.daemon
