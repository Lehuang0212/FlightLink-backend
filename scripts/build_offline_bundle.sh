#!/usr/bin/env bash
# Build an offline installation bundle for a confirmed Linux target (Ubuntu 26.04.1 LTS x86_64).
# Run ON the target Ubuntu 26.04.1 LTS x86_64 build host (or matching container) so all binary
# wheels match the exact system Python ABI. Never modifies global OS apt/pip sources.
set -euo pipefail

OUT_DIR="${1:-dist/offline-ubuntu26.04-x86_64}"
PIP_INDEX_ARG=()
if [[ -n "${FLIGHTLINK_PIP_INDEX_URL:-}" ]]; then
  # Optional Tsinghua (https://pypi.tuna.tsinghua.edu.cn/simple) or
  # Aliyun (https://mirrors.aliyun.com/pypi/simple) mirror passed per-invocation only.
  PIP_INDEX_ARG=(--index-url "${FLIGHTLINK_PIP_INDEX_URL}")
fi

rm -rf "${OUT_DIR}"
mkdir -p "${OUT_DIR}/wheels" "${OUT_DIR}/deploy/systemd"

python3 --version > "${OUT_DIR}/TARGET_ENV.txt"
uname -m >> "${OUT_DIR}/TARGET_ENV.txt"
if [[ -f /etc/os-release ]]; then
  grep -E '^(PRETTY_NAME|VERSION_ID|ID)=' /etc/os-release >> "${OUT_DIR}/TARGET_ENV.txt"
fi

# Build flightlink-ecs wheel (explicitly excludes .agents/ dev skills)
python3 -m pip wheel --no-deps --wheel-dir "${OUT_DIR}/wheels" .
python3 -m pip download "${PIP_INDEX_ARG[@]}" --dest "${OUT_DIR}/wheels" .

cp -a deploy/systemd/* "${OUT_DIR}/deploy/systemd/"
cp -a docs/operations-and-packaging.md "${OUT_DIR}/"

# Verify .agents/ is absent from built wheel
python3 -c '
import zipfile, pathlib, sys
whls = list(pathlib.Path(sys.argv[1]).glob("flightlink_ecs-*.whl"))
assert len(whls) == 1
with zipfile.ZipFile(whls[0]) as zf:
    leaked = [n for n in zf.namelist() if ".agents" in n]
    assert not leaked, f"Dev skills leaked into wheel: {leaked}"
' "${OUT_DIR}/wheels"

(cd "${OUT_DIR}" && find . -type f ! -name 'SHA256SUMS.txt' -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS.txt)
echo "Offline bundle built and checksummed at ${OUT_DIR}"
