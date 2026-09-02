# RunPodFarm v2 pod image
# Ubuntu 22.04 + CUDA runtime + sshd + worker daemon.
# Houdini is NOT baked in -- it comes from the network volume at
# /workspace/houdini/<HOUDINI_VERSION> (see pod/entrypoint.sh).
# Same image runs on both the CPU sync pod and the GPU render pods; on a
# CPU pod the CUDA runtime libs are simply unused.

FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04

LABEL maintainer="RunPodFarm"
LABEL description="RunPodFarm v2 pod - sshd + worker daemon, Houdini from network volume"

ENV DEBIAN_FRONTEND=noninteractive
ENV NVIDIA_VISIBLE_DEVICES=all
ENV NVIDIA_DRIVER_CAPABILITIES=compute,utility,graphics

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 openssh-server rsync curl ca-certificates zstd unzip \
    libgl1-mesa-glx libglu1-mesa libxi6 libxmu6 libxrandr2 libxcursor1 \
    libxinerama1 libxss1 libasound2 libxkbcommon0 libnss3 libdbus-1-3 \
    && rm -rf /var/lib/apt/lists/* \
    && curl -sSL https://rclone.org/install.sh | bash

RUN sed -i 's/#PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config

COPY pod/worker.py pod/housekeeping.py pod/entrypoint.sh /opt/rpfarm/
RUN chmod +x /opt/rpfarm/entrypoint.sh

EXPOSE 22 4440 4442 8000

ENTRYPOINT ["/opt/rpfarm/entrypoint.sh"]
