# BitBrowser Backfill Engine

基于 Playwright 与 BitBrowser CDP 的数仓采集 RPA。项目围绕同一套稳定的 Worker、任务账本、异常隔离和页面回收能力，提供两种运行模式。

| 模式 | 入口 | 用途 |
| --- | --- | --- |
| 历史补采 | `backfill_engine.py` | 将日期范围切分为区块，由多个既有 Worker 通过共享任务池补采历史数据。 |
| 日常采集 | `daily_engine.py` | 重启指定 Bit 浏览器、按 pkl Cookie 重建业务平台登录态、创建 Worker，并让失败的单日任务立即回队重试。 |

`daily_notify_agent.py` 是日常采集的旁路巡检器：它读取各客户目录中的 `.env`、`daily_results.jsonl` 和 `daily_run.log`，定时发送一条本机汇总飞书消息，不参与任何浏览器操作。

## 文档入口

- [历史补采架构与执行流程](ARCHITECTURE.md)
- [日常采集部署与配置说明](DAILY_MODE.md)
- [飞书巡检通知器说明](DAILY_NOTIFY_AGENT.md)
- [历史补采项目复盘](project_retrospective.md)
- [弹窗层级与遮挡问题复盘](popup_layering_fix_retrospective.md)

## 目录结构

```text
backfill-daily-mode/
  backfill_engine.py       # 历史补采核心与通用 Worker 能力
  daily_engine.py          # 日常采集入口
  auth_manager.py          # pkl Cookie 登录态重建预检
  browser_manager.py       # Bit 浏览器启动、关闭与 CDP 地址获取
  task_ledger.py           # JSONL 任务账本与重试结果汇总
  daily_notify_agent.py    # 本机飞书巡检通知器
  .env.example             # 历史补采 / 日常采集配置模板
  notify_agent.env.example # 飞书通知器配置模板
```

真实 `.env`、Cookie、日志、JSONL 账本、PyInstaller 产物均不会提交到仓库。

## 开发与打包

```powershell
uv sync

# 历史补采
uv run pyinstaller --onefile --name backfill_engine backfill_engine.py

# 日常采集
uv run pyinstaller --onefile --noconsole --name daily_engine daily_engine.py

# 飞书巡检通知器
uv run pyinstaller --onefile --noconsole --name daily_notify_agent daily_notify_agent.py
```

当前项目使用命令行参数打包，不要求仓库预先存在 `.spec`。PyInstaller 首次执行上述命令时会在构建目录生成同名 `.spec`、`build/` 和 `dist/`；真正需要部署的是 `dist/` 中的 EXE。只有后续需要固定图标、版本资源、额外数据文件或隐藏导入时，才有必要把整理后的 `.spec` 提交到仓库并改用 `pyinstaller xxx.spec`。

部署时将对应 EXE 与其配置文件放在同一目录：`daily_engine.exe` 使用 `.env`，`daily_notify_agent.exe` 使用 `notify_agent.env`。
