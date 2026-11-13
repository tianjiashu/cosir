"""启动期解析用户登录 shell 的环境，合并进后端进程环境。

为什么在这里修：从 Dock / 访达启动桌面应用时，宿主只拿到 launchd 给的最小 PATH，后端照单继承，
它派生的一切子进程（``execute_terminal``、交互终端 shell、MCP stdio 服务）与它自己的 shell 探测
都跟着找不到 nvm / pyenv / Homebrew 的命令。在这些消费者共同的进程树根上解析一次，比在每个工具
上各加 login 参数少一类遗漏；与 VS Code 主进程的 ``resolveShellEnv`` 同一做法。

调用时机是契约：``app.__main__`` 在日志就绪之后、``Settings.load()`` 之前调用一次，早于工具注册、
shell 探测、MCP 连接池与 uvicorn reload 子进程。放在 ``Settings.load()`` 之前，``.env``「不覆盖
已存在变量」的语义才保持不变：用户 shell 导出的同名变量优先。

不负责：决定哪些变量交给用户命令（见 ``command_environment``）；Windows（GUI 进程本来就从注册表
拿到完整环境，直接跳过）。
"""

import contextlib
import os
import secrets
import signal
import subprocess
import time
from collections.abc import MutableMapping

from app.config.logging.logger import log
from app.tools.shell.command_environment import HOST_VARIABLE_PREFIX
from app.tools.shell.host_shells import user_default_shell
from app.utils.host_platform import is_windows

# 用户 rc 可据此跳过不适合在解析时执行的东西（tmux、交互提示等），对应 VS Code 的
# VSCODE_RESOLVING_ENVIRONMENT。只设在解析用的 shell 上，不进合并结果。
RESOLVING_FLAG = "COSIR_RESOLVING_ENVIRONMENT"

_TIMEOUT_SECONDS = 10.0

# 不合并的键：解析标记本身，以及 shell 自己维护的进程簿记（``_`` 会变成 /usr/bin/env，PWD 是解析
# shell 的目录）。
_NOT_MERGED = frozenset({RESOLVING_FLAG, "_", "SHLVL", "PWD", "OLDPWD"})


class _ResolveError(Exception):
    """解析失败但可回退：shell 异常退出、输出里找不到标记。"""


def apply_login_shell_environment(environ: MutableMapping[str, str] | None = None) -> None:
    """解析用户登录 shell 的环境并合并进 ``environ``（缺省为 ``os.environ``）。

    合并规则：解析结果覆盖同名变量，但 ``CODING_AGENT_*`` 以宿主注入为准（rc 不该改写宿主与
    后端的契约），解析标记与 shell 簿记变量不合并；解析结果里没有的变量保留。

    失败（shell 不存在、超时、异常退出、输出无标记）只记 warning 并保留原环境，不阻断启动：
    最坏情况就是现在的行为。

    副作用:
        启动一次用户的交互登录 shell（会执行用户的 profile / rc），最多等 10 秒；改写进程环境。
    """
    if is_windows():
        return
    target = os.environ if environ is None else environ
    shell = user_default_shell(target)
    started = time.monotonic()
    try:
        resolved = _resolve(shell, target)
    except (OSError, subprocess.TimeoutExpired, _ResolveError) as exc:
        log.warning(
            "login_shell_env_resolve_failed",
            extra={
                "msg": "解析用户登录 shell 环境失败，沿用当前进程环境",
                "data": {"shell": shell, "error_type": type(exc).__name__, "error": str(exc)},
            },
        )
        return
    changed = []
    for key, value in resolved.items():
        if key.startswith(HOST_VARIABLE_PREFIX) or key in _NOT_MERGED:
            continue
        if target.get(key) != value:
            target[key] = value
            changed.append(key)
    log.info(
        "login_shell_env_resolved",
        extra={
            "msg": "已合并用户登录 shell 环境",
            "data": {
                "shell": shell,
                "duration_ms": round((time.monotonic() - started) * 1000),
                "changed_keys": sorted(changed),
            },
        },
    )


def _resolve(shell: str, environ: MutableMapping[str, str]) -> dict[str, str]:
    """启动 ``shell`` 打印环境并解析。

    打印用 ``/usr/bin/env -0`` 而不是解释器自己：打包后的后端是冻结可执行文件，不能 ``-c`` 执行
    代码。绝对路径绕开用户 alias / 函数；随机标记把结果与 rc 打印的内容隔开；NUL 分隔保住多行值。
    cwd 取主目录，避免 direnv、nvm 自动切换这类按目录生效的 rc 逻辑吃到后端自己的目录。
    """
    mark = secrets.token_hex(8)
    script = f"/usr/bin/printf %s {mark}; /usr/bin/env -0; /usr/bin/printf %s {mark}"
    name = os.path.basename(shell)
    args = ["-ic", script] if name in {"csh", "tcsh"} else ["-i", "-l", "-c", script]
    process = subprocess.Popen(  # noqa: S603 - 用户自己的登录 shell
        [shell, *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        cwd=os.path.expanduser("~"),
        env={**environ, RESOLVING_FLAG: "1"},
        # 独立进程组：超时时连同 rc 拉起的子进程一起杀掉，否则它们握着 stdout 会让读取一直挂着。
        start_new_session=True,
    )
    try:
        stdout, _ = process.communicate(timeout=_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
        # 换了会话的后代杀不到、可能一直握着 stdout，收尸也只等一小会儿。
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.communicate(timeout=1)
        raise
    return _parse(stdout, mark.encode("ascii"), process.returncode)


def _parse(stdout: bytes, mark: bytes, returncode: int) -> dict[str, str]:
    start = stdout.find(mark)
    end = stdout.find(mark, start + len(mark)) if start >= 0 else -1
    if end < 0:
        # 有标记就采用：rc 里某条命令失败也可能让退出码非零，但标记之间的环境仍然完整。
        raise _ResolveError(f"environment markers not found (exit code {returncode})")
    resolved: dict[str, str] = {}
    for entry in stdout[start + len(mark) : end].split(b"\0"):
        key, separator, value = entry.partition(b"=")
        if separator and key:
            resolved[os.fsdecode(key)] = os.fsdecode(value)
    return resolved
