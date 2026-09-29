# Aliyun Security Group Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate Aliyun ECS security-group ingress rules with backend channel CRUD and expose a read-only cloud preflight for Ubuntu/Rocky deployments.

**Architecture:** Add a provider-configured ECS SDK V2 adapter using the ECS instance RAM role, an authenticated read-only preflight endpoint, and a persistent mapping for backend-owned rule IDs. Coordinate channel CRUD as a compensating workflow: create ingress before starting a new router, add replacement ingress before updating, and revoke only mapped rule IDs after the new state is active.

**Tech Stack:** Python 3.12+, FastAPI, SQLite schema migration, Alibaba Cloud ECS Python SDK V2, Alibaba Cloud Credentials SDK, `unittest` and injected fake ECS clients.

**Spec:** `docs/superpowers/specs/2026-09-29-aliyun-security-group-integration-design.md`

## Global Constraints

- Only manage Aliyun ECS security-group ingress rules; never issue UFW, firewalld, nftables, or other host-firewall commands.
- Support Ubuntu and Rocky Linux deployment with systemd; document distro-specific dependency installation and actual `tcpdump` path configuration.
- Use ECS RAM instance-role credentials; do not add long-lived AccessKey configuration.
- DTU source defaults to `0.0.0.0/0`; create only exact per-channel UDP destination ports such as `5760/5760` or `5761/5761`.
- GCS sources come from explicit `FLIGHTLINK_ALIYUN_GCS_SOURCE_CIDRS` configuration; protocol is TCP or UDP as specified by the channel.
- Keep the preflight endpoint read-only and admin-authenticated; it proves query access, not write permissions.
- Do not change front-end files or automatically create cloud rules during application startup.
- This workspace and its parent are not Git repositories; do not initialize Git or claim commits. Keep changes in the existing backend directory.
- Use Python's standard-library `unittest`; do not add a test framework dependency.

## Review Focus

- A matching rule already exists without FlightLink's ownership marker: the channel may use it, but create/update/delete must never claim or revoke that external rule.
- A rule is created in Aliyun but the API response or follow-up describe call fails: keep enough pending state to retry or clean up, and do not start a newly created router prematurely.
- Aliyun returns a stale/missing rule ID during revoke: re-read the security group and treat an already-absent rule as reconciled without deleting unrelated rules.
- Pagination splits managed rules across `DescribeSecurityGroupAttribute` pages: preflight and reconciliation must inspect all pages before reporting success.
- A router restart or capture stop fails during an update/delete: preserve potentially active ingress and channel/rule records until a safe retry.

---

### Task 1: Aliyun SDK configuration, adapter, and read-only preflight

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `src/flightlink_backend/config.py`
- Create: `src/flightlink_backend/services/aliyun_security_group.py`
- Create: `src/flightlink_backend/schemas/security_group.py`
- Create: `src/flightlink_backend/api/integrations.py`
- Modify: `src/flightlink_backend/main.py`
- Create: `tests/__init__.py`
- Test: `tests/test_aliyun_security_group.py`

**Interfaces:**
- Produces `IngressRule(protocol: str, port: int, source_cidr: str, description: str)`, `SecurityGroupRule(rule_id: str, protocol: str, port_range: str, source_cidr: str | None, policy: str, description: str | None, direction: str)`, and `SecurityGroupSnapshot(region_id: str, security_group_id: str, security_group_name: str | None, rules: list[SecurityGroupRule])`.
- Produces `SecurityGroupProvider.describe_security_group() -> SecurityGroupSnapshot`, `authorize_rule(rule: IngressRule) -> None`, and `revoke_rule(rule_id: str) -> None`.
- Produces `build_security_group_preflight(provider: SecurityGroupProvider) -> SecurityGroupPreflightPublic` and `get_security_group_provider() -> SecurityGroupProvider`.
- Produces `GET /api/v1/integrations/aliyun/security-group/preflight`, protected by `get_current_admin`.
- Provider modes are `disabled` and `aliyun`; environment variables and defaults follow the spec.

- [x] **Step 1: Write failing standard-library tests**
  - `test_preflight_reports_disabled_provider`
  - `test_preflight_returns_target_and_all_paginated_ingress_rules`
  - `test_preflight_reports_missing_configuration_without_sdk_call`
  - `test_preflight_sanitizes_credential_and_openapi_errors`
  - Assert that the preflight route has the existing admin dependency and does not call authorize/revoke.
- [x] **Step 2: Run the tests and confirm they fail for missing provider/schema/route behavior**
  - Run: `uv run python -m unittest tests.test_aliyun_security_group -v`
  - Expected: FAIL because the Aliyun provider and preflight interface do not exist.
- [x] **Step 3: Add ECS Python SDK V2 and Credentials dependencies; implement typed settings, an injected provider interface, the production RAM-role SDK adapter, paginated describe, sanitized errors, schemas, and the protected endpoint**
  - SDK imports must be delayed behind the Aliyun provider so local `disabled` mode never contacts ECS metadata.
  - The client uses `type='ecs_ram_role'` and optional `FLIGHTLINK_ALIYUN_ECS_ROLE_NAME`; no AccessKey values are accepted from app settings.
- [x] **Step 4: Run the focused tests**
  - Run: `uv run python -m unittest tests.test_aliyun_security_group -v`
  - Expected: all preflight and adapter tests pass; test doubles record only describe calls during preflight.
- [x] **Step 5: Refresh and validate the lockfile**
  - Run: `uv lock`
  - Expected: `uv.lock` includes both Alibaba SDK packages and their transitive dependencies.
  - Run: `uv sync --frozen`
  - Expected: project dependencies install into the managed environment.

### Task 2: SQLite schema v4 and public channel security-group state

**Files:**
- Modify: `src/flightlink_backend/database.py`
- Modify: `src/flightlink_backend/schemas/channels.py`
- Create: `src/flightlink_backend/services/security_group_records.py`
- Test: `tests/test_security_group_records.py`

**Interfaces:**
- Produces channel state values `disabled`, `pending`, `synced`, `cleanup_pending`, and `error`.
- Produces `SecurityGroupRuleRecord(id: int | None, rule_id: str | None, channel_id: UUID, role: Literal['uav', 'ground_station'], protocol: Literal['tcp', 'udp'], port: int, source_cidr: str, description: str, state: Literal['pending_discovery', 'active', 'pending_revoke'])`, linked to a channel with `ON DELETE RESTRICT`; non-null Aliyun `SecurityGroupRuleId` values are unique.
- Produces `SecurityGroupChannelPublic(state: Literal['disabled', 'pending', 'synced', 'cleanup_pending', 'error'], error: str | None)`; `ChannelPublic.security_group` exposes that state and the last safe-to-display error.
- Repository functions are `list_rule_records(connection, channel_id) -> list[SecurityGroupRuleRecord]`, `save_rule_record(connection, record) -> int`, `set_channel_security_group_state(connection, channel_id, state, error=None) -> None`, and `remove_rule_record(connection, record_id: int) -> None`.

- [x] **Step 1: Write failing migration and repository tests**
  - `test_schema_v3_migrates_channels_to_pending_security_group_state`
  - `test_managed_rule_rows_preserve_channel_until_rules_are_removed`
  - `test_channel_public_includes_security_group_state`
  - Assert a schema-3 database retains channel/listener data after migration.
- [x] **Step 2: Run tests and confirm they fail on schema version and missing fields**
  - Run: `uv run python -m unittest tests.test_security_group_records -v`
  - Expected: FAIL because schema v4 and rule persistence do not exist.
- [x] **Step 3: Implement migration v4, rule mapping operations, and public state schema**
  - New channel rule rows must use `ON DELETE RESTRICT` so a failed cloud revoke cannot cascade away the retry record.
  - Use parameterized SQLite statements and existing transaction conventions.
- [x] **Step 4: Run focused migration and repository tests**
  - Run: `uv run python -m unittest tests.test_security_group_records -v`
  - Expected: schema-3 upgrade and rule persistence assertions pass.

### Task 3: Owned-rule reconciliation and compensation

**Files:**
- Modify: `src/flightlink_backend/services/aliyun_security_group.py`
- Create: `src/flightlink_backend/services/security_group_rules.py`
- Modify: `src/flightlink_backend/services/security_group_records.py`
- Test: `tests/test_security_group_rules.py`

**Interfaces:**
- Produces `SecurityGroupSyncResult(state: Literal['disabled', 'pending', 'synced', 'cleanup_pending', 'error'], error: str | None, managed_rule_ids: tuple[str, ...])`.
- Produces `ensure_channel_rules(connection: sqlite3.Connection, channel_id: UUID, payload: ChannelWrite) -> SecurityGroupSyncResult`.
- Produces `revoke_channel_rules(connection: sqlite3.Connection, channel_id: UUID) -> SecurityGroupSyncResult`.
- Rule identity is the exact tuple `(direction, protocol, port, source_cidr, policy)` plus a FlightLink description marker containing channel UUID and role.

- [x] **Step 1: Write failing rule reconciliation tests**
  - `test_ensure_opens_only_exact_uav_and_gcs_ports_for_configured_cidrs`
  - `test_matching_external_rule_is_reused_but_never_recorded_as_owned`
  - `test_managed_rule_id_is_read_back_and_persisted_after_authorize`
  - `test_authorize_success_without_readback_is_saved_as_pending_error`
  - `test_partial_authorize_failure_keeps_successful_rule_for_retry`
  - `test_revoke_uses_only_persisted_rule_ids`
  - `test_stale_rule_id_is_reconciled_after_describe_confirms_absent`
- [x] **Step 2: Run tests and confirm they fail because reconciliation is absent**
  - Run: `uv run python -m unittest tests.test_security_group_rules -v`
  - Expected: FAIL because channel rule reconciliation methods do not exist.
- [x] **Step 3: Implement source-CIDR parsing, ownership markers, pre-query duplicate handling, authorize/readback, persistence, and precise revoke/compensation**
  - DTU default source is `0.0.0.0/0`, but each rule's destination is one configured UDP port only.
  - GCS CIDRs are explicit and protocol/port follow the channel config.
  - When an external exact match exists without FlightLink's description marker, treat it as usable but unmanaged.
  - When revocation reports an unknown ID, re-describe before deciding whether the record is stale; never revoke by broad tuple matching.
- [x] **Step 4: Run focused reconciliation tests**
  - Run: `uv run python -m unittest tests.test_security_group_rules -v`
  - Expected: all ownership, retry, pagination, and compensation assertions pass.

### Task 4: Integrate create/update/delete channel lifecycle

**Files:**
- Modify: `src/flightlink_backend/api/channels.py`
- Modify: `src/flightlink_backend/services/channels.py`
- Create: `src/flightlink_backend/services/channel_security_group.py`
- Modify: `src/flightlink_backend/main.py`
- Test: `tests/test_channel_security_group_lifecycle.py`

**Interfaces:**
- Produces `create_managed_channel(connection: sqlite3.Connection, payload: ChannelWrite) -> ChannelPublic`, `update_managed_channel(connection: sqlite3.Connection, channel_id: UUID, payload: ChannelWrite) -> ChannelPublic`, and `delete_managed_channel(connection: sqlite3.Connection, channel_id: UUID) -> None` orchestration functions consumed by channel API routes.
- With provider `aliyun`, channel creation returns the created channel even when cloud synchronization fails, with `security_group.state=error`; it does not start a new router until cloud rules are confirmed. With provider `disabled`, it preserves the current local-only behavior and starts services normally.
- Update retries the same payload, creates new rules before changing router config, and removes stale managed rule IDs only after runtime reaches the desired state.
- Delete stops services first and retains channel/rule records if cloud revoke fails.

- [x] **Step 1: Write failing lifecycle tests using a fake Aliyun provider and fake router/capture managers**
  - `test_create_syncs_security_group_before_starting_router`
  - `test_create_preserves_local_behavior_when_provider_disabled`
  - `test_create_failure_keeps_channel_error_and_does_not_start_router`
  - `test_update_authorizes_new_ports_before_local_config_switch`
  - `test_update_keeps_old_rule_when_router_restart_fails`
  - `test_update_reports_cleanup_pending_when_old_rule_revoke_fails`
  - `test_delete_stops_services_before_revoke_and_preserves_records_on_failure`
  - `test_delete_keeps_rules_when_capture_stop_fails`
  - `test_delete_retries_after_rule_was_already_removed_in_cloud`
  - `test_startup_reconciliation_never_mutates_security_group`
- [x] **Step 2: Run tests and confirm they fail on existing local-only channel routes**
  - Run: `uv run python -m unittest tests.test_channel_security_group_lifecycle -v`
  - Expected: FAIL because channel CRUD does not call the cloud/security-group workflow.
- [x] **Step 3: Implement lifecycle orchestration and wire existing routes to it**
  - Keep external/manual rules unowned and untouched.
  - Persist safe error state and preserve retryable mappings after partial operations.
  - Keep application-startup reconciliation free of Aliyun mutations; do not change existing router/capture startup behavior.
- [x] **Step 4: Run focused lifecycle tests**
  - Run: `uv run python -m unittest tests.test_channel_security_group_lifecycle -v`
  - Expected: operation ordering and all failure-state assertions pass.

### Task 5: Ubuntu/Rocky deployment documentation and full local verification

**Files:**
- Modify: `README.md`
- Modify: `test_main.http`
- Test: `tests/test_documentation_contract.py`
- Test: all files under `tests/`

**Interfaces:**
- Document all provider environment variables, exact RAM API actions/resource scope, instance-role attachment, read-only preflight, and expected limitation that preflight cannot prove write permissions.
- Document Ubuntu `apt` and Rocky `dnf` dependency steps; keep host firewall commands out of the deployment path.
- Add HTTP examples for preflight and a test channel pair `5761/14553`, update to another pair, and delete.
- Add the ECS/QGC/DTU real-connection checklist; distinguish automated fake-client tests from real cloud/hardware acceptance.

- [x] **Step 1: Add the failing documentation/API-contract checks**
  - `test_readme_has_chinese_and_english_aliyun_configuration_sections`
  - `test_http_examples_use_admin_session_and_document_preflight_and_channel_crud`
- [x] **Step 2: Run documentation contract tests and confirm they fail**
  - Run: `uv run python -m unittest tests.test_documentation_contract -v`
  - Expected: FAIL until both README language sections and HTTP examples document the new flow.
- [x] **Step 3: Update README in Chinese and English and add HTTP examples**
  - Include the DTU `0.0.0.0/0` source with destination restricted to each selected UDP port, and recommend a narrow QGC source CIDR.
  - Explain that actual device/QGC connectivity must be checked on the ECS after the user supplies cloud settings and deploys the backend.
- [x] **Step 4: Run the full automated verification suite**
  - Run: `uv run python -m unittest discover -s tests -v`
  - Expected: all tests pass.
  - Run: `uv run python -m compileall -q src`
  - Expected: exit code 0.
  - Run: `uv lock --check`
  - Expected: dependency lock is consistent with `pyproject.toml`.

## Execution Notes

- The backend directory is not in a Git repository and no separate worktree is available. Do not initialize Git; changes remain in `D:\Workspace\Mavproxy\FlightLink-backend` unless the user later provides a repository workflow.
- Actual ECS mutation and device/QGC connectivity tests are not run from the development workstation. After the automated implementation is complete, provide the exact service environment file entries and commands for the user-configured ECS, then verify the returned preflight and test results with the user.
- Preserve red/green evidence for each test cycle and do not claim live-cloud acceptance until a real ECS test has succeeded.
