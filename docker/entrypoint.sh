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
