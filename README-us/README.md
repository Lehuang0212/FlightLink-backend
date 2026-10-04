# FlightLink-Console Backend

[中文](../README-cn/README.md) | [English](README.md) | [Repository home](../README.md)

## Introduction

FlightLink-Console manages MAVLink links between a flight controller, a 4G DTU, an ECS server, and QGC. A channel is identified by a pair of server ports, such as DTU UDP `5760` and QGC TCP `14552`. Channels are server configurations, not permanent UAV identities.

Features include channel CRUD, Router service control, Alibaba Cloud security-group synchronization, administrator sessions, live telemetry, flight-controller text messages, Router logs, live packet summaries, and PCAP downloads. [Frontend repository](https://github.com/Lehuang0212/FlightLink-fronted).

## Project structure

```text
src/flightlink_backend/
├── api/                 API routes
├── schemas/             Request and response models
├── services/            Router, cloud, telemetry, and capture services
├── config.py            Environment settings
├── database.py          SQLite and migrations
├── security.py          Passwords and sessions
├── cli.py               ConsoleUseradd
└── main.py              FastAPI entry point
deploy/
├── flightlink-run       Ubuntu / Rocky installer
├── sbin/                Restricted service and capture helpers
└── systemd/             API, Router, and capture units
tests/                   Automated tests
docs/                    Design and implementation documents
.env.example             Environment template
test_main.http           API request examples
pyproject.toml / uv.lock  Dependencies and lockfile
```

## Implementation

- Python 3.12, FastAPI, Uvicorn, and pymavlink; SQLite WAL stores accounts, sessions, and channel settings.
- Each channel has a Router configuration and an ECS-side `flightlink-router@UUID.service`.
- Router forwards telemetry copies to loopback UDP. The backend passively parses them and keeps telemetry, messages, and summaries in memory.
- The backend does not request telemetry streams. Without QGC, it displays what the flight controller already sends. Link quality estimates MAVLink sequence gaps, not cellular signal strength.
- tcpdump rotates PCAP every 10 minutes, with 24-hour retention and a 1 GiB target size. Downloads only include closed segments.
- Administrators are created by CLI. Cloud calls use temporary Instance RAM Role credentials. Port changes add and revoke rules rather than modifying rules in place.

## Usage

### 1. Prepare ECS

Supported: Ubuntu 24.04 and Rocky Linux 9 with systemd. Complete the [RAM setup](#alibaba-cloud-security-group-configuration) below first and preserve SSH ingress. This guide assumes the host firewall is disabled and public ingress is controlled by Alibaba Cloud security groups.

**Ubuntu 24.04:**

```bash
sudo apt-get update
sudo apt-get install -y git
```

**Rocky Linux 9:**

```bash
sudo dnf install -y git
```

### 2. Deploy

The installer requires `/opt/flightlink/backend`:

```bash
sudo install -d -o root -g root -m 0755 /opt/flightlink
sudo git clone https://github.com/Lehuang0212/FlightLink-backend.git /opt/flightlink/backend
cd /opt/flightlink/backend
sudo bash deploy/flightlink-run
```

Answer the prompts:

| Setting | Value |
| --- | --- |
| Region ID | ECS region, for example `cn-hangzhou` |
| Security group ID | The target `sg-...` attached to ECS |
| DTU source CIDR | `0.0.0.0/0` |
| QGC source CIDR | QGC computer's public IPv4 plus `/32`; comma-separated for multiple sources |
| RAM role name | Leave blank for discovery, or specify the attached role |
| Administrator | Enter a real username and password |

The script installs dependencies, Router, helpers, and systemd units. Environment settings go to `/etc/flightlink/api.env`; runtime data goes to `/var/lib/flightlink`. Existing configuration and data are preserved.

```bash
sudo systemctl status flightlink-api.service --no-pager
curl -fsS http://127.0.0.1:8000/api/v1/health
```

FastAPI listens on `127.0.0.1:8000`. Nginx serves the management UI; do not expose public port `8000`.

### 3. Configure the environment

```bash
sudo vi /etc/flightlink/api.env
```

```ini
FLIGHTLINK_SECURITY_GROUP_PROVIDER=aliyun
FLIGHTLINK_ALIYUN_REGION_ID=cn-hangzhou
FLIGHTLINK_ALIYUN_SECURITY_GROUP_ID=replace-with-security-group-id
FLIGHTLINK_ALIYUN_ECS_ROLE_NAME=
FLIGHTLINK_ALIYUN_UAV_SOURCE_CIDRS=0.0.0.0/0
FLIGHTLINK_ALIYUN_GCS_SOURCE_CIDRS=replace-with-qgc-public-ip/32
FLIGHTLINK_ALIYUN_API_TIMEOUT_SECONDS=10
FLIGHTLINK_SESSION_COOKIE_SECURE=false
```

Replace the region too. Use `false` for HTTP testing and `true` for HTTPS. Other options: [.env.example](../.env.example).

```bash
sudo systemctl restart flightlink-api.service
```

### 4. Add administrators

The installer prompts for the first administrator. Add more with:

```bash
sudo -u flightlink-api env \
  FLIGHTLINK_DATA_DIR=/var/lib/flightlink \
  FLIGHTLINK_DATABASE_PATH=/var/lib/flightlink/flightlink.sqlite3 \
  /opt/flightlink/backend/.venv/bin/ConsoleUseradd
```

### 5. Connect the UAV and QGC

1. Deploy the [frontend](https://github.com/Lehuang0212/FlightLink-fronted/blob/main/README-us/README.md) and sign in.
2. In the same browser, open `http://ECS_PUBLIC_IP:18443/api/v1/integrations/aliyun/security-group/preflight`; expect `state=ready`. Use your HTTPS URL for an HTTPS deployment.
3. The preflight is read-only and does not verify write permissions; `write_access_checked=false` is expected.
4. Create and enable UDP `5760` / TCP `14552`. Check cloud synchronization, Router, and capture service states.
5. Configure DTU: UDP → ECS public IP → `5760`.
6. Configure QGC: TCP → ECS public IP → `14552`.
7. View telemetry, Router logs, flight messages, and live packet summaries. Download PCAP after rotation.
8. To check updates and deletion, temporarily use UDP `5761` / TCP `14553`, update both clients, then delete the test channel.

### 6. Update and inspect logs

```bash
cd /opt/flightlink/backend
sudo git pull --ff-only origin main
sudo bash deploy/flightlink-run
sudo journalctl -u flightlink-api.service -n 100 --no-pager
sudo journalctl -u 'flightlink-router@*.service' -n 100 --no-pager
```

After updating the capture helper, restart an already-running capture instance if necessary. Replace the UUID:

```bash
sudo systemctl restart flightlink-capture@CHANNEL_UUID.service
```

## Alibaba Cloud security-group configuration

### 1. Create an Instance RAM Role

RAM console → Identities → Roles → Create Role:

| Setting | Example |
| --- | --- |
| Trusted entity / principal type | Alibaba Cloud service / Cloud Service |
| Trusted service / principal name | ECS |
| Role type | Normal Service Role, when this option is shown |
| Role name | `FlightLinkEcsRole` |

The role's trust policy should contain:

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

`Principal.Service` selects ECS as the trusted service. `sts:AssumeRole` lets that service assume the role. **The trust policy does not grant security-group permissions**; attach the access policy below. [Official role guide](https://www.alibabacloud.com/help/en/ram/user-guide/create-a-ram-role-for-a-trusted-alibaba-cloud-service).

### 2. Create a custom access policy

RAM console → Permissions → Policies → Create Policy → JSON / Script editor. Example name: `FlightLinkSecurityGroupPolicy`.

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

Policy explanation:

| Field / action | Meaning |
| --- | --- |
| `Version: "1"` | RAM policy syntax version |
| `Statement` | Permission statements |
| `Effect: "Allow"` | Allows the listed operations |
| `DescribeSecurityGroupAttribute` | Reads rules for preflight and synchronization |
| `AuthorizeSecurityGroup` | Adds DTU / QGC ingress rules |
| `RevokeSecurityGroup` | Removes rules recorded and managed by the backend |
| `Resource: "*"` | Applies these operations to all matching resources in the account, not a single security group |

This example simplifies deployment. The backend uses only its configured group, but that setting **is not a RAM permission boundary**. For narrower access, scope actions that support resource-level authorization using the official table. Current code does not require `ecs:ModifySecurityGroupRule` or `ecs:DescribeSecurityGroups`. [Custom policy guide](https://www.alibabacloud.com/help/en/ram/create-a-custom-policy), [ECS authorization table](https://www.alibabacloud.com/help/en/ram/api-elastic-compute-service).

### 3. Grant permissions and attach the role

1. Open `FlightLinkEcsRole` in RAM and attach `FlightLinkSecurityGroupPolicy`.
2. ECS console → select region and instance → Instance Settings → Attach/Detach RAM Role → select the role.
3. Open the instance's security groups and record the actual group ID. Find the region ID in ECS / OpenAPI, for example `cn-hangzhou`.
4. Enter these values in the installer or `/etc/flightlink/api.env`, then restart the API. The SDK obtains temporary credentials automatically; no long-lived AccessKey is needed.

If ECS already has a role, attach the policy to that role rather than replacing it. [Official attachment guide](https://www.alibabacloud.com/help/en/ecs/user-guide/attach-an-instance-ram-role-to-an-ecs-instance).

### 4. Configure ingress

| Purpose | Inbound rule | Source |
| --- | --- | --- |
| SSH | Current SSH TCP port | Administrator public IPv4 /32 |
| Management UI | TCP `18443`, added manually | Administrator public IPv4 /32 |
| DTU | UDP `5760`, created by backend | `0.0.0.0/0` |
| QGC | TCP `14552`, created by backend | QGC public IPv4 /32 |

Use public source addresses and update settings or rules when they change. The backend does not modify host firewalls or security-group egress rules.
