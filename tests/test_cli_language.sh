#!/usr/bin/env bash
# Local smoke test: no root, systemd, cloud credentials or installed CLI required.
set -eu
root=$(cd "$(dirname "$0")/.." && pwd)
CONFIG_DIR=$(mktemp -d)
trap 'rm -rf -- "$CONFIG_DIR"' EXIT
source "$root/deploy/flightlink-language.sh"
[[ $FL_LANGUAGE == zh ]]
choose_language en
[[ $(<"$CONFIG_DIR/language") == en ]]
FL_LANGUAGE=zh; load_language
[[ $(msg 示例 example) == example ]]
if choose_language invalid; then exit 1; fi
[[ $(<"$CONFIG_DIR/language") == en ]]
choose_language <<< 1
[[ $(<"$CONFIG_DIR/language") == zh ]]
if bash "$root/deploy/flightlink-run" stop; then exit 1; fi
