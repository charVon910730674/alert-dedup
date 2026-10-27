#!/usr/bin/env bash
# 安装 / 升级 alert-dedup。
# 用法: sudo ./scripts/install.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX="${PREFIX:-/opt/alert-dedup}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
SERVICE="alert-dedup.service"

if [[ "${EUID}" -ne 0 ]]; then
  echo "请用 root 运行: sudo $0" >&2
  exit 1
fi

install -d -m 0755 "${PREFIX}"
install -m 0755 "${ROOT}/dedup_proxy.py" "${PREFIX}/dedup_proxy.py"

if [[ -f "${PREFIX}/config.json" ]]; then
  BAK="${PREFIX}/config.json.bak.$(date +%Y%m%d%H%M%S)"
  cp -a "${PREFIX}/config.json" "${BAK}"
  echo "保留已有配置，备份: ${BAK}"
else
  install -m 0644 "${ROOT}/config.example.json" "${PREFIX}/config.json"
  echo "已生成默认配置: ${PREFIX}/config.json"
fi

install -m 0644 "${ROOT}/systemd/${SERVICE}" "${UNIT_DIR}/${SERVICE}"

systemctl daemon-reload
systemctl enable "${SERVICE}" >/dev/null
systemctl restart "${SERVICE}"

echo
systemctl --no-pager --lines=5 status "${SERVICE}"
