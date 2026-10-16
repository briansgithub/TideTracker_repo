# Debian 12.11 slim, linux/amd64 image manifest published 2025-08-13.
# Keep both the dated tag and architecture-specific digest: changing either is
# a reviewed builder-environment upgrade.
FROM --platform=linux/amd64 debian:bookworm-20250811-slim@sha256:cea2634840f5a87503d8210e4df97b9f23a2acd67ff860a76c133d963032f866

ARG DEBIAN_FRONTEND=noninteractive

# Use immutable Debian snapshots so rebuilding the tool container cannot pick
# up newer native partition/filesystem utilities without a source change.
RUN rm -f /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources \
    && printf '%s\n' \
        'Types: deb' \
        'URIs: http://snapshot.debian.org/archive/debian/20250811T000000Z' \
        'Suites: bookworm bookworm-updates' \
        'Components: main' \
        'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg' \
        'Check-Valid-Until: no' \
        '' \
        'Types: deb' \
        'URIs: http://snapshot.debian.org/archive/debian-security/20250811T000000Z' \
        'Suites: bookworm-security' \
        'Components: main' \
        'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg' \
        'Check-Valid-Until: no' \
        > /etc/apt/sources.list.d/tidetracker-snapshot.sources \
    && dpkg --add-architecture armhf \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        apt \
        apt-rdepends \
        apt-utils \
        binfmt-support \
        ca-certificates \
        coreutils \
        dash \
        dosfstools \
        dpkg-dev \
        e2fsprogs \
        fdisk \
        file \
        findutils \
        gawk \
        grep \
        gzip \
        jq \
        kmod \
        mount \
        openssh-client \
        openssl \
        procps \
        python3 \
        qemu-user-static \
        sed \
        tar \
        udev \
        util-linux \
        xz-utils \
    && rm -rf /var/lib/apt/lists/*

ENV LC_ALL=C.UTF-8 \
    LANG=C.UTF-8 \
    TZ=UTC

COPY deploy/container/preflight-sparse-image.sh /usr/local/bin/tidetracker-builder-preflight
RUN chmod 0755 /usr/local/bin/tidetracker-builder-preflight

WORKDIR /workspace
