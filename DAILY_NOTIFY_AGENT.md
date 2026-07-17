# daily-mode 飞书巡检通知器

`daily_notify_agent.py` 是 daily-mode 的旁路巡检工具。它不参与采集或浏览器控制，只负责定时扫描**本机**客户目录，读取 `.env`、`daily_results.jsonl`、`daily_run.log`，然后发送一条飞书汇总消息。

## 目录约定

推荐把通知器放在 `dailyfill` 根目录：

```text
dailyfill/
  daily_notify_agent.exe
  notify_agent.env

  jd/
    235-极米_1/
      daily_engine.exe
      .env
      daily_results.jsonl
      daily_run.log

  jd_douyin/
    154-安克_1/
      daily_engine.exe
      .env
      daily_results.jsonl
      daily_run.log
```

通知器会递归扫描 `dailyfill` 下所有名为 `.env` 的文件。每个 `.env` 所在目录就是一个客户任务目录。

## 客户 .env 需要增加的字段

```env
CUSTOMER_NAME=235_极米
REPORT_READY_TIME=08:45
```

- `CUSTOMER_NAME`：飞书消息里显示的客户名；不填时使用文件夹名。
- `REPORT_READY_TIME`：到这个时间后，该客户才纳入飞书汇总。

不再需要 `REPORT_PLATFORMS`。客户采集什么平台由目录或你自己的命名管理，通知器只关心“几点纳入汇报”。

## notify_agent.env

复制模板：

```powershell
copy notify_agent.env.example notify_agent.env
```

核心配置：

```env
CLIENTS_ROOT=
FEISHU_WEBHOOK_URL=
NOTIFY_TITLE=Daily RPA 巡检｜6号机
NOTIFY_START_TIME=09:00
NOTIFY_END_TIME=18:00
NOTIFY_INTERVAL_MINUTES=30
STALE_LOG_MINUTES=20
```

`CLIENTS_ROOT` 留空时，默认读取 `notify_agent.env` 所在目录。也就是说，如果通知器 exe 和 `notify_agent.env` 都放在 `dailyfill` 根目录，`CLIENTS_ROOT` 可以不用填。

`NOTIFY_TITLE` 可以直接带机器名，例如：

```env
NOTIFY_TITLE=Daily RPA 巡检｜6号机
```

`DAILY_RESULTS_FILENAME` 和 `DAILY_LOG_FILENAME` 通常保持默认值即可；它们必须与 `daily_engine.py` 的产物文件名一致。

## 消息格式

示例：

```text
【Daily RPA 巡检｜6号机】2026-07-09 09:30

汇总：完成 3｜运行中 2｜未开始 1｜需关注 1

✅ 154_安克｜完成｜4/4
⏳ 235_极米｜运行中｜1/3
⚪ 259_伊利｜未开始｜0/2｜未发现今日账本记录
⚠️ 233_西门子｜运行中｜2/5｜log 25 分钟未更新，疑似故障
```

## 手动发送一次

会真实发送飞书：

```powershell
daily_notify_agent.exe --once
```

源码运行：

```powershell
uv run python daily_notify_agent.py --once
```

## 常驻运行

```powershell
daily_notify_agent.exe
```

默认会在配置的时间窗口内按固定间隔发送通知。例如 `09:00` 到 `18:00`，每 30 分钟一次。

## 当前边界与后续扩展

当前版本只汇总一台服务器的本地 `dailyfill` 目录，因此多台服务器会各自发送一条消息。

未来如需全机统一汇总，推荐保留本机扫描逻辑，让每台服务器只向共享目录写入一份状态 JSON，再由一台中心通知器读取这些 JSON 并发送唯一的飞书汇总。这样不需要让中心机器直接读取所有服务器的日志和客户目录。

## 打包

```powershell
uv run pyinstaller --onefile --noconsole --name daily_notify_agent daily_notify_agent.py
```
