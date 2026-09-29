# FlightLink-Console ECS Native systemd Deployment Preparation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prepare the backend directory for the user to upload to GitHub and deploy natively on one ECS for a real `5760/14552` UAV/QGC link test.

**Architecture:** Keep FastAPI, `mavlink-routerd`, and tcpdump on the ECS host under systemd. Add an API service unit alongside the existing per-channel router/capture units, a placeholder-only environment example, bilingual deployment instructions, and repository ignore rules. Initialize Git only in `FlightLink-backend`; leave remote setup, staging, commits, and push to the user.

**Tech Stack:** Python 3.12+, FastAPI, `uv`, SQLite, Alibaba Cloud ECS SDK with ECS RAM instance role, Uvicorn, systemd, existing `mavlink-routerd` and tcpdump units.

**Spec:** `docs/superpowers/specs/2026-09-29-ecs-native-systemd-deployment-design.md`

## Global Constraints

- Use `FlightLink-backend` as the Git repository root; do not include the parent workspace or frontend directory.
- Run FastAPI as a non-root systemd service bound to `127.0.0.1:8000`; manage it through an SSH local-forward.
- Keep `flightlink-router@<uuid>.service` and `flightlink-capture@<uuid>.service` on the ECS host and preserve the fixed root-owned helper/sudoers boundary.
- Do not set `NoNewPrivileges=true` on the API service; its restricted `sudo` call must be able to use the setuid sudo executable to invoke the helper.
- Use `/etc/flightlink/api.env` for real ECS settings and the attached instance RAM role for cloud credentials. No long-lived AccessKey values enter the repository.
- The test channel uses DTU UDP `5760` and QGC TCP `14552`; the QGC CIDR is the known public egress CIDR.
- Do not configure ECS host-firewall rules, add a public API-port ingress rule, run live cloud/hardware tests, delete local developer caches, configure a Git remote, stage/commit files, or push to GitHub.
- Do not add or run automated tests for this file-preparation phase; verify source-control exclusions and deployment file consistency with read-only checks.

## Review Focus

- API service privilege path — ensure the unit runs as `flightlink-api`, retains the existing narrow sudo helper path, and does not set `NoNewPrivileges=true` or run FastAPI as root; statically inspect the generated unit.
- Management API exposure — ensure Uvicorn binds to `127.0.0.1:8000` and the guide uses SSH forwarding without opening an API security-group port; inspect unit and bilingual README.
- Secret and runtime-file leakage — `.env`, `.env.*` except `.env.example`, virtual environments, caches, SQLite/WAL, PCAPs, and `.superpowers/` are ignored; `git check-ignore` must match them while `.env.example` stays visible.
- Configuration contract — the unit's `EnvironmentFile`, working directory, executable, writable data path, and the README's `.env.example` instructions must agree; compare the exact paths and variable names.
- Repository boundary and upload ownership — `git rev-parse --show-toplevel` must resolve to `FlightLink-backend`, with no remote, index entries, or commits; the user performs all GitHub connection and upload actions.

---

### Task 1: GitHub source hygiene and environment example

**Files:**
- Modify: `.gitignore`
- Create: `.env.example`

**Interfaces:**
- `.env.example` documents the variable names consumed by `src/flightlink_backend/config.py`; it is an example only and is never loaded directly by systemd.
- Real ECS configuration is stored outside the checkout at `/etc/flightlink/api.env`.

- [x] **Step 1: Update ignore rules**
  - Keep existing environment/database exclusions.
  - Add `.env.*` while explicitly allowing `.env.example`.
  - Ignore `.idea/`, `.superpowers/`, `*.egg-info/`, build/cache/coverage output, PCAP files, and generated log files.
  - Do not ignore `uv.lock`, tests, formal `docs/superpowers/specs/` or `docs/superpowers/plans/`, deployment units, or root `.env.example`.

- [x] **Step 2: Add the safe environment example**
  - Add the runtime paths under `/var/lib/flightlink`, `FLIGHTLINK_ROUTER_MANAGER=systemd`, the existing helper/sudo paths, secure session-cookie settings, and all Aliyun provider variables.
  - Use placeholders for region, security-group ID, QGC source CIDR, and optional role name; include no real account IDs, public IPs, passwords, tokens, or AccessKeys.
  - Set UAV source to `0.0.0.0/0`, GCS source to a placeholder `/32`, and timeout to `10`.

- [x] **Step 3: Review the prepared source boundary**
  - Confirm `.env.example` names match `config.py` and no real `.env` is copied or created in the checkout.
  - Do not remove `.venv`, `.idea`, caches, or unrelated parent-workspace files; ignore rules are sufficient for GitHub cleanliness.

### Task 2: FastAPI systemd unit and bilingual deployment guide

**Files:**
- Create: `deploy/systemd/flightlink-api.service`
- Modify: `README.md`
- Modify: `test_main.http` to use the approved ECS acceptance port pair

**Interfaces:**
- The unit loads `/etc/flightlink/api.env`, runs `/opt/flightlink/backend/.venv/bin/uvicorn flightlink_backend.main:app`, and uses `/var/lib/flightlink` for writable application state.
- Existing `flightlink-router@.service`, `flightlink-capture@.service`, helper, and sudoers files remain the service-control interface.

- [x] **Step 1: Add the API service unit**
  - Run as `flightlink-api` with supplementary `flightlink-router` and `flightlink-capture` groups.
  - Start after and want `network-online.target`; restart on failure.
  - Bind Uvicorn to `127.0.0.1:8000`; use `/etc/flightlink/api.env` and `/var/lib/flightlink`.
  - Keep systemd filesystem protections compatible with `/var/lib/flightlink` writes and host helper execution.
  - Do not set `NoNewPrivileges=true`, which would block the API's existing `sudo`-to-helper flow; do not run FastAPI as root.

- [x] **Step 2: Extend the Chinese and English README deployment instructions**
  - Explain the repository clone/install path, Python 3.12+/`uv` setup, creation of the API/router/capture users and directories, and use of the existing router/capture systemd units.
  - Show how the API unit references `/etc/flightlink/api.env`, how to install/reload/restart the unit, and how to create the admin using the same data-directory setting.
  - Include SSH local-forward access to the loopback-only API; explicitly state not to open port 8000 in the Alibaba security group.
  - Give the real port-pair flow: read-only preflight, create UDP `5760` plus TCP `14552`, send MAVLink from the DTU, connect QGC over TCP, and inspect channel telemetry, `/packets`, `/messages`, and `/logs`.
  - Explain that the local fake-provider tests are not part of this deployment task and that real connectivity is checked later by the user on the ECS.
  - Keep Chinese and English descriptions aligned and add no host-firewall commands.

- [x] **Step 3: Perform read-only consistency review**
  - Compare the unit's user, groups, executable, working directory, environment-file path, loopback bind, and writable paths against the README and `.env.example`.
  - Confirm the example file contains placeholders only and the unit does not include `NoNewPrivileges=true`.

### Task 3: Initialize the local backend Git repository

**Files:**
- Create: `.git/` inside `FlightLink-backend` only

**Interfaces:**
- The repository root is `D:\Workspace\Mavproxy\FlightLink-backend`.
- The user will configure the GitHub remote, stage/commit files, and upload them independently.

- [x] **Step 1: Initialize Git on the prepared backend directory**
  - Run `git init -b main` from `FlightLink-backend` after Tasks 1 and 2 are complete.
  - Do not add or stage files, create a commit, configure a remote, or push.

- [x] **Step 2: Verify repository boundary and ignore behavior**
  - Confirm `git rev-parse --show-toplevel` resolves to `FlightLink-backend`.
  - Confirm `.env`, `.env.production`, `.venv/`, `data/`, `*.egg-info/`, PCAP/log files, and `.superpowers/` are ignored; confirm `.env.example` is not ignored.
  - Confirm `git remote -v` is empty and `git status` shows no staged entries.
  - Report the initial branch and the untracked source files for the user to review before their GitHub upload.
