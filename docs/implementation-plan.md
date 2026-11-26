# FlightLink ECS 后端 1.0｜实施计划（含已审核增量变更与 P0 环境档案）

> **目标仓库**：`D:\Workspace\Mavproxy\FlightLink-backend`（远端 `https://github.com/Lehuang0212/FlightLink-backend.git`，新后端在 `main` 分支，旧版保留在 `old-version-0.1` 分支）
> **只读交互参考**：`D:\Workspace\Mavproxy\FLightLink1.0-preview`（严禁写入后端代码、Skills 或部署文件）

---

## 一、P0｜项目 Skills 与运行环境核实档案

### 1.1 项目级 Skills（已完成）
- 已按固定提交安装在 `.agents/skills/`，记录于 [dev-skills.lock.md](dev-skills.lock.md)：
  - `fastapi-templates`（MIT，`wshobson/agents @ c96b5f6552dba32e4492307493ee613581430715`）
  - `openapi-spec-generation`（MIT，`wshobson/agents @ c96b5f6552dba32e4492307493ee613581430715`）
  - `property-based-testing`（CC BY-SA 4.0，`trailofbits/skills @ 149d7b21f4067765b82e67029730b42d228ef4b4`，已移除上游 `effort: low` 覆盖项并按条款注明修改）
- 开发资料不进入 ECS 运行包，不做全局安装。

### 1.2 已核实的外部运行环境

| 核实项 | 目标交付 ECS（生产目标） | 阿里云测试 ECS | 开发宿主机 |
|---|---|---|---|
| 主机标识 | `iZ2ze2jnljrud0l8sur3t1Z` | `aliyun-ecs` | 本地 Windows 11 |
| 发行版与版本 | **Ubuntu 26.04.1 LTS** (`resolute`) | **Rocky Linux 9.7** (`Blue Onyx`, `el9`) | Windows 11 |
| CPU 架构 | `x86_64` | `x86_64` | `x86_64` |
| `systemd` 版本 | `259 (259.5-0ubuntu3.4)`（AppArmor） | `252 (252-67.el9_8.6.rocky.0.1)`（SELinux） | — |
| 磁盘容量与旧版状态 | 容量足够，未运行旧版 `0.1` | 实测时核验 | — |
| 阿里云地域 | — | 北京（`cn-beijing`） | — |
| ECS 实例 RAM 角色 | — | `FlightLinkEcsSecurityGroupRole` | — |
| 安全组 ID | — | `sg-2ze93ogksv8g4i7tc8vd`（默认安全组；建议正式/生产另建独立专用业务安全组挂接至同 ECS，以隔离 SSH/管理规则） | — |
| DTU / GCS 来源策略 | `0.0.0.0/0`（显式配置，代码不默认放开全网） | `0.0.0.0/0`（显式配置，代码不默认放开全网） | — |

> **离线包适用性声明**：目标离线包仅针对 **Ubuntu 26.04.1 LTS (`x86_64`)** 对应的 Python ABI 构建；不在未实机验证前声称适用于其他发行版或 Rocky Linux 9.7。

---

## 二、P1｜冻结业务契约与单包三组件架构

### 2.1 部署架构与权限边界
- 单 ECS、Python FastAPI、SQLite（WAL 模式）、本地 systemd、每端口组独立 `mavlink-routerd` 实例，最多 5 组。
- 一个安装包，三个职责组件（不引入 Redis、Celery、Kubernetes、强制 Docker，首版不做抓包服务）：
  1. `flightlink-api`：登录、鉴权、只读查询、受理写操作、日志/归档分页与鉴权文件读取。**不持有 systemd 或阿里云安全组写权限**；API 启动、健康检查与任何 GET 请求不得启动监听、采集或云同步。
  2. `flightlink-agent`：唯一通道变更执行器；按组串行执行持久化操作，管理本产品 Router / 旁路 MAVProxy 实例、端口与专用安全组规则。不得“发现配置文件就自动启动”。
  3. `flightlink-archive`：持续采集 Router systemd 日志与 MAVProxy 会话输出，维护会话切片、索引、原子封存与 7×24h 留存清理。不得重启 Router、改端口或回退配置；无客户端在线也正常采集。
- MAVProxy 为**旁路观察进程**，不串在 DTU → GCS 转发路径中；内部观察端点不占用用户端口、不新增公网安全组规则。

### 2.2 产品与数据身份
- 一台飞机一个端口组（可命名如“一号机”）。`group_id` 不可变；组名和端口可变，不可作身份。删组后同名同端口重建必须获得新的 `group_id`。
- 每组仅配置 `dtu_port`（UDP 入站）+ `gcs_port`（TCP 或 UDP）。端口号在整台 ECS 跨组、跨角色全局唯一。
- `sysid` 仅从真实飞控 MAVLink 消息读取，不由后台或 DTU 链路心跳分配。
- 数据库分表持久化：运行意图、生效配置版本、候选版本、实际观测服务状态、操作及执行步骤、端口预留、归档组身份、会话索引、切片文件清单、采集游标、云安全组规则账本。
- 正式版禁用预览演示账号；管理入口默认本机或受限管理网，远程强制 HTTPS；已删组历史仍须鉴权。

### 2.3 `/api/v1` 契约与已审核增量变更
已冻结于 [api/openapi.yaml](api/openapi.yaml) 与 [frontend-integration.md](frontend-integration.md)：
1. **桌面端默认自动保存**：未来 Python 桌面端默认自动保存每个已封存飞控会话，分别保存 **Router systemd 日志**、**MAVProxy 会话日志**及**切片元数据（`metadata.json`）**。Windows 默认路径为系统“文档”目录下 `FlightLink/Logs`；实际路径由客户端按 OS 解析，**不由 ECS 指定**。正常桌面流程无下载开关、导出按钮、保存对话框或来源选择。
2. **增量清单与鉴权文件读取**：`GET /api/v1/archive/changes` 只返回**已封存**切片元数据（稳定 `ecs_instance_id`、`group_id`、`session_id`、文件 `file_id`、大小、SHA-256、到期时间及来源缺口），看页面和轮询清单不传正文、不启动采集；文件经 `GET /api/v1/archive/sessions/{session_id}/files/{file_id}` 鉴权读取；原有 `GET /api/v1/archive/sessions/{session_id}/download?source=...` 保留供 CLI 或兼容用途，非桌面端常规按钮。
3. **统一同步任务与非阻塞游标**：自动下载与手动重试共用同一同步任务，按 `(ecs_instance_id, session_id, source)` 去重；临时文件校验通过后原子改名，已校验文件不重下；**先持久化待处理任务再推进发现游标**，单切片失败不阻塞后续切片也不丢单；严格区分「云端本身缺少某路日志（`missing_on_server`）」与「客户端下载失败（`download_failed`）」。
4. **本地保存状态权威与失败补下**：保存状态由本地客户端根据实际写盘结果维护，ECS 绝不以 HTTP 200 推断“已保存到本地”。仅自动下载最终失败时显示失败提示和「重新下载日志」并跳至对应历史切片；手动重试只补缺失文件，精准区分网络错误、磁盘权限、空间不足与云端已过期。
5. **删组与 7 天留存**：删组不删历史；云端自闭片起保留 7×24h，到期不删本地副本；客户端离线错过保留窗口时报告缺口、不循环重试已过期文件。

---

## 三、P2–P5 推进与验收门禁

- **P2（生命周期、崩溃恢复与属性测试）**：单事务预留意图/版本/代次 → 事务外串行执行外部动作 → 重启盘点与 `needs_verification`；Hypothesis 状态机与确定性故障注入全覆盖。
- **P3（Router、旁路 MAVProxy、阿里云与旧版迁移）**：双组同 SYSID 隔离、UDP 组禁开 TCP 5760、旁路失效/背压下转发证据、RAM 角色 + 规则归属对账、超时先查实态、旧版只读停止导入。
- **P4（会话归档、7 天留存、单盘边界与本地自动保存）**：有效飞控心跳开片、持续写盘与游标去重、原子封存与下载租约防争删、磁盘配额降级标缺口、交付 `docs/examples/python_archive_sync.py` 及 5 类增量验收用例。
- **P5（CLI、契约防漂移 CI、打包文档与验收）**：全套 `flightlink` CLI、CI 零漂移校验、Ubuntu 26.04.1 LTS `x86_64` 打包脚本与升级/回滚/恢复文档、`docs/acceptance.md`（离线 / Linux 实进程 / 真实阿里云三类证据严格分离）及最终放行场景。
