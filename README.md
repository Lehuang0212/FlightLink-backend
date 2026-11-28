# FlightLink ECS Backend 1.0

FlightLink ECS 1.0 后端：面向“飞控 + 4G DTU + 阿里云 ECS + 地面站”的多机 MAVLink 隔离转发控制面。支持最多 5 组独立 `mavlink-routerd` 通道与旁路 `MAVProxy` 观测、阿里云 RAM 最小权限安全组自动同步、真实可空遥测、双源实时日志，以及 7 天飞控会话归档与桌面端增量自动保存（兼容 **Ubuntu 26.04 LTS** 与 **Rocky Linux 9.x**）。

> 旧版 v0.1 代码已完整归档于 [`old-version-0.1`](../../tree/old-version-0.1) 分支，当前 `main` 分支为正式 **1.0** 版本。

---

## 一、项目介绍

### 核心架构（一包三组件，职责严格隔离）
FlightLink 1.0 采用单机 Python FastAPI + SQLite（WAL）+ 本地 `systemd` 架构，无需 Redis、Celery、Kubernetes 或强制 Docker：

| 组件 | Systemd 服务 | 职责与权限边界 |
|---|---|---|
| **控制与只读 API** | `flightlink-api.service` | 提供 `/api/v1` REST 接口与正式账号认证；**无 `systemd` / 阿里云安全组写权限**，所有 `GET` 与 `/health` 零副作用；写操作统一异步受理返回 `202 Accepted` |
| **通道与安全组代理** | `flightlink-agent.service` | 唯一状态变更执行器；每组独立串行调度 `flightlink-router@<gid>`、回环旁路 `flightlink-mavproxy@<gid>` 与阿里云安全组；周期性刷新本地运行态与云安全组观测；**整机重启绝不自动拉起飞机通道** |
| **连续归档采集器** | `flightlink-archive.service` | 连续采集每个端口组的 Router 与 MAVProxy 日志，按飞控真实心跳自动切片封存（`router.log`、`mavproxy.log`、`metadata.json`），执行 7×24h 留存与磁盘配额保护 |

### 关键特性
- **多机硬隔离**：单机最多管理 **5 个端口组**，每组独立 `mavlink-routerd` 进程（DTU 固定 UDP，GCS 支持 TCP / UDP），UDP 地面站强制 `TcpServerPort = 0` 杜绝意外占用 TCP 5760；同 SYSID 多机零串流。
- **旁路观测不阻断**：`MAVProxy` 仅通过 `127.0.0.1` 回环 tap 旁路接入，绝不串联在 DTU ↔ GCS 转发链路上，旁路或归档异常不影响正常链路转发。
- **阿里云 RAM 最小权限托管**：通过 ECS 实例元数据（IMDSv2）自动获取临时凭证，仅需单安全组最小权限（`DescribeSecurityGroupAttribute` / `AuthorizeSecurityGroup` / `RevokeSecurityGroup`），按 `flightlink:v1:<ecs_id>:<group_id>:<role>` 精确管理规则，**绝不误删手工安全组规则**。
- **桌面端默认自动保存契约**：提供 `/api/v1/archive/changes` 增量元数据清单与 `/api/v1/archive/sessions/{id}/files/{file_id}` 鉴权单文件读取，配合本地 SHA-256 原子落盘校验。
- **可自定义管理端口**：API 默认监听 `127.0.0.1:8000`，部署与启动时均可通过 `--port` / `--host` 自定义。

---

## 二、部署前：全量旧环境清理命令

如果目标主机曾安装过旧版 v0.1 或之前测试过 1.0，**强烈建议先清理旧环境再部署**（避免旧账号主组冲突、旧 `service-control.conf` 覆盖权限或残留抓包服务干扰）。

### 1. 先拉取仓库（获取清理与安装脚本）
```bash
# Ubuntu 26.04
sudo apt-get update && sudo apt-get install -y git curl ca-certificates iproute2 sqlite3 python3 python3-venv python3-pip

# Rocky Linux 9.x（如为 Rocky 请改跑此行）
# sudo dnf install -y git curl ca-certificates iproute sqlite python3.12 python3.12-pip

cd ~
git clone https://github.com/Lehuang0212/FlightLink-backend.git
cd ~/FlightLink-backend
```

### 2. 场景 A：彻底清理旧版 v0.1（Ubuntu 26.04 / 24.04）
将完全停止并禁用旧版 `flightlink-api` / `flightlink-router@*` / `flightlink-capture@*`，删除旧版 systemd 单元、sudoers、env、SQLite 数据库、PCAP 抓包目录、日志（含 journald）以及旧系统账号/用户组（`flightlink-api`、`mavlink-router`、`flightlink-capture`），同时**保留 `/usr/bin/mavlink-routerd`** 供 1.0 直接复用：

```bash
# 第一步：预览将要停止和删除的全部内容（零改动）
sudo bash scripts/purge_legacy_v01_ubuntu26.sh --dry-run

# 第二步：确认无误后执行全量清理
sudo bash scripts/purge_legacy_v01_ubuntu26.sh --yes

# 如需在删旧代码前顺带撤销旧版托管的阿里云安全组规则，追加 --clean-cloud：
# sudo bash scripts/purge_legacy_v01_ubuntu26.sh --yes --clean-cloud
```

### 3. 场景 B：已装过 FlightLink 1.0 时的 CLI 清理命令
```bash
# 仅清理旧版 v0.1 残留（安全无损，不碰 1.0 数据与服务）
sudo flightlink clear --dry-run
sudo flightlink clear

# 全量重置当前 1.0 宿主机环境（停服、回收本机归属云安全组规则并保护人工规则、清 unit/env/DB；默认保留历史归档）
sudo flightlink clear --all --yes

# 连同 /var/lib/flightlink/archive 历史归档一并彻底清空（慎用）
sudo flightlink clear --all --yes --purge-archives
```

---

## 三、部署教程

> Ubuntu 26.04 完整逐行图文级操作指南另见 [docs/ubuntu-26.04-deployment.md](docs/ubuntu-26.04-deployment.md)。

### 第 1 步：确认或编译 `mavlink-routerd`
```bash
which mavlink-routerd && mavlink-routerd --version
```
若主机尚未安装 `mavlink-routerd`（Ubuntu 26.04 编译示例）：
```bash
sudo apt-get install -y meson ninja-build pkg-config gcc g++ libsystemd-dev
sudo git clone --depth 1 --recurse-submodules https://github.com/mavlink-router/mavlink-router.git /usr/local/src/mavlink-router
cd /usr/local/src/mavlink-router
sudo meson setup build --prefix=/usr -Dsystemdsystemunitdir=/usr/lib/systemd/system
sudo ninja -C build install
cd ~/FlightLink-backend
```

### 第 2 步：一键安装 FlightLink 1.0（不修改全局 apt/dnf/pip 源）
```bash
cd ~/FlightLink-backend
sudo bash scripts/install.sh --mirror aliyun --with-mavproxy
```
> 可选 `--mirror tsinghua` 使用清华镜像，或 `--offline-dir <目录>` 执行同构离线包安装。

### 第 3 步：初始化主机环境与管理员账号
`--port` 默认 `8000`，可按需改为其他端口（如 `18080`）：

```bash
sudo flightlink init \
  --admin-user ops_admin \
  --admin-password '<你的管理员强密码>' \
  --dtu-cidr 0.0.0.0/0 \
  --gcs-cidr 0.0.0.0/0 \
  --host 127.0.0.1 \
  --port 18080
```
> - 推荐保持 `--host 127.0.0.1`，远程通过 **HTTPS 反向代理（Nginx/Caddy 443）** 或 **SSH 隧道** 接入；
> - 仅在联调阶段临时直连时，可改为 `--host 0.0.0.0` 并在阿里云安全组**手工**放行该 TCP 端口（建议限制为本机公网 IP `/32`）。

### 第 4 步：绑定阿里云实例 RAM 角色与业务安全组
```bash
sudo flightlink connect --ram \
  --region cn-beijing \
  --security-group <你的业务安全组ID>
```
> 不传 `--ram-role` 时自动发现当前 ECS 挂载的 RAM 角色；传入则严格核对。

### 第 5 步：启动管理服务并验收
```bash
# 启动 3 个管理服务（不会自动拉起任何飞机端口组）
sudo flightlink run

# 检查服务状态与端口监听
systemctl is-active flightlink-api flightlink-agent flightlink-archive
sudo ss -H -l -n -t -p | grep 18080

# 健康探针、只读自检与安全组对账
curl -s http://127.0.0.1:18080/api/v1/health
echo ""
sudo flightlink doctor
sudo flightlink firewall diff
```

验证正式管理员登录（获取 Bearer Token）：
```bash
curl -s -X POST http://127.0.0.1:18080/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"ops_admin","password":"<你的管理员强密码>"}'
echo ""
```

### 第 6 步：本地桌面端接入（SSH 隧道示例）
在本地 Windows PowerShell 运行并保持开启：
```powershell
ssh -N -L 18080:127.0.0.1:18080 <用户名>@<ECS公网IP>
```
本地桌面端连接地址填写 `http://127.0.0.1:18080`，使用 `ops_admin` 账号登录即可。

---

## 四、常用运维与升级命令

```bash
# 拉取最新代码并原地升级（保留数据库、归档与安全组配置）
cd ~/FlightLink-backend
git pull --ff-only origin main
sudo /opt/flightlink/venv/bin/pip install --no-deps --force-reinstall .
sudo flightlink init
sudo systemctl restart flightlink-api flightlink-agent flightlink-archive

# 在线修改 API 监听端口并重启 API
sudo flightlink run --port 8000

# 端口组管理示例（CLI 与 Web/桌面端共享同一操作队列与审计链路）
sudo flightlink group create --name 一号机 --dtu-port 5760 --gcs tcp:14552
sudo flightlink group start <group_id>
sudo flightlink group update <group_id> --gcs udp:14554 --dry-run
sudo flightlink group delete <group_id> --wait

# 添加只读账号 / 查看历史归档 / 导出单次会话归档
sudo flightlink user add --username ops_viewer --password '<密码>' --role viewer
sudo flightlink history list --include-deleted-groups
sudo flightlink history export <session_id> --source all
```

---

## 五、文档索引

- **Ubuntu 26.04 详细部署手册**：[docs/ubuntu-26.04-deployment.md](docs/ubuntu-26.04-deployment.md)
- **离线打包、升级、回滚与数据库恢复**：[docs/operations-and-packaging.md](docs/operations-and-packaging.md)
- **OpenAPI 3.1 接口契约**：[docs/api/openapi.yaml](docs/api/openapi.yaml)
- **前端 / 桌面端集成指南**：[docs/frontend-integration.md](docs/frontend-integration.md)
- **PySide6/QML 本地桌面端交接说明**：[docs/desktop-client-handoff.md](docs/desktop-client-handoff.md)
- **Python 自动归档同步参考实现**：[docs/examples/python_archive_sync.py](docs/examples/python_archive_sync.py)
- **验收报告与真机证据**：[docs/acceptance.md](docs/acceptance.md)
