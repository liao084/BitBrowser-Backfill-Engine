# Go / Fyne Launcher

一个 Go / Fyne 启动器，默认进入每日采集，通过顶部按钮切换历史补采。保持现有 Python 引擎、JSON 配置和客户 `.env` 格式，不嵌入 Python，不引入 RPC。原 Python Launcher 暂时保留，现有 CI 暂不切换。

## 当前交付范围

- Mac 版：用于界面审计、客户配置读写、任务增删改、配置预览和引擎文件同步。
- Windows 版可用 `build-windows.sh` 在 Mac 上交叉构建，只交付 EXE，沿用业务环境已有 JSON。用户已验证启动与界面；后续变更仍需按实际场景验证。
- Mac 明确禁用“保存配置并启动”，不会尝试运行 Windows 引擎。
- 每日采集默认不保留浏览器窗口；缺少该字段的旧 `.env` 沿用 Launcher 默认值，显式配置的保留选择仍正常恢复。
- 客户操作、任务操作和底部保存操作分别采用组内等宽按钮，随可用宽度伸缩。
- 左侧为客户与浏览器、运行参数、业务平台；右侧为任务编辑和任务清单。左右分区可拖动调整，左侧内容可独立滚动，不自动改成上下两区。
- 每种模式保留独立的未保存表单与任务，切换不会写文件。首次切换才加载该模式配置；配置加载失败不替换当前有效视图，可修复文件后再次切换。
- 采用已审计的浅色/深色主题、系统字体、10px 表单网格间距。日期支持手填 `YYYY-MM-DD` 与原生日历选择；勾选“截至昨天”时结束日期及其日历按钮一起禁用。Daily 任务偏移不显示额外说明。
- 数字输入框右侧嵌入上下箭头；整数单步为 1，会话时长单步为 0.1 小时，沿用原字段上下限。空值、非法输入与禁用状态不通过箭头自动改写，仍保留手填。
- 客户下拉列表最多完整显示 8 行，其余滚动查看；空间不足时根据窗口位置缩短或向上展开，保留手工输入客户名称。
- 支持空任务提示、选中后启用更新/删除、撤销最近一次删除。
- 字体优先读取本机的无衬线中文字体：Mac 系统黑体 Heiti SC，Windows 微软雅黑 UI。Mac 的 PingFangUI 在当前 Fyne 直接读取时出现文字不显示，暂不采用。字体缺失时回退默认字体，保留启动器配色；不复制、嵌入或随应用分发系统字体。
- 兼容性样例的源实现已替换，旧样例产物已移出 `dist`。

## 构建 Mac 应用

需要 Go、Apple Clang / Xcode Command Line Tools。在本目录执行：

```sh
sh build-mac.sh
```

产物和配置位置：

```text
dist/
├── Launcher.app                              # 统一入口，默认每日采集
├── backfill/
│   ├── _release/
│   │   ├── backfill_launcher_config.json
│   │   └── backfill_engine.exe               # 手工放入，非 Mac 引擎
│   └── 客户名称/.env
└── dailyfill/
    ├── _release/
    │   ├── dailyfill_launcher_config.json
    │   └── daily_engine.exe                   # 手工放入，非 Mac 引擎
    └── 客户名称/.env                           # 也支持既有分组目录
```

构建脚本只在配置文件不存在时复制仓库的脱敏模板，绝不覆盖已维护的 JSON。没有引擎文件也能保存配置，界面会明确提示未复制引擎。

`Launcher.app` 从应用包外层寻找并列的部署目录，不把客户文件写入 `Contents`。移动应用时，请连同 `backfill` / `dailyfill` 目录一起移动。初始配置中的客户和浏览器 ID 都只是占位示例，正式使用前需自行维护。

开发时也可显式指定模式和部署根目录：

```sh
go run . -mode backfill -root ./dist/backfill
go run . -mode daily -root ./dist/dailyfill
```

`-mode` 只指定初始选中模式，省略时为 `daily`。`-root` 是该初始模式的部署目录（含 `_release` 和客户目录），不是整个仓库目录；另一模式使用它旁边的 `backfill` / `dailyfill` 目录。例如上面的两个命令均可在窗口中切换模式，仍从 `dist/backfill` 与 `dist/dailyfill` 分别读写。默认构建只生成一个应用，不再使用链接参数生成两个入口。

## 构建 Windows 应用

需要 Go 和 MinGW-w64。在本目录执行：

```sh
sh build-windows.sh
```

唯一产物为 `dist/windows/Launcher.exe`。构建不读取或复制配置，不生成 ZIP。将 EXE 放在现有 `dailyfill` 与 `backfill` 的共同父目录，继续读取业务环境中的 `dailyfill/_release/dailyfill_launcher_config.json` 与 `backfill/_release/backfill_launcher_config.json`，采集引擎沿用现有 EXE。运行时修改部署 JSON 即可，无须重编译；已加载模式在当前进程内缓存，修改 JSON 后重新打开启动器。

静态链接 MinGW 运行库；交付前核对 PE 类型、GUI 子系统及导入 DLL。该检查不代替 Windows 目标机器运行测试。

## 保持的业务语义

- Backfill 只扫描根目录的直接子目录，支持 BitBrowser 与外部 CDP、日期范围、截至昨天、区块天数、会话轮换参数。
- Daily 递归发现已有 `.env`，排除 `_release`；重名客户目录报错，避免保存到错误位置。新客户仍放在根目录下。
- 客户切换先恢复默认值，再加载客户 `.env`；“新建客户”清空专属字段和任务。
- Daily 的报告准备时间、任务入口、平台定义来自 Launcher JSON；平台任意扩展字段（包括 `auth_mode` / `auth_params`）都会保留。Cookie 目录与原 Python 版一致，可从既有 `.env` 加载。
- Daily 清楚区分全局日期偏移与单条任务偏移。每条保存的任务都带显式偏移，0 表示当天。
- 保存仍是重新生成基础 `.env`，不保留手工补充项；界面会要求确认。加载失败会阻止保存，避免误覆盖原配置。
- `.env` 读写支持项目模板的赋值、注释、引号、多行 JSON、中文和转义；不执行 shell，也不展开当前机器的环境变量占位符。
- 任务 JSON 使用显式结构体导出；补采字段固定按 `card`、`start`、`end`、`chunk_days` 排列，任务数组顺序与界面列表一致。
- 同步引擎使用临时文件替换；Windows 按完整 EXE 路径识别运行实例，运行时不覆盖 EXE、不重复启动。无法核对同名进程路径时报告错误而非继续覆盖。
- 为避免保存到 `_release` 或逃出部署目录，客户名禁止路径字符、Windows 设备保留名和保留目录；拒绝已有目录符号链接指向根目录外。

## 源码职责

- `main.go`：入口、模式及部署目录。
- `config.go`：JSON 配置、默认值、客户发现、统一校验。
- `env.go`：与 Python 模板兼容的 `.env` 解析、生成和加载。
- `instance.go`：客户目录边界、配置保存和引擎复制。
- `ui_shell.go`：统一窗口、模式切换、各模式编辑视图缓存。
- `ui.go` / `ui_tasks.go`：客户表单、任务编辑、表格、撤销删除、预览和保存确认。
- `ui_controls.go`：数字微调箭头、日历入口、限高客户选择列表。
- `ui_layout.go`：左右面板、字段网格与表格列宽布局。
- `fonts.go`：系统中文字体选择和主题；TTC 字重只在内存中转换成 Fyne 可读取的单字体资源。
- `process_windows.go` / `process_other.go`：平台相关的运行检测和启动边界。
- `build-mac.sh` / `*.Info.plist`：可复现的 Mac app 打包。

## 审计建议

先在 `dist` 的脱敏配置中测试模式往返、各模式编辑保留、客户切换、新建、日期填写、任务增删改及预览。需要测试保存时使用新建的审计客户，不要覆盖真实客户配置。检查生成 `.env` 的任务、偏移、平台字段和心跳参数，再由现有 Python 引擎验证；Mac 版不会启动引擎。

实际 Windows 兼容性与实例启动在目标机器实测。测试文件不加入项目；二进制、部署 JSON 和客户 `.env` 都保持 Git 忽略。
