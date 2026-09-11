# SWR 一键推送

## 发给别人（本机 Windows）

只要这些文件，或发 `dist\swr-push-helper-*.zip`：

```
start.bat  stop.bat  server.py  services.json  test-plans.json  test-suites/  test_runner.py  unittest_case_runner.py  shell_case_runner.py  web/  README.md  deploy-linux.sh  config.example.json  base-images/
```

## 部署到 Linux 服务器（推荐给多人共用）

1. 把整个目录拷到服务器，例如 `/opt/swr-push-helper`
2. 在 GitHub 创建 **PAT**（不要用账号密码，GitHub 已不支持密码拉代码）：  
   https://github.com/settings/tokens （勾选 `repo`）
3. 在服务器执行：

```bash
export GH_TOKEN=ghp_你的PAT
bash deploy-linux.sh
```

4. 浏览器打开 `http://服务器IP/`
5. 别人只需粘贴**自己的 SWR 临时登录指令**；拉私有 GitHub 代码用服务器上的 Token，无需再登 GitHub

之后升级**只改代码，不换目录**：

```bash
# 推荐：原地 git 快进（不碰 data/、config.json、logs/）
bash /opt/swr-push-helper/scripts/update-server.sh

# 只能用 tar 包时：覆盖代码，保留运行时数据
bash /opt/swr-push-helper/scripts/apply-release.sh /tmp/robot-ci.tar.gz
```

禁止 `mv /opt/swr-push-helper /opt/swr-push-helper.bak.*` 再解压新包。收藏、环境、账号会话都在 `data/robot-ci.db`，整目录搬走等于换一台空库。`git reset --hard` 只动已跟踪文件，不会删 gitignore 的 `data/`。

## 第一阶段测试

工具会在拉取服务代码后执行 `test-plans.json` 中受控的 UT/DT 测试，并在任务日志和页面显示总用例、通过、失败和错误数及失败用例摘要。第一阶段测试不阻断镜像构建或 SWR 推送；数据库、Redis、Temporal 等外部依赖测试不在测试计划中执行。

批量构建时，每个所选微服务都按“拉取代码 → 执行该服务 UT/DT → 构建 → 推送 → 归档”的顺序独立执行。页面按微服务显示独立的测试结果卡片和分页表格，单个服务测试失败不会阻止该服务或后续服务继续构建。SWR 登录检测会向仓库发起真实 manifest 探测；仅明确确认登录有效时才开始需要推送的任务，鉴权失败后立即停止后续 SWR 操作。

`multica-server` 构建前必须在页面填写 Daemon 版本，格式为 `vMAJOR.MINOR.PATCH`，例如 `v1.2.3`。前后端使用同一规则校验；工具不修改微服务配置文件，而是通过 `DAEMON_RELEASE_VERSION` 调用仓库 `.cid/build.yaml` 声明的 `build/package/build.sh build-cce`，由微服务构建脚本把对应版本写入镜像内的 release manifest。最近一次成功提交构建的合法版本会保存在服务器 `logs/last-daemon-version.json` 并对所有浏览器回填；浏览器本地值只作为服务器状态不可用时的兜底。

`multica-fleet` 来自独立仓库 `censong574-spec/multica-fleet`，属于仅归档服务，不登录或推送 SWR。工具调用仓库 `.cid/build.yaml` 声明的 `build/package/build.sh pack`，在同一归档目录生成包含 Fleet、OpenCode、Hermes 三个镜像的单个 tar，以及 `multica-fleet.env` 和已固化本次 tag 的 `deploy-multica-fleet.sh`。

测试运行器会在宿主机持久复用 `~/.cache/robot-ci-tests` 下的 Go build/module、GOPATH 和 pip 下载缓存。即使 systemd 未设置 `HOME`、`GOCACHE`、`GOMODCACHE` 或 `GOPATH`，运行器也会自动补齐并创建目录；可用 `SWR_TEST_CACHE_ROOT` 指定其他缓存根目录。

所有服务的业务范围、测试根目录和新增用例约定集中记录在 `test-suites/README.md` 与 `test-suites/catalog.json`。测试代码保留在各服务仓库，与业务代码同版本提交；部署工具只维护安全的执行白名单。

## 对方电脑 / 使用方需要

- 能访问该网页
- 华为云 SWR 临时登录指令 + `public_ai` 推送权限

## 本地归档（nginx）

SWR 推送成功后，还会把镜像 `docker save` 到本机 nginx 目录，按构建时间建子目录：

```
/usr/share/nginx/html/images/YYYYMMDDHHMMSS/<image>_<tag>.tar
```

页面支持勾选多个微服务一次构建；同一任务里的镜像会归档到**同一个**时间戳目录。

每个微服务的分支选择框后提供独立刷新按钮。页面初始化最多并发加载 3 个仓库的分支，后端只缓存成功结果 60 秒；手动刷新会绕过前后端缓存并重新读取该仓库的最新分支。查询失败只在对应微服务行提示，失败结果不会缓存，也不会再把回退的 `main` 伪装成完整分支列表。GitHub API 备用查询支持分页。

## 多人并发与页面刷新

服务器最多同时跑 `max_concurrent_jobs` 个构建任务（默认 5）。**同一微服务也可以并发**（每个任务 clone 到 `<workspace_root>/<仓库名>--<job_id>`），但 **mattermost** 与 **kibana-service** 例外：全机各只允许 **1 个** 并发（`max_concurrent: 1`）。mattermost 与别人共用 `/opt/ai/build-cache/build.swap` 和约 3.6G 内存；kibana 在仓库 `build/package/build.sh` 里自建 swap 并需要约 8GB Node 堆。`public-service` 仍是共享旁路目录（加锁更新、不删除），供尚未完全自包含的构建脚本读取共享资源。达到全局或单服务并发上限时新请求返回 `409`。

页面按游标增量读取运行中日志，避免大日志重复传输和慢请求乱序覆盖。任务日志只显示当前这一次构建；运行中可点「停止任务」强制杀掉当前构建进程。本浏览器（`client_id`）的历史任务在「构建历史」里分页查看，点「查看日志」再打开那一次的详情。浏览器只在本地保存 10 分钟“最近查看的 job ID”，用于规避刷新时任务恰好完成造成的日志回显丢失。已完成的 job ID 不占用锁。新任务开始时会清掉该服务已结束任务留下的 `--<job_id>` 目录。

「产物管理」与「构建历史」都按同一策略控量：**最多保留 100 条**；超过 100 时自动删最旧的，**只留最新 50 条**（产物是全局列表，历史按每个 `client_id` 单独计数）。helper 启动时也会扫一遍旧记录。

每次构建在独立临时目录里重新 `git clone`（`<仓库名>--<job_id>`），构建结束后保留本次 checkout 方便失败排查；该服务下一次新任务开始时再删掉已结束任务的旧目录。Go/Python/npm 编译临时文件走 `/home/ci`（`TMPDIR`/`GOTMPDIR`），不写 1.8G 的 `/tmp` tmpfs。SWR 推送并归档成功后只删除本次业务镜像（`local/<image>:<tag>` 和对应 SWR tag）；磁盘紧张时会再跑 `docker image prune -af` 清未用镜像，但 **`local/ai-go-toolchain`、`local/ai-jdk-*`、`local/ai-ubuntu-*` 与 Fleet OpenCode/Hermes 镜像会保留**。日志栈底包的制作脚本在仓库 `base-images/`。

磁盘回收在创建归档目录、`docker save` 之前、以及任务结束（成功或失败）时执行：

1. **始终** `swapoff` 并删除 `/opt/ai/build-cache/build.swap` 和 `/home/ci/**/build.swap`（mattermost 编译期临时交换文件，编完不应占盘）。**例外**：另有 mattermost 任务仍在编译时跳过，避免把别人正在用的 swap 卸掉。
2. 使用率超过 **80%** 时，先删 `/usr/share/nginx/html/images` 下较旧的时间戳归档（优先保留最近 3 个；仍高于 80% 则删到只剩最新 1 个）。被删掉的包在「产物管理」里会显示为已失效。
3. 触发回收时（使用率曾达到 **80%**），先删较旧归档；**无论归档后是否已低于 80%**，都会跑 **`docker image prune -af`**（保留 `local/ai-go-toolchain`、`local/ai-jdk-*`、`local/ai-ubuntu-*` 与 Fleet OpenCode/Hermes）。若 image prune 后仍 ≥ 80%，再跑 **`docker builder prune -af`** 回收 BuildKit 层缓存。

`multica-fleet` 是例外的缓存优化场景：工具仍会重新克隆源码，但会把 Fleet 生成的 `.build-source.sha256` 和 `.build-image-ids` 保存到 `<workspace_root>/.robot-ci-cache/multica-fleet/runtime-images/`，并在新 checkout 中恢复。Fleet 构建脚本只有在新源码指纹一致、两个 Runtime Docker 镜像仍存在且 Image ID 一致时才跳过 OpenCode/Hermes 重建；源码、Dockerfile、依赖或本地镜像发生变化时会自动重新构建。三镜像合并归档仍会在每次任务中重新生成，因此不会复用或误发旧归档。工具会把最终 tar 权限统一设为 `0644` 供 nginx 下载，并原样保留 Fleet 生成的 `.meta` 和 `.sha256`；离线部署脚本使用 `.sha256` 校验大文件下载完整性。

可通过 nginx 直接下载，例如：`http://服务器IP/images/YYYYMMDDHHMMSS/<image>_<tag>.tar`

`config.json` 相关项：

```json
{
  "archive_enabled": true,
  "archive_required": true,
  "archive_root": "/usr/share/nginx/html/images",
  "test_policy": "report_only",
  "ci_tmp_root": "/home/ci"
}
```

`test_policy` 支持 `report_only` 和 `blocking`，默认是 `report_only`。默认策略下 UT、DT 失败会记录并展示结果，但不会阻断后续构建、归档和推送；只有显式配置为 `blocking` 时才在测试失败后终止当前服务。

## 仓库级 `.cid/build.yaml`

已整改的服务仓以 `.cid/build.yaml` 作为唯一构建契约。robot-ci 在拉取分支后读取该文件，并按 `scripts` 顺序执行启用的 UT、DT 和镜像构建步骤；测试报告格式及路径、构建超时、镜像名称和交付方式也来自该文件。

- `dependencies`、`machine` 由固定构建机环境管理，禁止写入仓库 YAML。
- `continue_on_error` 由 robot-ci 全局 `test_policy` 管理，禁止写入仓库 YAML。默认 `report_only`，测试失败只展示结果，不阻断构建和推送。
- `enabled: false` 的测试阶段不执行，也不会作为“跳过用例”展示。
- 普通服务只在仓内构建本地镜像，SWR 登录、标签、推送、重试、校验和服务器归档由 robot-ci 统一执行。
- 仓库构建入口是 `build/package/build.sh`；部署模板和本地部署脚本在 `build/deploy/`。根目录不再保留 `build-image.sh` / `deploy.sh`。
- `multica-fleet` 的 `delivery: archive-only` 仍只生成包含三个镜像、环境模板和部署脚本的下载包，不推送 SWR。
- `ops-router` 的 `delivery: archive-only` 主机安装包从 `.cid/output/` 读取；旧分支的 `runtime-images/cce-export/` 仍兼容。

robot-ci 会校验 YAML 的服务 ID、唯一构建步骤、测试类型、报告格式、artifact 交付配置和禁止字段。主机安装包按 `artifacts.package.pattern` 从仓库工作区发现；YAML 无效时，当前服务明确失败，不会静默伪装为旧流程。

## CCE Gamma 测试

真实 Gamma 环境的流程为「构建并推送镜像 → CCE rollout 替换该负载镜像并等待就绪 → 执行服务仓 Gamma 用例」。服务在 `.cid/build.yaml` 增加 `type: test`、`test_type: gamma` 的步骤，并像 UT/DT 一样提供 `junit`、`go-json`、`case-json` 或 `unittest` 报告路径。Gamma 用例不会在构建前执行，只有用户在流水线勾选「gamma测试」且 rollout 成功后才运行；未声明用例或未生成报告会让 Gamma 阶段失败，不能误报通过。

环境管理中为每个 CCE 环境配置「Gamma 测试地址」（无用户名、密码的 `http(s)` 基址）。平台仅注入 `GAMMA_BASE_URL`、环境 ID/名称、Region、集群和负载等非敏感变量；跳板机和节点凭据绝不传给服务仓测试命令。Gamma 节点的「测试结果 / 日志详情」使用与 UT/DT 相同的分页用例表、失败摘要和日志视图。

## 安全提醒

- 不要把账号密码发到聊天或写进仓库
- 服务器上的 `config.json` 含 Token，权限应为 `600`
- 聊天里暴露过的密码请立刻修改

## GM Agent 镜像构建与部署

构建页面中的 `gmagent` 服务对应：

- 仓库：`https://github.com/rollingfruit/gmagent.git`
- 默认分支：`codex/cloud-im-orchestration`
- 本地镜像：`local/gmagent:<YYMMDDHHMM>_<short-sha>`
- SWR 镜像：`<swr_registry>/<swr_org>/gmagent:<YYMMDDHHMM>_<short-sha>`
- 本地归档：`<archive_root>/<YYYYMMDDHHMMSS>/gmagent_<tag>.tar`

构建机只需要 CI/推送变量，不需要任何 GM Agent 运行凭据：

- `SWR_GITHUB_TOKEN` 或 `GITHUB_TOKEN`：拉取私有 GitHub 仓库时使用。
- `SWR_GITHUB_SSH_KEY`：使用 SSH 拉取时的私钥路径，可替代 GitHub Token。
- `SWR_ARCHIVE_ROOT`、`SWR_ARCHIVE_ENABLED`、`SWR_ARCHIVE_REQUIRED`：控制镜像归档。
- SWR 登录使用页面提交的华为云临时 `docker login` 命令；凭据只进入构建机的 Docker credential store，不写入仓库或镜像。
- `CCE_GIT_HASH`、`CCE_SKIP_EXPORT`、`CCE_UPLOAD` 等由 robot-ci 注入，不应作为容器运行变量。

robot-ci 在启动构建进程前会移除 `GM_*`、模型供应商、数据库、S3、Mattermost、MemoryService 和 Multica 等运行变量，防止宿主机上已有的运行凭据意外成为构建输入。GM Agent 的 Dockerfile 不接收运行时 secret build arguments。

### Kubernetes 运行时配置

建议把非敏感配置写入 Deployment 的 `env`，把凭据写入独立 Kubernetes Secret，并用 `envFrom.secretRef` 或 `secretKeyRef` 注入。最低生产配置：

```text
GM_AGENT_ENVIRONMENT=production
GM_AGENT_ORCHESTRATION_ENABLED=1
GM_AGENT_ORCHESTRATION_TOKEN=<与 Semantic Schedule 一致的内部 Bearer Token>
GM_AGENT_DATABASE_URL=postgresql://<user>:<password>@<host>:5432/<database>?sslmode=require
GM_AGENT_MODEL=<provider-qualified-model>
GM_AGENT_AVAILABLE_MODELS=<允许使用的模型列表>
GM_AGENT_AUTO_CREATE_SCHEMA=1
GM_AGENT_EMBEDDED_WORKERS=1
GM_AGENT_TIMEZONE=Asia/Shanghai
GM_AGENT_TASK_FORMATION_POLICY=auto_authorized
GM_AGENT_FALLBACK_TO_MOCK=0
GM_AGENT_LOG_PAYLOAD_MODE=metadata
GM_EXPLICIT_MENTION_MODEL_FAILURE_POLICY=execute_minimal
```

按模型供应商至少配置一组 Secret：

- DeepSeek：`DEEPSEEK_API_KEY`，可选 `DEEPSEEK_BASE_URL`。
- ModelArts：`MODELARTS_API_KEY`（也接受 `HUAWEI_MODELARTS_API_KEY`）、`MODELARTS_BASE_URL`，可选 `MODELARTS_API_MODE`。
- OpenAI：`OPENAI_API_KEY`。
- 百炼：`DASHSCOPE_API_KEY` 或 `BAILIAN_API_KEY`，可选 `BAILIAN_BASE_URL`。

模型容灾还可配置：

```text
GM_AGENT_MODEL_FAILOVER_ENABLED=1
GM_AGENT_INTENT_FALLBACK_MODELS=<provider:model,provider:model>
GM_AGENT_MODEL_ATTEMPT_TIMEOUT_SECONDS=30
GM_AGENT_MODEL_TOTAL_TIMEOUT_SECONDS=60
GM_AGENT_MODEL_CIRCUIT_FAIL_THRESHOLD=3
GM_AGENT_MODEL_CIRCUIT_COOLDOWN_SECONDS=30
```

与现有 IM 栈集成时，Semantic Schedule 需要配置：

```text
SEMENTIC_GMAGENT_SERVER=http://127.0.0.1:3030
SEMENTIC_GMAGENT_AUTH_TOKEN=<与 GM_AGENT_ORCHESTRATION_TOKEN 相同>
```

如果 GM Agent 不是 sidecar，应把 `SEMENTIC_GMAGENT_SERVER` 改为对应 Kubernetes Service 地址。Token 必须通过 Secret 注入，不能写进 Deployment YAML、镜像或 ConfigMap。

### 端口与挂载

- `3030/TCP`：GM Agent HTTP API 和 `/healthz`。
- `3040/TCP`：可选任务执行 Dispatcher；未启用时不需要暴露。
- `/var/log/gmagent`：建议挂载持久日志目录；需要动态观测决策日志时设置 `GM_AGENT_DECISION_LOG=/var/log/gmagent/decision.jsonl`。
- `/app/data`：仅使用本地 SQLite 或本地任务图时挂载；分别把 `GM_AGENT_DB_PATH` 和 `GM_TASK_GRAPH_DIR` 指向该卷内路径。
- `GM_TASK_GRAPH_DIR` 指向的目录：只有使用本地任务图文件输出时才需要持久挂载。

生产环境使用 PostgreSQL 时不需要持久化镜像内 SQLite 文件，也不要同时设置 `GM_AGENT_DB_PATH`。如仅用于本地开发并选择 SQLite，则应单独挂载 `GM_AGENT_DB_PATH` 所在目录；SQLite 不适合多副本生产部署。

如启用 S3/OBS 任务图存储，再通过 Secret 注入 `AWS_ACCESS_KEY_ID`、`AWS_SECRET_ACCESS_KEY`，并配置 `GM_TASK_GRAPH_S3_ENDPOINT`、`GM_TASK_GRAPH_S3_REGION`、`GM_TASK_GRAPH_S3_BUCKET`、`GM_TASK_GRAPH_S3_PREFIX`。未启用对象存储时不要配置这些变量。

只有启用独立 Outbox Relay 时才配置 `GM_IM_RELAY_WEBHOOK_URL` 与 Secret `GM_IM_RELAY_BEARER_TOKEN`；只有启用 Task Dispatcher 时才配置 `GM_EXECUTION_CORE_URL`、`GM_TASK_DISPATCHER_API_BASE` 等 `GM_TASK_DISPATCHER_*` 变量。API/Worker/Relay/Dispatcher 必须共用同一个 `GM_AGENT_DATABASE_URL`。

### Secret 示例

以下命令只展示键名；实际值必须由部署平台或受控环境变量提供：

```bash
kubectl create secret generic gmagent-runtime \
  --from-literal=GM_AGENT_ORCHESTRATION_TOKEN="$GM_AGENT_ORCHESTRATION_TOKEN" \
  --from-literal=GM_AGENT_DATABASE_URL="$GM_AGENT_DATABASE_URL" \
  --from-literal=DEEPSEEK_API_KEY="$DEEPSEEK_API_KEY"
```

不要把 Secret 明文、SWR 临时登录密码、GitHub PAT 或模型 API Key 提交到 robot-ci、GM Agent 仓库、构建日志和镜像归档。

## CI 隔离 Gamma E2E

运行流水线时选择「CI 隔离 E2E」，勾选 gamma 测试及 E01/E02/E03（默认全部），可选择是否执行基线对照。此目标不调用 CCE 部署；原有 CCE 环境部署仍是独立选项。

构建成功后捕获镜像 ID、交接已归档 TAR，并校验镜像配置摘要和 TAR 哈希。Pipeline Hub 使用现有单队列、独立 Docker 执行基线与候选；构建槽位在等待 E2E 前释放。测试失败会使 Gamma 节点失败，构建及 SWR 产物仍保留。构建详情和步骤日志中的报告 URL 可直接点击，重启后历史记录保留链接。

服务端依赖现有 Pipeline Hub：控制 API 位于回环 `8792`，报告位于 `8080`；公网不开放内部提交接口。部署前配置：

- `/etc/pr-e2e/artifact-baseline.json`：完整基线的服务名、仓库、40 位源码 SHA、确切镜像 ID；只允许已有映射的服务。
- `/etc/pr-e2e/secrets/worker-token`：内部控制凭据，仅服务端读取。
- `/var/lib/pr-e2e/artifact-inbox/<build-id>`：独立镜像交接目录，运行用户 `pr-e2e` 可读。
- `GAMMA_E2E_BASELINE_FILE`、`GAMMA_E2E_TOKEN_FILE`、`GAMMA_E2E_ARCHIVE_ROOT` 可覆盖对应路径，默认归档根为 `/usr/share/nginx/html/images`。

同一构建 ID 重复提交复用记录，输入不同则拒绝。停止构建等待不会取消已入队的 Hub 任务；控制服务中断时保留链接且不判通过。镜像 inbox 暂需按已结束且已归档任务人工清理，不能做全局 Docker 清理。

该模式验证构建产物及选定链路，不表示某个 PR 可独立合入；代码检视、GitHub 回写和 NewLink 通知默认不参与。Multica Server 与测试 Daemon 应来自同一提交，推荐直接提取 Server 镜像内的 Daemon，并记录二进制哈希。

链接渲染仅识别 HTTP/HTTPS，通过 DOM 文本节点创建，不执行日志中的 HTML；新窗口使用 `noopener noreferrer`。测试入口：`python -m unittest discover -s tests -p test_gamma_bridge.py`；安装 Playwright 后可运行 `node tests/test_log_links.cjs`（使用本机 Chrome）。
