#!/bin/sh

set -eu

ROOT_DIR="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
APP_DIR="${ROOT_DIR}/看板程序"
PYTHON_BIN="${PYTHON_BIN:-python3}"

cd "${APP_DIR}"
if [ ! -x .venv/bin/python ]; then
  "${PYTHON_BIN}" -m venv .venv
fi
.venv/bin/python -m pip install -r requirements.txt
[ -f .env ] || cp .env.example .env

mkdir -p \
  "${ROOT_DIR}/业务数据/公司投递" \
  "${ROOT_DIR}/业务数据/同步记录" \
  "${ROOT_DIR}/业务数据/待更新" \
  "${ROOT_DIR}/业务数据/邮件导出" \
  "${ROOT_DIR}/历史备份/飞书数据快照"

.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m tools.doctor

printf '%s\n' "Installation is ready. Start with:"
printf '%s\n' "  cd '${APP_DIR}' && .venv/bin/python -m tools.dashboard"
printf '%s\n' "Feishu and QQ mail remain disabled until explicitly configured."
