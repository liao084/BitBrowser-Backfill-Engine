# Backfill & Daily Collection Engine

基于 Playwright CDP 的数仓采集 RPA。项目围绕同一套稳定的 Worker、任务账本、异常隔离和页面回收能力，历史补采可连接 BitBrowser，也可接管已开启远程调试的 Edge、Chrome 等 Chromium 浏览器。

| 模式 | 入口 | 用途 |
| --- | --- | --- |
| 历史补采 | `backfill_engine.py` | 通过 `.env` 选择 BitBrowser 或外部 Chromium CDP，将日期范围切分为区块并由多个既有 Worker 动态补采。 |
| 日常采集 | `daily_engine.py` | 重启指定 Bit 浏览器、按平台配置执行登录预检、创建 Worker，并让失败的单日任务立即回队重试。 |

`daily_notify_agent.py` 是日常采集的旁路巡检器：它读取各客户目录中的 `.env`、`daily_run_status.json`、`daily_results.jsonl` 和 `daily_run.log`，定时发送一条可展开客户详情的本机飞书汇总卡片，不参与任何浏览器操作。

## 文档入口

- [历史补采架构与执行流程](ARCHITECTURE.md)
- [日常采集部署与配置说明](DAILY_MODE.md)
- [飞书巡检通知器说明](DAILY_NOTIFY_AGENT.md)

## 目录结构

```text
backfill/
  backfill_engine.py       # 历史补采入口与通用 Worker 能力
  backfill_launcher.py     # Backfill 客户配置和实例启动 GUI
  browser_connector.py     # BitBrowser / 外部 Chromium CDP 连接器
  daily_engine.py          # 日常采集入口
  dailyfill_launcher.py    # Dailyfill 客户配置和实例启动 GUI
  auth_manager.py          # 登录预检编排与 AuthReport 汇总
  login_flows.py           # pkl Cookie、1688 等具体登录流程
  browser_manager.py       # Bit 浏览器启动、关闭与 CDP 地址获取
  task_ledger.py           # JSONL 任务账本与重试结果汇总
  daily_run_status.py      # Daily 单次运行状态与跨进程归属保护
  daily_notify_agent.py    # 本机飞书巡检通知器
  .env.example             # 历史补采 / 日常采集配置模板
  backfill_launcher_config.example.json # Backfill Launcher 配置模板
  dailyfill_launcher_config.example.json # Dailyfill Launcher 配置模板
  notify_agent.env.example # 飞书通知器配置模板
```

真实 `.env`、Cookie、日志、JSONL 账本、Daily 运行状态和 PyInstaller 产物均不会提交到仓库。

## 开发与打包

```powershell
uv sync

# 历史补采
uv run pyinstaller --onefile --name backfill_engine backfill_engine.py

# 日常采集
uv run pyinstaller --onefile --noconsole --name daily_engine daily_engine.py

# Backfill 客户实例管理器
uv run pyinstaller --onefile --noconsole --name backfill_launcher backfill_launcher.py

# Dailyfill 客户实例管理器
uv run pyinstaller --onefile --noconsole --name dailyfill_launcher dailyfill_launcher.py

# 飞书巡检通知器
uv run pyinstaller --onefile --name daily_notify_agent daily_notify_agent.py
```

部署时将对应 EXE 与其配置文件放在同一目录：`backfill_engine.exe` 和 `daily_engine.exe` 使用 `.env`，`daily_notify_agent.exe` 使用 `notify_agent.env`。

CI 生成的两个 Launcher 部署包使用相同结构：Launcher 位于部署包根目录，对应的 Engine 与 Launcher 配置位于 `backfill\_release` 或 `dailyfill\_release`。`_release` 中只保存通用发布文件，客户实例由 Launcher 在对应主目录下创建。

历史补采的浏览器连接方式由 `.env` 决定：

- `BROWSER_TYPE=bitbrowser`：读取 `BITE_ID`，通过比特浏览器本地 API 获取 CDP 地址；
- `BROWSER_TYPE=external_cdp`：读取 `CDP_ADDRESS`，连接已经通过 `--remote-debugging-port` 开启远程调试的 Edge、Chrome 等 Chromium 浏览器。

两种方式都要求浏览器中已经准备好并登录 `datatoolcenter` Worker 页面。历史模式还可通过 `WORKER_HEARTBEAT_SILENCE_SECONDS` 和 `BUSINESS_HEARTBEAT_SILENCE_SECONDS` 调整不同业务速度下的静默阈值；后者必须大于前者。
