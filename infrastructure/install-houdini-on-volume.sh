#!/bin/bash
# Install Houdini from B2 to Network Volume at /workspace/houdini
# This script runs ON a RunPod pod with Network Volume mounted.
set -euo pipefail

HOUDINI_VERSION="20.5.684"
ISO_NAME="houdini-${HOUDINI_VERSION}-linux_x86_64_gcc11.2.iso"
B2_URL="https://f003.backblazeb2.com/file/runpodfarm-juicefs/installers/${ISO_NAME}"
INSTALL_DIR="/workspace/houdini"
TMP_DIR="/tmp/houdini_install"

echo "=== Houdini ${HOUDINI_VERSION} Installation ==="
echo "Target: ${INSTALL_DIR}"

# Check if already installed
if [ -f "${INSTALL_DIR}/houdini_setup_bash" ]; then
    echo "Houdini already installed at ${INSTALL_DIR}"
    source "${INSTALL_DIR}/houdini_setup_bash"
    hython --version 2>/dev/null || true
    echo "DONE: already installed"
    exit 0
fi

# Install dependencies
echo "Installing dependencies..."
apt-get update -qq
apt-get install -y -qq p7zip-full curl libxss1 libasound2 > /dev/null 2>&1

# Download ISO from B2
mkdir -p "${TMP_DIR}"
echo "Downloading ${ISO_NAME} from B2..."
if [ ! -f "${TMP_DIR}/${ISO_NAME}" ]; then
    curl -L -o "${TMP_DIR}/${ISO_NAME}" "${B2_URL}"
fi
ls -lh "${TMP_DIR}/${ISO_NAME}"

# Extract ISO (no need for mount/FUSE)
echo "Extracting ISO..."
cd "${TMP_DIR}"
7z x -y "${ISO_NAME}" -o"${TMP_DIR}/extracted" > /dev/null 2>&1

# Find the installer script
INSTALLER=$(find "${TMP_DIR}/extracted" -name "houdini.install" -type f | head -1)
if [ -z "$INSTALLER" ]; then
    echo "ERROR: houdini.install not found in ISO"
    ls -R "${TMP_DIR}/extracted/"
    exit 1
fi
echo "Found installer: ${INSTALLER}"

# Run the installer (silent, accept EULA)
echo "Running Houdini installer..."
chmod +x "${INSTALLER}"
"${INSTALLER}" \
    --auto-install \
    --accept-EULA 2024-01-01 \
    --install-houdini \
    --no-install-license \
    --no-install-menus \
    --no-install-bin-symlink \
    --install-dir "${INSTALL_DIR}" \
    2>&1 || {
        # Try alternative EULA date
        echo "Retrying with different EULA date..."
        "${INSTALLER}" \
            --auto-install \
            --accept-EULA 2025-01-01 \
            --install-houdini \
            --no-install-license \
            --no-install-menus \
            --no-install-bin-symlink \
            --install-dir "${INSTALL_DIR}" \
            2>&1
    }

# Verify installation
echo "Verifying installation..."
if [ -f "${INSTALL_DIR}/houdini_setup_bash" ]; then
    source "${INSTALL_DIR}/houdini_setup_bash"
    echo "Houdini version: $(hython --version 2>&1 || echo 'version check needs license')"
    echo "HFS: ${HFS:-not set}"
    echo "DONE: Houdini installed to ${INSTALL_DIR}"
else
    echo "ERROR: houdini_setup_bash not found after installation"
    ls -la "${INSTALL_DIR}/" 2>/dev/null || echo "Install dir does not exist"
    exit 1
fi

# Cleanup temp files
echo "Cleaning up..."
rm -rf "${TMP_DIR}"
echo "=== Installation complete ==="
