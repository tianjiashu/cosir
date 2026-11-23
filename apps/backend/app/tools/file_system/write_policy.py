"""文件工具的写前检查（先读、读全文、版本未变），以及写入期间对目标的占用：两个文件工具不会同时改同一处。

终端对文件系统不透明，不参与检查与占用，写入范围只由 OS 沙箱划定。
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Literal

from app.config.logging.logger import log
from app.core.tool.schemas import ToolExecutionContext
from app.tools.file_system.facts.file_version import FileVersion
from app.tools.file_system.knowledge import FileKnowledge


@dataclass(frozen=True)
class WriteTarget:
    path: Path
    action: Literal["create", "modify"]
    version: FileVersion | None
    whole: bool = False


class WriteRefused(ValueError):
    """写入前的拒绝原因，附带可供工具卡片展示的提示。"""

    def __init__(self, message: str, hint: str) -> None:
        super().__init__(message)
        self.hint = hint


class FileWritePolicy:
    def __init__(self) -> None:
        self._lock = Lock()
        self._active: dict[object, tuple[WriteTarget, ...]] = {}

    @contextmanager
    def reserve(
        self,
        targets: Sequence[WriteTarget],
        context: ToolExecutionContext,
    ) -> Iterator[None]:
        """按这次调用可用的读取证据一次性校验并占用全部目标，直到写入收尾。"""
        knowledge = FileKnowledge.of(context)
        call_id = context.tool_call_id
        lease = object()
        with self._lock:
            for target in targets:
                self._check(target, knowledge, call_id)
            if targets:
                self._active[lease] = tuple(targets)
        try:
            yield
        finally:
            if targets:
                with self._lock:
                    del self._active[lease]

    def _check(self, target: WriteTarget, knowledge: FileKnowledge, call_id: str) -> None:
        path = target.path
        version = FileVersion.of(path)
        hint = "文件已变化"
        if target.action == "create" and (path.exists() or path.is_symlink()):
            reason = "the creation target already exists"
        elif version != target.version:
            reason = "the target changed during preparation. Read its current contents and retry"
        elif any(
            self._overlaps(target, active) for batch in self._active.values() for active in batch
        ):
            reason, hint = (
                "another tool is modifying this target. Wait for it to finish or stop it",
                "正在修改",
            )
        elif version is not None and target.action != "create" and not knowledge.has_read(version):
            changed = knowledge.previous(path, version)
            reason = (
                "the file changed after you last read it"
                if changed
                else "you have not read it in this conversation"
            )
            reason += (
                ". Read it with read_file (terminal output does not count), wait for the result, "
                "then issue a new modification"
            )
            hint = "文件已变化" if changed else "需先读取"
        elif version is not None and target.whole and not knowledge.has_read_whole(version):
            reason, hint = "read the entire current file with read_file before replacing it", "需读全文"
        else:
            return
        log.info(
            "file_write_refused",
            extra={"data": {"tool_call_id": call_id, "path": str(path), "reason": reason}},
        )
        raise WriteRefused(f"{path}: {reason}.", hint)

    @staticmethod
    def _overlaps(left: WriteTarget, right: WriteTarget) -> bool:
        if (
            left.path == right.path
            or left.path in right.path.parents
            or right.path in left.path.parents
        ):
            return True
        return (
            left.version is not None
            and right.version is not None
            and left.version.identity == right.version.identity
        )


file_write_policy = FileWritePolicy()
