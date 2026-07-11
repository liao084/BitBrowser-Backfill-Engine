# Daily Mode 使用说明

`daily_engine.py` 在历史补采核心业务动作之上增加了浏览器重启、平台登录态重建预检、动态 Worker 页面、共享任务池和多轮总体重试。

## 部署文件

将以下文件放在同一目录：

- `daily_engine.exe`
- `.env`
- `COOKIE` 目录或 `.env` 中指定的其他 Cookie 目录

程序核心逻辑保存在 EXE 中。更换客户、卡片、日期或并发量时，只需使用 Notepad++ 修改 `.env`，不需要重新打包。

## 创建配置

复制 `.env.example` 并重命名为 `.env`。真实 `.env` 已被 Git 忽略，不会上传仓库。

Daily-mode 使用以下字段：

| 字段 | 含义 |
| --- | --- |
| `BITE_ID` | 比特浏览器 ID |
| `GC_PAGE_URL_MARKERS` | 需要由业务页面 GC 监控的 URL 片段数组 |
| `WORKER_COUNT` | Worker 页面数量上限；实际数量不会超过任务数 |
| `MAX_ATTEMPTS` | 每个失败任务最多执行的总轮数，包含首次执行 |
| `TARGET_DATE_OFFSET_DAYS` | 默认目标日期相对今天向前偏移的天数 |
| `TARGET_DATE` | 可选的统一指定日期；留空时使用日期偏移 |
| `COOKIE_DIR` | pkl Cookie 文件目录 |
| `TASK_URL` | datatoolcenter 工作台地址；省略时使用源码默认值 |
| `DAILY_TASKS` | 每日任务卡片 JSON 数组；单项可用 `date` 覆盖统一日期 |
| `PLATFORMS` | 本客户所有可能触发业务执行页的平台 JSON 数组；程序会按顺序重建每个平台的 pkl Cookie 登录态 |
| `CUSTOMER_NAME` | 仅供 `daily_notify_agent.py` 在飞书中显示客户名称；daily_engine 不读取 |
| `REPORT_READY_TIME` | 仅供 `daily_notify_agent.py` 判断该客户从几点起纳入汇总；daily_engine 不读取 |

JSON 字段必须写在一行，使用双引号以及小写的 `true` / `false`。建议将 `.env` 保存为 UTF-8。

## 运行结果

- 日志追加写入 EXE 同目录的 `daily_run.log`。
- 本次任务结果覆盖写入 `daily_results.jsonl`。
- 正常完成、降级完成或失败结束后均保留比特浏览器，方便人工巡检和临时操作。
- 登录态重建预检开始时会清理一次浏览器中的旧 Cookie，再按 `PLATFORMS` 逐个平台注入对应 pkl Cookie 并验证。
- `context.clear_cookies()` 只在预检开始时运行一次；不能在每个平台注入前运行，否则后一个平台会清掉前一个平台的 Cookie。
- 登录态重建全部失败时不会创建任务池；失败平台页面会保留供人工登录。

## 打包

在 Windows 项目目录执行：

```powershell
uv sync
uv run pyinstaller --onefile --noconsole --name daily_engine daily_engine.py
```

`.env` 是部署时的外部文件，不要使用 PyInstaller 打进 EXE。
