# FlightLink 1.0 前端与 Python 桌面端集成指南

> 对应契约文件：[api/openapi.yaml](api/openapi.yaml)
> 适用对象：FlightLink Web 控制台、未来 Python 桌面端（PySide6 / QML）及本地自动归档同步模块（参考实现：[examples/python_archive_sync.py](examples/python_archive_sync.py)）。

---

## 1. 全局约定与安全边界

1. **基础路径与传输安全**：
   - 所有业务接口位于 `/api/v1`。
   - 管理入口默认仅监听本机（`127.0.0.1`）或受限管理网；远程访问**必须使用 HTTPS**。
2. **标识符与时间**：
   - 所有资源 ID（`ecs_instance_id`、`group_id`、`operation_id`、`session_id`、`file_id`）均为**不透明字符串**；组名与端口可变，不可作为身份。
   - 删组后同名同端口重建必得新 `group_id`，前端不得按名称或端口合并历史与运行态数据。
   - 所有时间均为 **UTC RFC 3339**（以 `Z` 结尾），由客户端按本地时区展示。
3. **只读无副作用保证**：
   - 调用 `/health`、`/overview`、`/groups`、`/logs`、`/archive/*` 等任意 `GET` 请求，绝不启动 Router 监听、日志采集或云端写同步。
4. **统一错误响应**：
   ```json
   {
     "error": {
       "code": "CONFIG_VERSION_MISMATCH",
       "message": "Group configuration version has changed; refresh group details before retrying.",
       "retryable": false,
       "details": [{"field": "config_version", "message": "expected=1, current=2"}],
       "request_id": "req_01JABC009"
     }
   }
   ```

---

## 2. 认证、令牌失效与读写权限

正式版**完全禁用预览演示账号**。初始管理员通过 `flightlink init` 或 `flightlink user add` 创建。

- **网页端 / Python 桌面端**：
  1. `POST /api/v1/auth/login` 获取 `access_token`、`refresh_token`、`expires_at`、`user`。
  2. 所有受保护接口携带 `Authorization: Bearer <access_token>`（**已删组的历史归档与文件读取同样必须鉴权**）。
  3. 遇到 `401 TOKEN_EXPIRED` 时调用 `POST /api/v1/auth/refresh` 轮换令牌；刷新失败则清态回登录页。
  4. 退出时调用 `POST /api/v1/auth/logout` 吊销 `refresh_token`。
- **无界面 Python 同步脚本**：
  - 使用 `Authorization: Bearer <token>`（支持登录令牌或 CLI 签发的可吊销只读 `viewer` 令牌 `flk_...`）。
- **权限角色**：
  - `admin`：全量读写与操作重试。
  - `viewer`：全量只读（总览、端口组、实时日志、归档浏览、增量清单、鉴权文件读取）；调用写接口返回 `403 INSUFFICIENT_ROLE`。

---

## 3. 按页面集成顺序与轮询频率

### 3.1 登录页
1. （可选）`GET /api/v1/health` 检测连通性与延迟。
2. `POST /api/v1/auth/login` 登录并校验角色。
3. 登录成功后启动后台归档增量同步循环（见第 6 节）。

### 3.2 总览页（`#/overview`）
- 调用：`GET /api/v1/overview`（建议 `2–3s` 轮询；页面隐藏或用户暂停实时更新时停止）。
- 展示 `components`（`flightlink-api`、`flightlink-agent`、`flightlink-archive`）、`disk` 水位与各组摘要；若 `stale === true`，明确标注“观测数据已过期”及 `observed_at`。

### 3.3 端口组列表与详情页（`#/groups`、`#/groups/{group_id}`）
- 列表：`GET /api/v1/groups?q=&filter=all`（`2–3s`）。
- 详情：`GET /api/v1/groups/{group_id}`（`1–2s`）。
- **三项服务独立表达**：`router`、`mavproxy`、`security_group` 各自带 `state`、`observed_at`、`stale`。
- **12 项遥测空值约定**：无可靠真实来源时一律返回 `null`，前端显示 `—`，**绝不将 `null` 伪装为 `0` 或 `false`**。
- **地面站指令**：`recent_gcs_commands` 严格只读观测，不提供指令发送入口。
- **已删除组契约**：`GET /api/v1/groups/{group_id}` 返回 `404 GROUP_NOT_FOUND` 时，查询 `GET /api/v1/archive/groups/{group_id}`；若返回 `200`，展示“端口组已删除，历史归档仍可查看”并跳转 `#/history/{group_id}`。

---

## 4. 异步写操作、幂等键与异常处理

所有写接口（`POST /groups`、`PATCH/DELETE /groups/{id}`、`POST /groups/{id}/actions/{action}`、`POST /operations/{id}/retry`）均返回 **`202 Accepted`**：

1. **`202` 是已受理，不是成功**：客户端按 `operation.operation_id` 轮询 `GET /api/v1/operations/{operation_id}`（前 5s 每 `500ms`，之后 `1–2s`）直至终态（`succeeded` / `failed` / `needs_verification` / `cancelled`）。
2. **`Idempotency-Key`**：每次新建提交生成唯一 UUIDv4；网络超时重发同一提交时复用原键；修改任何请求字段必须换新键，否则服务端返回 `409 IDEMPOTENCY_CONFLICT`。
3. **版本并发检查**：编辑/删除时附带 `If-Match: "v<config_version>"` 或 `expected_config_version`，不匹配返回 `412 CONFIG_VERSION_MISMATCH`，前端提示刷新后再编辑。
4. **操作待核实（`needs_verification`）**：展示各 `steps` 明细与 `retry_hint`，引导管理员在核实云端/系统实态后通过 `POST /api/v1/operations/{id}/retry` 继续核验或补清理，绝不猜测成功或重复新建。

---

## 5. 实时日志页（`#/logs`）

- 调用：`GET /api/v1/logs?group_id=&source=all&level=all&q=&cursor=&limit=100`（`1–2s`）。
- 无新日志返回 `items: []` 与原 `next_cursor`。
- **清屏**与**暂停滚动**均为客户端本地行为，不请求后端写接口。
- 收到 `410 CURSOR_EXPIRED` 时重置 `cursor=""` 并提示已跳过早期实时缓冲。
- `GET /api/v1/logs/export` 仅作 CLI/排障显式导出，不作为桌面端常规 UI 按钮。

---

## 6. 历史归档与未来 Python 桌面端默认自动保存

### 6.1 历史页面浏览（只读元数据 + 展开分页正文）
- 列表：`GET /api/v1/archive/groups?include_deleted=true`（支持活动空态组与已删组，带 `name_snapshot` 与端口快照，不返回正文）。
- 切片列表：`GET /api/v1/archive/groups/{group_id}/sessions?limit=30`（不返回正文）。
- 展开正文：`GET /api/v1/archive/sessions/{session_id}/entries?source=router|mavproxy&cursor=&limit=200`。

### 6.2 桌面端默认自动保存与非阻塞增量同步（核心契约）
1. **无常规下载交互**：未来 Python 桌面端默认自动保存每个已封存飞控会话，分别保存 **Router systemd 日志（`router.log`）**、**MAVProxy 会话日志（`mavproxy.log`）**和**切片元数据（`metadata.json`）**。正常流程**无下载开关、无导出按钮、无保存对话框、无来源选择**。
2. **本地路径由客户端解析**：Windows 默认保存至系统“文档”目录下的 `FlightLink/Logs`（客户端调用系统 API 解析，不由 ECS 下发）；其他 OS 按本地规范解析。建议子目录：`<LocalLogsRoot>/<ecs_instance_id>/<group_id>/<session_id>/`。
3. **只查增量元数据清单 + 持久化任务后再推进游标**：
   - 轮询 `GET /api/v1/archive/changes?cursor=<saved_cursor>&limit=50`（建议 `10–30s`）。
   - 该接口只返回**已封存**切片元数据（稳定实例/组/切片/文件 ID、大小、SHA-256、到期时间及来源缺口），不传正文、不启动采集。
   - **先**将发现的切片与待拉取文件持久化到本地任务库（按 `(ecs_instance_id, session_id, source)` 去重），**再**推进并保存 `next_cursor`。单切片失败绝不阻塞后续切片发现，也不会因游标推进而丢单。
4. **单文件鉴权读取与原子落盘**：
   - 自动保存与手动重试共用同一同步任务，逐个拉取 `GET /api/v1/archive/sessions/{session_id}/files/{file_id}`。
   - 先写同目录临时文件（`.part`），校验字节长度与 SHA-256（对照清单及 `X-Content-SHA256` 头）完全一致后原子改名（`os.replace`）。
   - **已校验文件永不重下**；重启或手动重试仅补拉缺失文件。
5. **本地保存状态权威 & 严格区分两类问题**：
   - 本地保存状态由客户端根据实际写盘与校验结果维护，**绝不由 ECS 以 HTTP 200 推断“已保存到本地”**。
   - **云端本身缺少某路日志**（`availability === "missing_on_server"` 或 `degraded_quota_exceeded`）：标记为 `saved_with_source_gaps`（标明缺少来源与 `missing_reason`），**绝不显示为完全保存成功，也不提示用户重试**。
   - **客户端下载失败**（网络/5xx/校验不匹配/磁盘权限/磁盘满）：仅在自动重试最终失败后展示失败提示与**「重新下载日志」**，点击跳转至对应历史切片；手动重试仅补缺失文件，并精准区分原因：
     - 网络异常 → 提示网络恢复后点击「重新下载日志」
     - 磁盘权限不足（`PermissionError`/`EACCES`）→ 提示检查目录写权限
     - 磁盘空间不足（`ENOSPC`）→ 提示清理磁盘空间
     - 云端已过期（`410 ARCHIVE_EXPIRED`）→ 提示云端超过 7 天保留期，**停止重试，不循环请求**
6. **删组与 7 天留存**：
   - 删组后未到期切片仍可通过 `/archive/changes` 与 `/archive/sessions/{session_id}/files/{file_id}` 正常保存或手动补下载。
   - 云端 7×24h 到期清理绝不删除客户端本地副本。
   - 客户端离线超过保留期导致 `GET /archive/changes` 返回 `410 CURSOR_EXPIRED` 时，记录留存窗缺口提示“可能缺失早期切片”，随后以 `cursor=""` 重新对账（自动跳过本地已校验文件）。
7. **CLI / 兼容下载接口**：
   - `GET /api/v1/archive/sessions/{session_id}/download?source=router|mavproxy|metadata|all` 仅保留供 CLI（`flightlink history export`）或兼容用途，不作为桌面端常规按钮。

---

## 7. HTTP 状态码速查

| 状态码 | 典型 `error.code` | 前端 / 客户端处理 |
|---|---|---|
| `202` | — | 写操作已受理，轮询 `GET /api/v1/operations/{operation_id}` |
| `401` | `AUTH_REQUIRED` / `TOKEN_EXPIRED` / `INVALID_CREDENTIALS` | 刷新令牌，失败则重登 |
| `403` | `INSUFFICIENT_ROLE` | 提示无写权限 |
| `404` | `GROUP_NOT_FOUND` / `ARCHIVE_SOURCE_MISSING` / `SESSION_NOT_FOUND` | 组 404 时转查归档；云端缺源不重试 |
| `409` | `PORT_CONFLICT` / `GROUP_NAME_CONFLICT` / `GROUP_LIMIT_REACHED` / `IDEMPOTENCY_CONFLICT` / `GROUP_BUSY` / `GROUP_DELETING` | 展示明确冲突原因，不盲目重试 |
| `410` | `ARCHIVE_EXPIRED` / `CURSOR_EXPIRED` | 已过期停重试并标缺口；游标失效记缺口并空游标对账 |
| `412` | `CONFIG_VERSION_MISMATCH` | 刷新组详情后再提交 |
| `422` | `VALIDATION_ERROR` | 高亮表单校验错误 |
