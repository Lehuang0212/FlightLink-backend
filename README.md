# FlightLink-Console 后端

FlightLink-Console 管理平台的 FastAPI 后端。

## 本地运行

需要 Python 3.12 或更新版本，以及 uv。Windows 本地开发时先设置 `FLIGHTLINK_ROUTER_MANAGER=disabled`，以免尝试控制 Linux systemd：

```powershell
$env:FLIGHTLINK_ROUTER_MANAGER = "disabled"
uv sync
uv run uvicorn flightlink_backend.main:app --host 127.0.0.1 --port 8000 --reload
```

另开一个终端创建管理员，命令会交互式提示输入用户名和密码：

```bash
uv run ConsoleUseradd
```

服务启动时会创建 SQLite 数据库并执行结构迁移。默认数据库位置为当前目录的 `./data/flightlink.sqlite3`。管理员只能通过 `ConsoleUseradd` 命令行交互添加，密码输入时不会回显，也不会作为命令行参数传递。用户名须为 3–64 位 ASCII 字母、数字、点、下划线或连字符；密码至少 8 位。系统没有公开注册接口。

## 当前 API

- GET /api/v1/health：检查应用和数据库。
- POST /api/v1/auth/login、POST /api/v1/auth/logout、GET /api/v1/auth/me：管理员登录、退出和身份查询。
- GET /api/v1/channels、GET /api/v1/channels/{id}：读取端口组、mavlink-router 运行状态和实时遥测，需要管理员会话。
- GET /api/v1/integrations/aliyun/security-group/preflight：管理员只读检查阿里云安全组查询权限和入方向规则。
- POST /api/v1/channels：创建端口组并生成路由配置。
- PUT /api/v1/channels/{id}：修改端口和地面站协议，重新生成配置并同步服务状态。
- DELETE /api/v1/channels/{id}：停止服务后删除端口组和配置。
- POST /api/v1/channels/{id}/restart：重启已启用的路由服务。
- GET /api/v1/channels/{id}/logs?limit=100：读取 mavlink-router 的 systemd 日志。
- GET /api/v1/channels/{id}/messages?limit=100：读取飞控发出的 MAVLink STATUSTEXT 文本。
- GET /api/v1/channels/{id}/packets?limit=100：读取实时抓包摘要内存缓冲区。
- GET /api/v1/channels/{id}/captures：列出已轮转的 PCAP 文件；GET /api/v1/channels/{id}/captures/{file_name} 下载已关闭的文件。

## 端口组和遥测

每个端口组是一对服务器端入口：4G DTU 使用 MAVLink UDP 入站端口，地面站默认使用 TCP，也可设为 UDP。每组由独立的 mavlink-routerd systemd 模板服务管理。数据库 UUID 是服务器配置键，不是无人机硬件身份。

每个端口组还会分配一个本机遥测镜像 UDP 端口，并写入 SQLite 和路由配置。mavlink-router 将 MAVLink 副本发送到 127.0.0.1，FastAPI 被动解析副本；这不会占用 DTU 入站端口，也不会把管理服务插进飞控与 QGC 的控制数据路径。新端口从本机 40000–49999 范围中挑选，并避开数据库已登记的 UDP 监听端口。

telemetry 字段包括飞控心跳在线状态、SYSID/COMPID、机型、ArduPilot 飞行模式、解锁状态、电池、GPS、海拔/相对高度、地速、收发速率、最近 MAVLink 包时间、链路质量和可选 RADIO_STATUS。飞控文本日志在 messages 接口读取。

MAVLink 链路质量参考 Mission Planner 的开源实现：按飞控 SYSID/COMPID 跟踪 8 位包序号，用序号间隙估算丢包，再以已收到包数除以已收到与估算丢包总数计算百分比。滚动计数每 5 秒乘以 0.8 衰减；无数据时显示质量每秒乘以 0.8 衰减。此值代表 MAVLink 包传输质量，不是 4G 的 RSRP/RSRQ。若收到 RADIO_STATUS，只展示遥测电台的原始字段，不能据此推断 EC05-DNC 的蜂窝信号。

TCP 地面站在线状态和对端地址通过 Linux 的 /proc/net/tcp* 连接表读取。UDP 地面站依据 GCS MAVLink 心跳判断在线，并由抓包摘要补充 UDP 对端地址。抓包也用于识别 4G DTU 入站数据报的源地址并填充 uav_peer。飞控在线状态与地面站连接状态分别计算。

实时遥测和最近 100 条 STATUSTEXT 保存在内存中，不会逐包写 SQLite。SQLite WAL 仅承载管理员、端口组和会话等低频配置数据。

## 抓包与保留策略

每个已启用端口组启动一个受限的 systemd 抓包单元。助手只抓该组的 UAV UDP 端口，以及地面站配置的 TCP 或 UDP 端口。抓包由两个低权限 tcpdump 子进程完成：一个以完整快照长度写 PCAP 并按时间轮转；另一个使用行缓冲输出摘要，通过本机回环 UDP 转给遥测监听器。摘要只保留最近 500 条在内存中，不写数据库或逐包写入 journald。前端可轮询 packets 接口展示近实时包数据。

默认每 600 秒轮转一个 PCAP；后台每 300 秒清理超过 24 小时的文件，并优先删除最旧文件以将总量控制在 1 GiB 左右。正在写入的最新片段会跳过清理；如果当前写入文件本身已超过容量上限，实际占用会暂时高于上限，直到片段轮转。下载接口只提供已经关闭的文件。抓包需要服务器安装 tcpdump，网络包捕获权限限制在专用 flightlink-capture 账号和 CAP_NET_RAW systemd 能力中。

## 配置和 ECS 部署

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| FLIGHTLINK_DATA_DIR | ./data | 默认数据目录 |
| FLIGHTLINK_DATABASE_PATH | 数据目录下 flightlink.sqlite3 | SQLite 数据库路径 |
| FLIGHTLINK_ROUTER_CONFIG_DIR | 数据目录下 router-configs | 每个端口组的路由配置目录 |
| FLIGHTLINK_CAPTURE_CONFIG_DIR | 数据目录下 capture-configs | 抓包助手读取的每频道 JSON 配置目录 |
| FLIGHTLINK_CAPTURE_DIR | 数据目录下 captures | PCAP 文件目录 |
| FLIGHTLINK_TCPDUMP_PATH | /usr/sbin/tcpdump | tcpdump 可执行文件路径，由抓包 systemd 单元环境文件读取 |
| FLIGHTLINK_PCAP_ROTATE_SECONDS | 600 | PCAP 轮转周期（秒，最大 86400） |
| FLIGHTLINK_PCAP_RETENTION_SECONDS | 86400 | PCAP 最长保留时间（秒） |
| FLIGHTLINK_PCAP_MAX_BYTES | 1073741824 | PCAP 目录目标容量（字节） |
| FLIGHTLINK_PCAP_CLEANUP_INTERVAL_SECONDS | 300 | PCAP 清理间隔（秒） |
| FLIGHTLINK_ROUTER_MANAGER | systemd | 服务控制器；本地开发可设为 disabled |
| FLIGHTLINK_ROUTER_CONTROL_HELPER | /usr/local/sbin/flightlink-routerctl | 受限的 root 服务控制助手 |
| FLIGHTLINK_SUDO_PATH | /usr/bin/sudo | 调用助手的非交互式权限边界 |
| FLIGHTLINK_ROUTER_COMMAND_TIMEOUT_SECONDS | 10 | 服务控制、状态和日志命令超时 |
| FLIGHTLINK_SESSION_TTL_SECONDS | 43200 | 管理员会话有效期 |
| FLIGHTLINK_SESSION_COOKIE_NAME | flightlink_session | HTTP-only 会话 Cookie 名称 |
| FLIGHTLINK_SESSION_COOKIE_SECURE | true | 生产 HTTPS 环境应启用安全 Cookie |

使用本项目提供的 SSH 本地转发、通过 `http://127.0.0.1:8000` 管理时，示例环境文件将安全 Cookie 关闭；这是本机回环访问场景。通过 HTTPS 提供管理界面时应启用 `FLIGHTLINK_SESSION_COOKIE_SECURE=true`。运行 API 和 `ConsoleUseradd` 时须使用相同的数据库环境设置。

生产部署要求 FastAPI、systemd、mavlink-routerd 和 tcpdump 位于同一 ECS 主机上；当前没有实现容器到宿主机的控制桥接。按下方“一键部署”步骤创建专用服务账号和目录，并安装本项目提供的 router、capture 与 API systemd 单元。API 数据目录归 API 账号所有；路由配置目录允许 API 写入并允许 mavlink-router 组读取；抓包配置和 PCAP 目录由 API 与专用抓包组按只读/读写职责共享。

助手只允许 API 账号执行固定的路由/抓包服务启停、状态和路由日志操作；助手必须归 root 所有，且不能由 API 账号写入。API 服务应在 network-online.target 后启动，启动协调会恢复已启用但未运行的路由和抓包单元。主机防火墙不会由本服务修改。阿里云安全组可按下方“阿里云安全组配置”单独启用后由后端管理。

安全组功能关闭时，端口组沿用本地配置与服务管理流程。启用阿里云 provider 后，创建会先确认精确入方向规则再启动服务；更新先准备新规则，再切换本地配置和服务，成功后才清理旧的后端托管规则；删除或禁用会先停止服务再清理托管规则。发生失败时会保留可重试的端口组/规则状态。若路由或抓包单元无法停止，删除会被拒绝。

创建端口组时可提交 UAV UDP 端口、地面站端口、地面站协议和启用状态。示例 JSON 见 English 部分。响应中的 UUID 和配置文件名用于后续修改、删除或重启。

## 阿里云安全组配置

安全组 provider 默认关闭。开启后，FastAPI 使用运行它的 ECS 实例 RAM 角色凭证调用 ECS OpenAPI；不在环境文件、数据库或请求中填写长期 AccessKey。只有阿里云安全组入方向规则会被管理，Ubuntu/Rocky 主机防火墙与出方向规则不会被修改。

### RAM 角色和权限

在 RAM 控制台创建一个可信实体为“阿里云服务 / 云服务器 ECS”的 RAM 角色，附加自定义权限策略，再将角色作为“实例 RAM 角色”绑定到运行 FastAPI 的 ECS。已有 ECS 实例可在 ECS 实例详情的“实例设置 / 授予或收回 RAM 角色”中绑定；一台 ECS 同时只能绑定一个实例 RAM 角色。如果这台 ECS 已绑定其他角色，应把此自定义策略并入正在使用的角色，而不是直接替换角色。ECS SDK 会从实例元数据取得临时凭证；`FLIGHTLINK_ALIYUN_ECS_ROLE_NAME` 可选，留空时自动发现。

策略只需要以下三个 Action：

```json
{
  "Version": "1",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ecs:DescribeSecurityGroupAttribute",
        "ecs:RevokeSecurityGroup"
      ],
      "Resource": "acs:ecs:cn-hangzhou:1234567890123456:securitygroup/sg-example"
    },
    {
      "Effect": "Allow",
      "Action": "ecs:AuthorizeSecurityGroup",
      "Resource": "*"
    }
  ]
}
```

将地域、账号 ID、安全组 ID 替换为实际值。`DescribeSecurityGroupAttribute` 与 `RevokeSecurityGroup` 可限定到目标安全组 ARN；ECS 当前将 `AuthorizeSecurityGroup` 定义为不支持资源级授权，所以该 Action 的 `Resource` 必须是 `*`。这意味着 IAM 策略本身不能只把新增规则权限限制到一个安全组 ID；可使用 `ecs:SecurityGroupIpProtocols` 与 `ecs:SecurityGroupSourceCidrIps` 条件键进一步限制协议和来源网段，来源条件需要覆盖下面配置的 DTU 与 QGC CIDR。后端 API 仍只会向环境中配置的目标安全组提交精确端口规则。请保护绑定该角色的 ECS 实例及其元数据凭证。无需授予 `ecs:ModifySecurityGroupRule`、安全组删除、实例操作或 RAM 管理权限。

阿里云控制台操作顺序：

1. RAM 控制台 → 身份管理 → 角色，创建可信服务为 ECS 的普通服务角色。
2. 在 RAM 策略中创建上述自定义策略，将其授权给该角色。
3. ECS 控制台选择正在运行 FastAPI 的实例与正确地域，在实例详情中绑定实例 RAM 角色。
4. 在 ECS 控制台打开该实例所关联的目标安全组，记录地域 ID（如 `cn-hangzhou`）和安全组 ID（如 `sg-...`）。地域和安全组必须属于同一目标。
5. 将下表环境变量写入 FastAPI 的 systemd 环境文件，然后重启 API。

### 环境变量与规则范围

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `FLIGHTLINK_SECURITY_GROUP_PROVIDER` | `disabled` | 生产环境设为 `aliyun`；关闭时只管理本地通道，不调用云 API |
| `FLIGHTLINK_ALIYUN_REGION_ID` | 无 | 目标安全组地域，例如 `cn-hangzhou` |
| `FLIGHTLINK_ALIYUN_SECURITY_GROUP_ID` | 无 | 目标安全组 ID，例如 `sg-...` |
| `FLIGHTLINK_ALIYUN_ECS_ROLE_NAME` | 自动发现 | 可选 ECS 实例 RAM 角色名 |
| `FLIGHTLINK_ALIYUN_UAV_SOURCE_CIDRS` | `0.0.0.0/0` | DTU 来源 IPv4 CIDR 列表，逗号分隔 |
| `FLIGHTLINK_ALIYUN_GCS_SOURCE_CIDRS` | 无 | 启用阿里云功能时必填；QGC 来源 IPv4 CIDR 列表，逗号分隔 |
| `FLIGHTLINK_ALIYUN_API_TIMEOUT_SECONDS` | `10` | ECS OpenAPI 请求超时秒数 |

将下列内容写入 API 环境文件。本项目提供的 `deploy/systemd/flightlink-api.service` 使用 `/etc/flightlink/api.env`：

```ini
FLIGHTLINK_SECURITY_GROUP_PROVIDER=aliyun
FLIGHTLINK_ALIYUN_REGION_ID=cn-hangzhou
FLIGHTLINK_ALIYUN_SECURITY_GROUP_ID=sg-replace-with-your-id
# 可选；留空或删除该行则由 SDK 自动发现实例角色
FLIGHTLINK_ALIYUN_ECS_ROLE_NAME=FlightLinkEcsSecurityGroupRole
FLIGHTLINK_ALIYUN_UAV_SOURCE_CIDRS=0.0.0.0/0
FLIGHTLINK_ALIYUN_GCS_SOURCE_CIDRS=203.0.113.25/32
FLIGHTLINK_ALIYUN_API_TIMEOUT_SECONDS=10
```

将 `cn-hangzhou`、安全组 ID、RAM 角色名和 QGC 示例网段替换为实际值；`203.0.113.25/32` 是文档示例地址，不可直接用于真实 QGC。确认 API unit 引用了该文件后运行 `sudo systemctl daemon-reload` 和 `sudo systemctl restart flightlink-api.service`（若服务 unit 名称不同，请替换）。

DTU 来源可以是 `0.0.0.0/0`，但每架通道只开放其配置的一个 UDP 目标端口，例如 `5760/5760`；不会开放 UDP 全端口。QGC 建议使用固定公网地址的 `/32` CIDR，避免开放给全网。QGC 协议与端口按通道配置，默认为 TCP，也可设为 UDP。当前 `FLIGHTLINK_ALIYUN_GCS_SOURCE_CIDRS` 是全局来源列表，会应用到所有端口组。

后端只撤销 SQLite 中记录为自己创建的安全组规则 ID。已存在的完全匹配外部规则可以复用，但不会被认领或删除；用户手工添加的其他规则不受影响。应用启动时不会自动创建、修改或删除云端规则，已有通道需通过管理员端口组更新显式同步。

### 只读预检

管理员登录后调用 `GET /api/v1/integrations/aliyun/security-group/preflight`。`ready` 且 `read_access_verified=true` 表示实例角色能读取所配置安全组并列出入方向规则；`write_access_checked=false` 是预期结果，因为预检只执行查询，不会试写规则。因此预检成功不能证明 `AuthorizeSecurityGroup` / `RevokeSecurityGroup` 的写权限正确，后续须使用专用测试端口做真实 CRUD 验收。缺少地域、安全组或 QGC CIDR 时会报告 `not_configured`；云查询失败会返回不含凭证的错误信息。

### Ubuntu / Rocky 依赖

`deploy/flightlink-run` 会检查并补齐 Python 3.12、uv、tcpdump、systemd 构建依赖，并在主机尚未安装时从上游源码构建 `mavlink-routerd`。支持 Ubuntu 24.04 和 Rocky Linux 9；ECS 需要能访问软件源、Astral uv 安装源和 GitHub。若 ECS 尚未安装 Git，先按系统安装：Ubuntu 使用 `sudo apt-get update && sudo apt-get install -y git`，Rocky 使用 `sudo dnf install -y git`。

先在阿里云控制台给 ECS 绑定具备本项目所需权限的实例 RAM 角色。安装脚本不会配置主机防火墙或前端 Nginx，也不会把 AccessKey 写入主机。

### ECS、DTU 与 QGC 实际验收

本地自动化测试使用假的云服务与临时 SQLite 数据库；它们验证调用顺序、规则归属和失败补偿，不会联系阿里云，也不能证明 DTU/QGC 实际可达。完成 RAM 角色和服务环境配置后，在目标 ECS 上按顺序验收：

1. 登录管理平台，调用只读预检，确认 `state=ready`、地域和安全组 ID 正确；记录 `write_access_checked=false`。
2. 创建测试通道：DTU UDP `5760`，QGC TCP `14552`，启用通道。检查 API 响应的 `security_group.state=synced`、mavlink-router 服务状态，以及阿里云安全组只出现对应来源/单端口的入方向规则。
3. 配置测试 DTU 将 MAVLink UDP 发往 ECS 公网地址的 `5760`，再让 QGC 连接 ECS 公网地址的 TCP `14552`；确认飞控心跳与 QGC 状态，并检查 `logs`、`packets` 和 `messages`。
4. 如需验证更新流程，可改用另一对端口（例如 UDP `5761` / TCP `14553`，或明确选择 QGC UDP），确认新规则先出现、服务切换成功后旧的后端托管规则才清理，并重新连接验证。
5. 删除测试通道，确认服务先停止，且只删除 FlightLink 记录的测试规则；确认共享/手工规则仍保留。

部署者通过启动脚本输入阿里云地域、安全组 ID 和来源 CIDR；脚本会将参数写入 ECS 的 `/etc/flightlink/api.env`。RAM 角色仍需先在阿里云控制台绑定。不要把凭证或带真实会话 Cookie 的 `.http` 文件提交到代码库。云端实际连通验收应在上述配置完成后进行。

### Ubuntu / Rocky 原生 systemd 一键部署

在 ECS 上克隆后端仓库到固定目录，再运行安装脚本。脚本会先安装和检查运行依赖，再检查阿里云安全组参数；参数缺失时会用中文终端提示补齐地域、安全组 ID、DTU 来源 CIDR 和 QGC 来源 CIDR。DTU 来源网段留空默认 `0.0.0.0/0`，这表示允许任意来源，建议填写具体公网 IP 的 `/32` 网段。RAM 角色名可留空由 SDK 自动发现，**不要输入长期 AccessKey**。运行前先在阿里云控制台将 RAM 角色绑定到这台 ECS 并授予所需权限。若尚无管理员账号，脚本会用中文提示创建用户名和密码；两项配置完成后才启动后端。

```bash
sudo install -d -o root -g root -m 0755 /opt/flightlink
sudo git clone https://github.com/Lehuang0212/FlightLink-backend.git /opt/flightlink/backend
cd /opt/flightlink/backend
sudo bash deploy/flightlink-run
```

脚本会安装并同步依赖，创建专用账号与数据目录，生成权限受限的环境文件，安装 systemd 单元和受限服务助手，并启用 API 开机启动。已存在的环境文件和 SQLite/抓包数据会保留。成功后可用以下命令查看和管理 API：

```bash
sudo systemctl status flightlink-api.service
sudo systemctl restart flightlink-api.service
sudo journalctl -u flightlink-api.service -f
```

后续仍可使用原有的 `ConsoleUseradd` 命令添加管理员；在 ECS 上运行：

```bash
sudo -u flightlink-api env FLIGHTLINK_DATA_DIR=/var/lib/flightlink FLIGHTLINK_DATABASE_PATH=/var/lib/flightlink/flightlink.sqlite3 /opt/flightlink/backend/.venv/bin/ConsoleUseradd
```

后端只监听 ECS 本机 `127.0.0.1:8000`，不要在阿里云安全组开放 TCP `8000`。临时管理可通过 SSH 本地转发访问 API：

```bash
ssh -N -L 8000:127.0.0.1:8000 <ecs-user>@<ecs-public-ip>
```

当前脚本只安装后端。前端构建和 Nginx 反向代理需要单独部署；生产代理应将 `/api/` 转发到 `127.0.0.1:8000`，并通过 HTTPS 提供网页。HTTPS 部署时将 `/etc/flightlink/api.env` 中的 `FLIGHTLINK_SESSION_COOKIE_SECURE` 改为 `true` 后重启 API。

发布后端更新时，在仓库目录执行 `sudo git pull --ff-only`，然后再次运行 `sudo bash deploy/flightlink-run`；脚本会同步依赖、重新加载服务配置并重启 API，不会改动 `/var/lib/flightlink` 数据。

---

# FlightLink-Console backend

FastAPI service for the FlightLink-Console management platform.

## Local setup

Python 3.12 or newer and `uv` are required.

```bash
uv sync
uv run uvicorn flightlink_backend.main:app --host 127.0.0.1 --port 8000 --reload
```

Before starting locally, set `FLIGHTLINK_ROUTER_MANAGER=disabled` so the Windows development machine does not try to control Linux systemd. In PowerShell:

```powershell
$env:FLIGHTLINK_ROUTER_MANAGER = "disabled"
```

The API creates the SQLite database and applies its schema migrations on startup. By default, it stores data in `./data/flightlink.sqlite3` relative to the current working directory.

In another terminal, create an administrator interactively:

```bash
uv run ConsoleUseradd
```

The password is prompted without echo and is never accepted as a command-line argument. Usernames must be 3-64 ASCII letters, digits, dots, underscores or hyphens. Passwords must have at least 8 characters.

## Configuration

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `FLIGHTLINK_DATA_DIR` | `./data` | Data directory used for the default database path |
| `FLIGHTLINK_DATABASE_PATH` | `$FLIGHTLINK_DATA_DIR/flightlink.sqlite3` | Explicit SQLite database path |
| `FLIGHTLINK_ROUTER_CONFIG_DIR` | `$FLIGHTLINK_DATA_DIR/router-configs` | Directory for managed per-channel mavlink-router config files |
| `FLIGHTLINK_CAPTURE_CONFIG_DIR` | `$FLIGHTLINK_DATA_DIR/capture-configs` | Directory for per-channel JSON files consumed by the capture runner |
| `FLIGHTLINK_CAPTURE_DIR` | `$FLIGHTLINK_DATA_DIR/captures` | Rotated PCAP directory |
| `FLIGHTLINK_TCPDUMP_PATH` | `/usr/sbin/tcpdump` | tcpdump executable path, read by the capture unit environment file |
| `FLIGHTLINK_PCAP_ROTATE_SECONDS` | `600` | PCAP rotation interval in seconds (maximum 86400) |
| `FLIGHTLINK_PCAP_RETENTION_SECONDS` | `86400` | Maximum PCAP age in seconds |
| `FLIGHTLINK_PCAP_MAX_BYTES` | `1073741824` | Target maximum total PCAP bytes |
| `FLIGHTLINK_PCAP_CLEANUP_INTERVAL_SECONDS` | `300` | PCAP retention cleanup interval in seconds |
| `FLIGHTLINK_ROUTER_MANAGER` | `systemd` | Router process controller: `systemd` or `disabled` (local development) |
| `FLIGHTLINK_ROUTER_CONTROL_HELPER` | `/usr/local/sbin/flightlink-routerctl` | Root-owned, restricted router control helper path |
| `FLIGHTLINK_SUDO_PATH` | `/usr/bin/sudo` | Non-interactive privilege boundary for the helper |
| `FLIGHTLINK_ROUTER_COMMAND_TIMEOUT_SECONDS` | `10` | Timeout for service control, status and log requests |
| `FLIGHTLINK_SESSION_TTL_SECONDS` | `43200` | Administrator session lifetime |
| `FLIGHTLINK_SESSION_COOKIE_NAME` | `flightlink_session` | HTTP-only session cookie name |
| `FLIGHTLINK_SESSION_COOKIE_SECURE` | `true` | Require HTTPS for the session cookie |

For the SSH local-forward access described below, the example sets `FLIGHTLINK_SESSION_COOKIE_SECURE=false` while the browser connects to `http://127.0.0.1:8000`. This is for local loopback access. Set it to `true` when the management UI/API is served over HTTPS. Run the API and `ConsoleUseradd` with the same database environment settings.

## Initial API

The channel telemetry object reports heartbeat-based vehicle online status; SYSID/COMPID; vehicle type; ArduPilot mode and armed state; battery, GPS, altitude, and ground speed; recent MAVLink packet time and byte rates; packet-sequence link quality; and RADIO_STATUS values if present. Flight-controller STATUSTEXT messages are kept in a bounded in-memory buffer and read from GET /api/v1/channels/{id}/messages?limit=100.

Each channel also gets a persisted local UDP monitor port, normally selected from 40000–49999. The router sends a passive MAVLink copy to 127.0.0.1:<monitor-port>; FastAPI does not bind the DTU listener or enter the command forwarding path.

Link quality follows Mission Planner's MAVLink sequence-gap approach: count missing 8-bit sequence values per vehicle system/component, then calculate received packets divided by received plus inferred lost packets. Weighted received/lost counters decay by 0.8 every five seconds, and the displayed value decays by 0.8 per idle second. Mission Planner documents this HUD indicator as an averaged percentage of good packets and its source computes loss from packet sequence gaps ([Mission Planner HUD documentation](https://ardupilot.org/planner/docs/flight-data-screen.html), [packet-loss implementation](https://github.com/ArduPilot/MissionPlanner/blob/master/ExtLibs/ArduPilot/Mavlink/MAVLinkInterface.cs)). This is MAVLink transport quality, not cellular RSRP/RSRQ. RADIO_STATUS reports telemetry-radio metrics and must not be presented as E840-TTL/EC05-DNC cellular signal strength.

For TCP, ground-station connection state and peer address are read from Linux /proc/net/tcp*. For UDP, the service uses recent GCS MAVLink heartbeats and packet summaries to report the remote peer address. Packet capture also identifies the DTU's inbound source address as telemetry.uav_peer. Ground-station state and flight-controller heartbeat state are reported separately.

Per-channel packet capture provides near-real-time summaries from `GET /api/v1/channels/{id}/packets?limit=100`. The bounded in-memory buffer holds at most 500 summaries per channel; the UI can poll this endpoint. A second line-buffered tcpdump process sends summaries over loopback to the existing telemetry listener. Summaries are not written to SQLite or emitted one-by-one to journald.

Read rotated PCAP metadata from `GET /api/v1/channels/{id}/captures`; download a closed segment from `GET /api/v1/channels/{id}/captures/{file_name}`. The API rejects downloads of the newest segment while it is being written. By default, tcpdump rotates every 600 seconds. A background cleanup runs every 300 seconds, removes files older than 24 hours, and removes oldest files to target a 1 GiB total. A recent newest segment is protected while it is in its rotation window. If the active segment alone exceeds the configured capacity, storage can temporarily exceed the target until rotation. Capture filters include only the channel's UAV UDP port and its configured ground-station TCP or UDP port.

Live telemetry and the most recent 100 STATUSTEXT messages stay in memory; the API does not write each MAVLink packet to SQLite. SQLite WAL is used for low-frequency admin, channel, and session data.

- `GET /api/v1/health` — application and database health.
- `POST /api/v1/auth/login` — verifies credentials and sets an HTTP-only session cookie.
- `POST /api/v1/auth/logout` — revokes the current session and clears the cookie.
- `GET /api/v1/auth/me` — returns the authenticated administrator.
- `GET /api/v1/integrations/aliyun/security-group/preflight` — admin-only, read-only check of configured Aliyun security-group query access and ingress rules.
- `GET /api/v1/channels` — list configured port channels (administrator session required).
- `POST /api/v1/channels` — create a port channel and generate its mavlink-router config.
- `GET /api/v1/channels/{id}` — return a port channel.
- `PUT /api/v1/channels/{id}` — replace its port/protocol settings, regenerate its config, then restart or stop the service to match `enabled`.
- `DELETE /api/v1/channels/{id}` — stop its service, then remove the port channel and config.
- `POST /api/v1/channels/{id}/restart` — restart an enabled router channel.
- `GET /api/v1/channels/{id}/logs?limit=100` — return recent systemd journal lines (limit 1–500).
- `GET /api/v1/channels/{id}/packets?limit=100` — return recent in-memory packet summaries (maximum 500).
- `GET /api/v1/channels/{id}/captures` — list rotated PCAP segments.
- `GET /api/v1/channels/{id}/captures/{file_name}` — download a closed PCAP segment; the current segment returns HTTP 409.

Each channel pairs one UDP listener for the UAV/4G DTU with one ground-station listener. Ground station protocol defaults to TCP and can be set to UDP. Port conflicts are checked per transport, so a TCP listener and UDP listener may use the same numeric port. Router configs are written atomically as `channel-<uuid>.conf`. Each channel is controlled by a systemd template instance named `flightlink-router@<uuid>.service`; the channel UUID is a server-side configuration key, not an aircraft hardware identity. At API startup, configs are synchronized and enabled channels are started if they are not already active. Service output is captured by journald.

Channel responses include `runtime.state` (`active`, `inactive`, `failed`, `starting`, `stopping`, or `unknown`), process ID, activation time, systemd result, and any control error. `unknown` is returned when process management is disabled or unavailable. Logs are read from the channel's systemd journal, with the API limiting each response to at most 500 lines.

## Alibaba Cloud security-group configuration

The security-group provider is disabled by default. When enabled, FastAPI uses the ECS instance RAM role attached to the host running the API to call ECS OpenAPI. Do not put a long-term AccessKey in an environment file, database, or request. The backend manages Alibaba Cloud ingress rules only; it does not change Ubuntu/Rocky host-firewall rules or egress rules.

### RAM role and permissions

In the RAM console, create a standard role whose trusted entity is Alibaba Cloud service / Elastic Compute Service (ECS), attach a custom policy, and attach the role to the ECS instance running FastAPI as its Instance RAM Role. For an existing instance, use the role assignment action on the ECS instance details page. An ECS instance can have only one instance RAM role; if one is already attached, add this policy to that role instead of replacing it. The ECS SDK obtains temporary credentials from instance metadata. `FLIGHTLINK_ALIYUN_ECS_ROLE_NAME` is optional; leave it unset for automatic discovery.

The policy needs these three actions:

```json
{
  "Version": "1",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ecs:DescribeSecurityGroupAttribute",
        "ecs:RevokeSecurityGroup"
      ],
      "Resource": "acs:ecs:cn-hangzhou:1234567890123456:securitygroup/sg-example"
    },
    {
      "Effect": "Allow",
      "Action": "ecs:AuthorizeSecurityGroup",
      "Resource": "*"
    }
  ]
}
```

Replace the region, account ID, and security-group ID. `DescribeSecurityGroupAttribute` and `RevokeSecurityGroup` support a target security-group ARN. ECS currently defines `AuthorizeSecurityGroup` as not supporting resource-level authorization, so its `Resource` must be `*`. This means RAM cannot scope that add-rule permission to one security-group ID. RAM conditions `ecs:SecurityGroupIpProtocols` and `ecs:SecurityGroupSourceCidrIps` can further restrict protocols and source ranges; include the DTU and GCS CIDRs configured below. The backend itself submits exact-port rules only to the security group from its environment. Protect the ECS instance and its role credentials. Do not grant `ecs:ModifySecurityGroupRule`, security-group deletion, instance-management, or RAM-management permissions.

Console setup:

1. In RAM, open Identity Management → Roles and create a standard service role trusted by ECS.
2. Create the custom policy above and grant it to that role.
3. In ECS, select the region and instance running FastAPI, then attach the role as its Instance RAM Role.
4. Open the security group associated with that instance and note its region ID (for example, `cn-hangzhou`) and security-group ID (for example, `sg-...`). Both values must identify the same target.
5. Add the environment variables below to the FastAPI systemd environment file and restart the API.

Official references: [create a RAM role for an Alibaba Cloud service](https://www.alibabacloud.com/help/en/ram/user-guide/create-a-ram-role-for-a-trusted-alibaba-cloud-service), [attach an instance RAM role](https://www.alibabacloud.com/help/en/ecs/user-guide/attach-an-instance-ram-role-to-an-ecs-instance), [DescribeSecurityGroupAttribute](https://www.alibabacloud.com/help/en/ecs/developer-reference/api-ecs-2014-05-26-describesecuritygroupattribute), [AuthorizeSecurityGroup](https://www.alibabacloud.com/help/en/ecs/developer-reference/api-ecs-2014-05-26-authorizesecuritygroup), and [RevokeSecurityGroup](https://www.alibabacloud.com/help/en/ecs/developer-reference/api-ecs-2014-05-26-revokesecuritygroup).

### Environment variables and rule scope

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `FLIGHTLINK_SECURITY_GROUP_PROVIDER` | `disabled` | Set to `aliyun` in production; disabled mode manages local channels only and makes no cloud calls |
| `FLIGHTLINK_ALIYUN_REGION_ID` | unset | Region of the target security group, such as `cn-hangzhou` |
| `FLIGHTLINK_ALIYUN_SECURITY_GROUP_ID` | unset | Target security-group ID, such as `sg-...` |
| `FLIGHTLINK_ALIYUN_ECS_ROLE_NAME` | auto-discovered | Optional ECS instance RAM role name |
| `FLIGHTLINK_ALIYUN_UAV_SOURCE_CIDRS` | `0.0.0.0/0` | Comma-separated IPv4 CIDRs allowed to send DTU traffic |
| `FLIGHTLINK_ALIYUN_GCS_SOURCE_CIDRS` | unset | Required in Aliyun mode; comma-separated IPv4 CIDRs allowed to connect from QGC |
| `FLIGHTLINK_ALIYUN_API_TIMEOUT_SECONDS` | `10` | ECS OpenAPI timeout |

Put these values in the API environment file. The supplied `deploy/systemd/flightlink-api.service` reads `/etc/flightlink/api.env`:

```ini
FLIGHTLINK_SECURITY_GROUP_PROVIDER=aliyun
FLIGHTLINK_ALIYUN_REGION_ID=cn-hangzhou
FLIGHTLINK_ALIYUN_SECURITY_GROUP_ID=sg-replace-with-your-id
# Optional; omit this line to let the SDK discover the instance role
FLIGHTLINK_ALIYUN_ECS_ROLE_NAME=FlightLinkEcsSecurityGroupRole
FLIGHTLINK_ALIYUN_UAV_SOURCE_CIDRS=0.0.0.0/0
FLIGHTLINK_ALIYUN_GCS_SOURCE_CIDRS=203.0.113.25/32
FLIGHTLINK_ALIYUN_API_TIMEOUT_SECONDS=10
```

Replace the region, security-group ID, role name, and example QGC range. `203.0.113.25/32` is documentation-only and must not be used for a real QGC client. Once the API unit references this file, run `sudo systemctl daemon-reload` and `sudo systemctl restart flightlink-api.service` (replace the unit name if your installation uses another name).

The DTU source may be `0.0.0.0/0`, but each channel opens only its configured single UDP destination port, for example `5760/5760`; it does not open all UDP ports. Prefer a fixed public QGC address with a `/32` CIDR. The ground-station protocol and port are per channel, with TCP as the default and UDP as an option. `FLIGHTLINK_ALIYUN_GCS_SOURCE_CIDRS` is currently global and applies to every channel.

The backend revokes only security-group rule IDs recorded as created by FlightLink in SQLite. An exact matching rule created outside FlightLink may be reused, but it is never claimed or deleted. Other manually managed rules remain untouched. Application startup does not create, update, or delete cloud rules; explicitly update existing channels through the admin API to synchronize them.

### Read-only preflight

After admin login, call `GET /api/v1/integrations/aliyun/security-group/preflight`. A `ready` response with `read_access_verified=true` means the instance role can read the configured security group and list its ingress rules. `write_access_checked=false` is expected: preflight performs queries only and does not attempt a rule write. Preflight does not verify write permissions, so a successful response does not prove that `AuthorizeSecurityGroup` or `RevokeSecurityGroup` works; verify them with a real test-port CRUD cycle. Missing region, security-group ID, or GCS CIDRs return `not_configured`; a cloud query failure returns a credential-sanitized error.

### Ubuntu and Rocky Linux dependencies

`deploy/flightlink-run` checks and installs Python 3.12, uv, tcpdump, systemd build dependencies, and builds upstream `mavlink-routerd` if it is not already installed. It supports Ubuntu 24.04 and Rocky Linux 9. The ECS needs access to the OS package repositories, Astral's uv installer, and GitHub. If Git is missing, install it first (`sudo apt-get update && sudo apt-get install -y git` on Ubuntu; `sudo dnf install -y git` on Rocky).

Attach an ECS instance RAM role with this project's required permissions in the Alibaba Cloud console before installation. The script does not configure the host firewall or front-end Nginx, and it never asks for or stores an AccessKey.

### ECS, DTU, and QGC acceptance checklist

Local automated tests use fake cloud providers and temporary SQLite databases. They verify call order, rule ownership, and compensation behavior, but they do not contact Alibaba Cloud or prove real DTU/QGC connectivity. After configuring the ECS role and service environment, perform this acceptance on the target ECS:

1. Sign in to the management platform and run read-only preflight. Confirm `state=ready` and the expected region/security-group ID; note `write_access_checked=false`.
2. Create a test channel with DTU UDP `5760`, QGC TCP `14552`, and the channel enabled. Check `security_group.state=synced`, the mavlink-router service state, and that Alibaba Cloud shows only the required source/one-port ingress rules.
3. Configure the test DTU to send MAVLink UDP to the ECS public address on `5760`; connect QGC to the ECS public address on TCP `14552`. Confirm the flight-controller heartbeat and QGC state, then inspect `logs`, `packets`, and `messages`.
4. To verify the update flow, change the channel to another pair, such as UDP `5761` / TCP `14553` (or explicitly choose QGC UDP). Confirm replacement rules are added first, old FlightLink-owned rules are removed only after the service switches successfully, and both clients reconnect.
5. Delete the test channel. Confirm services stop first and only FlightLink-owned test rules are removed; shared or manually created rules must remain.

The deployer enters the Alibaba Cloud region, security-group ID, and source CIDRs in the installer; it writes them to `/etc/flightlink/api.env`. Attach the RAM role in the Alibaba Cloud console first. Do not commit credentials or an `.http` file containing a real session cookie. Real cloud and hardware acceptance takes place only after that configuration is complete.

### One-command native systemd setup on Ubuntu / Rocky

Clone the backend repository to the fixed path on the ECS and run the installer. It checks and installs the runtime dependencies first. When Alibaba Cloud security-group settings are incomplete, it prompts in Chinese for the region, security-group ID, UAV source CIDR, and QGC source CIDR. Leaving the UAV source range blank defaults to `0.0.0.0/0`, which allows any source; use a specific public IP `/32` when possible. Leave the optional RAM role name blank for SDK discovery. **Do not enter a long-lived AccessKey.** Attach the RAM role to the ECS and grant it the required permissions in the Alibaba Cloud console before running the script. If no administrator exists, the script also prompts in Chinese for the first username and password. The API starts only after both the cloud settings and an administrator are ready.

```bash
sudo install -d -o root -g root -m 0755 /opt/flightlink
sudo git clone https://github.com/Lehuang0212/FlightLink-backend.git /opt/flightlink/backend
cd /opt/flightlink/backend
sudo bash deploy/flightlink-run
```

The script installs and syncs dependencies, creates dedicated service accounts and data directories, writes protected environment files, installs the systemd units and restricted service helper, and enables API startup at boot. Existing environment files and SQLite/capture data are preserved. Manage the API with:

```bash
sudo systemctl status flightlink-api.service
sudo systemctl restart flightlink-api.service
sudo journalctl -u flightlink-api.service -f
```

The existing `ConsoleUseradd` command remains available for adding administrators later:

```bash
sudo -u flightlink-api env FLIGHTLINK_DATA_DIR=/var/lib/flightlink FLIGHTLINK_DATABASE_PATH=/var/lib/flightlink/flightlink.sqlite3 /opt/flightlink/backend/.venv/bin/ConsoleUseradd
```

The backend listens only on the ECS loopback address `127.0.0.1:8000`. Do not open TCP `8000` in the Alibaba Cloud security group. For temporary administration, create an SSH local-forward:

```bash
ssh -N -L 8000:127.0.0.1:8000 <ecs-user>@<ecs-public-ip>
```

This script installs the backend only. Build and deploy the front end and its Nginx reverse proxy separately; in production Nginx should forward `/api/` to `127.0.0.1:8000` and serve the web UI over HTTPS. For HTTPS, set `FLIGHTLINK_SESSION_COOKIE_SECURE=true` in `/etc/flightlink/api.env` and restart the API.

To deploy a backend update, run `sudo git pull --ff-only` in the repository, then run `sudo bash deploy/flightlink-run` again. The script syncs dependencies, reloads service configuration, and restarts the API without changing `/var/lib/flightlink` data.

There is no public registration endpoint. Administrator accounts are created only through ConsoleUseradd.
