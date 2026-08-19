# Dailyfill 飞书巡检通知器

`daily_notify_agent.py` 是 Dailyfill 的旁路巡检工具。它不参与采集或浏览器控制，只负责定时扫描**本机**客户目录，读取 `.env`、`daily_run_status.json`、`daily_results.jsonl`、`daily_run.log`，然后发送一条可展开客户详情的飞书汇总卡片。

## 目录约定

推荐把通知器放在 `dailyfill` 根目录：

```text
dailyfill/
  daily_notify_agent.exe
  notify_agent.env

  jd/
    235_客户1/
      daily_engine.exe
      .env
      daily_run_status.json
      daily_results.jsonl
      daily_run.log

  jd_douyin/
    218_客户2/
      daily_engine.exe
      .env
      daily_run_status.json
      daily_results.jsonl
      daily_run.log
```

通知器会递归扫描 `dailyfill` 下所有名为 `.env` 的文件。每个 `.env` 所在目录就是一个客户任务目录。
通知器会用 `DAILY_TASKS` 中每条任务的 `card_id` 和
`target_date_offset_days` 还原当天应该出现的 `task_id`；这两个字段
缺失或无效时，该客户会显示为“配置异常”。

## 客户 .env 需要增加的字段

```env
CUSTOMER_NAME=235_客户1
REPORT_READY_TIME=08:45
```

- `CUSTOMER_NAME`：飞书消息里显示的客户名；不填时使用文件夹名。
- `REPORT_READY_TIME`：到这个时间后，该客户才纳入飞书汇总。

不再需要 `REPORT_PLATFORMS`。平台分类由部署目录维护，通知器只依据 `REPORT_READY_TIME` 判断客户何时纳入汇总。

## notify_agent.env

复制模板：

```powershell
copy notify_agent.env.example notify_agent.env
```

核心配置：

```env
CLIENTS_ROOT=
FEISHU_WEBHOOK_URL=
NOTIFY_TITLE=Daily RPA 巡检｜节点A
NOTIFY_START_TIME=09:00
NOTIFY_END_TIME=18:00
NOTIFY_INTERVAL_MINUTES=30
STALE_LOG_MINUTES=20
```

`CLIENTS_ROOT` 留空时，默认读取 `notify_agent.env` 所在目录。也就是说，如果通知器 exe 和 `notify_agent.env` 都放在 `dailyfill` 根目录，`CLIENTS_ROOT` 可以不用填。

`NOTIFY_TITLE` 可以直接带机器名，例如：

```env
NOTIFY_TITLE=Daily RPA 巡检｜节点A
```

`DAILY_STATUS_FILENAME`、`DAILY_RESULTS_FILENAME` 和 `DAILY_LOG_FILENAME` 通常保持默认值即可；它们必须与 `daily_engine.py` 的产物文件名一致。

## 状态判定

- 今天的 `daily_run_status.json` 尚未把 `ledger_reset` 标为 `true` 时，不读取旧 JSONL，避免上一轮结果污染本轮通知。
- 状态文件的 `run_date` 不是今天时，客户显示为“未开始”，同样忽略旧 JSONL。
- `auth_results` 中失败的平台会直接显示在客户详情中；全部平台失败时显示“登录异常”。
- 任意客户存在登录失效平台时，汇总下方会额外显示登录告警并 `@所有人`；登录正常时不生成该告警。登录失效持续期间，每轮巡检都会继续提醒。
- 没有状态文件的旧版 DailyEngine 仍按原方式读取 JSONL，便于渐进升级。
- JSONL 是任务完成情况的真值；log 最后修改时间只用于补充“疑似故障”提示。
- 任务最新结果包含非空 `detail_missing_categories` 时，该任务显示为默认收起的二级折叠面板，点击后逐行展示全部缺失类目；`null` 和空列表继续显示为普通任务行。

## 消息格式

示例：

```text
【Daily RPA 巡检｜节点A】2026-07-09 09:30

汇总：完成 3｜运行中 2｜未开始 1｜需关注 1

🚨 登录异常，请及时处理：@所有人

✅ 235_客户1｜完成｜4/4
⏳ 218_客户2｜运行中｜1/3
⚪ 252_客户3｜未开始｜0/2｜未发现今日账本记录
⚠️ 233_客户4｜运行中｜2/5｜log 25 分钟未更新，疑似故障
```

点击客户行可展开平台登录异常、任务名称、卡片 ID、目标日期、尝试次数以及可信的剩余缺失条数。存在具体缺失类目的任务会显示第二级下拉框，点击任务行后才展开类目列表。

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
