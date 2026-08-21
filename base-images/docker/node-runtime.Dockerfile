ARG BUILD_BASE_IMAGE=local/ai-ubuntu-build:22.04
ARG RUNTIME_BASE_IMAGE=local/ai-ubuntu-runtime:22.04
ARG UBUNTU_APT_MIRROR=repo.huaweicloud.com

FROM ${BUILD_BASE_IMAGE} AS node-build
ARG NODE_VERSION=24.18.0
ARG NODE_SOURCE_ARCHIVE=deps/node-v24.18.0.tar.gz
ARG UBUNTU_APT_MIRROR
ARG MAKE_JOBS=2
ENV DEBIAN_FRONTEND=noninteractive \
    PATH=/opt/node/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

RUN rm -f /etc/apt/apt.conf.d/docker-clean \
    && printf '%s\n' \
      "deb http://${UBUNTU_APT_MIRROR}/ubuntu/ jammy main" \
      "deb http://${UBUNTU_APT_MIRROR}/ubuntu/ jammy-updates main" \
      "deb http://${UBUNTU_APT_MIRROR}/ubuntu/ jammy-security main" \
      "deb http://${UBUNTU_APT_MIRROR}/ubuntu/ jammy universe" \
      > /etc/apt/sources.list \
    && apt-get -o Acquire::ForceIPv4=true -o Acquire::Retries=5 update \
    && apt-get install -y --no-install-recommends \
        build-essential ca-certificates curl python3 python3-distutils \
        libssl-dev zlib1g-dev libbz2-dev libreadline-dev libsqlite3-dev \
        libffi-dev liblzma-dev xz-utils

COPY ${NODE_SOURCE_ARCHIVE} /tmp/node-src.tar.gz
RUN mkdir -p /tmp/node-src \
    && tar -xzf /tmp/node-src.tar.gz -C /tmp/node-src --strip-components=1 \
    && rm -f /tmp/node-src.tar.gz \
    && cd /tmp/node-src \
    && ./configure --prefix=/opt/node --with-intl=small-icu \
    && make -j"${MAKE_JOBS}" \
    && make install \
    && /opt/node/bin/node --version \
    && /opt/node/bin/npm --version \
    && rm -rf /tmp/node-src

FROM node-build AS node-runtime-files
RUN rm -rf \
      /opt/node/include \
      /opt/node/share \
      /opt/node/lib/node_modules/npm/docs \
      /opt/node/lib/node_modules/npm/man

FROM ${RUNTIME_BASE_IMAGE} AS node-runtime
ARG UBUNTU_APT_MIRROR
ENV DEBIAN_FRONTEND=noninteractive \
    PATH=/opt/node/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
RUN rm -f /etc/apt/apt.conf.d/docker-clean \
    && printf '%s\n' \
      "deb http://${UBUNTU_APT_MIRROR}/ubuntu/ jammy main" \
      "deb http://${UBUNTU_APT_MIRROR}/ubuntu/ jammy-updates main" \
      "deb http://${UBUNTU_APT_MIRROR}/ubuntu/ jammy-security main" \
      > /etc/apt/sources.list \
    && apt-get -o Acquire::ForceIPv4=true -o Acquire::Retries=5 update \
    && apt-get install -y --no-install-recommends ca-certificates libssl3 zlib1g \
    && rm -rf /var/lib/apt/lists/*
COPY --from=node-runtime-files /opt/node /opt/node
RUN /opt/node/bin/node --version
ENTRYPOINT []
CMD ["node", "--version"]
