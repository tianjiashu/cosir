#!/usr/bin/env bash
#
# 只启动桌面客户端：适用于后端由 IDEA / PyCharm Debug 单独启动的场景。
# 本脚本不会启动或清理 8000 后端端口，只负责拉起 Vite + Tauri dev。
#
# 用法:
#   bash scripts/dev-client.sh
#   ./scripts/dev-client.sh
#   npm run dev:client
#
# 日志位置:
#   logs/frontend.log  前端 tauri dev / vite / cargo 构建输出

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
LOGS_DIR="${REPO_ROOT}/logs"
DESKTOP_DIR="${REPO_ROOT}/apps/desktop"

FRONTEND_LOG="${LOGS_DIR}/frontend.log"
FRONTEND_PID_FILE="${LOGS_DIR}/.frontend.pid"

mkdir -p "${LOGS_DIR}"

echo "[dev-client] 仓库根: ${REPO_ROOT}"
echo "[dev-client] 日志目录: ${LOGS_DIR}"

free_frontend_port() {
  local port="1420"
  local pids
  pids="$(lsof -tiTCP:"${port}" -sTCP:LISTEN -n -P 2>/dev/null)"
  if [ -z "${pids}" ]; then
    return 0
  fi

  echo "[dev-client] 前端端口 ${port} 已被占用，占用进程如下，正在清理:"
  lsof -iTCP:"${port}" -sTCP:LISTEN -n -P 2>/dev/null | sed 's/^/[dev-client]   /'
  for pid in ${pids}; do
    echo "[dev-client]   终止前端占用进程 PID=${pid}"
    kill "${pid}" 2>/dev/null || true
  done

  local waited=0
  while lsof -iTCP:"${port}" -sTCP:LISTEN -n -P >/dev/null 2>&1; do
    [ "${waited}" -ge 5 ] && break
    sleep 1
    waited=$((waited + 1))
  done

  if lsof -iTCP:"${port}" -sTCP:LISTEN -n -P >/dev/null 2>&1; then
    echo "[dev-client] 错误: 前端端口 ${port} 仍被占用，请手动处理:" >&2
    echo "[dev-client]   lsof -iTCP:${port} -sTCP:LISTEN" >&2
    return 1
  fi

  echo "[dev-client] 前端端口 ${port} 已清理。"
  return 0
}

if ! free_frontend_port; then
  exit 1
fi

if ! lsof -iTCP:8000 -sTCP:LISTEN -n -P >/dev/null 2>&1; then
  echo "[dev-client] 提示: 未检测到 8000 后端端口。请先在 IDEA Debug 中启动后端。"
fi

rm -f "${FRONTEND_PID_FILE}"

echo "[dev-client] 启动前端 tauri dev -> ${FRONTEND_LOG}"
(
  cd "${DESKTOP_DIR}"
  exec npm run tauri dev
) > "${FRONTEND_LOG}" 2>&1 &
FRONTEND_PID=$!
echo "${FRONTEND_PID}" > "${FRONTEND_PID_FILE}"

cleanup() {
  [ -n "${CLEANED:-}" ] && return
  CLEANED=1
  echo
  echo "[dev-client] 正在停止客户端..."
  kill "${FRONTEND_PID}" 2>/dev/null || true
  pkill -P "${FRONTEND_PID}" 2>/dev/null || true
  pkill -f "[n]pm run tauri" 2>/dev/null || true
  rm -f "${FRONTEND_PID_FILE}"
  echo "[dev-client] 已停止。日志: ${FRONTEND_LOG}"
}

trap cleanup INT TERM EXIT

echo "[dev-client] 客户端已启动 (前端 PID=${FRONTEND_PID})"
echo "[dev-client] 按 Ctrl+C 停止客户端。实时日志: tail -f ${FRONTEND_LOG}"

while kill -0 "${FRONTEND_PID}" 2>/dev/null; do
  sleep 1
done
