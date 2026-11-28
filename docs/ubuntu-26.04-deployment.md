# FlightLink ECS 后端 1.0｜Ubuntu 26.04 LTS 完整部署教程（Git Clone）

> **适用环境**：`Ubuntu 26.04 LTS (Resolute Raccoon)`、`x86_64`、`systemd 259`（AppArmor）。
> **前提**：ECS 已绑定具备目标业务安全组最小权限（`ecs:DescribeSecurityGroupAttribute`、`ecs:AuthorizeSecurityGroup`、`ecs:RevokeSecurityGroup`）的实例 RAM 角色（如 `FlightLinkEcsSecurityGroupRole`）。

---

## 第 0 步：先拉取仓库（如主机有旧版 v0.1，先用仓库内脚本一键清理）

```bash
sudo apt-get update
sudo apt-get install -y git curl ca-certificates iproute2 sqlite3 python3 python3-venv python3-pip

cd ~
git clone https://github.com/Lehuang0212/FlightLink-backend.git
cd ~/FlightLink-backend
```

### （仅限曾装过旧版 v0.1 的机器）完全停服并清理旧版日志、环境、账号
> **说明**：旧版 v0.1 创建的 `flightlink-api` 用户主组为 `flightlink-api`（而非 1.0 的 `flightlink` 共享组），且残留 `flightlink-capture@.service`、`flightlink-api.service.d`、`sudoers` 等文件。建议在部署 1.0 前执行彻底清理；该脚本默认**保留已编译好的 `/usr/bin/mavlink-routerd`** 供 1.0 直接复用。

```bash
# 1. 先预览将要停止和删除的内容（零副作用）
sudo bash scripts/purge_legacy_v01_ubuntu26.sh --dry-run

# 2. 确认无误后执行清理（如需同时通过旧版维护命令撤销旧版托管安全组规则，追加 --clean-cloud）
sudo bash scripts/purge_legacy_v01_ubuntu26.sh --yes
```

常用可选参数：
- `--clean-cloud`：在删旧代码前先清理旧版托管的阿里云安全组规则；
- `--keep-backups`：保留 `/var/backups/flightlink`；
- `--keep-journal`：跳过 `journalctl --rotate && --vacuum-time=1s`。

---

## 第 1 步：确认或安装 `mavlink-routerd`

先检查主机是否已有 `mavlink-routerd`：

```bash
which mavlink-routerd && mavlink-routerd --version
```

若尚未安装，从上游源码编译安装到 `/usr/bin/mavlink-routerd`：

```bash
sudo apt-get install -y meson ninja-build pkg-config gcc g++ libsystemd-dev
sudo git clone --depth 1 --recurse-submodules https://github.com/mavlink-router/mavlink-router.git /usr/local/src/mavlink-router
cd /usr/local/src/mavlink-router
sudo meson setup build --prefix=/usr -Dsystemdsystemunitdir=/usr/lib/systemd/system
sudo ninja -C build install
mavlink-routerd --version
cd ~/FlightLink-backend
```

---

## 第 2 步：安装 FlightLink 1.0 运行环境（不修改全局 apt/pip 源）

使用仓库内置安装脚本（自动创建 `/opt/flightlink/venv`，安装 `flightlink-ecs` 与旁路 `MAVProxy`，并软链 `/usr/local/bin/flightlink` 与 `/usr/bin/flightlink`）：

```bash
cd ~/FlightLink-backend
sudo bash scripts/install.sh --mirror aliyun --with-mavproxy
```
> 如需清华镜像，把 `--mirror aliyun` 改为 `--mirror tsinghua`；海外直连可省略 `--mirror`。

---

## 第 3 步：初始化主机（隔离账号、systemd、Polkit、数据库与端口）

`--port` 默认为 `8000`，可按实际需要自定义（例如 `18080`）：

- **方式 A（推荐，最安全）**：仅监听本机 `127.0.0.1`，本地桌面端通过 **SSH 隧道** 或 **Nginx/Caddy HTTPS 反代** 访问：
  ```bash
  sudo flightlink init \
    --admin-user ops_admin \
    --admin-password '<你的管理员强密码>' \
    --dtu-cidr 0.0.0.0/0 \
    --gcs-cidr 0.0.0.0/0 \
    --host 127.0.0.1 \
    --port 18080
  ```
- **方式 B（仅联调阶段临时直连）**：把 `--host 127.0.0.1` 改为 `--host 0.0.0.0`，并在阿里云控制台**手工**添加入方向规则放行 `TCP 18080`（建议源地址限制为你本机公网 IP `/32`；手工规则不会被 FlightLink 误删）。

---

## 第 4 步：绑定阿里云 ECS 实例 RAM 角色与业务安全组

```bash
sudo flightlink connect --ram \
  --region cn-beijing \
  --security-group <你的业务安全组ID>
```
> 不传 `--ram-role` 时自动发现当前 ECS 挂载的实例 RAM 角色；如需强校验指定角色名，追加 `--ram-role <角色名>`。

---

## 第 5 步：启动三个管理服务并逐项验收

```bash
# 1. 启动管理服务（不会批量自动拉起任何飞机端口组）
sudo flightlink run

# 2. 检查三服务均为 active 且监听目标端口
systemctl is-active flightlink-api flightlink-agent flightlink-archive
sudo ss -H -l -n -t -p | grep 18080

# 3. 健康接口、只读自检与安全组对账
curl -s http://127.0.0.1:18080/api/v1/health
echo ""
sudo flightlink doctor
sudo flightlink firewall diff
```

预期全部通过标志：
- `systemctl is-active` 输出三行 `active`；
- `/api/v1/health` 返回 `{"status":"ok","version":"1.0.0",...,"db_ok":true}`；
- `flightlink doctor` 中 `binaries.mavlink-routerd`、`binaries.mavproxy.py` 非空，`legacy_remnants.detected` 为 `false`，3 个 `components` 均为 `"status": "ok", "stale": false`；
- `flightlink firewall diff` 为 `"in_sync": true`。

### 可选：验证登录接口 & 创建只读账号
```bash
# 验证正式管理员登录
curl -s -X POST http://127.0.0.1:18080/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"ops_admin","password":"<你的管理员强密码>"}'
echo ""

# 可选：添加只读账号（viewer）
sudo flightlink user add --username ops_viewer --password '<只读强密码>' --role viewer
```

---

## 第 6 步：本地桌面端接入方式

### 方式 1：SSH 隧道（配合 `--host 127.0.0.1 --port 18080`，零公网暴露）
在本地 Windows PowerShell 运行并保持开启：
```powershell
ssh -N -L 18080:127.0.0.1:18080 <用户名>@<ECS公网IP>
```
桌面端登录地址填 `http://127.0.0.1:18080`。

### 方式 2：生产 HTTPS 反代（Caddy / Nginx 监听 443 -> `127.0.0.1:18080`）
保持 `flightlink-api` 仅监听 `127.0.0.1:18080`，在阿里云安全组手工放行 `TCP 443`，用 Nginx/Caddy 终结 TLS 后反代至 `http://127.0.0.1:18080`。

---

## 第 7 步：日常升级、改端口与排障速查

```bash
# 拉取最新代码并原地升级（保留数据库、归档与安全组配置）
cd ~/FlightLink-backend
git pull --ff-only origin main
sudo /opt/flightlink/venv/bin/pip install --no-deps --force-reinstall .
sudo flightlink init
sudo systemctl restart flightlink-api flightlink-agent flightlink-archive

# 在线切换 API 监听端口（自动持久化到 /etc/flightlink/flightlink.env 并重启 API）
sudo flightlink run --port 8000

# 查看服务日志
sudo journalctl -u flightlink-api -u flightlink-agent -u flightlink-archive -n 100 --no-pager

# 安全清理 1.0 孤立临时文件（不动数据库与归档）
sudo flightlink clear --runtime
```
