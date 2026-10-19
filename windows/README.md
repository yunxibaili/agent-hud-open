# Agent HUD · Windows 版（PySide6）

<img src="assets/agent-hud-icon-v1.png" width="112" alt="Agent HUD 可爱助手图标">

把 [jazzenchen/agent-hud-open](https://github.com/jazzenchen/agent-hud-open) 的**额度采集**与**停靠 / 悬停状态机**搬到 Windows：
一个贴边停靠的桌面悬浮 HUD。目前注册 16 种工具；额度、余额和本地会话用量的支持因客户端而异，具体见版本与移植状态说明。

### Windows v0.1.15 · 2026-10-19

- 撤回按数字扩展停靠栏宽度的处理，恢复紧凑布局。纯图标显示沿用既有 `dock.show_percent=false` 设置（新安装的默认值）；额度、余额、Token 与月度金额仍可在悬停提示、卡片和统计中查看。

### Windows v0.1.14 · 2026-10-19

- 修复顶部 DSH 数字的单位被裁掉：停靠栏原先只为 `100%` 预留宽度，现在按实际 Token、余额及月度费用文字测量并扩展数字区。沿用既有统一槽位布局与点击区域，在本次运行中保留已扩展宽度，避免数值刷新时宽度反复跳动。

### Windows v0.1.13 · 2026-10-19

- DeepSeek 官方 API 路由按请求开始时间估算峰谷费用：北京时间周一至周五 09:00–12:00、14:00–18:00 为高峰，其他时段、周末（包括调休上班的周末）及 2026 年节假日为谷价；峰价是谷价的两倍。规则与人民币单价核对于 2026-10-19，来源：[DeepSeek 官方价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)、[国务院 2026 年假期安排](https://www.beijing.gov.cn/cs/gncs/zcwj/202603/t20260327_4568275.html)。
- 复用上游 `DeepSeekPricing.swift` 的官方路由与缓存计价语义。Flash 谷价为每百万 Token 输入未命中 ¥1、缓存命中 ¥0.02、输出 ¥4；V4 Pro 对应 ¥4.5、¥0.15、¥13.5。按现行人民币目录估算，再沿用已有汇率换算显示美元，不冒充实际账单或历史费率。
- DSH 日志保留模型名及输入/输出/缓存分项；各 Agent 卡片底部新增近 24 小时模型用量与可计价费用，按用量排序，可随卡片滚动。未记录的模型显示“未记录模型”，未知费用显示“—”，完整模型列表也可在统计窗口查看。
- DSH 的卡片、统计及月度费用共用逐请求估算，不再叠加鲸鱼账本中的另一份费用；WorkBuddy、OpenCode Go 路由不会套用 DeepSeek 官方价格。分项、请求开始时间、受支持模型或年份节假日表缺失时不猜价格；目前节假日表仅核对到 2026 年，新增年份需更新后才估算。

### Windows v0.1.12 · 2026-10-19

- 按用户使用习惯，检测到 DSH 运行时，默认保留 WorkBuddy 停靠项及额度卡片，即使 WorkBuddy 客户端没有运行。DSH 退出后恢复按实际进程筛选；WorkBuddy 自己仍运行时继续显示，显式禁用的提供方仍保持禁用。
- 该项是关联额度入口，不代表检测到 WorkBuddy 进程或已确认它与 DSH 使用同一账户；沿用现有额度读取和客户端用量归属，不增加查询，也不复制 DSH 请求到 WorkBuddy。

### Windows v0.1.11 · 2026-10-19

- 区分执行客户端与额度来源：DSH 经 WorkBuddy 或 OpenCode Go 发出的请求仍归入 DSH，后两者只统计各自客户端的本地用量。汇总、月度及统计沿用同一份快照，不再通过跨卡片转移请求来表示计费来源。
- DSH 卡片及悬停提示列出近 24 小时日志确认的外部额度来源；不把历史路由视为当前配置，也不把 DeepSeek 余额或另一个客户端账户的额度套用到这些请求。账户关联尚未核验时明确提示余量无法确认；不会为此增加额度接口调用。
- 普通 JSONL 会话不再依赖可选 zstd 解码器；压缩记录仍需要原有解码器。复用上游 [usage-semantics.md](https://github.com/jazzenchen/agent-hud-open/blob/bf19ac98da11fe0f5967f5a124d42f0da4bf2cf9/docs/usage-semantics.md) 中执行客户端、计费池及账户独立的语义，不新增依赖。

### Windows v0.1.10 · 2026-10-19

- 顶部停靠栏只显示检测到实际进程的 Agent，不再以安装目录或历史日志代替运行状态。每 10 秒在后台读取当前 Windows 会话的进程快照，排除 HUD 自身及其查询子进程；未识别或检测失败时不把历史工具当作运行中。
- 运行状态只筛选停靠栏与当前 Agent 卡片。历史用量仍保留在统计、月度和全部 Agent 汇总中；「月」及托盘统计入口可继续使用。标准客户端进程和常见 Node/Python 入口已纳入；改名、自定义启动器、WSL 内部客户端尚未逐一验证。
- 修复卡片总高度超过屏幕后被截断的问题：卡片区域可滚动，标题固定；统一预留滚动条宽度，切换页时回到顶部。统计窗口也支持外层滚动，不再依赖 600px 最小高度挤压表格和说明文字。
- 修复从月度页按 Alt 悬停 Agent 时仍显示月度内容的问题；没有运行中的 Agent 时不再自动展开一个不存在的 Agent 卡片。
- 实现依据：[Windows Win32_Process](https://learn.microsoft.com/en-us/windows/win32/cimwin32prov/win32-process)、[Qt QScrollArea](https://doc.qt.io/qtforpython-6/PySide6/QtWidgets/QScrollArea.html)，复用既有隐藏进程调用和后台队列；不新增依赖，不记录进程命令行。

上游主要界面和差距（对照 `bf19ac9`）：

| 上游界面 | 作用 | Windows 当前差距 |
| --- | --- | --- |
| HUD 悬停/展开面板 | 额度、余额、会话、提醒及回到会话 | 已有额度与用量；缺真实会话列表和跳转 |
| 统计窗口：Token 页 | 时间、模型、Token 类型、费用、活动热图 | 已有基础统计；缺完整价格目录与账本去重 |
| 统计窗口：会话页及详情 | 搜索/按项目筛选，逐会话、轮次、调用、上下文、子 Agent | 尚未实现；是统计窗口内页面，不是每个 Agent 一个窗口 |
| 设置窗口 | 通用、Agent 选择、每屏显示、形态、布局、回调 | 目前主要为托盘菜单和 config.json，缺完整设置界面及多屏独立配置 |
| 批准/提问/回复卡片 | 展示请求与差异，用户批准或回答，回到会话 | 尚未接入真实客户端协议 |
| 首次启动引导 | 选择来源、运行方式和初始化设置 | 尚未实现 |

以上功能存在不代表均已做过 Windows GUI 实测；本轮 GUI 工具未返回可选中的 HUD 工具窗口，仍保留视觉验证边界。

### Windows v0.1.9 · 2026-10-19

- 修复新接入 Agent 没有计入旧「本月」页面的问题：月度和统计窗口复用同一份后台快照，按日历月统计，包含原生 OpenCode 日志及新增本地来源；没有额外重扫整月日志。
- 月度数字随后台快照更新，跨月不沿用上个月的计数；首次采集未完成显示「统计中」。新 Agent 使用统一名称表，未知费用和缺失记录继续标明边界。
- 读取失败会在快照中留下通用提示，不再无提示地显示为零用量；不输出日志正文、账户或凭据。

### Windows v0.1.8 · 2026-10-19

- 新增 Hermes Agent（含 profile）、ZCode、Devin CLI 的 SQLite 本地用量，以及 Copilot CLI 的关闭快照、Grok CLI 的更新/推理日志。SQLite 连接只读，跟踪 WAL 更新；每次解析限时 2.4 秒、最多 100,000 条，超过限制明确标记统计不完整，不修改客户端数据库。
- Hermes 使用模型用量表，没有该行时回退会话总数；记录归于首次使用时间，不能还原每次请求时刻。Copilot 只计关闭快照差值，首个不完整快照只作基线，不代表运行中实时用量。Grok 的逐次推理日志优先于同一会话的汇总，不以旧上下文大小估算消耗。
- 统计新增模型与 Token 类型筛选、缓存读取占输入比例、按星期和小时聚合的本地活动热图。图表、表格和热图遵循相同筛选；显示分项覆盖率，缺失模型或分项不猜测。
- Codex、OpenCode 和新增本地源的可用模型/缓存分项接入同一汇总；已记录的零费用保留为零，异常或未知费用不计入。按 Token 类型查看时不任意分摊费用。
- 参照上游 `bf19ac9` 的 [HermesSessions.swift](https://github.com/jazzenchen/agent-hud-open/blob/bf19ac98da11fe0f5967f5a124d42f0da4bf2cf9/Sources/AgentHUDCore/Providers/Hermes/HermesSessions.swift)、`ZCodeSessions.swift`、`DevinSessions.swift`、`CopilotSessions.swift`、`GrokSessions.swift` 和用量口径；继续复用既有缓存、扫描器和 Qt 绘图，不增加依赖。

### Windows v0.1.7 · 2026-10-19

- 新增 Claude Code、CodeBuddy、Pi、Kimi、Qwen Code、OpenClaw 的本地 Token 读取，自动发现已有会话目录；接入卡片、趋势、全部 Agent 汇总和多时段统计。不读取凭据，不发起推理或新的网络请求；这些新增接入尚无在线额度接口。
- 按上游字段处理请求去重、继承历史、缓存 Token、Qwen 分支和 OpenClaw 镜像排除；源码仅保留计数、模型和内部去重键，不保留提示词。大于 64 MiB、损坏或不可读的文件跳过并提示统计不完整。
- CodeBuddy 从 WorkBuddy 本地日志统计中分离，避免独立卡片上线后重复计数。复用现有扫描器和文件缓存，没有新数据库、服务或依赖。
- 新增 Devin CLI 每日/每周缓存额度，按上游 [DevinQuota.swift](https://github.com/jazzenchen/agent-hud-open/blob/bf19ac98da11fe0f5967f5a124d42f0da4bf2cf9/Sources/AgentHUDCore/Providers/Devin/DevinQuota.swift) 解码；仅展示订阅字段，不展示缓存中的账户身份。刷新仍由 CLI 自身完成；目前未读取 Devin 会话用量。
- 本地读取参照同版本的 `OpenAgentSessions.swift`、`QwenSessions.swift`、`TencentBuddySessions.swift` 和 `OpenClawSessions.swift`；沿用项目与第三方许可声明。未安装客户端时自动隐藏对应停靠项。

### Windows v0.1.6 · 2026-10-19

- 卡片右上角和托盘新增「用量统计」入口：5 小时、24 小时、7 天、30 天切换、Agent 筛选、堆叠趋势和来源已记录的美元费用。统计窗口不占任务栏，关闭后停止自己的刷新时钟。
- 扫描和汇总继续在既有后台线程执行；界面仅使用采集快照，未知费用显示 `—` 并标明计价覆盖比例，不把未知当免费，不发起推理请求。
- Codex 改按累计输入/输出差值计量，跳过继承历史与重复额度事件；归档副本按同名 rollout 的最新文件去重。修复月度/每日解析缓存共用导致较早记录漏计的问题。
- 修复 WorkBuddy 多个 glob 重复读取同一文件的问题；SQLite 缓存跟踪 WAL 变化，扫描范围扩至 32 天以覆盖本月及最近 30 天。
- 移植依据：上游 `bf19ac9` 的 [用量口径](https://github.com/jazzenchen/agent-hud-open/blob/bf19ac98da11fe0f5967f5a124d42f0da4bf2cf9/docs/usage-semantics.md) 与 `CodexTranscripts.swift`；复用 Windows 的 `TokenReading`、`draw_bars` 和托盘入口，无新依赖。

### Windows 移植状态

对照上游 `bf19ac9`（2026-10-18）。仓内 Swift 源码存在不等于 Windows 已提供相同功能。

| 能力 | Windows 状态 |
| --- | --- |
| 停靠、悬停、额度、提前耗尽预警 | 已实现，保留本机单实例与隐私设置 |
| 5h / 24h / 7d / 30d、按 Agent 查看 | v0.1.6 已实现；取决于客户端仍保留的历史 |
| 18 种 Agent 的全量接入 | 注册 16 种；Cursor、Grok Bot 未适配，新增接入主要为本地用量，并不都包含在线额度 |
| 按模型、Token 类型、缓存比例、热图 | v0.1.8 已实现；仅覆盖客户端实际记录的分项 |
| 会话、轮次、调用与回到会话 | 待移植 |
| 精确工作/等待状态、回复提醒 | 当前仅有 Token 活动推断，待接入真实生命周期 |
| 批准、提问、客户端回调 | 待移植；不能以模拟批准替代客户端协议 |
| 每屏独立布局与设置、多形态 Bot | 待移植 |
| 局域网、iPhone / Watch、iCloud Remote | 未实现；Apple 框架、官方服务及付费能力不能直接搬到 Windows |

### Windows v0.1.5 · 2026-10-19

- 应用与系统托盘启用新绘制的可爱图标，托盘右下角保留额度状态色点；继续仅驻留托盘，不占任务栏。
- 新增提前耗尽预警：即使未达到临界用量，按已观察的速度预计撑不到重置时，也会通过现有提醒条提示；每个窗口在同一重置周期内最多提醒一次预测风险。
- 卡片和预警共用预测计算。失败、超过 30 分钟、未来时间、不限量、已耗尽、没有未来重置时间或样本不足的数据不参与预测；首次观察保持静默。
- 预测至少需要 3 次有效采样且跨度不少于 9 分钟；依据已知周期起点截取样本，并从最后一次明显重置后重新估计。估算不会主动模拟未观察到的消耗，不保证实际耗尽时间。
- 复用现有 `History`、额度数据和提醒条，参照上游 [QuotaAlerts.swift](https://github.com/jazzenchen/agent-hud-open/blob/bf19ac98da11fe0f5967f5a124d42f0da4bf2cf9/Sources/AgentHUDCore/Logic/Alerts/QuotaAlerts.swift)、`QuotaMath.swift` 与 `UsageAnalytics.swift` 的规则，不增加依赖或额外采集请求。

### Windows v0.1.4 · 2026-10-19

- 按 Alt 悬停指定 Agent 时释放旧的点击焦点，让强制展开的目标生效；在面板空白处按 Alt 时仍保留当前目标。

### Windows v0.1.3 · 2026-10-19

- 卡片自动关闭时清除点击焦点，避免下一次悬停仍显示旧 Agent，以及重新点击同一图标时只触发关闭。固定面板和关闭前的防抖行为不变。

### Windows v0.1.2 · 2026-10-19

- 使用 [Qt QLockFile](https://doc.qt.io/qtforpython-6/PySide6/QtCore/QLockFile.html) 防止同目录重复启动 HUD；重复启动的守护进程会退出，不反复拉起副本。
- 收起完成后停止逐帧时钟，唤醒后恢复；动态内容使用 16ms 帧间隔，静止展开时降为 66ms。动画按真实经过时间推进。
- 临界额度提醒记录本次状态后再触发，避免在持续超过阈值时重复提醒；未安装的工具保持正常刷新周期。

### Windows v0.1.1 · 2026-10-19

- 恢复「月」按钮、独立月度页面和图标旁的美元估算；复用 `tokens.py` 的后台统计，扫描不占用界面线程。
- 修正恢复版与当前悬停、停靠及卡片接口的不兼容；切换月页面时清除已删除卡片的引用。
- 顶层透明窗口在同一屏幕和停靠方向下保留尺寸，仅改变内部面板大小；重绘时清理透明背景，各面板独立绘制阴影。按 [Windows 分层窗口规则](https://learn.microsoft.com/en-us/windows/win32/winmsg/window-features#layered-windows)，完全透明区域让鼠标点击穿过，无需在动画中反复改变原生窗口遮罩。
- 长提示按 HUD 可用宽度截断；切换页面时隐藏旧卡片并延迟销毁。额度读取失败时保留上次有效值并标记错误，按 `retry_seconds` 重试。
- 账户位置只显示连接状态，不显示邮箱或账户标识。
- 所有 GUI 启动方式均使用不占任务栏的工具窗口，仅保留系统托盘；`python hud.py --expand` 只负责展开并固定面板。
- 本地 `config.json`、用量历史、日志、个人头像、截图和恢复残片不提交到仓库。

这是对关键功能的重建，不代表所有历史补丁逐条恢复。文档下方原有动效说明仅供参考，以当前源码为准。

UI 用 **Python + PySide6** 重写（原版是 macOS 专属的 Swift + AppKit），但几何与状态机是**逐行照搬**原仓库的 Swift：

| 本仓库文件 | 原版 Swift |
| --- | --- |
| `windows/placement.py` | `Sources/AgentHUDDesktop/Notch/NotchGeometry.swift` |
| `windows/hover.py` | `Sources/AgentHUDDesktop/Notch/HoverMachine.swift` |
| `windows/peek.py` | `Sources/AgentHUDDesktop/Notch/Panel/AgentPeekView.swift` |
| `windows/panel.py` | `Sources/AgentHUDDesktop/Notch/Panel/HoverPanelView.swift` |
| `windows/dock.py` | `Sources/AgentHUDDesktop/Notch/Island/{IslandRootView,LogoQueueView}.swift` |

动效与计价是两个自成一体的新模块（原版没有对应物）：`windows/motion.py` 是动画基元，
`windows/money.py` 把各家的花费折算成美元。

配色与阈值同样取自原仓库：`Theme/StatusPalette.swift`（ok `#3DDC84` / warning `#FFD23F` / critical `#FF453A` / idle `#9A9AA0`）
与 `Logic/Alerts/AlertPolicy.swift`（**已用 <70% 绿、70%~90% 黄、≥90% 红**）。
品牌图标直接读 `Sources/AgentHUDDesktop/Resources/*.png`，映射照抄 `Components/AgentLogo.swift`
（Codex → `chatgpt.png` 着 OpenAI 绿 `#10A37F`，OpenCode → `opencode-dark.png`，Antigravity / WorkBuddy / DeepSeek 各自官方图）。

---

## 一、停靠：贴边，绝不悬空

| 状态 | 截图 |
| --- | --- |
| **① 平时贴边紧凑态**（鼠标移到顶边热区后滑出） | ![edge](screenshot-edge.png) |
| **贴边隐身**（无操作时只留 3px 流光细线，鼠标穿透） | ![peek](screenshot-peek.png) |
| **竖排停靠**（拖到左/右边缘自动转竖向） | ![side](screenshot-side.png) |

* **顶部刘海模式**：紧贴屏幕顶边 `y=0`，上面两角直角贴死顶沿、下面两角圆角（`NotchGeometry.notchCornerRadius`）。
* **四边磁吸**：拖拽松手后按指针到四边的距离取最近边吸附（`NotchGeometry.dragPlacement`，斜向平局保持原边），
  沿边位置用 0~1 的 `offset` 记录；**贴左/右边缘自动转竖排**，贴上下边缘横排。
* **贴边隐身（Edge Peek）**：不与鼠标互动时整条滑出屏幕，只留 **3px 彩色流光细线**（按各 Agent 等级上色），
  并置 `WS_EX_TRANSPARENT` **鼠标穿透**，不挡底层点击；鼠标碰到该边缘热区或**按住 Alt** 才滑出。右键菜单可关闭。

## 二、三级展开状态机（照搬 `HoverMachine.swift`）

原版注释写得很清楚：*"On a dock that separates its agents, hovering only peeks; a click asks for the agent's whole panel."*
本 HUD 的 5 个 Logo 就是"分离式 Dock"，所以：

| 级别 | 触发 | 表现 | 截图 |
| --- | --- | --- | --- |
| **1 静默** | 无 | 只显示贴边紧凑条（或 3px 细线） | ↑ 见上 |
| **2 单行 Hover Line** | 鼠标移到某个 Logo（`peek_delay` 0.1s） | **不弹大面板**，紧挨 Dock 滑出一条**与 Dock 等高**的单行胶囊，内容随指针在 Logo 间**实时切换** | ![hover](screenshot-hover.png) |
| **3 完整面板** | **左键点击图标** / **按住 Alt 悬停** / **静止 1.6s** | 向屏幕内侧展开完整卡片面板 | ![panel](screenshot.png) |

单行内容形如 `Codex · 5h 100% (42m) · 7d 53% (5d 21h)`（窗口名 + 已用百分比 + 重置倒计时），
无额度窗口的 Agent 显示余额或今日 Token。

**收起**：指针移出 Dock+面板区域后按 `collapse_delay`（250ms）防抖，单行条与大面板**一起平滑收起**；
再停留 `HIDE_AFTER`(0.6s) 后整条滑回屏幕边缘只剩 3px。状态机本身与 Swift 完全一致
（`collapsed / opening / open / closing` + `pointerEntered / pointerExited / timerFired / forceOpen / forceCollapse`）。

## 三、紧凑条、Apple Watch 式圆环与六套动效

### 3.1 圆环：锥形渐变 + 发光端点

圆环不是一根纯色实线，而是 `QConicalGradient`：从**尾端 72% 亮度**顺时针渐变到**头部满色**，
所以一段 14% 的弧读起来是"从暗到亮、收在自己头上的一道光"，而不是一截死板的断线。
弧线末端**不再画发光圆点**（那颗球视觉上过重，已移除），领跑的渐变头部本身就是高光。

* 弧统一从 **12 点方向（`startAngle = 90 * 16`）顺时针**起笔，`spanAngle` 取负值；
* 环的填充是**剩余量**（几乎没用的窗口画满圈，用光的画不出东西）；
* 颜色取该 Agent 的等级色（绿 / 黄 / 红 / 灰）。

### 3.2 六套动效

| # | 动效 | 时长 / 曲线 | 说明 |
| --- | --- | --- | --- |
| **1** | **启动入场** | 420ms `OutBack` | 胶囊从贴边处**滑入 16px → 0**，宽度从中心撑开、透明度 0 → 1，末尾微回弹 |
| | **错峰扫环** | 每槽 +70ms，650ms `OutCubic` | 首帧数据到达前，外围环转一段 **90° 彗星拖尾**加载弧；数据到了就从 0% 扫到真实剩余量并定格 |
| | **唤醒扫环** | 每槽 +**35ms**，**480ms** `OutCubic` | **只在「贴边隐藏 → 展开可见」那一下**触发：所有在线环先归零，再顺时针扫到各自真实刻度，图标旁百分比同步 **count-up**（0% → 目标）。程序首次出现与**双击手动刷新**同样触发；Dock 已可见时在图标间移动**不会**重置重扫 |
| **2** | **呼吸流光** | 3600ms 正弦（`InOutSine`） | 胶囊上沿的状态色细线慢速呼吸；有会话在跑时加快到 **1200ms** |
| | **刷新追逐波** | 600ms | 双击图标时每颗环跑一道顺时针高光 |
| **3** | **图标磁吸** | 150ms `OutQuad` | 指针所在图标与环放大到 **1.12×**，其余降到 **0.78** 透明度 |
| | **单行条形变** | 180ms `OutQuart` | 从图标 A 滑到 B 时**同一个胶囊**把宽度从 A 的文案长伸缩到 B，文字淡入切换——**绝不闪烁关闭再打开** |
| **4** | **面板出入场** | 开 240ms `OutCubic` / 关 160ms `InCubic` | 透明度 0 → 1 同时表面从贴边处**长出 12px**；收起是淡出缩回，不生硬闪现 |
| | **进度条生长** | 380ms `OutQuart` | 每次打开面板，卡片里的横向额度条从 0 滑到目标长度 |
| | **柱状图生长** | 320ms `OutCubic` | 96 格 24h 柱状图从底部向上长出来 |
| **5** | **数值补间** | 500ms `InOutCubic` | 刷新拿到新百分比时，环角度与卡片进度条从旧值滑到新值 |
| **6** | **贴边过渡** | 收 300ms `InOutCubic` / 出 220ms `OutBack` | 隐身时向边缘收拢成 3px 流光细线，唤醒时从边缘**弹性滑出**再落位 |

### 3.3 性能：静止时是 0

所有离散变化都交给 `QVariantAnimation`（Qt 用**一个内部定时器**驱动全部动画，跑完自己停），
界面里只有**一个**帧定时器负责环境动效，并且会自降速、自停：

| 状态 | 帧率 | 实测 CPU（单核占比） |
| --- | --- | --- |
| 有动画在跑（入场 / 扫环 / 形变 / 面板） | 16ms ≈ 60fps | 瞬时 |
| Dock 可见且空闲（呼吸） | 66ms ≈ 15fps | ~0.3% |
| **贴边隐身**（只剩细线呼吸） | 200ms ≈ 5fps | **~0.5%**（整机 16 核的 0.03%） |
| **贴边隐身且 `peek_sliver: 0`** | **停表** | **0%** |

关键的三条代价控制：

1. **面板打开时不做环境重绘**——面板是一大块半透明表面、上面还压着投影效果，
   为背后那点呼吸反复重绘它，实测要吃掉单核 **9%**；停掉后整个面板打开状态降到 **0.27%**；
2. **淡入淡出用完即拆**——`QGraphicsOpacityEffect` 只在 240ms 过渡期间挂上，
   动画一结束立刻摘掉，稳态绘制不走离屏缓冲；
3. **帧定时器自停**——隐身时直接 `stop()`，由 `_wake()` 在数据到达 / 唤出 / 悬停变化时重新点火。

右键菜单仍可切换 **Ring（圆环）** 与 **Glow（流光）**；Glow 模式下同样叠了一道细环显示进度。

## 四、主动提醒条（Reminder Line）

与 Dock 等高的单行胶囊，5 秒后自动收起，触发条件：

* 某窗口**从 <90% 越过 90%**（额度告警）；
* 刷新时检测到**额度刚被重置**（`used` 下降超过 5 个点，同 `AlertPolicy.resetRise`）；
* **本地会话完成一轮回复**（Token 增长停止）。

![reminder](screenshot-reminder.png)

## 五、花费统一成美元

各家的账本记的是自己的币种——DSH 鲸鱼账本是 **CNY**，OpenCode 数据库是 **USD**——
所以汇总卡不再并列 `≈¥82.19 + ≈$1.7`，而是按配置的汇率**折成一个美元数**（如 `≈$13.66`）。
汇率在 `config.json` 的 `currency.usd_cny`（CNY per 1 USD，默认 7.14）。
认不出的币种**不猜**，宁可显示 `--`；数据层仍是原始金额与原始币种，汇率只作用在渲染这一步。
Dock 槽位里的余额同样折算（`¥142` → `$19.9`）。

## 六、面板补齐：24h Token 柱状图 与 Burn rate 耗尽预警

* 被聚焦的 Agent 卡片底部多一条 **24 小时 / 每 15 分钟一格**的 Token 柱状图（96 格）。
* 数据来自 `windows/history.py`：每次刷新采样各 Provider 的"今日 Token"，取差值入桶；
  **首次打开**会用 Codex 本地 rollout 日志里带时间戳的 `token_count` 事件回填（同一份本机数据，不是造数）。
* **Burn rate 预警**：按最近 3 小时的 `已用%` 斜率推算"按当前速度还有多久耗尽"，
  若早于该窗口的重置时间，就在卡片里用警示色写出 `按当前速度，5h 额度约 2h 12m 后耗尽（8.3%/小时）`。

---

## 快速开始

| 做什么 | 怎么做 |
| --- | --- |
| **静默启动（推荐）** | 双击 **`run.vbs`**（无控制台窗口） |
| 静默启动（备选） | 双击 `start.bat` |
| 带控制台启动（排错） | `start.bat console` |
| 停止 HUD | `start.bat stop` |
| 安装 / 升级 PySide6 | `start.bat pip` |
| 命令行看一眼 5 个工具的额度 | `start.bat once` 或 `python check.py` |
| 离线自检（解析器 + 停靠几何） | `python selftest.py` |

> `start.bat` 会自动挑选**装了 PySide6 的解释器**（先试 `%LOCALAPPDATA%\Programs\Python\Python3*`，
> 再试 `py -3`、conda、PATH），所以同时装了多个 Python 也不会启动失败。

### 常驻守护（`watchdog.py`）

`run.vbs` / `start.bat` 默认启动的是 **`watchdog.py`** 而不是 `hud.py`：它负责拉起 HUD、等它退出、
**把退出码和 stderr 写进 `hud.stderr.log` 后自动重启**（连续 3 次秒退则退避 60 秒，避免崩溃循环）。
HUD 自己的右键菜单里点"退出"会写一个 `.hud-quit` 标记，守护进程看到就真的停下，不会又把它拉起来。

HUD 自身也做了两处加固：所有定时回调（悬停、时钟、动画、采集）都包了 `_guarded()`，
单个 Provider 出问题只记日志不会带走整个进程；`sys.excepthook` + `aboutToQuit` 会把未处理异常和
事件循环退出的原因写进 `hud.log`。

鼠标：**拖动**换边（松手自动吸附），**点击 Logo** 展开/收起面板，**右键**打开菜单
（立即刷新 / 显示百分比 / 圆环·流光 / 停靠位置：顶部刘海·上·下·左·右 / 贴边隐身 / 打开配置 / 打开日志 / 退出）。
托盘图标左键显示隐藏、右键同一套菜单，颜色跟随最差等级。

---

## 配置（`windows/config.json`）

```json
{
  "refresh_seconds": 120,
  "panel":  { "always_on_top": true },
  "dock": {
    "mode": "dock",          // dock | notch（刘海模式：贴死屏幕顶边，上两角直角）
    "edge": "top",           // top | bottom | left | right
    "offset": 0.5,           // 沿边位置 0~1
    "inset": 3,              // 与屏幕边缘的距离
    "show_percent": false,   // 是否在图标旁显示百分比
    "style": "ring",         // ring | glow
    "edge_peek": true,       // 贴边隐身（只留流光细线 + 鼠标穿透）
    "peek_sliver": 3,        // 隐身时留在屏上的像素数；0 = 完全缩进屏幕
    "hover_delay": 1.6,      // 静止多久后自动展开大面板（点击/Alt 立刻展开）
    "peek_delay": 0.1,       // 单行 Hover Line 出现的延迟
    "collapse_delay": 0.25   // 移开后收起的防抖时间
  },
  "currency": { "usd_cny": 7.14 },   // CNY per 1 USD，所有花费都按它折成美元
  "providers": {
    "codex":       { "enabled": true, "executable": null, "timeout": 30 },
    "opencode":    { "enabled": true },
    "antigravity": { "enabled": true, "timeout": 20 },
    "workbuddy":   { "enabled": true, "electron_paths": [], "tokens": {} },
    "dsh":         { "enabled": true, "homes": [] }
  }
}
```

`providers.<id>.enabled: false` 可以让某个工具**完全不采集、不显示**。
WorkBuddy 手填 Token 兜底：

```json
{
  "providers": {
    "workbuddy": {
      "electron_paths": ["D:\\workbuddy\\WorkBuddy.exe"],
      "tokens": {
        "cn":     { "access_token": "…", "uid": "b0fc80b1-…", "domain": "www.workbuddy.cn" },
        "global": { "access_token": "…", "uid": "1ddb2466-…", "domain": "www.workbuddy.ai" }
      }
    }
  }
}
```

**Windows 静默**：所有子进程（codex app-server、WorkBuddy Electron、PowerShell 探针）都带
`CREATE_NO_WINDOW` + `STARTF_USESHOWWINDOW`，定时刷新时桌面不会闪黑框。

---

## 文件结构

```
windows/
  run.vbs            双击静默启动（wscript，无窗口）
  start.bat          启动器（silent / console / once / pip），自动挑带 PySide6 的解释器
  hud.py             窗口装配、托盘、刷新调度、提醒
  placement.py       停靠几何（NotchGeometry.swift 的移植）
  hover.py           悬停状态机（HoverMachine.swift 的移植）+ 三级展开
  dock.py            紧凑图标条 + 锥形渐变进度环 + 唤醒扫环（横排/竖排、Ring/Glow）
  peek.py            单行 Hover Line 与提醒条
  panel.py           完整卡片面板 + 24h Token 柱状图 + Burn rate 预警
  history.py         本机用量采样（24h/15min 直方图、斜率）
  motion.py          动画基元（QVariantAnimation/Scalar/淡入淡出）
  money.py           汇率与美元格式化
  motion.py          动画基元（QVariantAnimation / Scalar / 淡入淡出）
  ui_theme.py        设计令牌、字体、品牌图标缓存
  check.py           一次性诊断：查询并打印 5 个 Provider
  selftest.py        离线自检（解析器 + 停靠几何 + 三级悬停 + 计价/圆环/动效参数）
  config.py/model.py 配置与数据模型
  winx.py            隐藏子进程、路径、TCP 监听表（数据采集层）
  providers/         codex / opencode / antigravity / workbuddy / dsh
```

---

## 数据采集层（本轮未改动）

| 工具 | 来源 | 端点 / 机制 |
| --- | --- | --- |
| **Codex** | `%USERPROFILE%\.codex`、`%LOCALAPPDATA%\OpenAI\Codex\bin\*\codex.exe` | `codex app-server --listen stdio://` → `initialize` / `account/rateLimits/read` / `account/read` |
| **OpenCode** | `auth.json`、`opencode.jsonc`、环境变量、DSH `.credentials.yaml` refs | `GET https://opencode.ai/zen/go/v1/usage`（需浏览器系 UA，否则 Cloudflare 403）、GLM、Kimi |
| **Antigravity** | `%USERPROFILE%\.gemini` + `Get-CimInstance Win32_Process` + `GetExtendedTcpTable` | `POST https://127.0.0.1:<port>/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary`，回退 `GetUserStatus` |
| **WorkBuddy** | `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\*.info` | 解密 `$wbEncrypted`（Electron `atRestSecretKey` + AES-256-GCM）→ `POST /v2/billing/meter/get-user-resource` |
| **DSH** | `%DSH_HOME%` 与 `%APPDATA%\ai.deepseek.harness.desktop\dsh` | `GET https://api.deepseek.com/user/balance` + `.dshw-usage.json` + 挂载插件 |

> `providers/*`、`winx.py`、`model.py` 本轮一行未动。新增的只有 `placement.py / hover.py / dock.py /
> peek.py / panel.py / history.py`，以及 `hud.py` 的重写与 `ui_theme.py` 的少量补充。

---

## 稳健性（长时间运行 + 多显示器）

* **看门狗**：`run.vbs` 拉起的是 `watchdog.py`，HUD 退出会被记录原因并自动重启（见上文）。
* **定时回调全部包 `_guarded()`**：任何一次刷新、渲染或悬停出错都只记日志，不会带走进程。
* **心跳**：`hud.log` 每 10 分钟写一行 `heartbeat level=... ok=n/5 revealed=...`，
  即使没有外部监控，日志本身也能证明事件循环还在转。
* **多显示器**：停靠几何全部以"当前屏幕矩形"为基准（不假设原点在 `(0,0)`），
  副屏在主屏左侧/上方（负坐标）时吸附、贴边隐身与唤醒热区都正确；
  分辨率变化或窗口换屏时会自动重新贴边。
* **空闲开销**：隐藏成一个 3px 细线时，实测 **0.13% 单核占用**（150 秒窗口），
  含定时刷新（每 120 秒拉一次 Codex/OpenCode/WorkBuddy/DSH，Antigravity 未运行时每 30 秒重试）的平均值约 0.4~0.9%。
  为此做过三处削减：悬停轮询在隐藏时从 60ms 放宽到 160ms；动画计时器在无运行会话且非流光模式时从 40ms 放宽到 750ms；
  `recent_tokens` 改为从事件尾部倒序扫描（原先每 40ms 都要遍历上千条 Token 事件，是最大的一处浪费）。
* **回归测试**：`python selftest.py` 覆盖
  **三级悬停状态机**（19 项：悬停只 peek、静止才开面板、点击立即开且可切换、空处悬停不武装计时器、提前触发的定时器保留原 deadline）
  与**停靠几何**（7 种屏幕尺寸含负原点 × 4 边 × 2 模式 × 5 个 offset，加 10 例拖拽吸附，共 571 项断言），
  另含 Antigravity 的进程/端口/响应解析。全部无需鼠标、无需联网、结果确定。

## 已知边界

* Antigravity 只在**运行中**才可读；未运行时 Dock 显示 `--`，面板给出提示（本轮已实机跑通：Gemini / 第三方 双额度池）。
* Codex 的"今日 Token"含缓存输入，口径与 Codex 客户端一致。
* "有活跃会话"用最近 4 分钟 Token 是否增长判定（Provider 层不暴露 running turn），据此驱动脉动与流光加速。
* "会话完成一轮回复"用 Token 增长停止近似；DSH 另有 `.dshw-turn.json` 的轮次计数可精确判定。
* 24h 柱状图需要两次采样才有差值；Codex 首次会用本机 rollout 日志回填，其余 Provider 随使用逐步累积。
* 原仓库其余 10 多个 Provider 不在本 Windows 版内；原 Swift 源码保持原样，macOS 构建不受影响。
