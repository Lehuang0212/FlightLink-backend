# FlightLink ECS 后端 1.0｜验收报告与证据清单

> **核心验收原则**：本报告严格区分**离线自动化验证**、**Linux 真实进程实测**、**真实阿里云 ECS 实测**三类证据，**绝不以 mock 替代真实 Linux 进程或真实阿里云通过**。

---

## 1. 版本与环境信息

| 项 | 值 |
|---|---|
| 后端包版本 | `flightlink-ecs 1.0.0` |
| 分支与基线 | `main`（旧版保留于 `old-version-0.1`） |
| 离线验证环境 | Windows 11 `x86_64`，CPython `3.14.6`，`fastapi 0.165.0`，`pydantic 2.14.4`，`pytest 9.2.0`，`hypothesis 6.170.17` |
| 目标交付 ECS（已核实档案） | Ubuntu 26.04.1 LTS (`resolute`)，`x86_64`，`systemd 259 (259.5-0ubuntu3.4)`，容量足够，未运行旧版 |
| 测试阿里云 ECS（已核实档案） | Rocky Linux 9.7 (`Blue Onyx`)，`x86_64`，`systemd 252`，北京地域 `cn-beijing`，RAM 角色 `FlightLinkEcsSecurityGroupRole`，安全组 `sg-2ze93ogksv8g4i7tc8vd`（默认安全组，正式建议独立专用业务安全组） |
| 业务公网来源策略 | DTU（UDP）与 GCS（TCP/UDP）均为显式配置 `0.0.0.0/0`（代码不默认放开全网） |

---

## 2. A 类证据｜离线自动化与契约验证（已实测通过）

### 2.1 复现命令
```bash
# 1. OpenAPI 3.1 契约零漂移检查
.venv/Scripts/python.exe -m flightlink.api.openapi_export

# 2. 全量单元、契约、Hypothesis 状态机 PBT、真实套接字隔离、归档自动同步、最终放行场景与 CLI 测试
.venv/Scripts/pytest.exe -v -p no:cacheprovider
```

### 2.2 实测结果摘要（20 / 20 通过，耗时 57.39s）

| 验收维度 | 测试文件 | 结果 | 覆盖核心断言 |
|---|---|---|---|
| OpenAPI 契约零漂移与只读无副作用 | [test_openapi_drift.py](../tests/test_openapi_drift.py) | **2 / 2 PASS** | `docs/api/openapi.yaml` 与 FastAPI 实时路由/方法/`operationId`/参数零漂移；API 启动、`/health` 及全部 `GET` 不产生操作、不启动监听、不写配置、不建切片 |
| 认证、RBAC、12 项遥测空值、幂等与 412/404/200 | [test_api_contract.py](../tests/test_api_contract.py) | **2 / 2 PASS** | 禁用预览演示账号；`viewer` 写操作 `403`；Refresh 轮换与 Logout 吊销；过期 Token `401 TOKEN_EXPIRED`；`Idempotency-Key` 同键异体 `409 IDEMPOTENCY_CONFLICT`；`If-Match` 冲突 `412`；无真实源时 12 项遥测严格返回 `null`；删组后 `/groups/{id}` 返回 `404`、`/archive/groups/{id}` 返回 `200` 且未鉴权返回 `401` |
| P2 生命周期、故障注入与 Hypothesis PBT | [test_p2_lifecycle_and_pbt.py](../tests/test_p2_lifecycle_and_pbt.py) | **6 / 6 PASS** | Hypothesis 状态机（30 例 × 20 步）验证任意操作序列下：无双组共用业务端口、已删组不复活、停机组改端口无新监听、不超过 5 组、重建必得新 `group_id`；确定性覆盖同号换协议（`tcp`→`udp`）、组内两端口互换、改名不重启、系统进程抢占、他组不因本组失败而重启、新链路成功撤旧规则失败只补清理不回退、删组旧启动任务被代次围栏拦截、`cleanup_pending` 不可复活、ECS 整机重启不自动拉起 |
| P3 Router 配置、双组同 SYSID 真实套接字与阿里云签名 | [test_p3_router_mavlink_aliyun.py](../tests/test_p3_router_mavlink_aliyun.py) | **5 / 5 PASS** | UDP 地面站配置严格写 `TcpServerPort = 0` 防止意外开 TCP 5760；两组真实 OS UDP/TCP 套接字同 `SYSID=1` 无串流；旁路观察崩溃/背压时 DTU↔GCS 持续收包；IMEI/文本 `heart`/GCS/MAVProxy 心跳不开片不设 `sysid`；阿里云 IMDSv2+RAM 签名请求严格按 `SecurityGroupRuleId` 撤销、不动人工 SSH 规则（注：使用 `httpx.MockTransport`，不计入 C 类真云证据）；旧版 v0.1 只读导入默认停止且活动旧版拒绝导入 |
| P4 归档、增量自动保存（全部 5 类增量验收）与最终放行场景 | [test_p4_archive_and_sync.py](../tests/test_p4_archive_and_sync.py) | **4 / 4 PASS** | ① 默认自动保存（`router.log` + `mavproxy.log` + `metadata.json`）；② 断网失败单切片不阻塞后续切片，网络恢复后手动补下仅拉缺文件；③ 部分文件成功后重启客户端绝不重下已校验文件；④ 删组后仍可补下载；⑤ 云端 7 天到期清理不影响本地已保存文件、过期未下文件 `410` 停重试不循环、离线超窗 `410 CURSOR_EXPIRED` 报告缺口并重新对账；区分云端本身缺源（`saved_with_source_gaps`，不提示重试）与客户端下载失败（细分网络 / `disk_permission_denied` / `disk_full`）；单盘配额降级标缺口；下载租约防争删；完整跑通“一号机删组撤规则超时 → 重启 API 与 agent → 旧组不复活他组不停 → 核清重建同端口不混日志 → 跨 7 天边界”最终放行场景 |
| P5 全套 CLI 命令 | [test_p5_cli.py](../tests/test_p5_cli.py) | **1 / 1 PASS** | 覆盖 `init`、`connect --ram`、`doctor`、`run`、`status`、`group create/start/update (--dry-run)/delete --wait`、`firewall diff/sync`、`op show`、`logs`、`history list --include-deleted-groups`、`history export --source all`、`stop`（不删归档） |

---

## 3. B 类证据｜Linux 真实进程实测（待接入测试主机验证）

以下项目需在真实 Linux 主机（测试机 Rocky Linux 9.7 `x86_64` 或目标机 Ubuntu 26.04.1 LTS `x86_64`）上安装真实 `mavlink-routerd` 与 `MAVProxy` 二进制后实测并补充实测日志：

- [ ] 真实 `mavlink-routerd` 双组运行（一组 TCP 地面站、一组 UDP 地面站，同 SYSID），使用 `ss -H -l -n -u -t -p` 实测监听端口（确认 UDP 组无 TCP 5760）。
- [ ] 真实 `mavproxy.py` 旁路观察行为验证（确认无主动回发干扰），以及 `systemctl stop flightlink-mavproxy@...`、`kill -9` 归档进程、日志背压期间另一端真实持续收包证据。
- [ ] 真实 `systemd`（`flightlink-api` / `flightlink-agent` / `flightlink-archive` / `flightlink-router@` / `flightlink-mavproxy@`）重启与主机重启不自动拉起组实测。
- [ ] 在目标 **Ubuntu 26.04.1 LTS `x86_64`** 同构环境运行 `scripts/build_offline_bundle.sh` 构建离线包，并在干净 Ubuntu 26.04.1 LTS 机器执行 `sha256sum -c SHA256SUMS.txt` 与离线安装验证。

---

## 4. C 类证据｜真实阿里云 ECS 实测（待接入测试主机验证）

以下项目需在已绑定 `FlightLinkEcsSecurityGroupRole` 的真实北京地域测试 ECS（`aliyun-ecs`）上执行并记录真实请求与控制台/API 回查证据（**不以 mock 替代**）：

- [ ] `flightlink connect --ram --region cn-beijing --security-group <业务安全组ID>` 真实 IMDSv2 / RAM 角色连通校验（建议挂接独立专用业务安全组以隔离默认组中的 SSH 规则）。
- [ ] 真实安全组入方向规则创建、归属标签回查、`firewall diff`、更新换端口补清理、删组撤规则（含超时/人工规则共存时绝不误删人工规则）实测。
- [ ] 真实云端完整跑通最终放行场景并归档实测输出。
