# Daily Mode 使用说明

`daily_engine.py` 在历史补采核心业务动作之上增加了浏览器重启、平台登录态重建预检、动态 Worker 页面、共享任务池和单任务即时重试。

## 部署文件

将以下文件放在同一目录：

- `daily_engine.exe`
- `.env`
- `COOKIE` 目录或 `.env` 中指定的其他 Cookie 目录

程序核心逻辑保存在 EXE 中。推荐使用 Dailyfill Launcher 创建和更新客户实例；也可以直接编辑 `.env` 调整客户、任务卡片 ID、日期或并发量，无需重新打包。

## 创建配置

复制 `.env.example` 并重命名为 `.env`。真实 `.env` 已被 Git 忽略，不会上传仓库。

Daily-mode 使用以下字段：

| 字段 | 含义 |
| --- | --- |
| `BITE_ID` | 比特浏览器 ID |
| `GC_PAGE_URL_MARKERS` | 需要由业务页面 GC 监控的 URL 片段数组 |
| `WORKER_COUNT` | Worker 页面数量上限；实际数量不会超过任务数 |
| `MAX_ATTEMPTS` | 每个单日任务最多执行次数，包含首次执行 |
| `KEEP_BROWSER_AFTER_RUN` | 全部任务成功时是否保留比特浏览器；默认 `true`。失败或未进入有效任务阶段时始终保留现场 |
| `TARGET_DATE_OFFSET_DAYS` | 旧配置和未填写单任务偏移时使用的默认日期偏移 |
| `TARGET_DATE` | 可选的统一指定日期；留空时使用日期偏移 |
| `COOKIE_DIR` | pkl Cookie 文件目录 |
| `TASK_URL` | datatoolcenter 工作台地址；省略时使用源码默认值 |
| `DAILY_TASKS` | 每日任务卡片 JSON 数组；`card_id` 表示数仓任务卡片 ID，单项可用 `target_date_offset_days` 设置独立偏移，或用 `date` 指定日期 |
| `PLATFORMS` | 本客户所有可能触发业务执行页的平台 JSON 数组；程序会按顺序重建每个平台的 pkl Cookie 登录态 |
| `CUSTOMER_NAME` | 仅供 `daily_notify_agent.py` 在飞书中显示客户名称；daily_engine 不读取 |
| `REPORT_READY_TIME` | 由 Dailyfill Launcher 配置统一生成；仅供 `daily_notify_agent.py` 判断该客户从几点起纳入汇总，daily_engine 不读取 |

JSON 字段必须写在一行，使用双引号以及小写的 `true` / `false`。建议将 `.env` 保存为 UTF-8。

任务日期优先级为：单任务 `date`、全局 `TARGET_DATE`、单任务
`target_date_offset_days`、全局 `TARGET_DATE_OFFSET_DAYS`。因此旧 `.env`
可以继续运行，新 Launcher 保存后则会为每个任务写入各自的日期偏移。
飞书通知器按 Launcher 格式还原当天预期任务，因此需要通知汇总的客户应确保每个任务均包含 `card_id` 和 `target_date_offset_days`。

## 运行结果

- 日志追加写入 EXE 同目录的 `daily_run.log`。
- 本次任务结果覆盖写入 `daily_results.jsonl`。
- 本轮流程状态原子覆盖写入 `daily_run_status.json`。它记录 `run_id`、运行日期、开始和更新时间、`running/finished` 阶段、账本是否已重置，以及各平台登录预检结果。
- 账本每次尝试除任务 ID、卡片 ID、日期、次数和成功状态外，还记录卡片标题 `task_name` 与后端最终检测到的 `missing_count`。确认无缺失时为 `0`，确认仍有缺失时为正整数，未获得可信终态检测时为 `null`。
- 结束汇总会记录浏览器启动、登录预检、Worker 初始化、任务池执行和本次总运行时间；流程提前失败时也至少记录已经完成的阶段和总耗时。
- 全部任务成功时，`KEEP_BROWSER_AFTER_RUN=true` 保留浏览器，设为 `false` 则自动关闭；登录失败、初始化失败或存在最终失败任务时始终保留现场供人工检查。
- 登录态重建预检开始时会清理一次浏览器中的旧 Cookie，再按 `PLATFORMS` 逐个平台注入对应 pkl Cookie 并验证。
- `context.clear_cookies()` 只在预检开始时运行一次；不能在每个平台注入前运行，否则后一个平台会清掉前一个平台的 Cookie。
- 登录态重建全部失败时不会创建任务池；失败平台页面会保留供人工登录。
- `daily_run_status.json` 在任务配置校验完成后先以 `ledger_reset=false` 创建，登录预检后写入平台结果，Worker 稳定且账本重置成功后改为 `true`，最外层退出时将阶段改为 `finished`。新进程启动后拥有状态文件，旧进程不能再覆盖它。
- 任务失败且未达到 `MAX_ATTEMPTS` 时，会立即以 `attempt + 1` 放回共享队列尾部，不再等待其他任务全部结束后进行总体重试。
- 健康 Worker 会持续等待队列；所有任务成功或达到各自执行上限后，调度器才统一停止 Worker。
- Backfill 与 Daily 共用的普通业务元素渲染、可见性和点击等待统一为 30 秒；5 秒 trial、页面导航与稳定等待、页面健康探针、120 秒 Worker 心跳和 180 秒 GC 均保持原值。
- Worker 并发监听“同步成功”和“数据补齐完成”。完成信号只表示业务队列遍历结束；脚本等待网页自动检测结果稳定，确认无缺失后才写入 `success=true`，仍有缺失或结果不可信时进入重试。未捕获完成信号且静默超时时执行主动后端复检兜底。
- 每次缺失检测都会先等待一级弹窗内的结果项标题 `div.testContent_list_title_dayType` 渲染，再等待 1 秒读取顶部统计；若统计仍是固定占位文本 `：表示缺失数据`，按 0、2、4 秒退避读取同一轮结果，连续 3 次仍未完成时记为失败。

## 打包

在 Windows 项目目录执行：

```powershell
uv sync
uv run pyinstaller --onefile --noconsole --name daily_engine daily_engine.py
```

不需要提前准备 `.spec`。上述命令会在当前 Windows 构建目录生成 `daily_engine.spec`，并把最终程序写入 `dist\daily_engine.exe`。当前命令已经能够稳定打包时，可以继续让本地或 CI 每次按相同参数生成 spec；只有需要复杂打包配置时再把 spec 纳入版本控制。

`.env` 是部署时的外部文件，不要使用 PyInstaller 打进 EXE。

## Dailyfill Launcher 部署

Launcher 使用与 Backfill Launcher 相同的桌面部署布局：

```text
Desktop\
├─ dailyfill_launcher.exe
└─ dailyfill\
   ├─ _release\
   │  ├─ daily_engine.exe
   │  └─ dailyfill_launcher_config.json
   ├─ JD\
   │  └─ 客户名\
   │     ├─ daily_engine.exe
   │     └─ .env
   └─ 新建客户\
      ├─ daily_engine.exe
      └─ .env
```

`dailyfill_launcher.exe` 固定读取同目录下
`dailyfill\_release\dailyfill_launcher_config.json`。Launcher 会递归扫描
`dailyfill` 下所有客户 `.env`，因此客户可以按 JD、SYCM、DY_JD
等平台目录分类；下拉框仅显示 `.env` 所在的客户目录名。新建客户会先
保存到 `dailyfill\客户名`，之后可人工移入平台分类目录。

保存时，Launcher 会从 `_release` 复制最新版 `daily_engine.exe`。
`defaults.task_url` 以及客户或默认的 `report_ready_time` 是 Launcher
权威值：加载旧 `.env` 时不会用其中的旧值覆盖，再次保存即会回写最新值。
`_release` 只保存通用发布文件，不存放客户实例。
