ARG BUILD_BASE_IMAGE=local/ai-ubuntu-build:22.04
ARG RUNTIME_BASE_IMAGE=local/ai-ubuntu-runtime:22.04
ARG UBUNTU_APT_MIRROR=repo.huaweicloud.com

# Official Node linux-x64 binary. Do not compile from source on Ubuntu 22.04/GCC 11.
FROM ${BUILD_BASE_IMAGE} AS node-unpack
ARG NODE_BINARY_ARCHIVE=deps/node-v24.18.0-linux-x64.tar.gz
COPY ${NODE_BINARY_ARCHIVE} /tmp/node.tar.gz
RUN mkdir -p /opt/node \
    && tar -xzf /tmp/node.tar.gz -C /opt/node --strip-components=1 \
    && rm -f /tmp/node.tar.gz \
    && test -x /opt/node/bin/node \
    && /opt/node/bin/node --version \
    && /opt/node/bin/npm --version

FROM ${BUILD_BASE_IMAGE} AS node-build
ENV PATH=/opt/node/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
COPY --from=node-unpack /opt/node /opt/node
RUN /opt/node/bin/node --version
ENTRYPOINT []
CMD ["node", "--version"]

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
COPY --from=node-unpack /opt/node /opt/node
RUN /opt/node/bin/node --version
ENTRYPOINT []
CMD ["node", "--version"]
