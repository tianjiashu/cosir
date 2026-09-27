#!/usr/bin/env bash

# macOS 桌面开发版启动脚本。
# 负责检查启动前置条件并从 apps/desktop 启动 Tauri；不负责安装依赖或管理后端进程。

set -euo pipefail

readonly SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly REPOSITORY_ROOT="$(CDPATH= cd -- "${SCRIPT_DIR}/.." && pwd)"
readonly DESKTOP_ROOT="${REPOSITORY_ROOT}/apps/desktop"
readonly DEV_PORT="${COSIR_DESKTOP_DEV_PORT:-3000}"

log_step() {
    printf '[cosir] %s\n' "$1"
}

fail() {
    printf '[cosir] 错误：%s\n' "$1" >&2
    exit 1
}

require_command() {
    local command_name="$1"
    local install_hint="$2"

    if ! command -v "$command_name" >/dev/null 2>&1; then
        fail "未找到 ${command_name}。${install_hint}"
    fi
}

if [[ ! -f "${DESKTOP_ROOT}/package.json" ]]; then
    fail "未找到桌面端 package.json：${DESKTOP_ROOT}"
fi

require_command "npm" "请先安装 Node.js。"
require_command "cargo" "请先安装 Rust 和 cargo-tauri。"
require_command "lsof" "macOS 通常自带 lsof，请检查系统环境。"

if ! cargo tauri --version >/dev/null 2>&1; then
    fail "未找到 cargo-tauri。请先执行：cargo install tauri-cli --version \"^2\""
fi

if [[ ! -d "${DESKTOP_ROOT}/node_modules" ]]; then
    fail "未找到 apps/desktop/node_modules。请先执行：npm ci --prefix apps/desktop"
fi

if pgrep -x "cosir-desktop" >/dev/null 2>&1; then
    log_step "Cosir 已在运行，不启动重复实例。"
    exit 0
fi

port_owners="$(lsof -nP -t -iTCP:"${DEV_PORT}" -sTCP:LISTEN 2>/dev/null || true)"
if [[ -n "${port_owners}" ]]; then
    owner_names="$(while read -r process_id; do
        ps -p "${process_id}" -o comm= 2>/dev/null || true
    done <<< "${port_owners}" | sort -u | paste -sd ', ' -)"
    owner_names="${owner_names:-未知进程}"
    fail "端口 ${DEV_PORT} 已被占用（${owner_names}）。请停止占用该端口的进程后重试。"
fi

log_step "正在启动 Tauri；Vite 和本机 FastAPI 后端由桌面应用生命周期管理。"
cd "${DESKTOP_ROOT}"

if (( $# > 0 )); then
    exec npm run tauri:dev -- "$@"
fi

exec npm run tauri:dev
