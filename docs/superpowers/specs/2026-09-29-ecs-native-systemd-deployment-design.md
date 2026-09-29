# FlightLink-Console ECS Native systemd Deployment Design

## Status

Proposed design approved in chat for the native systemd deployment direction. This document is ready for user review before an implementation plan is written.

## Goal

Prepare the backend for a GitHub-hosted deployment to one Ubuntu or Rocky Linux ECS and perform a real first-link acceptance test using one UAV/DTU and one QGC ground station:

- UAV/4G DTU sends MAVLink to ECS UDP port `5760`.
- QGC connects to ECS TCP port `14552`.
- The admin can inspect vehicle telemetry, packet summaries, flight-controller STATUSTEXT messages, and `mavlink-router` systemd logs.
- The backend creates the exact Alibaba Cloud security-group ingress rules using the ECS instance RAM role.

## Current State and Constraints

- The backend is a Python 3.12+ FastAPI project with SQLite, an Alibaba Cloud ECS SDK adapter, and a file-based `uv.lock`.
- `SystemdRouterManager` and `SystemdCaptureManager` call a fixed root-owned `flightlink-routerctl` helper through `sudo`.
- The helper invokes host `systemctl` and `journalctl` against `flightlink-router@<channel UUID>.service` and `flightlink-capture@<channel UUID>.service`.
- The router and capture template units are present in `deploy/systemd/`; a FastAPI API service unit is not present.
- Setting `FLIGHTLINK_ROUTER_MANAGER=disabled` prevents host-service calls. That mode cannot start the router or capture processes for a real flight-link test.
- No Docker deployment or host-control bridge currently exists. The approved first ECS deployment is native to the host; a generic API container is out of scope.
- The backend directory is not currently a Git repository. This work prepares files for a later GitHub upload but does not initialize Git, configure a remote, or push code.
- Do not delete unrelated files from the parent workspace. Generated developer files should be excluded from source control rather than removed from the user's working environment.
- Do not run UFW, firewalld, nftables, or other ECS host-firewall configuration. Ingress is managed only through the configured Alibaba Cloud security group.
- Do not put long-lived Alibaba Cloud AccessKey values in the repository or ECS environment file. Use the instance RAM role attached to the ECS running FastAPI.

## Deployment Shape

Deploy the FastAPI backend as a non-root systemd service on the ECS host. Keep `mavlink-routerd` and capture workers as their existing per-channel systemd template services. The API calls the existing root-owned helper only for the fixed service operations and reads status/log output through that helper.

The expected units are:

| Unit | Responsibility |
| --- | --- |
| `flightlink-api.service` | Runs Uvicorn/FastAPI, provides admin APIs and telemetry endpoints, calls Aliyun OpenAPI, and coordinates channel services. |
| `flightlink-router@<uuid>.service` | Runs one `mavlink-routerd` config generated for one channel/port pair. |
| `flightlink-capture@<uuid>.service` | Runs restricted tcpdump capture and near-real-time packet-summary output for one channel. |

The API service should start after `network-online.target`, run as a dedicated `flightlink-api` user, and use the host system Python virtual environment. It must have write access only to its data/config directories, supplementary access to the existing `flightlink-router` and `flightlink-capture` groups as needed, read access to router configuration files as required by the existing group model, and narrowly scoped sudo permission for `/usr/local/sbin/flightlink-routerctl`.

For the first acceptance deployment, bind Uvicorn to `127.0.0.1:8000`. Use an SSH local-forward from the operator workstation to reach admin endpoints. Do not add a public security-group rule for the management API port. The actual UAV and QGC listeners remain public through the managed security-group rules.

## Source-Tree and GitHub Preparation

Use `FlightLink-backend` as the backend repository root. The parent workspace contains other project/workbench material and should not be uploaded as the backend repository. The frontend directory is not part of this deployment-preparation change.

Retain application source, tests, `pyproject.toml`, `uv.lock`, bilingual README, formal design/deployment docs, router/capture units, and their restricted helpers. Extend `.gitignore` to exclude local-only or generated material, including:

- `.venv/`, `.idea/`, `__pycache__/`, `*.py[cod]`, and `*.egg-info/`;
- `.env` and machine-specific `.env.*` files while allowing `.env.example`;
- runtime `data/`, SQLite and WAL files, PCAP captures, and logs;
- transient `.superpowers/` execution ledgers and local tool caches.

Add a placeholder-only `.env.example` or equivalent deployment example. Real ECS values belong in `/etc/flightlink/api.env`, outside the cloned source tree, and must never be committed. Do not remove the local virtual environment or editor settings just to make the GitHub view clean; ignore rules keep them local.

## FastAPI Service and ECS Environment

Add `deploy/systemd/flightlink-api.service` for the host-native API process. It should:

- use a stable install path such as `/opt/flightlink/backend` and the project virtual environment's Uvicorn executable;
- set `WorkingDirectory` to the checked-out backend directory;
- include `EnvironmentFile=/etc/flightlink/api.env`;
- run as `flightlink-api`, restart on failure, and bind only `127.0.0.1:8000`;
- start after `network-online.target`;
- avoid running FastAPI itself as root.

The environment file contains the existing application settings for data/config/capture paths and the following Aliyun values:

```ini
FLIGHTLINK_SECURITY_GROUP_PROVIDER=aliyun
FLIGHTLINK_ALIYUN_REGION_ID=<target-region-id>
FLIGHTLINK_ALIYUN_SECURITY_GROUP_ID=<target-security-group-id>
# Optional; omit to use ECS RAM-role auto-discovery.
FLIGHTLINK_ALIYUN_ECS_ROLE_NAME=<attached-instance-role-name>
FLIGHTLINK_ALIYUN_UAV_SOURCE_CIDRS=0.0.0.0/0
FLIGHTLINK_ALIYUN_GCS_SOURCE_CIDRS=<qgc-public-egress-ip>/32
FLIGHTLINK_ALIYUN_API_TIMEOUT_SECONDS=10
```

The target ECS region and security-group ID must refer to the security group attached to that ECS. The QGC source list is global to managed channels and should be restricted to known QGC public egress CIDRs. DTU source may be `0.0.0.0/0`, but the application opens only the one configured UDP listener port for each channel.

Attach the instance RAM role to the ECS running FastAPI. The backend requires `ecs:DescribeSecurityGroupAttribute`, `ecs:AuthorizeSecurityGroup`, and `ecs:RevokeSecurityGroup`. The describe and revoke actions can be scoped to the target security-group ARN. Alibaba Cloud currently requires `Resource: "*"` for `ecs:AuthorizeSecurityGroup`; the custom policy may further use the documented protocol/source-CIDR condition keys. The backend itself passes the configured security-group ID and exact rule parameters. No `ecs:ModifySecurityGroupRule`, `ecs:DescribeSecurityGroups`, instance-management, or RAM-management permission is needed by the current backend.

The deployment guide must show how to install Python 3.12+, `uv`, `mavlink-routerd`, and tcpdump on Ubuntu/Rocky; create the API/router/capture users and directories; install the existing router/capture templates and root-owned helper; install the sudoers rule; create the API service and environment file; and start/restart the units. Do not include host-firewall commands.

## First Real-Device Acceptance

The first cloud/hardware acceptance is performed only after the user deploys the reviewed backend files and attaches/configures the RAM role:

1. Start the API and connect to `127.0.0.1:8000` over SSH forwarding.
2. Sign in as the administrator and call `GET /api/v1/integrations/aliyun/security-group/preflight`. Confirm the region/security-group target and `read_access_verified=true`. `write_access_checked=false` is expected because preflight is read-only.
3. Create one enabled channel with UAV UDP `5760` and ground-station TCP `14552`.
4. Confirm `security_group.state=synced`, the router and capture units are active, and FlightLink has added only its configured UDP `5760` DTU source rule and TCP `14552` QGC source rule. Existing unrelated rules, such as the operator's SSH rule, remain untouched.
5. Configure the DTU to send MAVLink to the ECS public address on UDP `5760`; configure QGC to connect by TCP to the same address on port `14552`.
6. Inspect `GET /api/v1/channels/{id}` for decoded heartbeat/vehicle telemetry and separate UAV/GCS state; inspect `/packets` for near-real-time packet summaries, `/messages` for flight-controller STATUSTEXT, and `/logs` for the per-channel `mavlink-router` journal.
7. Verify the QGC connection and vehicle heartbeat while observing that packet counters and timestamps continue to update. Report failures with the relevant sanitized preflight response, channel response, service status, packet summaries, and router log lines.

This acceptance is distinct from local fake-provider tests. The user controls the real ECS, DTU, QGC, and RAM policy. Do not claim live acceptance until the user has completed the real test and supplied its observed results. Do not automatically delete the test channel after a successful connection; wait for the user's instruction after they inspect the result.

## Out of Scope

- Docker or a container-to-host systemd control bridge.
- Frontend implementation.
- Public exposure of the admin API or management-port security-group rules.
- Changes to the ECS host firewall.
- Automatically pushing to GitHub, initializing a Git repository, or deleting local developer caches.
- 4G cellular signal strength estimates or flight-recording storage.
