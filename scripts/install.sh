#!/usr/bin/env bash
# FlightLink ECS 1.0 one-command Linux bootstrap installer (Rocky Linux 9.x & Ubuntu 24.04/26.04).
# Installs /opt/flightlink/venv + /usr/local/bin/flightlink without modifying global apt/dnf/pip sources.
#
# Usage:
#   Online (Aliyun mirror):   sudo bash scripts/install.sh --mirror aliyun
#   Online (Tsinghua mirror): sudo bash scripts/install.sh --mirror tsinghua
#   Offline bundle:           sudo bash scripts/install.sh --offline-dir ./offline-ubuntu26.04-x86_64
set -euo pipefail

INSTALL_ROOT="/opt/flightlink"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MIRROR=""
OFFLINE_DIR=""
WITH_MAVPROXY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mirror)
      MIRROR="$2"; shift 2 ;;
    --offline-dir)
      OFFLINE_DIR="$2"; shift 2 ;;
    --with-mavproxy)
      WITH_MAVPROXY=1; shift ;;
    *)
      echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

PIP_ARGS=()
if [[ -n "${OFFLINE_DIR}" ]]; then
  if [[ -f "${OFFLINE_DIR}/SHA256SUMS.txt" ]]; then
    (cd "${OFFLINE_DIR}" && sha256sum -c SHA256SUMS.txt)
  fi
  PIP_ARGS=(--no-index --find-links "${OFFLINE_DIR}/wheels")
elif [[ "${MIRROR}" == "aliyun" ]]; then
  PIP_ARGS=(--index-url https://mirrors.aliyun.com/pypi/simple)
elif [[ "${MIRROR}" == "tsinghua" ]]; then
  PIP_ARGS=(--index-url https://pypi.tuna.tsinghua.edu.cn/simple)
fi

pick_python() {
  for cand in python3.14 python3.13 python3.12 python3.11 python3; do
    if command -v "$cand" >/dev/null 2>&1; then
      if "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
        echo "$cand"
        return 0
      fi
    fi
  done
  return 1
}

PYTHON_BIN=""
if ! PYTHON_BIN="$(pick_python)"; then
  if [[ -n "${OFFLINE_DIR}" ]]; then
    echo "ERROR: Python >= 3.11 not found on host for offline install." >&2
    exit 1
  fi
  if command -v dnf >/dev/null 2>&1; then
    dnf install -y python3.12 python3.12-pip iproute sqlite
  elif command -v apt-get >/dev/null 2>&1; then
    apt-get update && apt-get install -y python3 python3-venv python3-pip iproute2 sqlite3
  fi
  PYTHON_BIN="$(pick_python)"
fi

echo "Using Python: $(${PYTHON_BIN} --version)"
mkdir -p "${INSTALL_ROOT}"
"${PYTHON_BIN}" -m venv "${INSTALL_ROOT}/venv"

if [[ -n "${OFFLINE_DIR}" ]]; then
  "${INSTALL_ROOT}/venv/bin/pip" install "${PIP_ARGS[@]}" flightlink-ecs
else
  "${INSTALL_ROOT}/venv/bin/pip" install "${PIP_ARGS[@]}" --upgrade pip
  "${INSTALL_ROOT}/venv/bin/pip" install "${PIP_ARGS[@]}" "${SRC_DIR}"
  if [[ "${WITH_MAVPROXY}" -eq 1 ]]; then
    "${INSTALL_ROOT}/venv/bin/pip" install "${PIP_ARGS[@]}" MAVProxy
  fi
fi

ln -sf "${INSTALL_ROOT}/venv/bin/flightlink" /usr/local/bin/flightlink
# Rocky/RHEL sudo secure_path excludes /usr/local/bin; also link into /usr/bin.
ln -sf "${INSTALL_ROOT}/venv/bin/flightlink" /usr/bin/flightlink

if command -v getenforce >/dev/null 2>&1; then
  if [[ "$(getenforce)" != "Disabled" ]] && command -v chcon >/dev/null 2>&1; then
    chcon -R -t bin_t "${INSTALL_ROOT}/venv/bin" || true
  fi
fi

echo ""
echo "FlightLink installed successfully! Next steps:"
echo "  1. sudo flightlink init --admin-user ops_admin --admin-password '<强密码>' --dtu-cidr 0.0.0.0/0 --gcs-cidr 0.0.0.0/0"
echo "  2. sudo flightlink connect --ram --region cn-beijing --security-group <业务安全组ID> [--ram-role <角色名>]"
echo "  3. sudo flightlink doctor"
echo "  4. sudo flightlink run"
