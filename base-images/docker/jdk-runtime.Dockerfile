ARG BUILD_BASE_IMAGE=local/ai-ubuntu-build:22.04-v1
ARG RUNTIME_BASE_IMAGE=local/ai-ubuntu-runtime:22.04-apt-v1
ARG UBUNTU_APT_MIRROR=repo.huaweicloud.com

# Use the official Temurin JDK 21 binary. Do not compile OpenJDK from source.
FROM ${BUILD_BASE_IMAGE} AS jdk-unpack
ARG BOOT_JDK_ARCHIVE=deps/OpenJDK21U-jdk_x64_linux_hotspot_21.0.12_8.tar.gz
COPY ${BOOT_JDK_ARCHIVE} /tmp/jdk.tar.gz
RUN mkdir -p /opt/jdk \
    && tar -xzf /tmp/jdk.tar.gz -C /opt/jdk --strip-components=1 \
    && rm -f /tmp/jdk.tar.gz \
    && /opt/jdk/bin/java -version

FROM ${BUILD_BASE_IMAGE} AS jdk-build
ENV JAVA_HOME=/opt/jdk \
    PATH=/opt/jdk/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
COPY --from=jdk-unpack /opt/jdk /opt/jdk
RUN /opt/jdk/bin/java -version
ENTRYPOINT []
CMD ["java", "-version"]

FROM ${RUNTIME_BASE_IMAGE} AS jdk-runtime
ARG UBUNTU_APT_MIRROR
ENV DEBIAN_FRONTEND=noninteractive \
    JAVA_HOME=/opt/jdk \
    PATH=/opt/jdk/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
RUN rm -f /etc/apt/apt.conf.d/docker-clean \
    && printf '%s\n' \
      "deb http://${UBUNTU_APT_MIRROR}/ubuntu/ jammy main" \
      "deb http://${UBUNTU_APT_MIRROR}/ubuntu/ jammy-updates main" \
      "deb http://${UBUNTU_APT_MIRROR}/ubuntu/ jammy-security main" \
      > /etc/apt/sources.list \
    && apt-get -o Acquire::ForceIPv4=true -o Acquire::Retries=5 update \
    && apt-get install -y --no-install-recommends \
        ca-certificates fontconfig libasound2 libfreetype6 libfontconfig1 zlib1g \
    && rm -rf /var/lib/apt/lists/*
COPY --from=jdk-unpack /opt/jdk /opt/jdk
RUN /opt/jdk/bin/java -version
ENTRYPOINT []
CMD ["java", "-version"]
