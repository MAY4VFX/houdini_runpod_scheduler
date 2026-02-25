# RunPodFarm Worker Image
# Ubuntu 22.04 + CUDA 12.x + Worker daemon
# Houdini is NOT included - it comes from Network Volume at /workspace
# Project files are on Network Volume at /workspace/projects

FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04

LABEL maintainer="RunPodFarm"
LABEL description="RunPodFarm Worker - distributed Houdini rendering on RunPod"

ENV DEBIAN_FRONTEND=noninteractive
ENV NVIDIA_VISIBLE_DEVICES=all
ENV NVIDIA_DRIVER_CAPABILITIES=compute,utility,graphics

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv \
    curl wget \
    libgl1-mesa-glx libglu1-mesa libxi6 libxmu6 libxrandr2 \
    libxcursor1 libxinerama1 libxss1 libasound2 \
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
