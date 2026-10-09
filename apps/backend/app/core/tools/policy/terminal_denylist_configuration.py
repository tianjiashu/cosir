"""终端工具读取系统级 deny-list 文件的边界。"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import regex

from app.config.logging.logger import log
from app.core.tools.policy.terminal_denylist import (
    DEFAULT_DENY_PATTERNS,
    compile_deny_patterns,
)
from app.core.tools.policy.terminal_denylist_contract import (
    MAX_TERMINAL_DENY_PATTERN_LENGTH,
    MAX_TERMINAL_DENY_PATTERNS,
)
from app.utils.path.system_cosir import system_terminal_denylist_file


class TerminalDenylistConfigurationError(ValueError):
    """终端 deny-list 内容不符合正则配置契约。"""


def read_terminal_deny_patterns(
    path: Path | None = None,
) -> tuple[regex.Pattern[str], ...]:
    """每次终端调用都从固定系统配置文件读取并校验 deny-list。

    子进程中的 ``execute_terminal`` 直接读取配置，不依赖父进程注入服务或把规则快照塞进
    ``ToolExecutionContext``。文件不存在时先原子发布默认配置；并发首次读取只有一个进程能
    创建目标文件，其他进程读取已发布文件。格式、规模或正则无效时抛出 ``ValueError``，由
    终端工具按 fail-closed 语义拒绝命令。
    """

    path = path or system_terminal_denylist_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    _create_default_configuration_if_missing(path)

    content = path.read_text(encoding="utf-8")
    return compile_terminal_deny_patterns(parse_terminal_deny_patterns(content))


def _create_default_configuration_if_missing(path: Path) -> None:
    """用同目录临时文件和硬链接原子创建默认配置，避免读到半写入内容。"""

    if path.exists():
        return

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(list(DEFAULT_DENY_PATTERNS), temporary, ensure_ascii=False, indent=2)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        try:
            os.link(temporary_path, path)
        except FileExistsError:
            return
        log.info(
            "configuration_terminal_denylist_initialized",
            extra={
                "msg": "终端 deny-list 缺失，已写入默认规则",
                "data": {"pattern_count": len(DEFAULT_DENY_PATTERNS)},
            },
        )
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def parse_terminal_deny_patterns(content: str) -> tuple[str, ...]:
    """解析只含正则字符串的 JSON 数组。"""

    try:
        value: Any = json.loads(content)
    except json.JSONDecodeError as exc:
        raise TerminalDenylistConfigurationError(
            f"终端 deny-list 不是有效 JSON: {exc.msg}"
        ) from exc
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise TerminalDenylistConfigurationError("终端 deny-list 必须是正则字符串数组")
    return tuple(value)


def compile_terminal_deny_patterns(
    patterns: Sequence[str],
) -> tuple[regex.Pattern[str], ...]:
    """限制配置规模并编译规则，避免损坏文件静默放行命令。"""

    if len(patterns) > MAX_TERMINAL_DENY_PATTERNS:
        raise TerminalDenylistConfigurationError(
            f"终端 deny-list pattern 数量超过限制: {len(patterns)} > "
            f"{MAX_TERMINAL_DENY_PATTERNS}"
        )
    for index, pattern in enumerate(patterns):
        if not pattern.strip():
            raise TerminalDenylistConfigurationError(
                f"终端 deny-list 第 {index + 1} 条 pattern 不能为空"
            )
        if len(pattern) > MAX_TERMINAL_DENY_PATTERN_LENGTH:
            raise TerminalDenylistConfigurationError(
                f"终端 deny-list 第 {index + 1} 条 pattern 超过长度限制: "
                f"{len(pattern)} > {MAX_TERMINAL_DENY_PATTERN_LENGTH}"
            )
    try:
        return compile_deny_patterns(patterns)
    except ValueError as exc:
        raise TerminalDenylistConfigurationError(f"终端 deny-list 含无效正则: {exc}") from exc
