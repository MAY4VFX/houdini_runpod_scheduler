#!/bin/bash
set -euo pipefail

echo "=== RunPodFarm Worker Starting ==="
echo "Pod ID: ${RUNPOD_POD_ID:-unknown}"
echo "GPU: $(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null || echo 'N/A')"

# --- Step 1: Mount JuiceFS ---
if [ -n "${JUICEFS_REDIS_URL:-}" ] && [ -n "${JUICEFS_BUCKET_URL:-}" ]; then
    echo "Mounting JuiceFS at /project..."
    mkdir -p /project

    # Write RSA key if provided
    if [ -n "${JUICEFS_RSA_KEY:-}" ]; then
        echo "$JUICEFS_RSA_KEY" > /tmp/juicefs-rsa.pem
        chmod 600 /tmp/juicefs-rsa.pem
    fi

    juicefs mount \
        "${JUICEFS_REDIS_URL}" \
        /project \
        --background \
        --cache-dir /tmp/jfs-cache \
        --cache-size 51200 \
        ${JUICEFS_CACHE_GROUP:+--cache-group "$JUICEFS_CACHE_GROUP"} \
        -o allow_other

    # Wait for mount
    for i in $(seq 1 30); do
        if mountpoint -q /project; then
            echo "JuiceFS mounted successfully"
            break
        fi
        sleep 1
    done

    if ! mountpoint -q /project; then
        echo "ERROR: JuiceFS mount failed"
        exit 1
    fi
else
    echo "WARNING: JuiceFS not configured, skipping mount"
fi

# --- Step 2: Setup Houdini environment ---
if [ -d "/workspace/houdini" ]; then
    echo "Setting up Houdini from Network Volume..."
    export HFS="/workspace/houdini"
    source "$HFS/houdini_setup_bash" 2>/dev/null || true

    # Houdini licensing via SideFX Online
    if [ -n "${SIDEFX_CLIENT_ID:-}" ] && [ -n "${SIDEFX_CLIENT_SECRET:-}" ]; then
        echo "Configuring SideFX Online licensing..."
        # hserver handles license checkout automatically when client credentials are set
        export HOUDINI_CLIENT_ID="${SIDEFX_CLIENT_ID}"
        export HOUDINI_CLIENT_SECRET="${SIDEFX_CLIENT_SECRET}"
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
