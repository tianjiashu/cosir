"""构建一条命令的有效权限；Rust 和沙箱外的终端观察消费同一份路径事实。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from app.config.constant import Constant
from app.config.logging.logger import log
from app.models.terminal_permissions import TerminalPermissionMode
from app.tools.shell.command_environment import user_command_environment
from app.utils.path.file_identity import FileIdentity, RootDirectory
from app.utils.path.system_cosir import (
    system_cosir_dir,
    system_cosir_skills_dir,
    system_scratch_dir,
)


class TerminalExecutionPolicy(BaseModel):
    """启动时冻结；后台读取沿用它，不随读取者的 Run 模式改变。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    full_access: bool
    network: bool
    writable_roots: tuple[str, ...] = ()
    writable_exceptions: tuple[str, ...] = ()
    denied_read: tuple[str, ...] = ()
    readable_exceptions: tuple[str, ...] = ()
    readonly_roots: tuple[str, ...] = ()

    @staticmethod
    def _contains(root: str, path: Path) -> bool:
        """沿用文件身份边界；macOS 大小写与 firmlink 别名不构成新权限。"""
        base = Path(root)
        try:
            if base.is_dir():
                return RootDirectory.at(base).contains(path)
            if base.exists() and path.exists():
                return FileIdentity.of(base) == FileIdentity.of(path)
        except OSError:
            pass
        # 不存在的目标仍需守住路径范围；权限计划的根在启动边界会再次验证存在性。
        left, right = str(path.resolve()), str(base.resolve())
        if sys.platform in {"darwin", "win32"}:
            left, right = left.casefold(), right.casefold()
        return left == right or left.startswith(right.rstrip(os.sep) + os.sep)

    def can_read(self, path: Path) -> bool:
        if self.full_access:
            return True
        resolved = path.resolve()
        return not any(self._contains(root, resolved) for root in self.denied_read) or any(
            self._contains(root, resolved) for root in self.readable_exceptions
        )

    @classmethod
    def for_command(
        cls,
        *,
        mode: TerminalPermissionMode,
        workspace: Path,
        writable_root: Path,
        scratch: Path,
        escalated: bool,
    ) -> TerminalExecutionPolicy:
        """升级时网络开放、可写根加上 ``/``；控制目录与系统数据仍由只读与禁读集合从每个写域里扣除。"""
        if mode == "full_access":
            return cls(full_access=True, network=True)
        reserved = workspace / ".cosir"
        data = system_cosir_dir().resolve()
        # 实际执行的控制程序不能被沙箱命令改写后用于下一次无沙箱启动。
        controls = [
            Path(
                os.environ.get(
                    "CODING_AGENT_BACKEND_CODE_ROOT", str(Path(__file__).resolve().parents[3])
                )
            ).resolve(),
            reserved.resolve(),
            data,
        ]
        controls.extend(
            Path(root).resolve()
            for root in json.loads(os.environ.get("CODING_AGENT_CONTROL_CODE_ROOTS", "[]"))
        )
        # 运行后端的解释器与它的标准库、依赖：由运行时事实推出，不靠宿主转交（开发期是 uv 管理的 CPython）。
        controls.extend({Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve()})
        roots = [writable_root.resolve()]
        if escalated:
            roots.append(Path("/"))
        return cls(
            full_access=False,
            network=escalated,
            writable_roots=tuple(dict.fromkeys(map(str, roots))),
            writable_exceptions=(str(scratch.resolve()),),
            denied_read=(str(data),),
            readable_exceptions=tuple(
                str(p.resolve())
                for p in (system_cosir_skills_dir(), system_scratch_dir())
                if p.is_dir()
            ),
            readonly_roots=tuple(dict.fromkeys(str(p.resolve()) for p in controls)),
        )


class SandboxCapability(BaseModel):
    model_config = ConfigDict(frozen=True)
    supported: bool
    reason: str = ""
    # Worker 给出了确定结论（跑通或明确不支持）；拉起失败、超时为 False，不能据此判定本机不支持。
    settled: bool = False


_probed: dict[str, SandboxCapability] = {}


def probe_sandbox(executable: str) -> SandboxCapability:
    """按实际 Worker 路径探测平台沙箱；真实启动仍可因权限或目录变化失败。

    只缓存确定的结论（Worker 跑通，或 Worker 明确返回不支持）：拉起失败、超时这类偶发情况不缓存，
    否则一次冷启动时的超时会让整个进程里的请求批准模式都不可用。
    """
    if not executable:
        return SandboxCapability(supported=False, reason="Terminal Worker 尚未配置")
    if (known := _probed.get(executable)) is not None:
        return known
    try:
        result = subprocess.run(  # noqa: S603
            [executable, "--probe-sandbox"],
            capture_output=True,
            env=user_command_environment(),
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning(
            "terminal_sandbox_probe_failed",
            extra={"msg": "沙箱探测没有得出结论，下次重试", "data": {"error": str(exc)}},
        )
        return SandboxCapability(supported=False, reason=str(exc))
    capability = SandboxCapability(
        supported=result.returncode == 0,
        reason=result.stderr.decode("utf-8", errors="replace")[-1500:] if result.returncode else "",
        settled=True,
    )
    _probed[executable] = capability
    return capability


def worker_sandbox() -> SandboxCapability:
    """本机 Terminal Worker 的沙箱能力（按 Worker 路径缓存确定结论，见 :func:`probe_sandbox`）。"""
    return probe_sandbox(os.environ.get(Constant.Terminal.WORKER_ENV, ""))


def default_terminal_permission_mode() -> TerminalPermissionMode:
    """新会话的终端权限：本机确定不支持沙箱时为完全访问权限，否则（含探测没有结论）请求批准。

    不支持沙箱时默认请求批准等于每条终端命令都失败；探测偶发失败不能把新会话降成完全访问。
    """
    capability = worker_sandbox()
    return "full_access" if capability.settled and not capability.supported else "request_approval"
