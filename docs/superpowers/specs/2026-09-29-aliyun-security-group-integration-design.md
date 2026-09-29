# FlightLink-Console 阿里云安全组集成设计

## 目标

让已登录管理员通过现有端口组 API 管理阿里云 ECS 安全组的入站规则，使无人机 DTU 的 UDP 端口和地面站选定的 TCP/UDP 端口与 mavlink-router 配置保持一致。增加一个只读预检接口，并提供在 ECS 上完成真实端口增删改和连通验收的操作说明。

## 已确认的边界

- 目标系统包括 Ubuntu 与 Rocky Linux。
- 后端仅调用阿里云 ECS 安全组 OpenAPI，不执行 UFW、firewalld、nftables 或其他主机防火墙命令。
- FastAPI、systemd 与 mavlink-router 当前部署在同一台 ECS；RAM 实例角色绑定到运行 FastAPI 的这台 ECS。
- 云端地域、安全组 ID、可选 RAM 角色名和来源 CIDR 由部署者写入服务环境配置；后端不保存长期 AccessKey。
- 当前阶段不做前端，也不在开发环境中直接修改真实安全组。
- 启动时不自动为已有端口组新增云端规则，避免升级部署时意外改变公网入站权限。启动协调继续按现有行为恢复已启用的本地路由与抓包服务；迁移后的端口组状态为 `pending`，网络可达性须通过预检和后端更新操作显式同步后确认。新建端口组只有在其云规则同步成功后才启动路由。

## 架构

新增独立的阿里云安全组服务，使用阿里云 ECS Python SDK V2 和 ECS RAM 实例角色凭证。服务只向指定地域、指定安全组发起请求；地域与安全组 ID 缺失或云服务未启用时，预检明确报告未配置状态。

安全组服务接口包含只读查询、确保规则存在、列出端口组管理的规则和按规则 ID 撤销规则。测试使用假的 ECS API 客户端验证调用与失败路径，不需要本机访问阿里云元数据服务。

所有云操作由现有管理员会话保护。预检接口建议为 `GET /api/v1/integrations/aliyun/security-group/preflight`。它使用 `DescribeSecurityGroupAttribute` 检查实例凭证是否可用、指定安全组是否可查询，并列出入站规则及后端规则记录差异。预检只验证读取权限；安全组写入权限要通过后续创建端口组实际验证。

## 安全组规则与归属

每个端口组需要两类入站规则：

| 角色 | 协议与端口 | 来源 |
| --- | --- | --- |
| 无人机/4G DTU | UDP，端口组的 `uav_udp_port` | `FLIGHTLINK_ALIYUN_UAV_SOURCE_CIDRS` |
| 地面站 | TCP 或 UDP，端口组的 `ground_station_port` | `FLIGHTLINK_ALIYUN_GCS_SOURCE_CIDRS` |

CIDR 配置为逗号分隔的 IPv4 CIDR 列表；每个 CIDR 生成一条规则。规则策略为 `Accept`，目的端口范围严格限定为端口组的单个监听端口（例如 `5760/5760` 或 `5761/5761`），不开放更大的端口段。按用户确认，DTU 来源规则使用 `0.0.0.0/0`，以适配可能变化的 4G 运营商出口地址；这只对已配置的无人机 UDP 监听端口生效。地面站优先配置固定公网地址的 `/32`。

每条由后端创建的规则使用包含端口组 UUID 和角色的专用描述标记。新增 API 返回请求 ID，但安全组规则 ID 通过 `DescribeSecurityGroupAttribute` 读取。后端仅记录和撤销带有自身标记的规则，并以规则 ID 执行撤销。若发现已存在但不带后端标记的完全匹配规则，则将其视作外部规则，不据为己有，也不在端口组删除时移除。

SQLite 增加安全组规则映射表，记录通道、角色、协议、端口、来源 CIDR、云规则 ID 和当前同步状态；端口组记录当前安全组同步状态及最近错误。授权成功但后续查询暂时失败时，映射行允许云规则 ID 为空，并以描述标记和期望规则参数进入 `pending_discovery`，以便稍后重新发现。规则记录使用本地数据库主键，云规则 ID 唯一但可空；删除端口组时，在所有被管理规则成功撤销或确认已不存在前保留这些记录，支持失败后重试。

端口组对外返回 `security_group.state`，取值为 `disabled`（provider 未启用）、`pending`（等待同步）、`synced`（所需规则已确认存在）、`cleanup_pending`（新配置可用但旧规则尚未清理）或 `error`（同步失败）。规则记录状态包括 `pending_discovery`（等待按描述标记发现云规则 ID）、`active` 和 `pending_revoke`。同时返回可展示的最近错误，不返回凭证或完整 API 请求签名。

## 端口组生命周期与失败处理

### 创建

1. 检查本地传输端口冲突，并创建端口组数据库记录和路由配置，状态先标记为待同步。
2. 查询目标安全组；对缺失的规则调用 `AuthorizeSecurityGroup`，再查询规则 ID 并保存映射。
3. provider 为 `aliyun` 时，云规则全部可用后标记为已同步，启动路由与抓包服务；provider 为 `disabled` 时跳过云操作并沿用当前本地创建与启动行为。
4. 若云操作部分成功后失败，保留端口组和成功创建的规则映射，标记错误且不启动新通道的路由；API 仍返回已创建资源及 `security_group.state=error`，管理员可通过相同配置的更新请求重试。

### 更新

1. 对新协议、端口或来源集合先新增缺失规则，不提前撤销旧规则。
2. 更新本地数据库与路由配置，并重启/停止服务以符合 `enabled` 设置。
3. 路由服务达到预期状态后，按规则 ID 撤销已不需要的旧规则并清理映射。
4. 云新增失败时保留旧配置；路由切换失败或旧规则撤销失败时保留仍可能被使用的规则，并报告待处理状态。不会因为清理失败而误删仍在运行的通道。

新增规则部分成功、但后续更新失败时，后端尝试撤销本次新建的规则；若补偿撤销也失败，则把规则映射保留为 `pending_revoke`，将端口组标记为 `cleanup_pending`，供后续更新或删除重试。

更新不调用 `ModifySecurityGroupRule`，而采用“先授权新规则、确认切换、再撤销旧规则”，以支持协议切换并减少中断。RAM 策略因此不需要 `ecs:ModifySecurityGroupRule`。

### 删除

1. 先停止抓包和 mavlink-router；若停止失败，不触碰安全组，也不删除端口组。
2. 按数据库记录的规则 ID 撤销后端管理的安全组规则；对于仍在 `pending_discovery` 的规则，先用描述标记和规则参数重新查询规则 ID，若云端已无对应规则则视为已撤销。
3. 云规则全部撤销后，移除本地路由/抓包配置、数据库映射和端口组记录。
4. 若撤销失败，保留端口组及失败映射，服务保持停止，管理员修复云权限或网络后重试删除。

## 部署配置

| 环境变量 | 说明 |
| --- | --- |
| `FLIGHTLINK_SECURITY_GROUP_PROVIDER` | `disabled` 或 `aliyun`；默认 `disabled`，生产 ECS 设置为 `aliyun` |
| `FLIGHTLINK_ALIYUN_REGION_ID` | 阿里云功能启用时必填；安全组所在地域，例如 `cn-hangzhou` |
| `FLIGHTLINK_ALIYUN_SECURITY_GROUP_ID` | 阿里云功能启用时必填；目标安全组 ID，例如 `sg-...` |
| `FLIGHTLINK_ALIYUN_ECS_ROLE_NAME` | 可选；ECS 实例 RAM 角色名，留空时由 SDK 从实例元数据自动发现 |
| `FLIGHTLINK_ALIYUN_UAV_SOURCE_CIDRS` | DTU 来源 IPv4 CIDR 列表，逗号分隔；默认 `0.0.0.0/0`，规则仅开放各端口组的单个 UDP 监听端口 |
| `FLIGHTLINK_ALIYUN_GCS_SOURCE_CIDRS` | 阿里云功能启用时必填；QGC 来源 IPv4 CIDR 列表，逗号分隔 |
| `FLIGHTLINK_ALIYUN_API_TIMEOUT_SECONDS` | ECS API 请求超时，默认 10 秒 |

不得在环境文件中配置长期 AccessKey。ECS 角色需绑定到 FastAPI 所在 ECS。角色策略所需 API Action 为 `ecs:DescribeSecurityGroupAttribute`、`ecs:AuthorizeSecurityGroup` 和 `ecs:RevokeSecurityGroup`。添加规则 API 当前要求 `Resource: "*"`；查询和撤销规则可限制到目标安全组 ARN，并可通过协议和来源 CIDR 条件进一步约束。

本设计针对当前 IPv4 DTU/QGC 接入；IPv6 来源规则暂不纳入本阶段。

## Ubuntu 与 Rocky Linux

SDK 集成、端口校验和数据库规则映射均为 Python 实现，不依赖发行版防火墙工具。运行时沿用 systemd、journal 和 Linux `/proc` 能力，不写发行版特定的防火墙配置。部署文档将同时给出 Ubuntu `apt` 与 Rocky `dnf` 的依赖安装方式，并要求按系统实际安装位置配置 `FLIGHTLINK_TCPDUMP_PATH`。不宣称本阶段已在两种发行版的真实 ECS 上分别运行验证。

## 验收

### 自动化验收

- 预检：provider disabled、缺配置、RAM 凭证/查询错误、安全组无规则及分页规则响应。
- 创建：分别生成 UDP 无人机与 TCP/UDP 地面站规则；精确识别外部重复规则；云 API 中途失败时保存部分状态并阻止服务启动。
- 更新：端口修改、TCP/UDP 切换、先开新规则后撤销旧规则、授权/重启/清理失败时的状态与保留行为。
- 删除：仅撤销后端管理的规则；服务无法停止或云端撤销失败时保留端口组记录以便重试。
- SQLite 迁移：现有 schema 3 数据升级后可正常读写新规则记录。

### ECS 实际验收

1. 在部署了新后端并绑定 RAM 角色的 ECS 上设置上述环境变量，调用只读预检。
2. 使用测试端口组，例如无人机 UDP `5761`、地面站 TCP `14553`；查询阿里云规则和本机监听状态。
3. 让已配置为目标 UDP 端口的 DTU 发包，并从 QGC 建立 TCP 连接，确认遥测/远控双向可用。
4. 将测试端口改为新端口并确认新规则先出现、服务切换后旧规则撤销；最后删除端口组并确认后端管理的规则已撤销。

自动化测试可在开发机模拟 ECS API；真实云端操作与无线链路验收必须在用户配置了 RAM 角色、地域、安全组及来源网段的 ECS 上执行。

## 官方接口参考

- [ECS Python SDK V2 示例](https://www.alibabacloud.com/help/en/ecs/developer-reference/example-on-how-to-use-ecs-sdk-for-python)
- [Python SDK ECS RAM 角色凭证](https://www.alibabacloud.com/help/en/sdk/developer-reference/v2-manage-python-access-credentials)
- [AuthorizeSecurityGroup](https://www.alibabacloud.com/help/en/ecs/developer-reference/api-ecs-2014-05-26-authorizesecuritygroup)
- [DescribeSecurityGroupAttribute](https://www.alibabacloud.com/help/en/ecs/developer-reference/api-ecs-2014-05-26-describesecuritygroupattribute)
- [RevokeSecurityGroup](https://www.alibabacloud.com/help/en/ecs/developer-reference/api-ecs-2014-05-26-revokesecuritygroup)
