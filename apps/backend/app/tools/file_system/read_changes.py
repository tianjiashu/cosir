"""读后变更通知：Agent 读过的文件在读之后被别人改了、删了，下一次模型请求前告诉它。

写入检查（``write_policy``）只在写的那一刻按版本拒绝；这里让模型更早知道，免得基于旧内容继续推理，
也让它知道是谁在改，好去问对方或问用户。它是 core 端口 ``core/tool/file_change_notices`` 的实现。

- 每次模型请求前由 core 经端口问一次（``notices.request_notices``）：读过的路径各 stat 一次，不监听文件。
- 比对起点是上下文里每个路径最近一次读到或自己写成的版本（``FileKnowledge.stale``）；写入者靠
  ``write_attribution``。本会话自己登记的变化不报：文件工具的写入已推进已读证据，终端命令改动的已读
  文件在命令结果里当场告知过。本会话还有后台命令在跑时，修改者不明的变化也可能是它们做的，措辞里带上。
- 基准是上一条同类通知记下的完整过期集合（Run 级通知的通用做法，见 ``NOTICE_LEVELS``）：只报新出现或
  又变了的条目；重读后离开过期集合，改了又改回去也不再提。基准取自上下文，重启、分叉、撤回都与模型
  实际看到的一致；写入检查不读这里。
- 只给路径与修改者，不贴 diff：内容由模型自己决定要不要重读。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from pydantic import JsonValue

from app.core.workflow.message_queue.message_bodies.notice import NoticeBody
from app.models.message_context.message_metadata.tool_message_metadata import ToolResultFacts
from app.tools.file_system.facts.file_stale_read import StaleRead
from app.tools.file_system.knowledge import FileKnowledge
from app.tools.file_system.write_attribution import FileWriter, write_attribution
from app.tools.tool_handler.terminal.command_results import command_results


class ReadChanges:
    """无状态；进程内只有一个实例，由 lifespan 注入 core 的端口。"""

    _LISTED = 12

    def notice(
        self,
        session_id: int,
        visible: Sequence[ToolResultFacts],
        baseline: list[dict[str, JsonValue]],
    ) -> NoticeBody | None:
        """有新的过期条目时返回通知（基准为完整过期集合），否则 ``None``；会读盘，工作线程调用。"""
        stale: list[tuple[StaleRead, FileWriter | None]] = []
        for path, current in FileKnowledge.from_facts(visible).stale():
            writer = write_attribution.writer_of(Path(path), current)
            if writer is not None and writer.session_id == session_id:
                continue
            stale.append((StaleRead(path=path, version=current), writer))
        told = {item.path: item.version for item in map(StaleRead.model_validate, baseline)}
        fresh = [
            (item, writer)
            for item, writer in stale
            if item.path not in told or told[item.path] != item.version
        ]
        if not fresh:
            return None
        running = bool(command_results.pending(session_id))
        return NoticeBody(
            content=self._describe(fresh, running=running),
            notice_kind="read_files_changed",
            stale_reads=[item.model_dump(mode="json") for item, _ in stale],
        )

    @classmethod
    def _describe(cls, fresh: list[tuple[StaleRead, FileWriter | None]], *, running: bool) -> str:
        rows = ["Files you read have changed on disk since you last read them:"]
        for item, writer in fresh[: cls._LISTED]:
            change = "deleted or moved" if item.version is None else "modified"
            rows.append(f"- {item.path!r}: {change}, {cls._writer(writer, running=running)}")
        if len(fresh) > cls._LISTED:
            rows.append(f"- {len(fresh) - cls._LISTED} more files")
        rows.append(
            "Re-read a file before relying on or editing it; treat these changes as intentional and "
            "do not revert them. If a file keeps changing under you, stop writing it and find out who "
            "is editing it (session_send to that session, or ask the user)."
        )
        return "\n".join(rows)

    @staticmethod
    def _writer(writer: FileWriter | None, *, running: bool) -> str:
        if writer is None:
            if running:
                return "by the user, another process, or one of your commands still running in the background"
            return "by the user or another process"
        if writer.by_command:
            return f"likely by a command in session {writer.session_id}"
        return f"by session {writer.session_id}"


read_changes = ReadChanges()
