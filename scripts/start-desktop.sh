#!/usr/bin/env bash

# macOS 桌面开发版启动脚本。
# 负责检查启动前置条件并从 apps/desktop 启动 Electron 开发版；不负责安装依赖或管理后端进程。
# 应用本身是单实例，重复启动由应用唤起已有窗口；开发期端口占用在下面单独检查。

set -euo pipefail

readonly SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly REPOSITORY_ROOT="$(CDPATH= cd -- "${SCRIPT_DIR}/.." && pwd)"
readonly DESKTOP_ROOT="${REPOSITORY_ROOT}/apps/desktop"
# 与 apps/desktop/scripts/run-electron-dev.mjs 的 RENDERER_PORT 一致（开发入口固定用这个端口）。
readonly DEV_PORT=3000

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
require_command "cargo" "请先安装 Rust（用于构建 Terminal Worker）。"
require_command "lsof" "macOS 通常自带 lsof，请检查系统环境。"

if [[ ! -d "${DESKTOP_ROOT}/node_modules" ]]; then
    fail "未找到 apps/desktop/node_modules。请先执行：cd apps/desktop && npm ci --include=dev"
fi

# node_modules 目录存在不代表安装完整：省略 devDependencies（NODE_ENV=production 或 --omit=dev）或被中断的安装
# 会留下缺失或被悬空软链指向的包，开发入口启动 Vite 时随后失败。
# 因此这里直接校验真正会被执行的 vite 可执行入口，而不是只判断目录是否存在。
if [[ ! -x "${DESKTOP_ROOT}/node_modules/.bin/vite" ]]; then
    fail "apps/desktop/node_modules 不完整：缺少可执行的 node_modules/.bin/vite（Vite 属于 devDependencies）。请先执行：cd apps/desktop && npm ci --include=dev"
fi

port_owners="$(lsof -nP -t -iTCP:"${DEV_PORT}" -sTCP:LISTEN 2>/dev/null || true)"
if [[ -n "${port_owners}" ]]; then
    owner_names="$(while read -r process_id; do
        ps -p "${process_id}" -o comm= 2>/dev/null || true
    done <<< "${port_owners}" | sort -u | paste -sd ', ' -)"
    owner_names="${owner_names:-未知进程}"
    fail "端口 ${DEV_PORT} 已被占用（${owner_names}）。请停止占用该端口的进程后重试。"
fi

log_step "正在启动 Electron 开发版；Terminal Worker、Vite 和本机 FastAPI 后端由开发入口与桌面应用生命周期管理。"
cd "${DESKTOP_ROOT}"

exec npm run electron:dev
