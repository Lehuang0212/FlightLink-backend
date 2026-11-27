# FlightLink Desktop 1.0｜本地桌面端工作描述与后端接口交接包

> **用途**：本文件是 FlightLink ECS 后端 1.0（已交付并通过真机验收）交付给本地桌面端开发 / 规划（如 GPT 制定实现 Plan）的权威工作描述与通信契约索引。
> **界面与交互参考（只读，严禁修改该目录）**：`D:\Workspace\Mavproxy\FLightLink1.0-preview`
> **配套权威交互文件（建议与本文件一并交给规划/开发侧）**：
> 1. [api/openapi.yaml](api/openapi.yaml) — OpenAPI 3.1 完整接口契约（已与后端路由零漂移校验，25 个接口）
> 2. [frontend-integration.md](frontend-integration.md) — 分页面调用顺序、轮询频率、异步操作与自动归档规则
> 3. [examples/python_archive_sync.py](examples/python_archive_sync.py) — Python 本地自动保存与增量同步参考实现（可直接模块化复用）

---

## 1. 技术栈与架构硬约束

| 维度 | 强制要求 |
|---|---|
| 界面层 | **PySide6 + Qt Quick / QML**（使用 `QGuiApplication` + `QQmlApplicationEngine`） |
| 业务层 | **Python 3.11+**（HTTP 客户端、认证令牌管理、轮询调度、异步操作追踪、本地 SQLite 同步状态机、文件原子落盘与 SHA-256 校验） |
| 严禁引入 | **禁止引入 `Qt Widgets`（`QApplication`/`QWidget`/`QMainWindow`）、禁止引入 `PyQt6`、禁止引入 `QtWebEngine`/Electron/内嵌浏览器** |
| 目标平台与窗口尺寸 | 优先 **Windows 10/11 `x86_64`**；主验收窗口尺寸：`1280×800`、`1440×900`、`1600×1000`（无横向溢出，支持缩小/最大化/还原/关闭及键盘快捷键） |
| 线程与响应性 | 所有网络请求（建议 `httpx`）与磁盘文件流式写入/SHA-256 校验必须在后台线程 / 工作器执行，**绝不阻塞 Qt 主线程 UI 渲染** |
| Python ↔ QML 桥接 | 使用 `QObject` ViewModel（`@Property` / `Signal` / `@Slot`）与 `QAbstractListModel`（端口组列表、实时日志 500 行环形缓冲、历史切片列表、切片日志分页），避免整列表重绘丢焦点或滚动跳动 |
| 项目隔离 | 桌面端必须建立在独立仓库/目录中，**严禁向 `D:\Workspace\Mavproxy\FLightLink1.0-preview` 或 `FlightLink-backend` 后端仓库写入桌面端代码** |

---

## 2. 界面与交互参考基线（对齐 `FLightLink1.0-preview`）

桌面端视觉体系、信息层级与页面结构直接对齐 `D:\Workspace\Mavproxy\FLightLink1.0-preview`（重点参考 `index.html`、`styles.css`、`app.js`、`out/p2-final-验收.md` 及 `out/p2-final-*.png` 截图）：

### 2.1 QML Theme 直接映射预览版 CSS Token
`FLightLink1.0-preview/styles.css` 已按 QML Theme 规范命名，可直接建 `Theme.qml` 单例一一对应：
- **配色**：`bg: #F8FAFD`、`surface: #FFFFFF`、`surfaceTint: #F0F4F9`、`field: #E9EEF6`、`divider: #E1E5EA`、`outline: #C4C7C5`、`primary: #0B57D0`、`onPrimary: #FFFFFF`、`primaryContainer: #D3E3FD`、`onPrimaryContainer: #041E49`、`fab: #C2E7FF`、`text: #1F1F1F`、`text2: #444746`、`text3: #747775`；语义色 `ok (#146C2E / #C4EED0)`、`warn (#8A5300 / #FFE7B8)`、`bad (#B3261E / #F9DEDC)`、`idle (#747775 / #EEF0F2)`；连线流光仅用于链路光点（`#4285F4 -> #9B72CB -> #D96570`），暂停实时更新或系统减少动画时停止/隐藏。
- **字号 7 档**：严格保留 `11/16`、`12/18`、`13/20`（默认正文）、`14/20`、`16/24`、`22/28`、`24/28`；等宽字体用于端口、SYSID、时间戳、日志正文。
- **布局尺寸**：标题栏 `40px`、底部状态栏 `24px`、左侧导航栏 `208px`（`<=1360px` 缩至 `192px`）、内容边距 `24px/20px`、列间距 `16px/12px`、日志行高 `20px`。

### 2.2 核心页面与交互清单（6 大视图 + 弹窗）
1. **登录视图（Login）**：
   - 居中 `360px` 卡片，支持密码显隐、错误提示、回车登录；
   - **正式版改动（相对预览版）**：**移除**“以演示模式进入”与预填演示账号 `admin/flightlink`；新增**服务器地址输入**（支持 `https://host[:port]` 或本机隧道 `http://127.0.0.1:8000` / 自定义端口如 `http://127.0.0.1:18080`，默认端口 `8000`，连接前可调用 `GET /api/v1/health` 测连通与延迟；记住最近服务器地址，不明文存储密码）。
2. **主壳框架（AppShell）**：
   - 顶部标题栏：应用标识、全局搜索框（`Ctrl+K` 搜索端口组/端口/日志）、窗口控制按钮；
   - 左侧边栏：ECS 连通状态与延迟（ms）、`新建端口组` FAB 按钮、4 个主导航（`总览` / `端口组` / `日志` / `历史日志`，支持 `Ctrl+1~4`）、3 组件服务状态汇总（`flightlink-api` / `flightlink-agent` / `flightlink-archive`）、左下角当前用户菜单（角色显示、退出登录）；
   - 底部状态栏：ECS 延迟、`实时更新 / 暂停` 开关、最近同步时间、自动归档同步状态摘要（**仅在有待重试/最终失败时显眼提示**）、版本号。
3. **总览页（Overview）**：
   - 首屏可见所有已配置端口组链路卡片（最多 5 组）、三组件健康与磁盘水位；无告警铃、无抓包入口、无“最近事件”冗余区；若任意组件或组 `stale === true`，明确提示观测已过期与 `observed_at`。
4. **端口组列表与新建/编辑/删除弹窗（Groups）**：
   - 搜索 + 状态筛选（`all` / `online` / `idle` / `attention`）；
   - 新建/编辑弹窗仅配**双端口**（DTU 固定 UDP，GCS 可选 TCP/UDP，无第三端口、不手工分配 SYSID）；本地前置校验范围 `1..65535` 与组内两端口不等；支持编辑前调用 `PATCH ...?dry_run=true` 预演是否需要重启 Router 及同步云安全组；
   - 未保存更改关闭弹窗需二次确认；删除需二次确认并提示“删除后未到期历史日志仍保留”。
5. **端口组详情页（Group Detail）**：
   - `1280` 首屏可见三项独立服务状态（`router` / `mavproxy` / `security_group`，各自含 `state`、`observed_at`、`stale`）与 **12 项核心遥测**；
   - `1280` 宽度下 Router / MAVProxy 实时日志用标签页切换，`1440/1600` 下用双窗格并排展示；
   - `recent_gcs_commands` 区域严格只读观测，**不提供发送指令入口**；
   - 支持启/停/重启 Router、手动同步安全组、操作步骤进度条与 `needs_verification` 状态下的「重试核验」。
6. **实时日志页（Logs）**：
   - 按端口组、来源（`all/router/mavproxy`）、级别（`all/INFO/WARN/ERROR/DEBUG`）、关键词筛选；
   - 本地 500 行环形缓冲，支持**清屏**与**暂停滚动**（纯本地行为，不调用后端写接口、不影响云端归档）；
   - 常规界面不放导出按钮。
7. **历史日志与切片详情页（History & History Detail）**：
   - 按端口组浏览（含**已删组**与活动空态组），展示 `name_snapshot` 与端口快照，不按名称或端口合并不同 `group_id`；
   - 切片列表默认只拉元数据，展开单个切片时才分页拉取 `router` / `mavproxy` 正文；
   - 展示**本机自动保存状态**（由本地 SQLite 状态决定），并遵循第 4 节的自动保存与失败补下交互规则。

---

## 3. 正式版相对预览版的关键差异（必须落实）

| 维度 | `FLightLink1.0-preview` 预览版 | FlightLink Desktop 1.0 正式版 |
|---|---|---|
| 认证与连接 | 离线 mock，预填 `admin/flightlink` 和演示模式 | 必须连接真实 ECS `/api/v1`（支持自定义端口，默认 `8000`；远程建议 HTTPS，本机隧道支持 HTTP）；**无演示账号**；`POST /auth/login` + `401 TOKEN_EXPIRED` 自动 `/auth/refresh` + 退出 `/auth/logout` |
| 角色权限 | 仅模拟管理员 | 区分 `admin` 与 `viewer`；`viewer` 隐藏或禁用所有写按钮，后端写接口返回 `403 INSUFFICIENT_ROLE` |
| 写操作语义 | 本地同步直接改状态 | 所有写接口返回 **`202 Accepted`（已受理，非已成功）**，必须轮询 `GET /operations/{id}` 展示步骤，处理 `needs_verification`、`Idempotency-Key`、`If-Match`（`412`） |
| 12 项遥测空值 | 模拟填充数值 | 无真实飞控数据时后端返回 **`null`**，界面严格显示 `—`，**绝不把 `null` 渲染为 `0` 或 `false`** |
| 已删组处理 | 内存中直接移除 | 删组完成后 `GET /groups/{id}` 返回 `404`，客户端自动查 `GET /archive/groups/{id}`，返回 `200` 则提示“端口组已删除，历史归档仍可查看”并跳转历史页 |
| 历史日志保存 | 手动点击单源/合并下载按钮 | **默认后台自动保存**每个已封存飞控会话（`router.log`、`mavproxy.log`、`metadata.json`）到系统“文档”目录；**正常流程无下载开关、无导出按钮、无保存对话框、无来源选择**；仅自动保存最终失败后提示并显示「重新下载日志」 |

---

## 4. 本地自动保存与增量归档同步契约（核心必做）

后端已经实现并测试通过了对应的全部服务端接口与 Python 参考客户端（见 [examples/python_archive_sync.py](examples/python_archive_sync.py)），桌面端应严格遵守以下 7 条不变量：

1. **本地目录由客户端本地解析（不由 ECS 下发）**：
   - Windows 使用系统 API `SHGetKnownFolderPath(FOLDERID_Documents)` 解析当前用户“文档”目录，默认根路径：`<Documents>\FlightLink\Logs`。
   - 推荐目录结构：`<LocalLogsRoot>\<ecs_instance_id>\<group_id>\<session_id>\`，内含：
     - `router.log`（Router systemd 服务日志）
     - `mavproxy.log`（MAVProxy 会话日志）
     - `metadata.json`（切片元数据）
2. **仅通过增量元数据接口发现新封存切片**：
   - 后台定时（建议 `10–30s`）轮询 `GET /api/v1/archive/changes?cursor=<saved_cursor>&limit=50`。
   - 该接口只返回**已封存**切片的元数据与文件清单（不含正文），不触发采集。
3. **先持久化本地待拉取任务，再推进 `next_cursor`**：
   - 本地 SQLite（如 `%LOCALAPPDATA%\FlightLink\sync_state.sqlite3`）按 `(ecs_instance_id, session_id, source)` 去重写入待下载文件任务，提交事务**之后**才更新本地 `sync_cursors.cursor = next_cursor`。
   - 单切片失败**绝不阻塞**后续切片拉取，也绝不会因游标推进丢单。
4. **单文件鉴权下载 + 临时文件 + SHA-256 原子落盘**：
   - 自动保存与手动重试共用同一同步任务模型，逐文件请求 `GET /api/v1/archive/sessions/{session_id}/files/{file_id}`（携带 `Authorization: Bearer <token>`）。
   - 流式写入同目录 `.part` 临时文件，下载完成后核对字节数 `size_bytes` 与 SHA-256（对照清单 `sha256` 及响应头 `X-Content-SHA256`），一致后调用 `os.replace()` 原子落盘。
   - **已校验通过的文件永不重复下载**；客户端重启或用户手动点击「重新下载日志」时，只补拉缺失文件。
5. **本地保存状态权威由客户端维护（不以 HTTP 200 推断已保存）**：
   - `saved`：该切片所有云端可用文件已落盘且 SHA-256 校验通过，且云端无来源缺口；
   - `saved_with_source_gaps`：所有可用文件已落盘校验，但云端本身缺某路源（`availability` 为 `missing_on_server` 或 `degraded_quota_exceeded`）→ 界面标明“已保存（云端缺少某来源）”及 `missing_reason`，**绝不显示为完全保存成功，也不提示用户重试**；
   - `failed`：客户端自动重试耗尽仍失败 → 弹出/显示失败提示与**「重新下载日志」**按钮，点击跳转对应历史切片并仅补拉缺失文件；按错误原因精确提示：
     - `network_error`：网络/服务端异常，提示网络恢复后点击「重新下载日志」
     - `disk_permission_denied`（`PermissionError` / `EACCES`）：提示检查本地目录写权限
     - `disk_full`（`ENOSPC`）：提示清理本机磁盘空间
     - `checksum_mismatch`：文件完整性校验失败，可重试
   - `expired_on_server`（服务端返回 `410 ARCHIVE_EXPIRED`）：提示云端已超过 7 天保留期，**立即停止重试，绝不循环请求**。
6. **删组与 7 天留存**：
   - 删组后未到期切片仍会正常出现在 `/archive/changes` 且可拉取文件（同样必须鉴权）。
   - 云端 7×24h 到期清理绝不删除本机已保存副本。
   - 若客户端离线超过 7 天，`/archive/changes` 返回 `410 CURSOR_EXPIRED`：本地记录留存窗缺口并提示“离线超过云端保留期，可能缺失早期切片”，随后以 `cursor=""` 重新对账（自动跳过本地已校验文件）。
7. **不使用的旧式按钮**：
   - `GET /api/v1/archive/sessions/{session_id}/download` 仅作 CLI/兼容保留，桌面端常规 UI **不调用**它，也不提供“批量导出按钮 / 自动保存开关 / 保存位置弹窗”。

---

## 5. 后端通信接口总览（`/api/v1`，完整定义见 `docs/api/openapi.yaml`）

### 5.1 连接地址与认证头
- **基础地址**：`{scheme}://{host}:{port}/api/v1`（由用户在登录页配置；后端默认端口 `8000`，部署时可通过 `flightlink init --port <PORT>` / `flightlink run --port <PORT>` 自定义任意合法端口）。
- **认证头**：除 `/health`、`/auth/login`、`/auth/refresh` 外，全部接口（含已删组归档与单文件读取）必须携带 `Authorization: Bearer <access_token>`。
- **时间格式**：全部时间戳为 UTC RFC 3339（`Z` 结尾），桌面端转换为本机时区显示。
- **不透明 ID**：`ecs_instance_id`、`group_id`、`operation_id`、`session_id`、`file_id` 均为不透明字符串，严禁做前缀拼接猜测或跨组复用。

### 5.2 全部 25 个接口清单

| 分类 | 方法与路径 | `operationId` | 桌面端用途与轮询/调用策略 |
|---|---|---|---|
| **Health** | `GET /api/v1/health` | `getHealth` | 登录前连通测试、侧边栏/状态栏 RTT 测速（严格只读无副作用） |
| **Auth** | `POST /api/v1/auth/login` | `login` | 正式账号密码登录，返回 `access_token`、`refresh_token`、`expires_at`、`user` |
| | `POST /api/v1/auth/refresh` | `refreshToken` | `401 TOKEN_EXPIRED` 时静默轮换令牌；刷新失败则清态回登录页 |
| | `POST /api/v1/auth/logout` | `logout` | 退出登录并吊销 `refresh_token` |
| | `GET /api/v1/auth/me` | `getCurrentUser` | 启动恢复会话时校验令牌与获取角色（`admin` / `viewer`） |
| **Overview** | `GET /api/v1/overview` | `getOverview` | 总览页（`2–3s` 轮询）：三组件状态、磁盘水位、全局指标、组摘要 |
| **Groups** | `GET /api/v1/groups` | `listGroups` | 端口组列表（`2–3s`，参数 `q`、`filter=all\|online\|idle\|attention`） |
| | `POST /api/v1/groups` | `createGroup` | 创建端口组（`admin`，带 `Idempotency-Key`，返回 **`202`**，初始停机态） |
| | `GET /api/v1/groups/{group_id}` | `getGroup` | 端口组详情（`1–2s`，含 `ETag: "vN"`、三项服务、12 项可空遥测、`recent_gcs_commands`；删组后返回 `404`） |
| | `PATCH /api/v1/groups/{group_id}` | `updateGroup` | 编辑名称/端口/GCS 协议（支持 `?dry_run=true` 预演；正式提交带 `Idempotency-Key` + `If-Match`，返回 **`202`**） |
| | `DELETE /api/v1/groups/{group_id}` | `deleteGroup` | 删除端口组（带 `Idempotency-Key` + 可选 `If-Match`，返回 **`202`**；保留历史归档） |
| | `POST /api/v1/groups/{group_id}/actions/{action}` | `triggerGroupAction` | `action` ∈ `start \| stop \| restart \| sync-security-group \| verify`（带 `Idempotency-Key`，返回 **`202`**） |
| **Operations** | `GET /api/v1/operations` | `listOperations` | 查询最近异步操作记录（可选 `group_id`、`status`、`limit`） |
| | `GET /api/v1/operations/{operation_id}` | `getOperation` | 轮询 `202` 操作状态与 `steps`（前 5s 每 `500ms`，之后 `1–2s` 直至终态） |
| | `POST /api/v1/operations/{operation_id}/retry` | `retryOperation` | 对 `retryable=true` 的 `failed` / `needs_verification` 操作发起重试（返回 **`202`**） |
| **Logs** | `GET /api/v1/logs` | `queryLiveLogs` | 实时游标日志（`1–2s`，`group_id`/`source`/`level`/`q`/`cursor`/`limit`；`410 CURSOR_EXPIRED` 重置游标） |
| | `GET /api/v1/logs/export` | `exportLiveLogs` | CLI/排障专用导出（桌面端常规 UI 不展示按钮） |
| **Archive** | `GET /api/v1/archive/groups` | `listArchiveGroups` | 历史日志左侧/列表页（`include_deleted=true`，仅元数据） |
| | `GET /api/v1/archive/groups/{group_id}` | `getArchiveGroup` | 单组归档元数据（已删组仍返回 `200`，用于 `GET /groups/{id}` `404` 跳转判定） |
| | `GET /api/v1/archive/groups/{group_id}/sessions` | `listArchiveGroupSessions` | 某组历史会话切片分页元数据（不含正文） |
| | `GET /api/v1/archive/sessions/{session_id}` | `getArchiveSession` | 单个切片元数据、来源可用性、缺口与 `files` 清单（不含正文） |
| | `GET /api/v1/archive/sessions/{session_id}/entries` | `getArchiveSessionEntries` | 展开切片时分页读取 `router` / `mavproxy` 日志正文 |
| | `GET /api/v1/archive/changes` | `listArchiveChanges` | **后台自动保存主清单接口**（`10–30s`，仅返回已封存切片元数据与 `next_cursor`） |
| | `GET /api/v1/archive/sessions/{session_id}/files/{file_id}` | `readArchiveSessionFile` | **自动保存与手动补下载主文件读取接口**（返回文件流 + `X-Content-SHA256`） |
| | `GET /api/v1/archive/sessions/{session_id}/download` | `downloadArchiveSession` | CLI/兼容下载接口（桌面端常规 UI 不调用） |

### 5.3 关键数据结构速查
- **`TelemetrySnapshot`（12 项核心遥测 + 链路质量，无真实源时严格 `null`）**：
  `flight_mode`、`armed`、`battery_remaining_pct`、`battery_voltage_v`、`gps_satellites`、`gps_fix_type`、`gps_hdop`、`altitude_msl_m`、`relative_altitude_m`、`ground_speed_mps`、`rx_rate_kbps`、`tx_rate_kbps`、`packets_lost`、`packets_received`、`packet_loss_pct`、`observed_at`、`stale`。
- **`GroupSummary` / `GroupDetail` 三项独立服务对象**：
  `router`、`mavproxy`、`security_group`（各自包含 `state`、`observed_at`、`stale`、`detail` 等）。
- **统一错误体**：
  `{"error": {"code": "...", "message": "...", "retryable": false, "details": [...], "request_id": "..."}}`
- **关键状态码处理**：
  - `202`：已受理，启动 `operation_id` 轮询；
  - `401`（`TOKEN_EXPIRED`）：单飞刷新 `/auth/refresh`，并发请求排队重放，刷新失败回登录页；
  - `403`（`INSUFFICIENT_ROLE`）：提示当前账号仅只读权限；
  - `404`（`GROUP_NOT_FOUND`）：转查 `/archive/groups/{id}`；
  - `409`（`PORT_CONFLICT` / `GROUP_NAME_CONFLICT` / `GROUP_LIMIT_REACHED` / `IDEMPOTENCY_CONFLICT` / `GROUP_BUSY` / `GROUP_DELETING`）：直接展示服务端冲突详情；
  - `410`（`ARCHIVE_EXPIRED` / `CURSOR_EXPIRED`）：过期停重试标缺口 / 游标失效记缺口后空游标对账；
  - `412`（`CONFIG_VERSION_MISMATCH`）：刷新端口组详情并提示版本变更。

---

## 6. 建议提交给 GPT 做桌面端 Plan 时重点明确的分层与里程碑

建议让 GPT 在规划桌面端工程时至少覆盖以下 6 个模块与验收点：
1. **应用骨架与主题系统（QML）**：`QGuiApplication` + `QQmlApplicationEngine`、`Theme.qml`（完整映射 `styles.css` Token 与 7 档字号）、无边框/原生窗口控制、`1280/1440/1600` 响应式断点与全局快捷键（`Ctrl+K`、`Ctrl+1~4`、`Esc`）。
2. **核心通信与会话层（Python）**：自定义服务器 URL/端口连接配置、健康探针与 RTT、`httpx` 后台客户端、Access/Refresh Token 自动单飞刷新、Windows 凭据安全存储（如 `keyring`）、`Idempotency-Key` 与 `ETag/If-Match` 封装、`202` 异步操作追踪器。
3. **响应式状态与模型层（Python ↔ QML）**：`AppController`、`OverviewVM`、`GroupListModel`、`GroupDetailVM`、`LiveLogModel`（500 行环形缓冲 + 本地清屏/暂停）、`HistoryGroupModel`、`HistorySessionModel`、`OperationTrackerModel`。
4. **后台默认自动保存引擎（Python）**：直接复用/适配 `docs/examples/python_archive_sync.py`，集成到本地 SQLite + `QThread` 调度器，向 QML 暴露每条切片的本机保存状态（`saved` / `saved_with_source_gaps` / `failed` / `expired_on_server`）与「重新下载日志」单切片补拉入口。
5. **离线模拟与真机联调测试**：使用 `pytest` + `pytest-qt`（纯 Qt Quick 场景验证）+ Mock HTTP 覆盖全部契约场景（含 401 刷新、409/412 冲突、12 项 `null` 显示 `—`、5 类增量归档同步验收），随后对接已上线的真实阿里云 ECS 后端联调。
6. **Windows 桌面打包**：仅依赖 `PySide6-Essentials` + `PySide6-Addons` 所需 QML 模块（剔除 `QtWidgets`、`QtWebEngine`），输出可直接运行的 Windows 安装包/绿色目录。
