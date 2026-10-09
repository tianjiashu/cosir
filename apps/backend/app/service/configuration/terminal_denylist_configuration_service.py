"""系统级终端 deny-list 的文件配置与正则校验。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from app.config.logging.logger import log
from app.core.tools.policy.terminal_denylist_configuration import (
    compile_terminal_deny_patterns,
    read_terminal_deny_patterns,
)
from app.service.configuration.file_store import ConfigurationFileStore
from app.utils.path.system_cosir import system_cosir_dir, system_terminal_denylist_file


@dataclass(frozen=True)
class TerminalDenylistDocument:
    """配置文件中的 pattern 列表及其系统路径。"""

    patterns: tuple[str, ...]
    path: Path


class TerminalDenylistConfigurationService:
    """读写系统级 deny-list；执行端每次读取，不缓存编译结果。"""

    def __init__(self) -> None:
        self.path = system_terminal_denylist_file()
        self.root = system_cosir_dir()
        self.store = ConfigurationFileStore()

    def read(self) -> TerminalDenylistDocument:
        """读取当前配置；缺失时由终端策略配置边界创建默认文件。"""

        with self.store.locked(self.path):
            compiled_patterns = read_terminal_deny_patterns(self.path)
        patterns = tuple(pattern.pattern for pattern in compiled_patterns)
        return TerminalDenylistDocument(patterns=patterns, path=self.path)

    def read_patterns(self) -> tuple[str, ...]:
        """读取当前文件并返回已完成正则语法校验的 pattern 字符串。"""

        return self.read().patterns

    def update(self, patterns: list[str]) -> TerminalDenylistDocument:
        """校验并原子替换 pattern 列表；下一次命令执行立即读取新配置。"""

        compile_terminal_deny_patterns(patterns)
        with self.store.locked(self.path):
            self.store.write_text_atomic(
                self.path,
                self._serialize(patterns),
                root=self.root,
            )
        log.info(
            "configuration_terminal_denylist_written",
            extra={
                "msg": "终端 deny-list 配置已保存",
                "data": {"pattern_count": len(patterns)},
            },
        )
        return TerminalDenylistDocument(patterns=tuple(patterns), path=self.path)

    @staticmethod
    def _serialize(patterns: list[str] | tuple[str, ...]) -> str:
        """将纯 pattern 列表格式化为稳定 JSON。"""

        return json.dumps(list(patterns), ensure_ascii=False, indent=2) + "\n"
