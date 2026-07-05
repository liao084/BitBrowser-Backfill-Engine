# BitBrowser Backfill Engine 架构与执行流程

本文档基于 2026-07-05 工作区中的 `backfill_engine.py` 与 `task_ledger.py`，用于：

- 按调用顺序通读代码；
- 在遗忘实现细节后快速恢复对脚本的理解；
- 回顾共享任务池、心跳、GC、账本和 Worker 熔断的设计关系；
- 解释每个模块在完整执行链路中的职责。

> Mermaid 是“图即代码”。GitHub、Obsidian、Typora 和 Notion 均可渲染本文中的主要图表。

## 一、先用一张思维导图认识系统

```mermaid
mindmap
  root((Backfill Engine))
    输入与连接
      .env 运行配置
        BITE_ID
        GC_PAGE_URL_MARKERS
      TASKS_CONFIG
        任务卡片
        起止日期
        日期区块大小
      比特浏览器 API
      Playwright CDP
    调度核心
      日期切分
      共享任务池
      多 Worker 动态领取
      健康 Worker 隔离
    单任务业务流
      清理旧弹窗
      打开任务卡片
      注入日期
      启动检测
      判断缺失数据
      全店补齐
      等待心跳
    后台守护
      Worker 红色提示回收
      Context 业务执行页 GC
      页面健康探测
    可靠性
      JSONL 任务账本
      首轮失败统一重试
      连续初始化失败熔断
      页面崩溃与断连隔离
    输出
      backfill_run.log
      backfill_results.jsonl
      最终成功失败汇总
```

## 二、总体架构

```mermaid
flowchart LR
    Config["本地 .env<br/>BITE_ID / TASKS_CONFIG / GC_PAGE_URL_MARKERS"]
    BitAPI["比特浏览器本地 API<br/>/browser/open"]

    subgraph Engine["BackfillEngine 总控进程"]
        Builder["任务构建器<br/>generate_date_chunks + build_tasks"]
        Pool["共享 asyncio.Queue"]
        Scheduler["轮次调度器<br/>_run_task_round"]
        Ledger["TaskLedger<br/>JSONL + asyncio.Lock"]
        Summary["最终结果汇总"]
    end

    subgraph Context["BrowserContext"]
        W1["datatoolcenter Worker 1"]
        W2["datatoolcenter Worker 2"]
        WN["datatoolcenter Worker N"]
        ToastGuard["每个 Worker 的<br/>红色提示事件监控器"]
        PageEvent["context.on page"]
    end

    subgraph Business["实际业务执行页面"]
        JD1["京东商智页面 A"]
        JDN["京东商智页面 N"]
        GC["Context 级业务执行页 GC<br/>180 秒静默回收 + 退出收尾"]
    end

    Log["backfill_run.log"]
    Result["backfill_results.jsonl"]

    Config --> Builder --> Scheduler
    BitAPI --> Context
    Scheduler --> Pool
    Pool --> W1
    Pool --> W2
    Pool --> WN
    W1 --> Ledger
    W2 --> Ledger
    WN --> Ledger
    Ledger --> Result
    Ledger --> Summary
    ToastGuard -.监控.-> W1
    ToastGuard -.监控.-> W2
    ToastGuard -.监控.-> WN
    W1 --> JD1
    WN --> JDN
    PageEvent --> GC
    GC -.监控并回收.-> JD1
    GC -.监控并回收.-> JDN
    Engine --> Log
    Context --> Log
    Business --> Log
```

架构中存在三条互相解耦的执行线：

1. **主业务线**：任务池 → Worker → 数仓弹窗 → 商智采集；
2. **页面资源线**：Context 捕获商智页面 → 心跳监控 → 僵尸页面回收；
3. **可观测与恢复线**：日志 + JSONL 账本 → 失败任务重建 → 第二轮重试。

## 三、推荐的代码阅读顺序

```mermaid
flowchart TD
    A["1. load_runtime_config + __main__<br/>读取 .env"] --> B["2. BackfillEngine.run<br/>掌握总控流程"]
    B --> C["3. build_tasks<br/>generate_date_chunks"]
    C --> D["4. _run_task_round<br/>建立共享队列"]
    D --> E["5. worker<br/>循环领取任务与熔断"]
    E --> F["6. execute_task<br/>阅读单任务业务主流程"]
    F --> G["7. wait_for_completion_or_heartbeat<br/>理解完成与卡死判断"]
    G --> H["8. _monitor_and_gc_page<br/>理解商智页面旁路 GC"]
    H --> I["9. _monitor_worker_error_toasts<br/>理解红色提示回收"]
    I --> J["10. TaskLedger<br/>理解重试与最终汇总"]
```

| 阅读层级 | 核心函数 | 需要回答的问题 |
|---|---|---|
| 总控 | `run()` | 浏览器、Context、Worker、守护协程和两轮执行如何组装？ |
| 调度 | `_run_task_round()` | 如何建立共享队列，如何筛选健康 Worker？ |
| Worker | `worker()` | 一个页面如何持续领取任务，何时熔断？ |
| 业务 | `execute_task()` | 一个日期区块如何完成检测与补齐？ |
| 状态判断 | `wait_for_completion_or_heartbeat()` | 120 秒静默后如何区分完成和卡死？ |
| 页面 GC | `_monitor_and_gc_page()` | 商智页面为什么独立于 Worker，何时关闭？ |
| UI 守护 | `_monitor_worker_error_toasts()` | 红色提示如何事件驱动回收并避免重复处理？ |
| 持久化 | `TaskLedger` | 首轮失败项如何变成第二轮任务？ |

## 四、程序启动与总控时序

```mermaid
sequenceDiagram
    autonumber
    participant Main as __main__
    participant Engine as BackfillEngine.run
    participant Bit as 比特浏览器 API
    participant PW as Playwright
    participant Ctx as BrowserContext
    participant Ledger as TaskLedger
    participant Workers as Worker 页面组

    Main->>Main: 从 .env 解析 BITE_ID、TASKS_CONFIG 和 GC URL
    Main->>Engine: asyncio.run(run(tasks_config))
    Engine->>Bit: POST /browser/open
    Bit-->>Engine: CDP 调试地址
    Engine->>PW: connect_over_cdp
    PW-->>Engine: Browser + Context
    Engine->>Ctx: context.on("page", _on_new_page)
    Engine->>Ctx: 扫描已有页面并部署延迟 URL 检查
    Engine->>Engine: 过滤 datatoolcenter 页面作为 Worker
    Engine->>Engine: 校验 tasks_config
    Engine->>Engine: 切分日期并生成唯一任务
    Engine->>Ledger: reset()
    Engine->>Workers: 为每个 Worker 启动红色提示监控器
    Engine->>Workers: 第一轮共享任务池执行
    Workers->>Ledger: 每个任务 append 一条结果
    Engine->>Ledger: failed_tasks(attempt=1)
    alt 存在失败任务且仍有健康 Worker
        Engine->>Workers: 第二轮统一重试
        Workers->>Ledger: 写入 attempt=2 结果
    else 没有失败任务
        Engine->>Engine: 跳过第二轮
    end
    Engine->>Ledger: summary(total_tasks)
    Ledger-->>Engine: 首轮、重试、最终统计
    Engine->>Workers: cancel 红色提示监控与延迟关闭任务
```

## 五、配置如何变成共享任务池

假设 `.env` 中的 `TASKS_CONFIG` 包含：

```dotenv
TASKS_CONFIG=[{"card":3,"start":"2025-07-01","end":"2025-07-07","chunk_days":3}]
```

会生成：

```text
card-3_2025-07-01_2025-07-03
card-3_2025-07-04_2025-07-06
card-3_2025-07-07_2025-07-07
```

```mermaid
flowchart TD
    A["读取一条 tasks_config"] --> B["解析 card / start / end / chunk_days"]
    B --> C["current_date = start"]
    C --> D{"current_date <= end?"}
    D -->|"否"| J["该配置切分结束"]
    D -->|"是"| E["chunk_end = current_date + chunk_days - 1"]
    E --> F{"chunk_end 超过 end?"}
    F -->|"是"| G["chunk_end = end"]
    F -->|"否"| H["保留 chunk_end"]
    G --> I["生成 task_id"]
    H --> I
    I --> K{"task_id 是否重复?"}
    K -->|"是"| L["跳过重复任务"]
    K -->|"否"| M["加入 tasks<br/>attempt = 1"]
    L --> N["current_date = chunk_end + 1 天"]
    M --> N
    N --> D
    J --> O["所有唯一任务 put_nowait 到 asyncio.Queue"]
```

日期切分使用 `datetime.strptime()`，因此不存在 `2025-09-31` 这种日期被静默接受的情况：非法日期会在任务池生成阶段直接抛出 `ValueError`，不会先生成第 31 个网页任务。

## 六、一轮共享任务池如何运行

```mermaid
flowchart TD
    Start["_run_task_round(tasks, worker_pages)"] --> Fill["将本轮任务全部放入 asyncio.Queue"]
    Fill --> HasWorker{"存在 Worker?"}
    HasWorker -->|"否"| AllFail["取出所有任务并写入失败账本"]
    HasWorker -->|"是"| Gather["为每个页面启动 worker 协程<br/>gather(return_exceptions=True)"]
    Gather --> Claim["各 Worker 使用 get_nowait 动态领取"]
    Claim --> Execute["execute_task"]
    Execute --> Record["TaskLedger.record"]
    Record --> More{"队列还有任务且 Worker 健康?"}
    More -->|"是"| Claim
    More -->|"否"| WorkerResult["Worker 返回 True 或 False"]
    WorkerResult --> Filter["仅保留返回 True 的健康页面"]
    Filter --> Drain["兜底清空无人处理的剩余任务并记失败"]
    Drain --> Return["返回 healthy_pages 给下一轮"]
    AllFail --> ReturnEmpty["返回空列表"]
```

共享池没有为任务预先绑定 Worker，所以执行顺序遵循：

- 队列中的任务保持配置展开后的先后顺序；
- 哪个 Worker 先空闲，哪个 Worker 就领取下一个任务；
- 不保证同一卡片始终由同一页面处理；
- 快 Worker 会自然承担更多任务，避免等待慢 Worker。

## 七、Worker 生命周期与熔断状态机

```mermaid
stateDiagram-v2
    [*] --> Healthy: Worker 启动
    Healthy --> Claiming: 从共享队列领取任务
    Claiming --> Finished: 队列为空
    Claiming --> Initializing: 领取成功
    Initializing --> Executing: 页面初始化成功
    Executing --> Healthy: 业务成功或普通任务失败<br/>初始化失败计数归零
    Initializing --> InitFailed: TaskPageInitializationError
    InitFailed --> Claiming: 连续失败少于 3 次
    InitFailed --> Fused: 连续失败达到 3 次
    Initializing --> Fused: 页面崩溃 / 关闭 / 断连 / 无响应
    Executing --> Fused: WorkerUnresponsiveError 或致命页面异常
    Finished --> [*]: 返回 True
    Fused --> [*]: 返回 False<br/>不参加下一轮
```

这里有一个关键区分：

- **业务任务失败**：任务写入 `success=false`，Worker 可以继续工作；
- **执行者失败**：Worker 熔断，停止领取后续任务；
- **普通初始化失败**：允许最多连续出现 3 次，给页面短暂恢复机会；
- **页面无响应或断连**：立即熔断，不消耗更多共享任务。

## 八、单个日期任务的完整业务流程

```mermaid
flowchart TD
    Start["execute_task(page, task)"] --> Init["初始化清理<br/>依次关闭三级、二级、一级弹窗"]
    Init --> OpenCard["按 card-1 下标点击任务卡片"]
    OpenCard --> Primary["等待一级 Drawer 与启动检测按钮可操作"]
    Primary --> InitOK{"初始化成功?"}
    InitOK -->|"否：致命异常"| Fatal["向上抛出<br/>Worker 立即熔断"]
    InitOK -->|"否：普通异常"| InitError["等待 5 秒<br/>抛出初始化失败异常"]
    InitOK -->|"是"| Restore["恢复一级弹窗状态"]
    Restore --> Dates["填入开始和结束日期<br/>每次按 Enter 触发 Vue 绑定"]
    Dates --> Detect["点击启动检测"]
    Detect --> Result["等待结果列表最多 45 秒"]
    Result --> Read["最多 3 次读取缺失数量"]
    Read --> Missing{"解析结果"}
    Missing -->|"无数字"| NoMissing["判定无缺失数据"]
    Missing -->|"> 0"| NeedFill["确认存在缺失数据"]
    Missing -->|"0 或负数"| RetryDetect{"检测重试少于 3 次?"}
    RetryDetect -->|"是"| Detect
    RetryDetect -->|"否"| NeedFill
    Missing -->|"读取异常"| NeedFill
    NoMissing --> Success["返回 True"]
    NeedFill --> Backfill["点击一级补齐数据"]
    Backfill --> Secondary["等待二级 Drawer"]
    Secondary --> Whole["点击全店补齐"]
    Whole --> ClickOK{"点击成功?"}
    ClickOK -->|"否且少于 3 次"| Recover["关闭二级/三级<br/>恢复一级后重新打开二级"]
    Recover --> Whole
    ClickOK -->|"最终失败"| SubmitFail["清理弹窗并返回 False"]
    ClickOK -->|"成功"| Submitted["task_submitted = True"]
    Submitted --> Heartbeat["进入 120 秒 Worker 心跳监听"]
    Heartbeat --> Final{"终态判断"}
    Final -->|"三级弹窗不存在"| Success
    Final -->|"三级弹窗仍存在"| Stuck["判定卡死<br/>按层级关闭弹窗"]
    Stuck --> Failed["返回 False"]
```

### 缺失量判断的业务兜底

| 页面文本结果 | 脚本判断 | 后续动作 |
|---|---|---|
| 找不到任何数字 | 无缺失 | 当前任务成功结束 |
| 数字大于 0 | 有缺失 | 进入补齐流程 |
| 数字等于 0 或为负数 | 前端渲染假象 | 重新启动检测，最多 3 次 |
| 读取文本发生普通异常 | 保守认为有缺失 | 强制进入补齐流程 |

## 九、三级弹窗层级与精准关闭

```mermaid
flowchart TD
    Page["datatoolcenter 页面"] --> P1["一级 Drawer<br/>锚点：#checkbutn"]
    P1 --> P2["二级 Drawer<br/>锚点：#loseDays_shop_btn"]
    P2 --> P3["三级 Dialog<br/>锚点：div.dialog-title"]

    Close["_close_layer_if_visible"] --> Visible["硬超时查询容器数量与可见性"]
    Visible --> Exists{"容器可见?"}
    Exists -->|"否"| Skip["无需关闭"]
    Exists -->|"是"| Unique["限定容器内部<br/>确认唯一 el-icon-close"]
    Unique --> Normal["Playwright 常规 click"]
    Normal --> Clicked{"5 秒内成功?"}
    Clicked -->|"否"| DOM["精准 DOM 降级<br/>node.click()"]
    Clicked -->|"是"| WaitHidden["等待容器 hidden"]
    DOM --> WaitHidden
    WaitHidden --> Hidden{"容器按时隐藏?"}
    Hidden -->|"是"| Closed["正常关闭成功"]
    Hidden -->|"否"| Probe["先执行页面健康探测"]
    Probe --> StillVisible{"容器仍可见?"}
    StillVisible -->|"否"| Boundary["视为超时边界完成关闭"]
    StillVisible -->|"是"| Unusable["抛出 WorkerUnresponsiveError"]
```

精准 DOM 点击并不是业务按钮的通用强制点击。它只用于：

- 已经被具体弹窗容器限定；
- 容器内部只有一个关闭叉号；
- 常规 Playwright 点击已经超时；
- 操作后能够验证容器确实隐藏。

容器在等待期限内正常进入 `hidden`，已经证明关闭动作和 DOM 状态观察均成功，因此直接视为关闭完成。只有等待 `hidden` 超时时，才额外执行 `document.readyState` 健康探测并二次查询容器：页面仍响应且容器恰好已经隐藏时，才视为“在超时边界完成关闭”。

`#checkbutn`、`#loseDays_shop_btn` 等业务按钮仍保留 Playwright 的遮挡和可操作性检查。

## 十、Worker 的 120 秒心跳与终态判断

```mermaid
sequenceDiagram
    autonumber
    participant W as datatoolcenter Worker
    participant Toast as 同步成功提示节点
    participant Dialog as 三级进度弹窗

    W->>W: 提交全店补齐
    loop 每一次同步成功事件
        W->>Toast: wait_for_selector(attached, 120s)
        alt 120 秒内捕获到新节点
            Toast-->>W: 返回固定 ElementHandle
            W->>Toast: 等待这个具体节点 hidden，最多 15 秒
            alt 节点按时隐藏
                W->>W: 释放句柄并重新等待下一次心跳
            else 节点 15 秒仍未隐藏
                W->>W: 停止心跳循环并进入终态检查
            end
        else 120 秒没有新节点
            W->>W: 静默期结束，进入终态检查
        end
    end
    W->>Dialog: 硬超时查询三级弹窗是否可见
    alt 三级弹窗不存在
        W-->>W: 判定正常完成，返回 True
    else 三级弹窗仍存在
        W->>Dialog: 精确关闭三级，再关闭二级
        W-->>W: 判定任务卡死，返回 False
    end
```

120 秒静默本身不等于成功。它只表示“已经没有新心跳”，最终必须结合三级弹窗：

- 三级弹窗消失：正常采集完成；
- 三级弹窗仍存在：数仓任务卡死。

## 十一、Context 级业务执行页面 GC

```mermaid
sequenceDiagram
    autonumber
    participant Ctx as BrowserContext
    participant Detect as _delayed_check
    participant JD as 业务执行页面
    participant GC as _monitor_and_gc_page
    participant Toast as 同步成功节点

    Ctx->>Detect: page 事件
    loop 最多检查 10 秒
        Detect->>JD: 读取 URL
        alt URL 包含 ppzh.jd.com
            Detect->>GC: create_task 独立监控
        else 尚未跳转到目标 URL
            Detect->>Detect: sleep 1 秒后重查
        end
    end

    loop 页面仍未关闭
        GC->>Toast: 等待新心跳 attached，最多 180 秒
        alt 捕获心跳
            Toast-->>GC: 固定当前 ElementHandle
            GC->>Toast: 等待当前节点 hidden，最多 15 秒
            alt 正常隐藏
                GC->>GC: 释放句柄，重新开始 180 秒等待
            else 节点异常滞留
                GC->>JD: 强制关闭页面
            end
        else 180 秒无心跳
            GC->>JD: 判定僵尸页面并强制关闭
        end
    end
```

GC 不维护业务执行页面与某个 Worker 的固定映射。原因是一个任务队列中的商智页面可能自动关闭并重新创建；Context 级捕获可以覆盖运行期间出现的全部业务页面生命周期，启动时已经存在的页面也会被扫描。

120 秒与 180 秒错开 60 秒：

- Worker 先判断数仓任务完成或卡死，并清理数仓弹窗；
- 商智 GC 后处理仍未自行消失的执行页面；
- 两套机制不需要互相持有引用。

### URL 识别规则与多平台扩展

当前纳入 GC 的页面由 `.env` 中的统一 URL 标记决定：

```dotenv
GC_PAGE_URL_MARKERS=["ppzh.jd.com"]
```

程序启动后，该 JSON 数组会转换为 `BackfillEngine.gc_page_url_markers` 元组。`_delayed_check()` 的实时页面捕获和程序结束时的残留页扫描都调用 `_is_gc_managed_page_url()`，因此不会出现两个地方分别维护多组 `or` 条件。未来增加抖音时，可以把对应域名标记追加到 `.env` 数组中。

但只增加 URL 的前提是该平台使用相同的心跳协议，即同样通过 `.el-message__content:has-text('同步成功')` 产生并隐藏成功节点。如果抖音的提示文字、DOM 或任务生命周期不同，就应进一步把配置扩展为“URL 标记 + 心跳选择器 + 静默时间”的平台策略，而不能只增加 URL。

### 程序退出前的 GC 收尾

主调度完成时，如果 Context 中已经没有业务执行页面，程序立即退出；如果仍有残留页面，则执行以下收尾：

1. 等待 `180 - 120 + 5 = 65` 秒，让已有 GC 协程完成剩余静默窗口；
2. 宽限期内页面全部自然关闭，则正常退出；
3. 宽限期后重新扫描 Context；
4. 对仍然残留的业务执行页面执行兜底关闭；
5. 完成收尾后再退出 Playwright，避免事件循环提前结束导致 GC 被取消。

## 十二、红色错误提示事件回收器

```mermaid
flowchart TD
    Start["每个 Worker 启动独立监控协程"] --> Wait["wait_for_selector timeout=0<br/>长期挂起等待新红色提示"]
    Wait --> Found["捕获一个具体 ElementHandle"]
    Found --> Mark["写入 data-rpa-error-close-scheduled 标记"]
    Mark --> Read["读取错误内容"]
    Read --> Task["create_task 延迟关闭任务"]
    Task --> Grace["保留 30 秒供人工观察"]
    Grace --> Alive{"页面和提示仍可见?"}
    Alive -->|"否"| Dispose["释放句柄"]
    Alive -->|"是"| Close["查找该提示内部专属叉号"]
    Close --> Normal["Playwright 常规点击"]
    Normal --> Covered{"被页面层遮挡?"}
    Covered -->|"是"| DOM["node.click 精准 DOM 点击"]
    Covered -->|"否"| Hidden["等待该节点 hidden 5 秒"]
    DOM --> Hidden
    Hidden --> Dispose
    Dispose --> Wait

    Mark -."标记始终保留".-> Once["同一个幽灵节点<br/>不会被重复调度"]
```

该机制是事件驱动的：没有红色提示时，协程阻塞在浏览器事件等待上，不会每秒轮询 DOM。

它与商智 GC 的共同思想是“捕获具体对象后管理它的生命周期”，但回收粒度不同：

- 红色提示回收器处理 Worker 页面内的 UI 节点；
- 商智 GC 处理整个商智标签页。

## 十三、短页面操作的硬超时与健康探测

```mermaid
flowchart LR
    Op["本应快速返回的页面操作<br/>count / is_visible / evaluate"] --> WaitFor["asyncio.wait_for<br/>默认 10 秒"]
    WaitFor --> Fast{"按时返回?"}
    Fast -->|"是"| Value["返回查询结果"]
    Fast -->|"否"| Error["WorkerUnresponsiveError"]
    Error --> Fatal["_fatal_page_error_reason"]
    Fatal --> Fuse["Worker 立即熔断"]

    Ready["page.evaluate<br/>document.readyState"] --> WaitFor
```

`document.readyState` 可能返回：

- `loading`：文档仍在加载；
- `interactive`：DOM 已构建；
- `complete`：页面及资源完成加载。

这里的主要目的不是要求页面必须达到 `complete`，而是验证浏览器渲染进程能否在 10 秒内执行一次 JavaScript 并返回合法状态。只要 JS 往返及时完成，就证明页面事件循环仍有响应。

硬超时只包裹理论上应快速完成的页面探针，不包裹完整补采任务，因此不会因为任务实际运行数小时而误杀 Worker。

## 十四、JSONL 账本与统一重试

每一次最终任务尝试写入一行：

```json
{"task_id":"card-3_2025-07-01_2025-07-01","card":3,"start":"2025-07-01","end":"2025-07-01","attempt":1,"success":false}
```

```mermaid
flowchart TD
    Reset["运行开始<br/>TaskLedger.reset"] --> Round1["第一轮任务执行"]
    Round1 --> Record1["各 Worker 在 asyncio.Lock 内<br/>追加 attempt=1 结果并 flush"]
    Record1 --> Load["第一轮全部 Worker 收敛后<br/>failed_tasks(attempt=1)"]
    Load --> Failed{"存在 success=false?"}
    Failed -->|"否"| Summary["summary"]
    Failed -->|"是"| Rebuild["复制 card/start/end<br/>attempt 改为 2"]
    Rebuild --> Healthy{"仍有健康 Worker?"}
    Healthy -->|"是"| Round2["失败任务进入新的共享池"]
    Healthy -->|"否"| FinalFail["没有 Worker 可重试"]
    Round2 --> Record2["追加 attempt=2 结果"]
    Record2 --> Summary
    FinalFail --> Summary
    Summary --> Latest["按 task_id 选择最高 attempt 结果"]
    Latest --> Report["输出首轮、重试、最终成功失败数量"]
```

当前 Backfill 策略是：

- 首轮所有任务执行完毕后才读取失败项；
- 失败任务统一进入第二轮；
- 第二轮只使用第一轮结束后仍健康的 Worker；
- 只进行一次总体重试；
- 两轮之间没有额外固定等待，也不是指数退避。

## 十五、异常分类与处理矩阵

| 异常类型 | 典型场景 | 当前任务 | 当前 Worker | 第二轮 |
|---|---|---|---|---|
| 普通业务失败 | 二级弹窗打不开、全店补齐未提交、任务判定卡死 | 写入失败 | 继续领取 | 任务进入重试 |
| 单次初始化失败 | 旧弹窗或页面状态暂时异常 | 写入失败 | 累计一次 | 任务进入重试 |
| 连续 3 次初始化失败 | 页面长期无法恢复到可操作状态 | 第 3 个任务写入失败 | 熔断 | 不参加第二轮 |
| 页面查询硬超时 | `count()`、`is_visible()`、JS 健康探测无响应 | 写入失败或结果未知 | 立即熔断 | 不参加第二轮 |
| 页面崩溃或断连 | Page、Target、Context、Browser 关闭 | 结果按提交状态记录说明 | 立即熔断 | 不参加第二轮 |
| 商智页面 180 秒无心跳 | 商智执行页成为僵尸页面 | 不直接决定账本结果 | 不绑定 Worker | GC 关闭商智页 |
| 红色提示遮挡 | 登录失效或接口异常导致提示堆积 | 主业务继续运行 | 监控器延迟回收 | 不直接影响轮次 |

## 十六、脚本模块职责说明

### 1. 配置与入口模块

`load_runtime_config()` 从源码或 exe 同目录的 `.env` 读取浏览器 ID、任务列表和 GC URL 标记。列表使用 JSON 表达并经过类型校验；入口随后创建 `BackfillEngine`，再通过 `asyncio.run()` 启动异步总控流程。真实 `.env` 只保留在本地，仓库仅提交 `.env.example`。

### 2. 浏览器连接模块

`get_debugger_address()` 调用比特浏览器本地 API，获得 CDP 调试地址。`run()` 使用 Playwright 接管已经登录的浏览器，并把 URL 包含 `datatoolcenter` 的标签页识别为 Worker。

### 3. 任务构建模块

`generate_date_chunks()` 按 `chunk_days` 切分历史区间；`build_tasks()` 为每个日期区块生成唯一 `task_id`，去除重复配置，然后形成首轮任务列表。

### 4. 轮次调度模块

`_run_task_round()` 把一轮任务放入共享 `asyncio.Queue`，为每个可用页面启动一个 `worker()` 协程。任务不预先绑定页面，由先空闲的 Worker 继续领取下一项，实现动态负载均衡。

### 5. Worker 执行与熔断模块

`worker()` 负责循环领取任务、调用 `execute_task()`、把结果写入账本，并维护连续初始化失败次数。普通业务失败不会淘汰 Worker；连续 3 次初始化失败、页面无响应、崩溃或断连会触发熔断。

### 6. 单任务业务模块

`execute_task()` 完成一个日期区块的全部业务操作：清理遗留弹窗、进入指定任务卡片、注入日期、启动检测、读取缺失量、打开二级弹窗、点击全店补齐，并进入心跳终态判断。

### 7. 弹窗定位与恢复模块

`_primary_drawer()`、`_secondary_drawer()` 和 `_progress_dialog()` 使用内部业务锚点区分三级容器。`_close_layer_if_visible()` 只对弹窗内部唯一叉号提供精准 DOM 降级；`_restore_primary_state()` 负责回到一级弹窗可操作状态。

### 8. Worker 心跳模块

`wait_for_completion_or_heartbeat()` 在数仓 Worker 页监听“同步成功”节点。120 秒没有新心跳后，脚本检查三级弹窗：三级弹窗消失表示正常完成，仍存在表示卡死并需要清理。

### 9. 业务执行页面 GC 模块

`_on_new_page()` 与 `_delayed_check()` 从 BrowserContext 层识别符合 URL 标记的业务执行页面，`_monitor_and_gc_page()` 独立监听每个页面的成功心跳。180 秒无心跳或单个心跳节点异常滞留时，GC 关闭该页面；主调度结束后，`_cleanup_remaining_gc_pages()` 再提供65秒宽限和最终兜底关闭。

### 10. 红色错误提示回收模块

`_monitor_worker_error_toasts()` 事件等待每个 Worker 页面的红色提示，为具体节点添加防重复标记，并创建延迟关闭任务。提示保留 30 秒后关闭；常规点击被遮挡时，仅对专属叉号使用 `node.click()`。

### 11. 页面健康与硬超时模块

`_await_page_operation()` 为本应快速完成的 DOM 查询增加 10 秒外层硬超时。`_assert_page_healthy()` 通过 `document.readyState` 执行一次 JavaScript 往返，用于确认超时页面的渲染事件循环是否还能响应。

### 12. 任务账本与重试模块

`TaskLedger` 使用 `asyncio.Lock` 串行追加 JSONL 结果。首轮结束后，`failed_tasks(attempt=1)` 从账本重建失败任务并把 `attempt` 改为 2；健康 Worker 统一执行第二轮，最后由 `summary()` 按每个任务的最新结果汇总。

## 十七、脚本完整运行逻辑摘要

1. 从 `.env` 读取并校验 `BITE_ID`、`TASKS_CONFIG` 和 `GC_PAGE_URL_MARKERS`；
2. 使用 `BITE_ID` 启动或连接指定比特浏览器；
3. 连接 BrowserContext，识别数仓 Worker 页面；
4. 在 Context 层挂载业务执行页面 GC，并扫描已有页面；
5. 把配置日期切分成唯一日期区块，生成首轮任务列表；
6. 重置 JSONL 任务账本，为每个 Worker 启动红色提示监控器；
7. 把首轮任务全部放入共享任务池，由多个 Worker 动态领取；
8. 每个 Worker 清理遗留弹窗，打开任务卡片并注入当前区间日期；
9. 检测缺失数据；无缺失则直接成功，有缺失则进入全店补齐；
10. 提交后监听 120 秒心跳，并结合三级弹窗区分正常完成和卡死；
11. 业务执行页 GC 独立使用 180 秒心跳回收没有正常关闭的页面；
12. 每个任务结束后立即把本次尝试结果追加到 JSONL；
13. Worker 发生普通任务失败时继续领取，发生致命页面异常时退出任务池；
14. 首轮全部 Worker 收敛后，从 JSONL 读取失败任务；
15. 仅由仍然健康的 Worker 对失败任务统一重试一次；
16. 根据每个 `task_id` 的最新尝试结果输出最终成功和失败汇总；
17. 停止红色提示监控器，并取消尚未完成的30秒延迟关闭协程；
18. 如果仍有业务执行页面，等待65秒交给 GC 自然收尾，再兜底关闭残留页面；
19. 退出 Playwright 连接，程序结束。
