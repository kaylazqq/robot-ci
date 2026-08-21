ARG BUILD_BASE_IMAGE=local/ai-ubuntu-build:22.04
ARG UBUNTU_APT_MIRROR=repo.huaweicloud.com

# Compile Go from source. The linux-amd64 tarball is only a bootstrap compiler.
FROM ${BUILD_BASE_IMAGE} AS go-build
ARG GO_VERSION=1.26.5
ARG GO_BOOTSTRAP_ARCHIVE=deps/go1.25.9.linux-amd64.tar.gz
ARG GO_SOURCE_ARCHIVE=deps/go1.26.5.src.tar.gz
ARG UBUNTU_APT_MIRROR
ENV DEBIAN_FRONTEND=noninteractive \
    GOROOT_BOOTSTRAP=/opt/go-bootstrap \
    GOROOT_FINAL=/usr/local/go \
    GOOS=linux \
    GOARCH=amd64

RUN rm -f /etc/apt/apt.conf.d/docker-clean \
    && printf '%s\n' \
      "deb http://${UBUNTU_APT_MIRROR}/ubuntu/ jammy main" \
      "deb http://${UBUNTU_APT_MIRROR}/ubuntu/ jammy-updates main" \
      "deb http://${UBUNTU_APT_MIRROR}/ubuntu/ jammy-security main" \
      > /etc/apt/sources.list \
    && apt-get -o Acquire::ForceIPv4=true -o Acquire::Retries=5 update \
    && apt-get install -y --no-install-recommends \
        bash build-essential ca-certificates curl git python3

COPY ${GO_BOOTSTRAP_ARCHIVE} /tmp/go-bootstrap.tar.gz
COPY ${GO_SOURCE_ARCHIVE} /tmp/go-src.tar.gz
RUN mkdir -p /opt/go-bootstrap /usr/local/go \
    && tar -xzf /tmp/go-bootstrap.tar.gz -C /opt/go-bootstrap --strip-components=1 \
    && tar -xzf /tmp/go-src.tar.gz -C /usr/local \
    && rm -f /tmp/go-bootstrap.tar.gz /tmp/go-src.tar.gz \
    && cd /usr/local/go/src \
    && ./make.bash \
    && rm -rf /opt/go-bootstrap /usr/local/go/pkg/bootstrap /usr/local/go/pkg/obj \
    && /usr/local/go/bin/go version

FROM ${BUILD_BASE_IMAGE} AS go-toolchain
ARG UBUNTU_APT_MIRROR
COPY --from=go-build /usr/local/go /usr/local/go
ENV DEBIAN_FRONTEND=noninteractive \
    PATH=/usr/local/go/bin:${PATH} \
    GOTOOLCHAIN=local \
    GOPROXY=https://goproxy.cn,direct
RUN rm -f /etc/apt/apt.conf.d/docker-clean \
    && printf '%s\n' \
      "deb http://${UBUNTU_APT_MIRROR}/ubuntu/ jammy main" \
      "deb http://${UBUNTU_APT_MIRROR}/ubuntu/ jammy-updates main" \
      "deb http://${UBUNTU_APT_MIRROR}/ubuntu/ jammy-security main" \
      > /etc/apt/sources.list \
    && apt-get -o Acquire::ForceIPv4=true -o Acquire::Retries=5 update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && /usr/local/go/bin/go version
