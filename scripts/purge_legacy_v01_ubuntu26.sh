#!/usr/bin/env bash
# FlightLink legacy v0.1 complete shutdown & host purge script for Ubuntu 26.04 (also works on 24.04).
#
# Based strictly on the `old-version-0.1` deployment inventory:
#   - Services:  flightlink-api.service, flightlink-router@*.service, flightlink-capture@*.service
#   - Drop-ins:  /etc/systemd/system/flightlink-api.service.d
#   - Helpers:   /usr/local/sbin/{flightlink,flightlink-routerctl,flightlink-capture-runner},
#                /usr/local/lib/flightlink, /etc/sudoers.d/flightlink-router-control
#   - Env/Cfg:   /etc/flightlink/{api.env,router.env,capture.env,language}
#   - Data/App:  /opt/flightlink/backend, /var/lib/flightlink/{flightlink.sqlite3*,router-configs,
#                capture-configs,captures}, /var/backups/flightlink, /run/flightlink-maintenance.lock
#   - Logs:      /var/log/flightlink* and systemd journald rotation/vacuum (unless --keep-journal)
#   - Accounts:  users  (flightlink-api, mavlink-router, flightlink-capture)
#                groups (flightlink-api, mavlink-router, flightlink-capture)
#
# Preserves `/usr/bin/mavlink-routerd` by default so FlightLink 1.0 can reuse it without recompiling.
#
# Usage:
#   sudo bash scripts/purge_legacy_v01_ubuntu26.sh --dry-run
#   sudo bash scripts/purge_legacy_v01_ubuntu26.sh --yes
#   sudo bash scripts/purge_legacy_v01_ubuntu26.sh --yes --clean-cloud
set -Eeuo pipefail
IFS=$'\n\t'

DRY_RUN=0
ASSUME_YES=0
CLEAN_CLOUD=0
KEEP_BACKUPS=0
KEEP_JOURNAL=0
REMOVE_ROUTER_SRC=0
FORCE=0

usage() {
  cat <<'EOF'
Usage: sudo bash scripts/purge_legacy_v01_ubuntu26.sh [OPTIONS]

Options:
  --dry-run            Preview all stop/delete actions without changing the host.
  -y, --yes            Skip interactive YES confirmation prompt.
  --clean-cloud        Run legacy v0.1 `maintenance clean` to revoke old tracked cloud SG rules
                       before removing /opt/flightlink/backend.
  --keep-backups       Keep /var/backups/flightlink instead of deleting it.
  --keep-journal       Do not rotate/vacuum systemd journald logs.
  --remove-router-src  Also remove /usr/local/src/mavlink-router build source directory
                       (keeps installed /usr/bin/mavlink-routerd binary).
  --force              Run even if FlightLink 1.0 files (/etc/flightlink/flightlink.env or
                       /opt/flightlink/venv) are detected.
  -h, --help           Show this help message.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)           DRY_RUN=1; shift ;;
    -y|--yes)            ASSUME_YES=1; shift ;;
    --clean-cloud)       CLEAN_CLOUD=1; shift ;;
    --keep-backups)      KEEP_BACKUPS=1; shift ;;
    --keep-journal)      KEEP_JOURNAL=1; shift ;;
    --remove-router-src) REMOVE_ROUTER_SRC=1; shift ;;
    --force)             FORCE=1; shift ;;
    -h|--help)           usage; exit 0 ;;
    *)                   echo "[ERROR] Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

log()  { printf '[purge-v0.1] %s\n' "$*"; }
warn() { printf '[purge-v0.1][WARN] %s\n' "$*" >&2; }
fail() { printf '[purge-v0.1][ERROR] %s\n' "$*" >&2; exit 1; }

run_cmd() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    printf '[dry-run] %s\n' "$*"
  else
    "$@"
  fi
}

remove_path() {
  local target="$1"
  if [[ -e "$target" || -L "$target" ]]; then
    if [[ "$DRY_RUN" -eq 1 ]]; then
      printf '[dry-run] rm -rf -- %s\n' "$target"
    else
      rm -rf -- "$target"
      log "Removed: $target"
    fi
  fi
}

read_env_value() {
  local file="$1" key="$2" fallback="$3" line value
  [[ -r "$file" ]] || { printf '%s' "$fallback"; return 0; }
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" == "$key="* ]] || continue
    value="${line#*=}"
    value="${value%$'\r'}"
    value="${value#\"}"
    value="${value%\"}"
    value="${value#\'}"
    value="${value%\'}"
    printf '%s' "$value"
    return 0
  done < "$file"
  printf '%s' "$fallback"
}

check_host() {
  [[ "$EUID" -eq 0 ]] || fail "Please run as root: sudo bash $0"
  [[ -d /run/systemd/system ]] || fail "systemd is required on this host."
  if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    source /etc/os-release
    log "Detected OS: ${PRETTY_NAME:-unknown} (systemd $(systemctl --version | head -n1 | awk '{print $2}'))"
    if [[ "${ID:-}" != "ubuntu" ]]; then
      warn "This script targets Ubuntu 26.04; detected ID=${ID:-unknown}. Continuing carefully."
    fi
  fi

  if [[ "$FORCE" -eq 0 ]]; then
    if [[ -f /etc/flightlink/flightlink.env || -d /opt/flightlink/venv ]]; then
      fail "FlightLink 1.0 files detected (/etc/flightlink/flightlink.env or /opt/flightlink/venv). Use 'sudo flightlink clear' for v0.1 remnants, or pass --force if you intentionally want a full purge."
    fi
  fi
}

collect_legacy_paths() {
  LEGACY_APP_DIR="/opt/flightlink/backend"
  LEGACY_CONFIG_DIR="/etc/flightlink"
  LEGACY_API_ENV="${LEGACY_CONFIG_DIR}/api.env"

  LEGACY_DATA_DIR="$(read_env_value "$LEGACY_API_ENV" FLIGHTLINK_DATA_DIR /var/lib/flightlink)"
  LEGACY_DB_PATH="$(read_env_value "$LEGACY_API_ENV" FLIGHTLINK_DATABASE_PATH "${LEGACY_DATA_DIR}/flightlink.sqlite3")"
  LEGACY_ROUTER_CFG_DIR="$(read_env_value "$LEGACY_API_ENV" FLIGHTLINK_ROUTER_CONFIG_DIR "${LEGACY_DATA_DIR}/router-configs")"
  LEGACY_CAPTURE_CFG_DIR="$(read_env_value "$LEGACY_API_ENV" FLIGHTLINK_CAPTURE_CONFIG_DIR "${LEGACY_DATA_DIR}/capture-configs")"
  LEGACY_CAPTURE_DIR="$(read_env_value "$LEGACY_API_ENV" FLIGHTLINK_CAPTURE_DIR "${LEGACY_DATA_DIR}/captures")"
}

confirm_execution() {
  if [[ "$DRY_RUN" -eq 1 || "$ASSUME_YES" -eq 1 ]]; then
    return 0
  fi
  cat <<EOF

[purge-v0.1] This will PERMANENTLY stop and remove legacy FlightLink v0.1:
  - Services:  flightlink-api.service, flightlink-router@*.service, flightlink-capture@*.service
  - App/Env:   ${LEGACY_APP_DIR}, ${LEGACY_CONFIG_DIR}/{api.env,router.env,capture.env,language}
  - Data/DB:   ${LEGACY_DB_PATH}*, ${LEGACY_ROUTER_CFG_DIR}, ${LEGACY_CAPTURE_CFG_DIR}, ${LEGACY_CAPTURE_DIR}
  - Backups:   $([[ "$KEEP_BACKUPS" -eq 1 ]] && echo "KEEP /var/backups/flightlink" || echo "DELETE /var/backups/flightlink")
  - Logs:      /var/log/flightlink* $([[ "$KEEP_JOURNAL" -eq 1 ]] && echo "(journald kept)" || echo "+ journald rotate & vacuum")
  - Accounts:  users & groups (flightlink-api, mavlink-router/flightlink-router, flightlink-capture)
  - Preserved: /usr/bin/mavlink-routerd (reused by FlightLink 1.0)

EOF
  local answer=""
  read -r -p "Type YES to proceed (anything else cancels): " answer
  [[ "$answer" == "YES" ]] || fail "Cancelled by user."
}

maybe_clean_legacy_cloud_rules() {
  [[ "$CLEAN_CLOUD" -eq 1 ]] || return 0
  if [[ -x "${LEGACY_APP_DIR}/.venv/bin/python" && -r "${LEGACY_API_ENV}" ]]; then
    log "Cleaning legacy v0.1 tracked cloud security group rules via v0.1 maintenance..."
    if [[ "$DRY_RUN" -eq 1 ]]; then
      printf '[dry-run] systemd-run --wait --pipe --collect --uid=flightlink-api --working-directory=%s --property=EnvironmentFile=%s %s -m flightlink_backend.maintenance clean\n' \
        "$LEGACY_APP_DIR" "$LEGACY_API_ENV" "${LEGACY_APP_DIR}/.venv/bin/python"
    else
      systemd-run --quiet --wait --pipe --collect \
        --uid=flightlink-api \
        --working-directory="$LEGACY_APP_DIR" \
        --property="EnvironmentFile=${LEGACY_API_ENV}" \
        "${LEGACY_APP_DIR}/.venv/bin/python" -m flightlink_backend.maintenance clean || \
        warn "Legacy cloud rule cleanup returned non-zero; please double-check Aliyun SG rules manually."
    fi
  else
    warn "--clean-cloud requested, but legacy venv or ${LEGACY_API_ENV} not found; skipping."
  fi
}

stop_and_disable_services() {
  log "Stopping and disabling legacy FlightLink systemd units..."
  local units=()
  while IFS= read -r u; do
    [[ -n "$u" ]] && units+=("$u")
  done < <(
    systemctl list-units --all --no-legend --plain --no-pager \
      'flightlink-api.service' \
      'flightlink-router@*.service' \
      'flightlink-capture@*.service' 2>/dev/null | awk '{print $1}' | sort -u
  )

  for u in "${units[@]}"; do
    run_cmd systemctl stop "$u" || true
  done

  local enabled_units=()
  while IFS= read -r u; do
    [[ -n "$u" ]] && enabled_units+=("$u")
  done < <(
    systemctl list-unit-files --no-legend --no-pager \
      'flightlink-api.service' \
      'flightlink-router@*.service' \
      'flightlink-capture@*.service' 2>/dev/null | awk '$2 ~ /^(enabled|enabled-runtime|indirect)$/ {print $1}' | sort -u
  )

  for u in "${enabled_units[@]}"; do
    run_cmd systemctl disable "$u" || true
  done

  # Kill any stray legacy processes still holding files/ports or account sessions
  for user in flightlink-api mavlink-router flightlink-capture; do
    if id "$user" >/dev/null 2>&1; then
      if [[ "$DRY_RUN" -eq 1 ]]; then
        printf '[dry-run] pkill -TERM -u %s || true; sleep 1; pkill -KILL -u %s || true\n' "$user" "$user"
      else
        pkill -TERM -u "$user" 2>/dev/null || true
      fi
    fi
  done
  if [[ "$DRY_RUN" -eq 0 ]]; then
    sleep 1
    for user in flightlink-api mavlink-router flightlink-capture; do
      if id "$user" >/dev/null 2>&1; then
        pkill -KILL -u "$user" 2>/dev/null || true
      fi
    done
  fi
}

remove_systemd_and_system_files() {
  log "Removing legacy systemd unit files, drop-ins, sudoers, and helper scripts..."
  local files=(
    /etc/systemd/system/flightlink-api.service
    /etc/systemd/system/flightlink-api.service.d
    /etc/systemd/system/flightlink-router@.service
    /etc/systemd/system/flightlink-capture@.service
    /etc/sudoers.d/flightlink-router-control
    /usr/local/sbin/flightlink-routerctl
    /usr/local/sbin/flightlink-capture-runner
    /usr/local/sbin/flightlink
    /usr/local/lib/flightlink
    /run/flightlink-maintenance.lock
  )
  for f in "${files[@]}"; do
    remove_path "$f"
  done

  run_cmd systemctl daemon-reload
  run_cmd systemctl reset-failed 'flightlink-*' 2>/dev/null || true
}

remove_env_data_and_app() {
  log "Removing legacy environment files, SQLite database, capture/router data, and v0.1 checkout..."
  remove_path "${LEGACY_CONFIG_DIR}/api.env"
  remove_path "${LEGACY_CONFIG_DIR}/router.env"
  remove_path "${LEGACY_CONFIG_DIR}/capture.env"
  remove_path "${LEGACY_CONFIG_DIR}/language"

  # Only remove /etc/flightlink if empty (protects any custom/1.0 files)
  if [[ -d "${LEGACY_CONFIG_DIR}" && -z "$(ls -A "${LEGACY_CONFIG_DIR}" 2>/dev/null)" ]]; then
    remove_path "${LEGACY_CONFIG_DIR}"
  fi

  remove_path "${LEGACY_DB_PATH}"
  remove_path "${LEGACY_DB_PATH}-wal"
  remove_path "${LEGACY_DB_PATH}-shm"
  remove_path "${LEGACY_ROUTER_CFG_DIR}"
  remove_path "${LEGACY_CAPTURE_CFG_DIR}"
  remove_path "${LEGACY_CAPTURE_DIR}"
  if [[ -d "${LEGACY_DATA_DIR}" && -z "$(ls -A "${LEGACY_DATA_DIR}" 2>/dev/null)" ]]; then
    remove_path "${LEGACY_DATA_DIR}"
  fi

  remove_path "${LEGACY_APP_DIR}"
  if [[ -d /opt/flightlink && -z "$(ls -A /opt/flightlink 2>/dev/null)" ]]; then
    remove_path /opt/flightlink
  fi

  if [[ "$KEEP_BACKUPS" -eq 0 ]]; then
    remove_path /var/backups/flightlink
  else
    log "Preserving /var/backups/flightlink (--keep-backups)."
  fi

  if [[ "$REMOVE_ROUTER_SRC" -eq 1 ]]; then
    remove_path /usr/local/src/mavlink-router
  fi
}

clean_logs() {
  log "Cleaning legacy log files and systemd journald entries..."
  shopt -s nullglob
  for p in /var/log/flightlink*; do
    remove_path "$p"
  done
  shopt -u nullglob

  if [[ "$KEEP_JOURNAL" -eq 0 ]]; then
    run_cmd journalctl --rotate || true
    run_cmd journalctl --vacuum-time=1s || true
  else
    log "Skipping journald vacuum (--keep-journal)."
  fi
}

remove_legacy_accounts() {
  log "Removing legacy v0.1 system accounts and groups..."
  # NOTE: Deleting legacy `flightlink-api` (whose primary group was `flightlink-api`, NOT `flightlink`)
  # is critical before installing FlightLink 1.0 so `flightlink init` recreates it under group `flightlink`.
  for user in flightlink-api mavlink-router flightlink-capture; do
    if getent passwd "$user" >/dev/null 2>&1; then
      run_cmd userdel "$user"
      [[ "$DRY_RUN" -eq 1 ]] || log "Deleted user: $user"
    fi
  done

  for grp in flightlink-api flightlink-router flightlink-capture; do
    if getent group "$grp" >/dev/null 2>&1; then
      run_cmd groupdel "$grp"
      [[ "$DRY_RUN" -eq 1 ]] || log "Deleted group: $grp"
    fi
  done
}

verify_clean_state() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    log "Dry-run complete. No changes were made."
    return 0
  fi

  log "Verification summary:"
  printf '  - Remaining flightlink systemd units: %s\n' \
    "$(systemctl list-unit-files 'flightlink*' --no-legend --no-pager 2>/dev/null | wc -l)"
  printf '  - Remaining legacy processes:         %s\n' \
    "$(pgrep -fa 'flightlink|mavlink-routerd' 2>/dev/null | grep -v 'purge_legacy_v01' | wc -l || true)"
  printf '  - Remaining legacy accounts:          %s\n' \
    "$(getent passwd flightlink-api mavlink-router flightlink-capture 2>/dev/null | wc -l)"
  printf '  - Remaining legacy groups:            %s\n' \
    "$(getent group flightlink-api flightlink-router flightlink-capture 2>/dev/null | wc -l)"
  printf '  - mavlink-routerd binary preserved:   %s\n' \
    "$(command -v mavlink-routerd 2>/dev/null || echo 'not installed')"
  log "Legacy v0.1 purge finished. Host is ready for FlightLink 1.0 deployment."
}

main() {
  check_host
  collect_legacy_paths
  confirm_execution
  maybe_clean_legacy_cloud_rules
  stop_and_disable_services
  remove_systemd_and_system_files
  remove_env_data_and_app
  clean_logs
  remove_legacy_accounts
  verify_clean_state
}

main "$@"
