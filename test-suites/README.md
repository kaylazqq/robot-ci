# 服务测试套件目录

本目录是 SWR Push Helper 的统一测试索引。测试代码仍保留在各微服务仓库中，和被测业务代码同一个提交版本演进；这里不复制测试文件，避免工具仓库的测试与服务代码脱节。

`catalog.json` 是唯一的服务测试目录：它记录服务 ID、业务范围、服务内测试根目录、第一阶段禁止的真实依赖，以及新增 UT/DT 的放置约定。`test-plans.json` 中的每个 profile 都对应一个 catalog 条目。

## 新增测试的约定

1. 只写不连接真实 PostgreSQL、Redis、Kafka、Temporal、Mattermost、SWR 或 CCE 的 UT/DT；使用 fake、mock、临时目录或本地 HTTP server。
2. Python 服务：优先放到服务仓库的 `tests/unit/` 或 `tests/dt/`；历史根目录 `tests/test_*.py` 会继续被执行。
3. Go 服务：纯逻辑测试使用普通 `*_test.go`；需要真实服务的测试必须加 `//go:build integration`，不能进入默认计划。
4. Shell 服务：放到 `tests/unit/*.sh` 或 `tests/dt/*.sh`，脚本应 `set -euo pipefail` 且不启动 Docker。
5. 增加用例后不需要修改服务器代码；只要在 catalog 声明的测试根目录/包内，下一次部署会自动执行。若新增测试根目录或语言，再同步更新 `catalog.json` 和 `test-plans.json`。

运行结果仅统计实际执行的用例；被 catalog 排除的集成测试不会被执行，也不会在页面显示为 skipped。
