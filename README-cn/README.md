# FlightLink-Console 后端

[中文](README.md) | [English](../README-us/README.md) | [仓库首页](../README.md)

## 项目介绍

FlightLink-Console 后端用于管理“飞控 + 4G DTU + ECS + QGC”的 MAVLink 通信链路。以一对服务器端口识别通信通道，例如 DTU 的 UDP `5760` 与 QGC 的 TCP `14552`；端口组可以连接不同无人机，不绑定固定设备身份。

支持端口组增删改、Router 服务控制、阿里云安全组同步、管理员登录、实时遥测、飞控文字消息、Router 日志、实时抓包摘要和 PCAP 下载。[前端仓库](https://github.com/Lehuang0212/FlightLink-fronted)。

## 项目结构

```text
src/flightlink_backend/
├── api/                 API 路由
├── schemas/             请求与响应模型
├── services/            Router、安全组、遥测、抓包服务
├── config.py            环境变量配置
├── database.py          SQLite 与迁移
├── security.py          密码和会话
├── cli.py               ConsoleUseradd
└── main.py              FastAPI 入口
deploy/
├── flightlink-run       Ubuntu / Rocky 安装入口
├── sbin/                受限服务控制与抓包助手
└── systemd/             API、Router、抓包服务单元
tests/                   自动化测试
docs/                    设计和实施文档
.env.example             环境配置示例
test_main.http           API 调用示例
pyproject.toml / uv.lock  依赖配置与锁定
```

## 实现说明

- Python 3.12、FastAPI、Uvicorn、pymavlink；SQLite WAL 保存账号、会话与端口配置。
- 每个端口组对应一个 Router 配置和 `flightlink-router@UUID.service`，实例运行在 ECS。
- Router 将遥测副本转发到本机 UDP，后端被动解析；遥测、消息和摘要保存在内存。
- 后端不主动请求数据流；无 QGC 时显示飞控已经发送的数据。链路质量是 MAVLink 序号估算值，不是 4G 信号强度。
- tcpdump 默认每 10 分钟切片，保留 24 小时，目标容量 1 GiB；仅已关闭的 PCAP 可以下载。
- 管理员仅由命令行添加；安全组通过 ECS 实例 RAM 角色临时凭证操作。修改端口使用新增、撤销规则，不调用原地修改接口。

## 使用教程

### 1. 准备 ECS

支持 Ubuntu 24.04、Rocky Linux 9，使用 systemd。先完成文末 [RAM 角色配置](#阿里云安全组配置)，保留 SSH 入方向规则。以下按主机防火墙已关闭、阿里云安全组管理公网入口部署。

**Ubuntu 24.04：**

```bash
sudo apt-get update
sudo apt-get install -y git
```

**Rocky Linux 9：**

```bash
sudo dnf install -y git
```

### 2. 部署后端

必须克隆到 `/opt/flightlink/backend`：

```bash
sudo install -d -o root -g root -m 0755 /opt/flightlink
sudo git clone https://github.com/Lehuang0212/FlightLink-backend.git /opt/flightlink/backend
cd /opt/flightlink/backend
sudo bash deploy/flightlink-run
```

按提示填写：

| 项目 | 填写内容 |
| --- | --- |
| 地域 ID | ECS 所在地域，例如 `cn-hangzhou` |
| 安全组 ID | ECS 关联的目标 `sg-...` |
| DTU 来源 CIDR | `0.0.0.0/0` |
| QGC 来源 CIDR | QGC 电脑公网 IPv4 加 `/32`；多个地址用逗号分隔 |
| RAM 角色名称 | 留空自动发现，或填写已绑定角色名 |
| 管理员 | 按提示输入真实用户名、密码 |

脚本安装依赖、Router、服务助手和 systemd 单元；环境写入 `/etc/flightlink/api.env`，数据位于 `/var/lib/flightlink`。已有配置和数据保留。

```bash
sudo systemctl status flightlink-api.service --no-pager
curl -fsS http://127.0.0.1:8000/api/v1/health
```

后端只监听 `127.0.0.1:8000`。管理网页由前端 Nginx 提供，不开放公网 `8000`。

### 3. 环境配置

```bash
sudo vi /etc/flightlink/api.env
```

```ini
FLIGHTLINK_SECURITY_GROUP_PROVIDER=aliyun
FLIGHTLINK_ALIYUN_REGION_ID=cn-hangzhou
FLIGHTLINK_ALIYUN_SECURITY_GROUP_ID=sg-替换为实际安全组ID
FLIGHTLINK_ALIYUN_ECS_ROLE_NAME=
FLIGHTLINK_ALIYUN_UAV_SOURCE_CIDRS=0.0.0.0/0
FLIGHTLINK_ALIYUN_GCS_SOURCE_CIDRS=替换为QGC公网IPv4/32
FLIGHTLINK_ALIYUN_API_TIMEOUT_SECONDS=10
FLIGHTLINK_SESSION_COOKIE_SECURE=false
```

`cn-hangzhou` 也须按实际地域替换。HTTP 实测使用 `false`；HTTPS 使用 `true`。其他配置见 [.env.example](../.env.example)。

```bash
sudo systemctl restart flightlink-api.service
```

### 4. 添加管理员

首次部署由脚本提示创建；之后使用：

```bash
sudo -u flightlink-api env \
  FLIGHTLINK_DATA_DIR=/var/lib/flightlink \
  FLIGHTLINK_DATABASE_PATH=/var/lib/flightlink/flightlink.sqlite3 \
  /opt/flightlink/backend/.venv/bin/ConsoleUseradd
```

### 5. 连接无人机和 QGC

1. 按[前端部署教程](https://github.com/Lehuang0212/FlightLink-fronted/blob/main/README-cn/README.md)部署网页并登录。
2. 同一浏览器打开 `http://ECS公网IP:18443/api/v1/integrations/aliyun/security-group/preflight`，确认 `state=ready`。HTTPS 部署使用对应 HTTPS 地址。
3. 预检为只读，`write_access_checked=false` 正常；它不验证写权限。
4. 新增并启用端口组：UDP `5760` / TCP `14552`，确认安全组已同步、Router 和抓包服务运行。
5. DTU 配置：UDP → ECS 公网 IP → `5760`。
6. QGC 配置：TCP → ECS 公网 IP → `14552`。
7. 查看遥测、Router 日志、飞控消息、实时抓包摘要；分片关闭后下载 PCAP。
8. 需要验收修改、删除时，可临时改为 UDP `5761` / TCP `14553` 并同步修改客户端，再删除测试组。

### 6. 更新与日志

```bash
cd /opt/flightlink/backend
sudo git pull --ff-only origin main
sudo bash deploy/flightlink-run
sudo journalctl -u flightlink-api.service -n 100 --no-pager
sudo journalctl -u 'flightlink-router@*.service' -n 100 --no-pager
```

更新抓包助手后，若已有抓包实例仍在运行，替换下方 UUID 并重启该实例：

```bash
sudo systemctl restart flightlink-capture@端口组UUID.service
```

## 阿里云安全组配置

### 1. 创建实例 RAM 角色

RAM 控制台 → 身份管理 → 角色 → 创建角色：

| 配置 | 示例 |
| --- | --- |
| 可信实体 | 阿里云服务 / 云服务 |
| 可信服务 | 云服务器 ECS |
| 角色类型 | 普通服务角色（页面提供此选项时） |
| 角色名称 | `FlightLinkEcsRole` |

角色的信任策略应包含：

```json
{
  "Version": "1",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Service": ["ecs.aliyuncs.com"]
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
```

`Principal.Service` 指定可信服务 ECS；`sts:AssumeRole` 允许该服务扮演角色。**信任策略不授予安全组操作权限**，操作权限由下一步策略提供。[官方角色教程](https://www.alibabacloud.com/help/en/ram/user-guide/create-a-ram-role-for-a-trusted-alibaba-cloud-service)

### 2. 创建自定义权限策略

RAM 控制台 → 权限管理 → 权限策略 → 创建权限策略 → 脚本编辑。名称示例：`FlightLinkSecurityGroupPolicy`。

```json
{
  "Version": "1",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ecs:DescribeSecurityGroupAttribute",
        "ecs:AuthorizeSecurityGroup",
        "ecs:RevokeSecurityGroup"
      ],
      "Resource": "*"
    }
  ]
}
```

策略解释：

| 字段 / 操作 | 含义 |
| --- | --- |
| `Version: "1"` | RAM 策略语法版本 |
| `Statement` | 权限规则列表 |
| `Effect: "Allow"` | 允许所列操作 |
| `DescribeSecurityGroupAttribute` | 读取目标安全组规则，支持预检和同步 |
| `AuthorizeSecurityGroup` | 添加 DTU / QGC 入方向规则 |
| `RevokeSecurityGroup` | 撤销后端记录并托管的规则 |
| `Resource: "*"` | 这些操作适用于账号下所有匹配资源，不是仅限某一个安全组 |

此示例便于部署；后端仅操作环境文件配置的安全组，但该配置**不是 RAM 权限边界**。需要更窄权限时按官方授权表限制支持资源级授权的操作。当前代码不需要 `ecs:ModifySecurityGroupRule` 或 `ecs:DescribeSecurityGroups`。[自定义策略教程](https://www.alibabacloud.com/help/zh/ram/create-a-custom-policy)，[ECS 授权表](https://www.alibabacloud.com/help/en/ram/api-elastic-compute-service)。

### 3. 授权并绑定 ECS

1. RAM 角色 `FlightLinkEcsRole` → 新增授权 → 附加 `FlightLinkSecurityGroupPolicy`。
2. ECS 控制台 → 选择地域、实例 → 实例设置 → 授予或收回 RAM 角色 → 选择该角色。
3. 实例详情 → 安全组，记录实际安全组 ID；地域 ID 可在 ECS / OpenAPI 页面查看，例如 `cn-hangzhou`。
4. 将这些值填写到安装提示或 `/etc/flightlink/api.env`，重启 API。SDK 自动获取临时凭证，不填写长期 AccessKey。

如果 ECS 已绑定其他角色，将策略附加到现有角色，避免直接替换。[官方绑定教程](https://www.alibabacloud.com/help/zh/ecs/user-guide/attach-an-instance-ram-role-to-an-ecs-instance)

### 4. 设置网络入口

| 用途 | 入方向规则 | 来源 |
| --- | --- | --- |
| SSH | 当前 SSH TCP 端口 | 管理员公网 IPv4 /32 |
| 管理网页 | TCP `18443`，手动添加 | 管理员公网 IPv4 /32 |
| DTU | UDP `5760`，后端创建 | `0.0.0.0/0` |
| QGC | TCP `14552`，后端创建 | QGC 公网 IPv4 /32 |

来源使用公网地址；地址变化后更新对应配置或规则。后端不修改主机防火墙，也不管理安全组出方向规则。
