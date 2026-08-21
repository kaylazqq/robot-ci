# CI 基础镜像

日志栈（ES / Kafka / Logstash / Grafana / Kibana / Filebeat）构建时用的 `local/ai-*` 底包，做在这台 CI 机器上。

```bash
cd /opt/swr-push-helper/base-images
bash scripts/build-bases.sh ubuntu jdk
```

| 镜像 | 做法 |
|------|------|
| `local/ai-ubuntu-build:22.04` / `local/ai-ubuntu-runtime:22.04` | 给现成 Ubuntu 打标签 |
| `local/ai-jdk-build:21.0.12` / `local/ai-jdk-runtime:21.0.12` | 解压 Temurin 官方 JDK 二进制（`/opt/ai/installers` 已有则复用） |
| `local/ai-go-toolchain:1.26.5` | 镜像已存在则复用，不重编 |

二进制包缓存在 `/opt/ai/installers`，不要提交 `deps/` 里的 tar。
