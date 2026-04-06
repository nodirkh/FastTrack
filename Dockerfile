FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    bc \
    bison \
    build-essential \
    cpio \
    flex \
    gcc \
    git \
    libelf-dev \
    libncurses-dev \
    libssl-dev \
    make \
    python3 \
    python3-pip \
    rsync \
    && rm -rf /var/lib/apt/lists/*

# FastTrack tool
COPY requirements.txt /fasttrack/requirements.txt
RUN pip3 install --no-cache-dir -r /fasttrack/requirements.txt

COPY iter/ /fasttrack/iter/
COPY launch.py /fasttrack/launch.py
COPY rootfs/ /fasttrack/rootfs/

ENV PYTHONPATH=/fasttrack

WORKDIR /linux
