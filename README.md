# SWR 一键推送

## 发给别人（本机 Windows）

只要这些文件，或发 `dist\swr-push-helper-*.zip`：

```
start.bat  stop.bat  server.py  services.json  test-plans.json  test-suites/  test_runner.py  unittest_case_runner.py  shell_case_runner.py  web/  README.md  deploy-linux.sh  config.example.json
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

4. 浏览器打开 `http://服务器IP:18888/`
5. 别人只需粘贴**自己的 SWR 临时登录指令**；拉私有 GitHub 代码用服务器上的 Token，无需再登 GitHub

## 第一阶段测试

工具会在拉取服务代码后执行 `test-plans.json` 中受控的 UT/DT 测试，并在任务日志和页面显示总用例、通过、失败和错误数及失败用例摘要。第一阶段测试不阻断镜像构建或 SWR 推送；数据库、Redis、Temporal 等外部依赖测试不在测试计划中执行。

批量构建时，每个所选微服务都按“拉取代码 → 执行该服务 UT/DT → 构建 → 推送 → 归档”的顺序独立执行。页面按微服务显示独立的测试结果卡片和分页表格，单个服务测试失败不会阻止该服务或后续服务继续构建。SWR 登录检测会向仓库发起真实 manifest 探测；仅明确确认登录有效时才开始需要推送的任务，鉴权失败后立即停止后续 SWR 操作。

`multica-server` 构建前必须在页面填写 Daemon 版本，格式为 `vMAJOR.MINOR.PATCH`，例如 `v1.2.3`。前后端使用同一规则校验；工具不修改微服务配置文件，而是通过 `DAEMON_RELEASE_VERSION` 调用仓库的 `./deploy.sh build-cce`，由微服务构建脚本把对应版本写入镜像内的 release manifest。最近一次成功提交构建的合法版本会保存在服务器 `logs/last-daemon-version.json` 并对所有浏览器回填；浏览器本地值只作为服务器状态不可用时的兜底。

`multica-fleet` 来自独立仓库 `censong574-spec/multica-fleet`，属于仅归档服务，不登录或推送 SWR。工具调用仓库的 `./deploy.sh pack`，在同一归档目录生成包含 Fleet、OpenCode、Hermes 三个镜像的单个 tar，以及 `multica-fleet.env` 和已固化本次 tag 的 `deploy-multica-fleet.sh`。

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

服务器同一时间只允许一个构建任务。锁定状态由后端运行中任务决定，所有浏览器共享；第二个构建请求会收到 `409`，页面随后自动附着并回显当前任务。空闲页面也会定时发现其他人新启动的任务并锁定构建按钮。

页面按游标增量读取运行中日志，避免大日志重复传输和慢请求乱序覆盖。浏览器只在本地保存 10 分钟“最近查看的 job ID”，用于规避刷新时任务恰好完成造成的日志和测试表格回显丢失；加载历史结果不会延长有效期。已完成的 job ID 不占用锁，后端确认空闲后可立即构建下一个任务。

每次构建开始时会先删除该服务旧的 workspace 再重新 `git clone`（避免历史产物堆积）。构建结束后保留本次 checkout，方便失败时上机排查；下次构建同一服务时再删掉重来。磁盘紧张时会自动清理 `/usr/share/nginx/html/images` 下较旧的时间戳归档目录（默认保留最近 3 个）。

`multica-fleet` 是例外的缓存优化场景：工具仍会重新克隆源码，但会把 Fleet 生成的 `.build-source.sha256` 和 `.build-image-ids` 保存到 `<workspace_root>/.robot-ci-cache/multica-fleet/runtime-images/`，并在新 checkout 中恢复。Fleet 构建脚本只有在新源码指纹一致、两个 Runtime Docker 镜像仍存在且 Image ID 一致时才跳过 OpenCode/Hermes 重建；源码、Dockerfile、依赖或本地镜像发生变化时会自动重新构建。三镜像合并归档仍会在每次任务中重新生成，因此不会复用或误发旧归档。工具会把最终 tar 权限统一设为 `0644` 供 nginx 下载，删除仅供仓库自身上传流程使用的 `.meta`；保留 `.sha256`，供离线部署脚本校验大文件下载完整性。

可通过 nginx 直接下载，例如：`http://服务器IP/images/YYYYMMDDHHMMSS/<image>_<tag>.tar`

`config.json` 相关项：

```json
{
  "archive_enabled": true,
  "archive_required": true,
  "archive_root": "/usr/share/nginx/html/images"
}
```

## 安全提醒

- 不要把账号密码发到聊天或写进仓库
- 服务器上的 `config.json` 含 Token，权限应为 `600`
- 聊天里暴露过的密码请立刻修改
