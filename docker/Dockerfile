# RunPodFarm Worker Image
# Ubuntu 22.04 + CUDA 12.x + Worker daemon
# Houdini is NOT included - it comes from Network Volume at /workspace
# Project files are on Network Volume at /workspace/projects

FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04

LABEL maintainer="RunPodFarm"
LABEL description="RunPodFarm Worker - distributed Houdini rendering on RunPod"

ENV DEBIAN_FRONTEND=noninteractive
ENV NVIDIA_VISIBLE_DEVICES=all
ENV NVIDIA_DRIVER_CAPABILITIES=all

# System dependencies for Houdini 20.5 + Karma XPU
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv \
    curl wget \
    # OpenGL / EGL (Karma XPU rendering)
    libgl1 libglx0 libegl1 libopengl0 libglu1-mesa \
    # X11 core (Houdini DSO dependencies)
    libxi6 libxmu6 libxrandr2 libxcursor1 libxinerama1 libxss1 \
    libxcomposite1 libxdamage1 libxfixes3 libxext6 libxrender1 \
    libxtst6 libxt6 \
    # XKB / DBus / PCI (BRAY_AccelGpu.so dependencies)
    libxkbcommon0 libdbus-1-3 libpci3 \
    # NSS / NSPR (Houdini DSO dependencies)
    libnss3 libnspr4 \
    # Fonts / misc
    libfontconfig1 libasound2 \
    # CUDA NVRTC (Karma XPU shader compilation)
    cuda-nvrtc-12-4 \
    # Xvfb for headless rendering
    xvfb \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install JuiceFS binary (used for B2<->NV sync without FUSE)
RUN curl -sSL https://github.com/juicedata/juicefs/releases/download/v1.2.1/juicefs-1.2.1-linux-amd64.tar.gz \
    | tar -xz -C /usr/local/bin juicefs \
    && chmod +x /usr/local/bin/juicefs

# Create worker directory
WORKDIR /opt/runpodfarm
COPY worker/ ./worker/
COPY docker/entrypoint.sh ./entrypoint.sh
RUN chmod +x entrypoint.sh

# Install Python dependencies
RUN pip3 install --no-cache-dir -r worker/requirements.txt

# Expose no ports - worker communicates via Redis only
ENTRYPOINT ["/opt/runpodfarm/entrypoint.sh"]
