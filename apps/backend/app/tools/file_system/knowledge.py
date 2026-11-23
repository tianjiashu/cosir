"""一次调用可用的文件读取证据：从 core 原样交还的事实里折叠。

已读资格只来自模型真正看到的内容：执行上下文带着本次请求模型可见的结果事实（上下文顺序，撤回、
分叉、重启后都与模型所见一致）与本批前面调用刚交出的事实。本批刚读到的还没给模型看过，不算已读；
刚写成的版本算（写入者知道自己写了什么）。正文被通用预算截断过的结果，读取证据作废：行号已不是
模型真实看到的内容。
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from app.core.tool.schemas import ToolExecutionContext
from app.models.message_context.message_metadata.tool_message_metadata import ToolResultFacts
from app.tools.file_system.facts.file_facts import FileFacts
from app.tools.file_system.facts.file_mutation import FileMutation
from app.tools.file_system.facts.file_read import FileRead
from app.tools.file_system.facts.file_version import FileVersion


class FileKnowledge:
    """读取证据与每个路径最近一次读到或自己写成的版本（读后变更通知的比对起点）。"""

    def __init__(self) -> None:
        self._reads: dict[FileVersion, list[FileRead]] = {}
        self._latest: dict[str, FileVersion] = {}

    @classmethod
    def of(cls, context: ToolExecutionContext) -> FileKnowledge:
        return cls.from_facts(context.visible_facts, batch=context.batch_facts)

    @classmethod
    def from_facts(
        cls, visible: Iterable[ToolResultFacts], *, batch: Iterable[ToolResultFacts] = ()
    ) -> FileKnowledge:
        knowledge = cls()
        for facts in visible:
            knowledge._observe(facts, include_reads=not facts.content_truncated)
        for facts in batch:
            knowledge._observe(facts, include_reads=False)
        return knowledge

    def _observe(self, result: ToolResultFacts, *, include_reads: bool) -> None:
        if result.file_facts is None:
            return
        facts = FileFacts.of_payload(result.file_facts)
        for mutation in facts.mutations:
            self._advance(mutation)
        if include_reads:
            for read in facts.reads:
                self._reads.setdefault(read.version, []).append(read)
                self._latest[read.path] = read.version

    def _advance(self, mutation: FileMutation) -> None:
        if mutation.after is None:
            return
        whole = mutation.whole
        if not whole:
            if mutation.before is None or not self.has_read(mutation.before):
                return
            whole = self.has_read_whole(mutation.before)
        self._reads.setdefault(mutation.after, []).append(
            FileRead(path=mutation.path, version=mutation.after, whole=whole)
        )
        self._latest[mutation.path] = mutation.after

    def has_read(self, version: FileVersion) -> bool:
        return version in self._reads

    def has_read_whole(self, version: FileVersion) -> bool:
        reads = self._reads.get(version, ())
        if any(read.whole for read in reads):
            return True
        total = next((read.total_lines for read in reads if read.total_lines is not None), None)
        if total is None:
            return False
        covered = 0
        for first, last in sorted(span for read in reads for span in read.lines):
            if first > covered + 1:
                return False
            covered = max(covered, last)
        return covered >= total

    def previous(self, path: Path, version: FileVersion) -> bool:
        return any(
            read.version.identity == version.identity or read.path == str(path)
            for reads in self._reads.values()
            for read in reads
        )

    def latest_reads(self) -> dict[str, FileVersion]:
        return dict(self._latest)

    def stale(self) -> list[tuple[str, FileVersion | None]]:
        """读过、但磁盘上已不是最近读到（或自己写成）的那一版的文件，按路径排序；删除或移走为 ``None``。

        会读盘（每个路径 stat 一次）。读后变更通知与终端命令收尾共用这一条比较规则。
        """
        stale = []
        for path, read in sorted(self._latest.items()):
            current = FileVersion.of(Path(path))
            if current != read:
                stale.append((path, current))
        return stale
