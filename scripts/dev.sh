#!/usr/bin/env bash
#
# 一条命令并行拉起本地开发环境：后端 uvicorn + 前端 tauri dev。
# 所有进程输出统一重定向到仓库根 logs/ 目录（符合 AGENTS.md 的“可排查日志”铁律）。
#
# 用法:
#   bash scripts/dev.sh          # 前台运行，Ctrl+C 同时终止前后端
#   ./scripts/dev.sh             # 需先 chmod +x scripts/dev.sh
#   npm run dev:all              # 仓库根 package.json 提供的等价入口
#
# 日志位置:
#   logs/backend.log   后端 uvicorn 进程输出（启动横幅、访问日志、异常栈）
#   logs/app.log       后端应用结构化日志（configure_logging 落盘）
#   logs/frontend.log  前端 tauri dev / vite / cargo 构建输出

set -uo pipefail

# 解析脚本所在目录，定位仓库根（scripts/ 的上一级）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
LOGS_DIR="${REPO_ROOT}/logs"

mkdir -p "${LOGS_DIR}"

BACKEND_LOG="${LOGS_DIR}/backend.log"
FRONTEND_LOG="${LOGS_DIR}/frontend.log"
BACKEND_PID_FILE="${LOGS_DIR}/.backend.pid"
FRONTEND_PID_FILE="${LOGS_DIR}/.frontend.pid"

BACKEND_DIR="${REPO_ROOT}/apps/backend"
DESKTOP_DIR="${REPO_ROOT}/apps/desktop"

echo "[dev] 仓库根: ${REPO_ROOT}"
echo "[dev] 日志目录: ${LOGS_DIR}"

# 后端虚拟环境
VENV_PY="${BACKEND_DIR}/.venv/bin/python"
if [ ! -x "${VENV_PY}" ]; then
  echo "[dev] 未找到后端虚拟环境: ${VENV_PY}" >&2
  echo "[dev] 请先创建虚拟环境并安装依赖:" >&2
  echo "[dev]   cd apps/backend && python -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

# 清理可能残留的 PID 文件
rm -f "${BACKEND_PID_FILE}" "${FRONTEND_PID_FILE}"

# 启动前检查端口占用，避免后端/前端启动即退出却无提示
check_port() {
  local port="$1"
  if lsof -iTCP:"${port}" -sTCP:LISTEN -n -P >/dev/null 2>&1; then
    echo "[dev] 错误: 端口 ${port} 已被占用，请先停止占用进程。" >&2
    echo "[dev] 排查: lsof -iTCP:${port} -sTCP:LISTEN" >&2
    return 1
  fi
  return 0
}
if ! check_port 8000 || ! check_port 1420; then
  exit 1
fi

# 启动后端：python -m app（日志统一到 logs/backend.log）
echo "[dev] 启动后端 uvicorn -> ${BACKEND_LOG}"
(
  cd "${BACKEND_DIR}"
  exec "${VENV_PY}" -m app
) > "${BACKEND_LOG}" 2>&1 &
BACKEND_PID=$!
echo "${BACKEND_PID}" > "${BACKEND_PID_FILE}"

# 启动前端：tauri dev（日志统一到 logs/frontend.log）
echo "[dev] 启动前端 tauri dev -> ${FRONTEND_LOG}"
(
  cd "${DESKTOP_DIR}"
  exec npm run tauri dev
) > "${FRONTEND_LOG}" 2>&1 &
FRONTEND_PID=$!
echo "${FRONTEND_PID}" > "${FRONTEND_PID_FILE}"

# 统一清理：终止前后端及其子进程（cargo/vite）
cleanup() {
  # 防止 INT/TERM/EXIT 多次触发导致重复清理
  [ -n "${CLEANED:-}" ] && return
  CLEANED=1
  echo
  echo "[dev] 正在停止开发环境..."
  kill "${BACKEND_PID}" 2>/dev/null || true
  kill "${FRONTEND_PID}" 2>/dev/null || true
  # 回收子进程（uvicorn reloader / cargo / vite）
  pkill -P "${BACKEND_PID}" 2>/dev/null || true
  pkill -P "${FRONTEND_PID}" 2>/dev/null || true
  # 兜底：reloader / cargo 子进程可能已脱离父进程成为孤儿，按入口特征清理
  pkill -f "[p]ython -m app" 2>/dev/null || true
  pkill -f "[n]pm run tauri" 2>/dev/null || true
  rm -f "${BACKEND_PID_FILE}" "${FRONTEND_PID_FILE}"
  echo "[dev] 已停止。日志: ${LOGS_DIR}/backend.log, ${LOGS_DIR}/frontend.log, ${LOGS_DIR}/app.log"
}

trap cleanup INT TERM EXIT

echo "[dev] 开发环境已启动 (后端 PID=${BACKEND_PID}, 前端 PID=${FRONTEND_PID})"
echo "[dev] 按 Ctrl+C 停止。实时日志: tail -f ${LOGS_DIR}/backend.log ${LOGS_DIR}/frontend.log"

# 等待任一进程退出；任一退出则清理另一个
# 注意：wait -n 在 macOS 自带 bash 3.2 与 zsh 下均不可用，改用轮询兼容写法
while kill -0 "${BACKEND_PID}" 2>/dev/null && kill -0 "${FRONTEND_PID}" 2>/dev/null; do
  sleep 1
done
exit 0
