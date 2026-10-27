#!/usr/bin/env bash
# 卸载 alert-dedup。
# 用法: sudo ./scripts/uninstall.sh [--purge]
#   --purge  同时删除 /opt/alert-dedup（配置与状态）
set -euo pipefail

PREFIX="${PREFIX:-/opt/alert-dedup}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
SERVICE="alert-dedup.service"
PURGE=false

if [[ "${1:-}" == "--purge" ]]; then
  PURGE=true
fi

if [[ "${EUID}" -ne 0 ]]; then
  echo "请用 root 运行: sudo $0 [--purge]" >&2
  exit 1
fi

systemctl disable --now "${SERVICE}" 2>/dev/null || true
rm -f "${UNIT_DIR}/${SERVICE}"
systemctl daemon-reload

if [[ "${PURGE}" == true ]]; then
  rm -rf "${PREFIX}"
  echo "已删除 ${PREFIX}"
else
  echo "已卸载服务，保留 ${PREFIX}（配置与状态）"
fi
