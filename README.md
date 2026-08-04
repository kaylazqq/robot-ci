# SWR 一键推送

## 发给别人（本机 Windows）

只要这些文件，或发 `dist\swr-push-helper-*.zip`：

```
start.bat  stop.bat  server.py  services.json  web/  README.md
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

## 对方电脑 / 使用方需要

- 能访问该网页
- 华为云 SWR 临时登录指令 + `public_ai` 推送权限

## 安全提醒

- 不要把账号密码发到聊天或写进仓库
- 服务器上的 `config.json` 含 Token，权限应为 `600`
- 聊天里暴露过的密码请立刻修改
