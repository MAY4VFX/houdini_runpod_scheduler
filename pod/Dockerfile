# RunPodFarm v2 pod image
# Ubuntu 22.04 + sshd + worker daemon. Deliberately thin.
#
# Houdini is NOT baked in -- it comes from the network volume at
# /workspace/houdini/<HOUDINI_VERSION> (see pod/entrypoint.sh).
#
# Base is plain `ubuntu:22.04`, NOT `nvidia/cuda:*-runtime`: on a GPU pod
# RunPod's container runtime injects the host NVIDIA driver and libcuda into
# the container, so the CUDA *runtime toolkit* in the image buys nothing and
# Houdini does not need it. It cost 1310 MiB of compressed layers (image went
# from ~1506 MiB to ~145 MiB), and that pull is paid on every cold pod start.
#
# Same image runs on the CPU sync pod and the GPU render pods.

FROM ubuntu:22.04

LABEL maintainer="RunPodFarm"
LABEL description="RunPodFarm v2 pod - sshd + worker daemon, Houdini from network volume"

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 openssh-server rsync curl ca-certificates zstd unzip \
    libgl1-mesa-glx libglu1-mesa libxi6 libxmu6 libxrandr2 libxcursor1 \
    libxinerama1 libxss1 libasound2 libxkbcommon0 libnss3 libdbus-1-3 \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://rclone.org/install.sh -o /tmp/rclone-install.sh \
    && bash /tmp/rclone-install.sh \
    && rm /tmp/rclone-install.sh

RUN sed -i 's/#PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config

COPY pod/worker.py pod/housekeeping.py pod/entrypoint.sh /opt/rpfarm/
RUN chmod +x /opt/rpfarm/entrypoint.sh

EXPOSE 22 4440 4442 8000

ENTRYPOINT ["/opt/rpfarm/entrypoint.sh"]
