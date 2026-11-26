# FlightLink ECS 1.0 部署、离线打包、升级、回滚与数据库恢复手册

## 1. 目标环境与离线包适用范围

- **目标交付 ECS**：`Ubuntu 26.04.1 LTS (Resolute Raccoon)`、`x86_64`、`systemd 259`（AppArmor）。
- **测试阿里云 ECS**：`Rocky Linux 9.7 (Blue Onyx)`、`x86_64`、`systemd 252`（SELinux）、北京地域 `cn-beijing`、RAM 角色 `FlightLinkEcsSecurityGroupRole`。
- **严禁混用离线包**：离线 wheel 包绑定具体 Linux 发行版与系统 Python 次版本 ABI；仅在目标 **Ubuntu 26.04.1 LTS `x86_64`** 同构环境构建并校验通过后，才可宣称适用于 Ubuntu 26.04.1 LTS。
- **开发 Skills 隔离**：`.agents/skills/` 仅作仓库开发资料，打包配置与 CI 均强制校验其**不进入** `flightlink_ecs-*.whl` 及 ECS 运行目录。

---

## 2. 安装方法（在线镜像锁定 & 离线包）

### 2.1 在线安装（可选清华 / 阿里云镜像，不修改全局系统源）
```bash
python3 -m venv /opt/flightlink/venv
# 仅在单次 pip 命令传入镜像，不写入 /etc/pip.conf 或修改 /etc/apt/sources.list
/opt/flightlink/venv/bin/pip install \
  --index-url https://mirrors.aliyun.com/pypi/simple \
  --require-hashes -r requirements.lock.txt   # 或指定锁定版本安装 flightlink_ecs-1.0.0-py3-none-any.whl
```
（如需清华镜像，将 `--index-url` 替换为 `https://pypi.tuna.tsinghua.edu.cn/simple`。）

### 2.2 离线包制作与校验（目标 Ubuntu 26.04.1 LTS `x86_64`）
```bash
# 1. 在同构 Ubuntu 26.04.1 LTS x86_64 构建机执行
FLIGHTLINK_PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple \
  bash scripts/build_offline_bundle.sh dist/offline-ubuntu26.04-x86_64

# 2. 拷贝至目标干净 ECS 并校验 SHA256SUMS.txt
cd offline-ubuntu26.04-x86_64
sha256sum -c SHA256SUMS.txt

# 3. 离线安装到独立虚拟环境
python3 -m venv /opt/flightlink/venv
/opt/flightlink/venv/bin/pip install --no-index --find-links ./wheels flightlink-ecs
```

### 2.3 初始化与系统服务安装
```bash
# 创建最小权限用户（flightlink-api 与 flightlink-agent 权限严格分离）
sudo groupadd --system flightlink
sudo useradd --system --gid flightlink --no-create-home flightlink-api
sudo useradd --system --gid flightlink --no-create-home flightlink-agent
sudo useradd --system --gid flightlink --no-create-home flightlink-archive

sudo install -d -o flightlink-agent -g flightlink -m 0770 /var/lib/flightlink /var/lib/flightlink/archive /var/lib/flightlink/router.d /var/lib/flightlink/run
sudo cp deploy/systemd/flightlink-*.service /etc/systemd/system/
sudo systemctl daemon-reload

# 初始化数据库与正式管理员（绝无演示账号）
/opt/flightlink/venv/bin/flightlink init \
  --admin-user ops_admin --admin-password '<强密码>' \
  --dtu-cidr 0.0.0.0/0 --gcs-cidr 0.0.0.0/0

# 验证并记录已有 RAM 角色和目标业务安全组（不创建角色、不提权）
/opt/flightlink/venv/bin/flightlink connect --ram \
  --region cn-beijing --security-group <专用业务安全组ID>

# 只读自检
/opt/flightlink/venv/bin/flightlink doctor

# 仅启动管理服务（不批量拉起飞机端口组）
/opt/flightlink/venv/bin/flightlink run
```

> **管理入口安全**：`flightlink-api` 默认仅监听 `127.0.0.1:8000`；远程访问必须经由 Nginx / Caddy TLS 反向代理提供 HTTPS，且管理入口安全组与业务端口安全组独立。

---

## 3. 升级与回滚流程

### 3.1 升级
1. 执行只读检查并备份控制库：
   ```bash
   flightlink doctor
   sqlite3 /var/lib/flightlink/control.sqlite3 ".backup '/var/backups/flightlink/control-$(date -u +%Y%m%dT%H%M%SZ).sqlite3'"
   ```
2. 停止管理服务（无需强停正常转发中的 Router，或按维护窗口执行 `flightlink stop`；注意 `flightlink stop` 绝不删归档）。
3. 备份当前 `/opt/flightlink/venv` 为 `/opt/flightlink/venv.prev`，安装新版本 wheel。
4. 启动管理服务 `flightlink run`，确认 `flightlink doctor` 与 `GET /api/v1/health` 正常；`flightlink-agent` 会先盘点现网 systemd / 套接字 / 云安全组，不盲目重启已有健康组。

### 3.2 回滚
1. `systemctl stop flightlink-api flightlink-agent flightlink-archive`
2. 切回 `/opt/flightlink/venv.prev` -> `/opt/flightlink/venv`。
3. 如有 schema 不兼容，先将故障库改名留证，再将升级前 `.backup` 文件恢复为 `/var/lib/flightlink/control.sqlite3`。
4. 启动管理服务并执行 `flightlink doctor`、`flightlink firewall diff` 核对现网。

---

## 4. SQLite 控制库故障与恢复说明

- **数据库丢失或损坏时的安全原则**：`flightlink-agent` 绝不根据磁盘残留 `.conf` 文件猜测启动通道，保留所有日志与归档证据，并将不一致项标为 `needs_verification`。
- **标准恢复步骤**：
  1. 停止 `flightlink-api`、`flightlink-agent`、`flightlink-archive`。
  2. 留存损坏库文件副本（含 `-wal`、`-shm`）用于事后排查：
     ```bash
     cp -a /var/lib/flightlink/control.sqlite3* /var/backups/flightlink/corrupt-$(date -u +%Y%m%dT%H%M%SZ)/
     ```
  3. 验证最近一次在线快照完整性：
     ```bash
     sqlite3 /var/backups/flightlink/control-latest.sqlite3 "PRAGMA integrity_check;"
     ```
  4. 原子替换恢复控制库并核对权限（`chown flightlink-agent:flightlink`、`chmod 0660`）。
  5. 启动管理服务后立即执行 `flightlink doctor` 与 `flightlink firewall diff`，逐组确认待核实项（`flightlink op show` / `flightlink firewall sync`）。
  6. `/var/lib/flightlink/archive/<group_id>/<session_id>/` 下的已封存日志与 `metadata.json` 独立存在磁盘，不会因控制库恢复而被删除。
