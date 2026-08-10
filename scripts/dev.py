#!/usr/bin/env python3
"""一条命令并行拉起本地开发环境：后端 (uvicorn) + 桌面 (tauri dev)。

逻辑严格对齐 scripts/dev.sh (macOS 版)，跨平台实现（Windows / macOS / Linux）：
  - 启动前清理 8000/1420 端口占用，超时未释放则报错退出；
  - 前后端日志统一重定向到 logs/ 目录；
  - 写入 PID 文件，Ctrl+C / 进程退出 / 脚本退出时优雅终止（先 SIGTERM 再 SIGKILL）；
  - 任一进程退出则联动清理另一个。

用法:
  python scripts/dev.py          # 前台运行，Ctrl+C 同时终止前后端
  npm run dev:all                # 仓库根 package.json 提供的等价入口（需更新为 python 版）

日志位置:
  logs/backend.log   后端 uvicorn 进程输出（启动横幅、访问日志、异常栈）
  logs/app.log       后端应用结构化日志（configure_logging 落盘）
  logs/frontend.log  前端 tauri dev / vite / cargo 构建输出
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
LOGS_DIR = os.path.join(REPO_ROOT, "logs")
BACKEND_LOG = os.path.join(LOGS_DIR, "backend.log")
FRONTEND_LOG = os.path.join(LOGS_DIR, "frontend.log")
BACKEND_PID_FILE = os.path.join(LOGS_DIR, ".backend.pid")
FRONTEND_PID_FILE = os.path.join(LOGS_DIR, ".frontend.pid")

BACKEND_DIR = os.path.join(REPO_ROOT, "apps", "backend")
DESKTOP_DIR = os.path.join(REPO_ROOT, "apps", "desktop")

BACKEND_PORT = 8000
FRONTEND_PORT = 1420


def log(msg: str) -> None:
    """打印带 [dev] 前缀的日志到 stdout。"""
    print(f"[dev] {msg}", flush=True)


def err(msg: str) -> None:
    """打印带 [dev] 前缀的错误日志到 stderr。"""
    print(f"[dev] {msg}", file=sys.stderr, flush=True)


def is_windows() -> bool:
    """返回当前是否运行于 Windows 平台。"""
    return os.name == "nt"


def venv_python() -> str:
    """返回后端虚拟环境的 python 可执行文件路径（按平台区分 bin / Scripts）。"""
    if is_windows():
        return os.path.join(BACKEND_DIR, ".venv", "Scripts", "python.exe")
    return os.path.join(BACKEND_DIR, ".venv", "bin", "python")


def find_listener_pids(port: int) -> list[int]:
    """返回占用指定 LISTEN 端口的进程 PID 列表（跨平台）。"""
    pids: list[int] = []
    if is_windows():
        out = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True,
            text=True,
        ).stdout
        for line in out.splitlines():
            cols = line.split()
            # 列格式: Proto  LocalAddress  ForeignAddress  State  PID
            if len(cols) >= 5 and cols[3] == "LISTENING":
                local = cols[1]
                if local.endswith(f":{port}"):
                    try:
                        pids.append(int(cols[4]))
                    except ValueError:
                        continue
    else:
        out = subprocess.run(
            ["lsof", "-tiTCP", f"{port}", "-sTCP:LISTEN", "-n", "-P"],
            capture_output=True,
            text=True,
        ).stdout
        for token in out.split():
            token = token.strip()
            if token.isdigit():
                pids.append(int(token))
    # 去重并保持顺序
    seen: set[int] = set()
    unique: list[int] = []
    for pid in pids:
        if pid not in seen:
            seen.add(pid)
            unique.append(pid)
    return unique


def kill_tree(pid: int) -> None:
    """优雅终止进程及其子进程（Windows 用 taskkill /T，类 Unix 用 os.kill）。"""
    if is_windows():
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T"],
            capture_output=True,
        )
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def kill_tree_force(pid: int) -> None:
    """强制终止进程及其子进程。"""
    if is_windows():
        subprocess.run(
            ["taskkill", "/F", "/PID", str(pid), "/T"],
            capture_output=True,
        )
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def free_port(port: int) -> bool:
    """清理端口占用：先优雅终止，等待最多 5 秒，未释放则强制终止；仍占用返回 False。"""
    pids = find_listener_pids(port)
    if not pids:
        log(f"端口 {port} 空闲")
        return True

    log(f"端口 {port} 已被占用，占用进程如下，正在清理:")
    # 打印占用进程详情，便于排查误杀
    if is_windows():
        detail = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True
        ).stdout
        for line in detail.splitlines():
            if f":{port} " in line and "LISTENING" in line:
                log(f"   {line.strip()}")
    else:
        detail = subprocess.run(
            ["lsof", "-iTCP", f"{port}", "-sTCP:LISTEN", "-n", "-P"],
            capture_output=True,
            text=True,
        ).stdout
        for line in detail.splitlines():
            log(f"   {line}")

    for pid in pids:
        log(f"   终止进程 PID={pid}")
        kill_tree(pid)

    waited = 0
    while find_listener_pids(port):
        if waited >= 5:
            break
        time.sleep(1)
        waited += 1

    pids = find_listener_pids(port)
    if pids:
        log(f"端口 {port} 5 秒内未释放，尝试强制终止...")
        for pid in pids:
            log(f"   强制终止 PID={pid}")
            kill_tree_force(pid)
        time.sleep(1)

    if find_listener_pids(port):
        err(f"错误: 端口 {port} 仍被占用，无法清理，请手动处理:")
        err(f"   {'netstat -ano | findstr :' + str(port) if is_windows() else 'lsof -iTCP:' + str(port) + ' -sTCP:LISTEN'}")
        return False

    log(f"端口 {port} 已清理，可继续启动。")
    return True


def is_alive(pid: int) -> bool:
    """返回进程是否仍存活。"""
    if is_windows():
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"],
            capture_output=True,
            text=True,
        ).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def write_pid_file(path: str, pid: int) -> None:
    """写入 PID 文件。"""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(str(pid))


def spawn(cmd: list[str] | str, cwd: str, log_path: str, shell: bool = False) -> int:
    """在指定目录启动子进程，输出重定向到日志文件，返回 PID。

    Windows 下 npm/node 等命令是 .cmd 包装，直接 Popen 列表无法解析扩展名，
    故对前端使用 shell=True 经 cmd.exe 解析（与 dev.sh 的 exec npm 行为一致）。
    """
    log_file = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        shell=shell,
        # Windows 下不继承控制台，避免 Ctrl+C 直接打到子进程
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if is_windows() else 0,
    )
    return proc.pid


def spawn_backend(vpy: str, cwd: str, log_path: str) -> int:
    """启动后端 python -m app。Windows 下经 cmd 解析路径。"""
    if is_windows():
        # 含空格的 vpy 路径需引号；用 cmd /c 解析扩展名。
        # 不在此处设置 LANGFUSE 相关变量，交由 apps/backend/.env 的
        # CODING_AGENT_LANGFUSE_ENABLED 控制（Settings 用 setdefault 加载，
        # 若在此 set 则 .env 永不生效）。
        cmd = (
            f'"{vpy}" -m app'
        )
        return spawn(cmd, cwd, log_path, shell=True)
    return spawn([vpy, "-m", "app"], cwd, log_path)


def spawn_frontend(cwd: str, log_path: str) -> int:
    """启动前端 npm run tauri dev。Windows 下 npm 为 .cmd 包装，须 shell=True 解析。"""
    if is_windows():
        return spawn("npm run tauri dev", cwd, log_path, shell=True)
    return spawn(["npm", "run", "tauri", "dev"], cwd, log_path)


def main() -> int:
    """启动并守护本地开发环境，返回进程退出码。"""
    # 解释器自检：避免使用已知的损坏混合安装 G:\Python\Python311
    if "G:\\Python\\Python311" in sys.executable.replace("/", "\\"):
        err("警告: 当前 python 来自损坏的 G:\\Python\\Python311 混合安装。")
        err("建议改用 uv 托管解释器运行: uv run python scripts/dev.py")
        err("子进程仍会使用 venv 内的正确解释器启动，可继续；但若异常请切换解释器。")

    log(f"仓库根: {REPO_ROOT}")
    log(f"日志目录: {LOGS_DIR}")

    os.makedirs(LOGS_DIR, exist_ok=True)

    vpy = venv_python()
    if not os.path.exists(vpy):
        err(f"未找到后端虚拟环境: {vpy}")
        err("请先创建虚拟环境并安装依赖:")
        err("  cd apps/backend && uv sync --group dev")
        return 1

    # 清理可能残留的 PID 文件
    for pid_file in (BACKEND_PID_FILE, FRONTEND_PID_FILE):
        if os.path.exists(pid_file):
            os.remove(pid_file)

    # 启动前检查并清理端口占用
    if not free_port(BACKEND_PORT) or not free_port(FRONTEND_PORT):
        return 1

    backend_pid: int | None = None
    frontend_pid: int | None = None
    cleaned = False

    def cleanup() -> None:
        """统一清理：终止前后端及其子进程，并删除 PID 文件。"""
        nonlocal cleaned
        if cleaned:
            return
        cleaned = True
        print()
        log("正在停止开发环境...")
        children: list[int] = []
        if backend_pid is not None:
            children.append(backend_pid)
        if frontend_pid is not None:
            children.append(frontend_pid)
        # 先优雅终止
        for pid in children:
            kill_tree(pid)
        # 等待子进程退出（最多 5 秒）
        for _ in range(5):
            if all(not is_alive(pid) for pid in children if pid is not None):
                break
            time.sleep(1)
        # 仍存活则强制终止
        for pid in children:
            if pid is not None and is_alive(pid):
                kill_tree_force(pid)
        # 注：不再做全局兜底清理。kill_tree 已用 /T（Windows）或进程树
        # （类 Unix）覆盖后端/前端及其全部子进程；按窗口标题或命令行特征
        # 的全局匹配会误杀同名无关进程（如窗口标题含 Coding-Agent 的 IDE）。
        for pid_file in (BACKEND_PID_FILE, FRONTEND_PID_FILE):
            if os.path.exists(pid_file):
                os.remove(pid_file)
        log(f"已停止。日志: {BACKEND_LOG}, {FRONTEND_LOG}, {os.path.join(LOGS_DIR, 'app.log')}")

    def handle_signal(signum, frame) -> None:
        """捕获 SIGINT / SIGTERM，触发清理后退出。"""
        cleanup()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # 启动后端
    log(f"启动后端 uvicorn -> {BACKEND_LOG}")
    backend_pid = spawn_backend(vpy, BACKEND_DIR, BACKEND_LOG)
    write_pid_file(BACKEND_PID_FILE, backend_pid)

    # 启动前端
    log(f"启动前端 tauri dev -> {FRONTEND_LOG}")
    frontend_pid = spawn_frontend(DESKTOP_DIR, FRONTEND_LOG)
    write_pid_file(FRONTEND_PID_FILE, frontend_pid)

    log(f"开发环境已启动 (后端 PID={backend_pid}, 前端 PID={frontend_pid})")
    log(f"按 Ctrl+C 停止。实时日志: tail -f {BACKEND_LOG} {FRONTEND_LOG}")

    # 轮询：任一进程退出则清理另一个
    try:
        while is_alive(backend_pid) and is_alive(frontend_pid):
            time.sleep(1)
    finally:
        cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
