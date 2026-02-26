#!/bin/bash
set -uo pipefail

echo "=== RunPodFarm Worker Starting ==="
echo "Pod ID: ${RUNPOD_POD_ID:-unknown}"
echo "GPU: $(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null || echo 'N/A')"

# --- Step 1: Setup project directory ---
# RunPod Network Volume is mounted at /workspace (shared across all pods).
# Project files live at /workspace/projects/ (synced by Desktop App or HDA).
# JuiceFS FUSE mount is NOT available on RunPod (no /dev/fuse access).
PROJECT_DIR="${PROJECT_DIR:-/workspace/projects}"
mkdir -p "$PROJECT_DIR" 2>/dev/null || true
echo "Project directory: $PROJECT_DIR"

# Symlink /project -> /workspace/projects for compatibility
if [ ! -e /project ]; then
    ln -s "$PROJECT_DIR" /project 2>/dev/null || true
fi

# --- Ensure OptiX (libnvoptix.so) is available for Karma XPU ---
# nvidia-container-toolkit should inject it, but some RunPod hosts don't.
# If missing, download the matching version from NVIDIA's apt repo.
if ! ldconfig -p 2>/dev/null | grep -q libnvoptix; then
    DRIVER_VER=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1)
    DRIVER_BRANCH="${DRIVER_VER%%.*}"
    echo "OptiX (libnvoptix.so) not found. Driver: ${DRIVER_VER}. Fetching..."
    cd /tmp
    apt-get update -qq 2>/dev/null
    if apt-get download "libnvidia-gl-${DRIVER_BRANCH}" 2>/dev/null; then
        mkdir -p /tmp/nvgl
        dpkg-deb -x libnvidia-gl-${DRIVER_BRANCH}_*.deb /tmp/nvgl
        if [ -f /tmp/nvgl/usr/lib/x86_64-linux-gnu/libnvoptix.so.${DRIVER_VER} ]; then
            cp /tmp/nvgl/usr/lib/x86_64-linux-gnu/libnvoptix.so.${DRIVER_VER} /usr/lib/x86_64-linux-gnu/
            ln -sf libnvoptix.so.${DRIVER_VER} /usr/lib/x86_64-linux-gnu/libnvoptix.so.1
            echo "Installed libnvoptix.so.${DRIVER_VER}"
        else
            # Try any libnvoptix in the package
            find /tmp/nvgl -name 'libnvoptix.so.*' -exec cp {} /usr/lib/x86_64-linux-gnu/ \;
            ls /usr/lib/x86_64-linux-gnu/libnvoptix.so.* 2>/dev/null | head -1 | xargs -I{} ln -sf {} /usr/lib/x86_64-linux-gnu/libnvoptix.so.1
            echo "Installed libnvoptix (fallback)"
        fi
        rm -rf /tmp/nvgl /tmp/libnvidia-gl-*.deb
    else
        echo "WARNING: Could not fetch libnvidia-gl-${DRIVER_BRANCH}. Karma XPU may not work."
    fi
    rm -rf /var/lib/apt/lists/*
    cd /opt/runpodfarm
fi
ldconfig 2>/dev/null || true

# --- Step 2: Setup Houdini environment ---
if [ -d "/workspace/houdini" ]; then
    echo "Setting up Houdini from Network Volume..."
    export HFS="/workspace/houdini"
    # Must cd first — houdini_setup_bash requires being in the install dir.
    # Disable set -u because houdini_setup_bash uses unset variables.
    # Save and restore working dir so worker module can be found.
    pushd "$HFS" > /dev/null && { set +u; source houdini_setup_bash; set -u; } 2>/dev/null; popd > /dev/null || true

    # Houdini licensing via remote sesinetd license server
    if [ -n "${SESINETD_HOST:-}" ]; then
        SESINETD_PORT="${SESINETD_PORT:-1715}"
        echo "Configuring license server: ${SESINETD_HOST}:${SESINETD_PORT}"
        # Kill any existing hserver and restart pointing to remote license server
        hserver -q 2>/dev/null || true
        sleep 1
        hserver --host "${SESINETD_HOST}" &
        sleep 2
        echo "License server connected: $(hserver -l 2>&1 | grep 'Connected To' || echo 'check failed')"
    fi

    # Set project-specific Houdini env
    export HOUDINI_PATH="$PROJECT_DIR/hda:&"
    export HOUDINI_TEMP_DIR="/tmp/houdini_temp"
    mkdir -p "$HOUDINI_TEMP_DIR"

    echo "Houdini ready: $(hython --version 2>/dev/null || echo 'version check failed')"
else
    echo "WARNING: Houdini not found at /workspace/houdini"
    echo "  Install Houdini to the Network Volume first."
fi

# --- Step 3: Start worker daemon ---
echo "Starting RunPodFarm Worker daemon..."
exec python3 -m worker.daemon
