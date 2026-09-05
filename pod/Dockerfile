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

# libegl1 is what makes Karma XPU work, and its absence is why XPU did not.
# Eight render tasks died identically with "Karma XPU delegate not supported on
# this machine" / "Unable to load render plugin: BRAY_HdKarmaXPU", and the
# obvious suspect was wrong: RunPod already sets
# NVIDIA_DRIVER_CAPABILITIES=compute,display,graphics,utility,video and
# libnvoptix.so.1 is present in the container. Bisected on a live pod --
# installing libegl1 alone flips `husk --list-renderers` from
# "BRAY_HdKarmaXPU (Karma XPU) - unsupported" to supported, and an XPU render
# then produces an image. The image had libGL (libgl1-mesa-glx) but never
# libEGL, which on Ubuntu 22.04 is a separate package; Karma XPU's support
# probe needs it to reach the driver's own libEGL_nvidia.
#
# libxcomposite1 is the same family, found in the same pass: without it
# HOUDINI_DSO_ERROR=2 reports load failures for COP2_GPU*, OBJ_ReLight and
# CHOP_Mouse3D on every pod. Not fatal to a render, but it is noise in every
# log and the package is tiny.
#
# libatomic1 is required by Houdini 22.0 (gcc 14.2 build): its `hython-bin`
# links libatomic.so.1 directly, and it is the ONLY unresolved `ldd` entry on
# a stock ubuntu:22.04. Houdini 20.5 did not need it -- it used Houdini's own
# bundled `libhboost_atomic` -- so this gap only appeared with the 22.0
# migration (Task 14), where hython died on every pod with
# "libatomic.so.1: cannot open shared object file".
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 openssh-server rsync curl ca-certificates zstd unzip \
    tini \
    libatomic1 \
    libgl1-mesa-glx libglu1-mesa libxi6 libxmu6 libxrandr2 libxcursor1 \
    libxinerama1 libxss1 libasound2 libxkbcommon0 libnss3 libdbus-1-3 \
    libegl1 libxcomposite1 \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://rclone.org/install.sh -o /tmp/rclone-install.sh \
    && bash /tmp/rclone-install.sh \
    && rm /tmp/rclone-install.sh

RUN sed -i 's/#PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config

COPY pod/worker.py pod/housekeeping.py pod/entrypoint.sh /opt/rpfarm/
RUN chmod +x /opt/rpfarm/entrypoint.sh

EXPOSE 22 4440 4442 8000

# tini as PID 1, purely to reap orphans.
#
# entrypoint.sh ends in `exec python3 worker.py`, so PID 1 was the worker. When
# an sshd session process exits, its sftp-server child is reparented to PID 1
# -- and a Python PID 1 never wait()s on adopted orphans, so every finished
# SFTP session left a <defunct> behind forever. Observed on a live sync pod:
# eleven of them after one upload, plus two orphaned sshd. They also made the
# liveness counter read "busy" permanently before it learned to skip zombies.
#
# tini exists to do exactly this and nothing else: it forwards signals and
# reaps whatever it inherits, without touching how the worker manages its own
# children (which is why a SIGCHLD handler in the worker would have been the
# wrong tool -- it would race with subprocess's own waitpid for task exit codes).
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/rpfarm/entrypoint.sh"]
