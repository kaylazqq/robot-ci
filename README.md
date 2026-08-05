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

批量构建时，每个所选微服务都按“拉取代码 → 执行该服务 UT/DT → 构建 → 推送 → 归档”的顺序独立执行。页面按微服务显示独立的测试结果卡片和分页表格，单个服务测试失败不会阻止该服务或后续服务继续构建。

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

每次构建开始时会先删除该服务旧的 workspace 再重新 `git clone`（避免历史产物堆积）。构建结束后保留本次 checkout，方便失败时上机排查；下次构建同一服务时再删掉重来。磁盘紧张时会自动清理 `/usr/share/nginx/html/images` 下较旧的时间戳归档目录（默认保留最近 3 个）。

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
