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

### 2.2 实测结果摘要（22 / 22 通过，耗时 55.78s）

| 验收维度 | 测试文件 | 结果 | 覆盖核心断言 |
|---|---|---|---|
| OpenAPI 契约零漂移与只读无副作用 | [test_openapi_drift.py](../tests/test_openapi_drift.py) | **2 / 2 PASS** | `docs/api/openapi.yaml` 与 FastAPI 实时路由/方法/`operationId`/参数零漂移；API 启动、`/health` 及全部 `GET` 不产生操作、不启动监听、不写配置、不建切片 |
| 认证、RBAC、12 项遥测空值、幂等与 412/404/200 | [test_api_contract.py](../tests/test_api_contract.py) | **2 / 2 PASS** | 禁用预览演示账号；`viewer` 写操作 `403`；Refresh 轮换与 Logout 吊销；过期 Token `401 TOKEN_EXPIRED`；`Idempotency-Key` 同键异体 `409 IDEMPOTENCY_CONFLICT`；`If-Match` 冲突 `412`；无真实源时 12 项遥测严格返回 `null`；删组后 `/groups/{id}` 返回 `404`、`/archive/groups/{id}` 返回 `200` 且未鉴权返回 `401` |
| P2 生命周期、故障注入与 Hypothesis PBT | [test_p2_lifecycle_and_pbt.py](../tests/test_p2_lifecycle_and_pbt.py) | **6 / 6 PASS** | Hypothesis 状态机（30 例 × 20 步）验证任意操作序列下：无双组共用业务端口、已删组不复活、停机组改端口无新监听、不超过 5 组、重建必得新 `group_id`；确定性覆盖同号换协议（`tcp`→`udp`）、组内两端口互换、改名不重启、系统进程抢占、他组不因本组失败而重启、新链路成功撤旧规则失败只补清理不回退、删组旧启动任务被代次围栏拦截、`cleanup_pending` 不可复活、ECS 整机重启不自动拉起 |
| P3 Router 配置、双组同 SYSID 真实套接字、systemd 回归与阿里云签名 | [test_p3_router_mavlink_aliyun.py](../tests/test_p3_router_mavlink_aliyun.py) | **6 / 6 PASS** | UDP 地面站配置严格写 `TcpServerPort = 0`（且不含被 `mavlink-routerd v4` 拒绝的 `LogMode = disabled`）；两组真实 OS UDP/TCP 套接字同 `SYSID=1` 无串流；旁路观察崩溃/背压时 DTU↔GCS 持续收包；IMEI/文本 `heart`/GCS/MAVProxy 心跳不开片不设 `sysid`；`LinuxSystemdSupervisor` 写 `mavproxy-<gid>.env`、端口绑定轮询重试与停机超时 `SIGKILL` 兜底；阿里云 IMDSv2+RAM 签名请求优先 `DescribeSecurityGroupAttribute`（兼容单 SG 最小权限策略）并严格按 `SecurityGroupRuleId` 撤销（注：单测使用 `httpx.MockTransport`，真云实测见第 4 节）；旧版 v0.1 只读导入默认停止且活动旧版拒绝导入 |
| P4 归档、增量自动保存（全部 5 类增量验收）与最终放行场景 | [test_p4_archive_and_sync.py](../tests/test_p4_archive_and_sync.py) | **4 / 4 PASS** | ① 默认自动保存（`router.log` + `mavproxy.log` + `metadata.json`）；② 断网失败单切片不阻塞后续切片，网络恢复后手动补下仅拉缺文件；③ 部分文件成功后重启客户端绝不重下已校验文件；④ 删组后仍可补下载；⑤ 云端 7 天到期清理不影响本地已保存文件、过期未下文件 `410` 停重试不循环、离线超窗 `410 CURSOR_EXPIRED` 报告缺口并重新对账；区分云端本身缺源（`saved_with_source_gaps`，不提示重试）与客户端下载失败（细分网络 / `disk_permission_denied` / `disk_full`）；单盘配额降级标缺口；下载租约防争删；完整跑通“一号机删组撤规则超时 → 重启 API 与 agent → 旧组不复活他组不停 → 核清重建同端口不混日志 → 跨 7 天边界”最终放行场景 |
| P5 全套 CLI 命令与 Linux 宿主机初始化回归 | [test_p5_cli.py](../tests/test_p5_cli.py) | **2 / 2 PASS** | 覆盖 `init`（含 `--system` 生成 5 个 unit / Polkit / `flightlink.env`、`ExecStart` 绑定 `sys.prefix` venv、重跑保留既有 JWT 与阿里云配置）、`connect --ram`、`doctor`、`run`、`status`、`group create/start/update (--dry-run)/delete --wait`、`firewall diff/sync`、`op show`、`logs`、`history list --include-deleted-groups`、`history export --source all`、`stop`（不删归档） |

---

## 3. B 类证据｜Linux 真实进程实测（Rocky Linux 9.7 部分已通过，其余待补测）

实测主机：Rocky Linux 9.7 (`Blue Onyx`, `x86_64`, `systemd 252`, `aliyun-ecs`)，实例 ID `ecs_9c66b003a8e26ea0`，真实二进制 `/bin/mavlink-routerd` (`v4-16-g2362c62`) 与 `/opt/flightlink/venv/bin/mavproxy.py`，基线提交 `13b16ea` → `39210bf`（实测时间 `2026-11-27 01:00–01:13 CST`）。

- [x] **3 管理服务最小权限拉起与只读健康校验**：`flightlink init` + `systemctl restart flightlink-api flightlink-agent flightlink-archive` 后三服务均为 `active`；`curl -s http://127.0.0.1:8000/api/v1/health` 返回 `{"status":"ok","version":"1.0.0","ecs_instance_id":"ecs_9c66b003a8e26ea0","db_ok":true}`；`sudo flightlink doctor` 显示 3 组件均为 `status: "ok", stale: false`，`flightlink-api` 仅绑定 `127.0.0.1:8000`。
- [x] **真实 `mavlink-routerd` 双组并行（一组 TCP GCS、一组 UDP GCS）与 `ss` 实测**：
  - 「一号机」(`grp_01a218bce3bf6ec4fd6741ed`, `pid=624655`)：`ss -H -l -n -u -t -p` 实测绑定 `udp 0.0.0.0:5760` + `tcp *:14552`，对应旁路 `flightlink-mavproxy@grp_01a218bce3bf6ec4fd6741ed` 为 `active` 且仅绑定回环 `udp 127.0.0.1:43495` (`mavproxy.py`, `pid=624661`)。
  - 「二号机UDP」(`grp_01a218d10b43ea470212fad9`, `pid=624704`)：`ss -H -l -n -u -t -p` 实测仅绑定 `udp 0.0.0.0:5761` + `udp 0.0.0.0:14553`，**确认未多开默认 `tcp/5760`**，旁路 tap 绑定 `127.0.0.1:39439`。
- [x] **真机暴露并闭环修复的 Linux 运维缺陷**：实测中先后捕获并修复 Rocky `sudo secure_path`（补充 `/usr/bin/flightlink` 软链）、Linux venv Python 软链解引用致 `203/EXEC`（改用 `sys.prefix`）、`mavlink-routerd v4` 拒绝 `LogMode = disabled`、`mavproxy.py` 忽略 `SIGTERM` 致删组超时（补充 `TimeoutStopSec=3` 与 `SIGKILL` 兜底，提交 `39210bf`）。
- [ ] **真实 `mavlink-routerd` 飞控/地面站双向收包与故障注入**：在真实 `mavlink-routerd` 转发实包期间验证 `mavproxy.py` 无主动回发干扰，以及 `systemctl stop flightlink-mavproxy@...`、`kill -9` 归档进程、日志背压期间另一端真实持续收包证据。
- [ ] **真实 ECS 整机重启不自动拉起组实测**：执行真实主机重启后确认 `flightlink-router@*.service` 均未自动启动。
- [ ] **目标 Ubuntu 26.04.1 LTS `x86_64` 离线包构建与干净机验收**：在同构 Ubuntu 26.04.1 LTS 环境运行 `scripts/build_offline_bundle.sh`，并在目标交付 ECS 上执行 `sha256sum -c SHA256SUMS.txt` 与离线安装验证。

---

## 4. C 类证据｜真实阿里云 ECS 实测（核心链路已通过，生产专组/换口场景待补测）

实测主机：北京地域测试 ECS（`cn-beijing`），实例 RAM 角色 `FlightLinkEcsSecurityGroupRole`，安全组 `sg-2ze93ogksv8g4i7tc8vd`（含 4 条人工存量规则），实例 ID `ecs_9c66b003a8e26ea0`，提交 `a691cfd` → `39210bf`（实测时间 `2026-11-27 00:30–01:13 CST`）。

- [x] **`flightlink connect --ram` 真实 IMDSv2 + RAM 角色最小权限校验**：自动发现实例绑定的 `FlightLinkEcsSecurityGroupRole`，并通过单安全组最小权限接口 `DescribeSecurityGroupAttribute` 校验通过（兼容未授权复数 `DescribeSecurityGroups` 的单 SG 严格策略）。
- [x] **真实阿里云安全组入方向授权、归属标签回查、`firewall diff` 与删组回收（不动人工规则）**：
  - 创建「一号机」（`udp/5760`, `tcp/14552`, `0.0.0.0/0`）与「二号机UDP」（`udp/5761`, `udp/14553`, `0.0.0.0/0`）均成功在真实安全组 `sg-2ze93ogksv8g4i7tc8vd` 写入带 `flightlink:v1:ecs_9c66b003a8e26ea0:<group_id>:<role>` 描述标记的规则；
  - 删除「一号机」（`op_01a218d3032854cd998d7105`）在 0.75s 内完成停机并自动撤销 `udp/5760` 与 `tcp/14552`；
  - 「二号机UDP」在首次删组因 `mavproxy.py` 停机超时中断后，`sudo flightlink firewall diff` 准确检出残留 `extra_owned`（`sgr-2zei66kwbva90y0szbaz` / `udp 14553` 与 `sgr-2zeczxlbvo3flamdroyn` / `udp 5761`、`in_sync: false`）；升级 `39210bf` 后幂等重跑 `flightlink group delete grp_01a218d10b43ea470212fad9 --wait`，再次执行 `sudo flightlink firewall diff` 收敛为 `{"configured": true, "security_group_id": "sg-2ze93ogksv8g4i7tc8vd", "missing": [], "extra_owned": [], "manual_ignored": 4, "in_sync": true}`，**4 条手工规则全程未被触碰**。
- [ ] **独立业务安全组与更新换端口补清理 / 完整放行场景云端实测**：在目标生产 ECS 挂接独立专用业务安全组后，补充真实云端“运行中改端口/换协议补清理”及完整放行场景实测输出。
