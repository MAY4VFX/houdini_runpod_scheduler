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

# --- Ensure OptiX libraries are available for Karma XPU ---
# nvidia-container-toolkit should inject libnvoptix.so, libnvidia-rtcore.so,
# and nvoptix.bin from the host driver, but some RunPod hosts have old
# container-toolkit (< 1.14.4) that doesn't inject these.
# If missing, extract them from the matching NVIDIA driver package.
export OPTIX_CACHE_PATH="/tmp/optix_cache"
mkdir -p "$OPTIX_CACHE_PATH" 2>/dev/null || true

DRIVER_VER=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1)
DRIVER_BRANCH="${DRIVER_VER%%.*}"
NEED_OPTIX=false

if ! ldconfig -p 2>/dev/null | grep -q libnvoptix; then
    NEED_OPTIX=true
    echo "OptiX: libnvoptix.so NOT found"
fi
if ! ldconfig -p 2>/dev/null | grep -q libnvidia-rtcore; then
    NEED_OPTIX=true
    echo "OptiX: libnvidia-rtcore.so NOT found"
fi
if [ ! -f /usr/share/nvidia/nvoptix.bin ]; then
    NEED_OPTIX=true
    echo "OptiX: nvoptix.bin NOT found"
fi

if [ "$NEED_OPTIX" = true ] && [ -n "$DRIVER_BRANCH" ]; then
    echo "Fetching OptiX libs from libnvidia-gl-${DRIVER_BRANCH} (driver ${DRIVER_VER})..."
    cd /tmp
    apt-get update -qq 2>/dev/null
    if apt-get download "libnvidia-gl-${DRIVER_BRANCH}" 2>/dev/null; then
        mkdir -p /tmp/nvgl
        dpkg-deb -x libnvidia-gl-${DRIVER_BRANCH}_*.deb /tmp/nvgl

        # Copy only missing files (don't overwrite host-injected libs)
        for lib in libnvoptix libnvidia-rtcore libnvidia-glcore; do
            for f in /tmp/nvgl/usr/lib/x86_64-linux-gnu/${lib}.so.*; do
                [ -f "$f" ] || continue
                dest="/usr/lib/x86_64-linux-gnu/$(basename "$f")"
                if [ ! -f "$dest" ]; then
                    cp "$f" "$dest"
                    echo "  Installed $(basename "$f")"
                fi
            done
        done
        # Create symlinks if missing
        if [ ! -f /usr/lib/x86_64-linux-gnu/libnvoptix.so.1 ]; then
            ls /usr/lib/x86_64-linux-gnu/libnvoptix.so.* 2>/dev/null | head -1 | \
                xargs -I{} ln -sf "$(basename {})" /usr/lib/x86_64-linux-gnu/libnvoptix.so.1
        fi
        # Copy nvoptix.bin (precompiled OptiX kernels)
        if [ ! -f /usr/share/nvidia/nvoptix.bin ]; then
            find /tmp/nvgl -name 'nvoptix.bin' -exec cp {} /usr/share/nvidia/ \; 2>/dev/null
            [ -f /usr/share/nvidia/nvoptix.bin ] && echo "  Installed nvoptix.bin"
        fi
        rm -rf /tmp/nvgl /tmp/libnvidia-gl-*.deb
    else
        echo "WARNING: Could not fetch libnvidia-gl-${DRIVER_BRANCH}. Karma XPU may not work."
    fi
    rm -rf /var/lib/apt/lists/*
    cd /opt/runpodfarm
fi
ldconfig 2>/dev/null || true
echo "OptiX: driver=${DRIVER_VER}, libnvoptix=$(ldconfig -p 2>/dev/null | grep -c libnvoptix), rtcore=$(ldconfig -p 2>/dev/null | grep -c libnvidia-rtcore), nvoptix.bin=$([ -f /usr/share/nvidia/nvoptix.bin ] && echo YES || echo NO)"

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
