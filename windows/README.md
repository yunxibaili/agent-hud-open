# Agent HUD · Windows 版（PySide6）

把 [jazzenchen/agent-hud-open](https://github.com/jazzenchen/agent-hud-open) 的**额度采集**与**停靠 / 悬停状态机**搬到 Windows：
一个**贴边停靠、绝不悬空**的桌面悬浮 HUD，只盯 **Codex / OpenCode / Antigravity / WorkBuddy / DSH** 这 5 个工具的额度。

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
