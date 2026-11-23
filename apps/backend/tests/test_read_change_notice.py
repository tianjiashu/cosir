"""读过的文件变化通知：只报新出现或又变了的过期条目，写明修改者，本会话文件工具写成的不报。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.core.workflow.message_queue.message_bodies.notice import notices_to_context
from app.models.message_context.message_metadata.tool_message_metadata import ToolResultFacts
from app.tools.file_system import read_changes as notice_module
from app.tools.file_system.facts.file_facts import FileFacts
from app.tools.file_system.facts.file_mutation import FileMutation
from app.tools.file_system.facts.file_read import FileRead
from app.tools.file_system.facts.file_version import FileVersion
from app.tools.file_system.knowledge import FileKnowledge
from app.tools.file_system.read_changes import ReadChanges
from app.tools.file_system.write_attribution import WriteAttribution

_SELF, _OTHER = 1, 7


@pytest.fixture
def attribution(monkeypatch) -> WriteAttribution:
    registry = WriteAttribution()
    monkeypatch.setattr(notice_module, "write_attribution", registry)
    return registry


def _rewrite(path: Path, text: str) -> FileVersion:
    before = path.stat().st_mtime_ns
    path.write_text(text)
    # 同一纳秒内重写时 mtime 可能不变：推后一点，保证版本一定不同。
    os.utime(path, ns=(before + 1_000_000, before + 1_000_000))
    version = FileVersion.of(path)
    assert version is not None
    return version


def _read(visible: list[ToolResultFacts], path: Path) -> None:
    version = FileVersion.of(path)
    assert version is not None
    visible.append(FileFacts(reads=(FileRead(path=str(path), version=version, whole=True),)).as_result())


def _modified(path: Path, before: FileVersion | None, after: FileVersion) -> FileFacts:
    return FileFacts(
        mutations=(FileMutation(path=str(path), operation="modify", before=before, after=after),)
    )


def _notice(visible: list[ToolResultFacts], baseline=None):
    return ReadChanges().notice(_SELF, visible, baseline or [])


def test_external_change_is_told_once_and_again_when_it_changes_again(
    tmp_path, attribution
) -> None:
    path = tmp_path / "a.py"
    path.write_text("one")
    visible: list[ToolResultFacts] = []
    _read(visible, path)
    assert _notice(visible) is None

    _rewrite(path, "two")
    notice = _notice(visible)
    assert notice is not None and notice.notice_kind == "read_files_changed"
    assert str(path) in notice.content and "by the user or another process" in notice.content
    baseline = notice.stale_reads or []
    assert _notice(visible, baseline) is None

    _rewrite(path, "three")
    assert _notice(visible, baseline) is not None


def test_rereading_clears_the_stale_entry(tmp_path, attribution) -> None:
    path = tmp_path / "a.py"
    path.write_text("one")
    visible: list[ToolResultFacts] = []
    _read(visible, path)
    _rewrite(path, "two")
    _read(visible, path)
    assert _notice(visible) is None


def test_deletion_is_reported(tmp_path, attribution) -> None:
    path = tmp_path / "a.py"
    path.write_text("one")
    visible: list[ToolResultFacts] = []
    _read(visible, path)
    path.unlink()
    notice = _notice(visible)
    assert notice is not None and "deleted or moved" in notice.content
    assert notice.stale_reads is not None and notice.stale_reads[0]["version"] is None


def test_own_file_tool_write_is_not_reported(tmp_path, attribution) -> None:
    """只有文件工具确认的写入算本会话的；终端改动对文件系统不透明，照样会报。"""
    edited = tmp_path / "edited.py"
    edited.write_text("one")
    visible: list[ToolResultFacts] = []
    _read(visible, edited)
    before = FileVersion.of(edited)
    attribution.record(_SELF, _modified(edited, before, _rewrite(edited, "two")))
    assert _notice(visible) is None


def test_writer_names_the_other_session(tmp_path, attribution) -> None:
    path = tmp_path / "a.py"
    path.write_text("one")
    visible: list[ToolResultFacts] = []
    _read(visible, path)
    before = FileVersion.of(path)
    attribution.record(_OTHER, _modified(path, before, _rewrite(path, "two")))
    notice = _notice(visible)
    assert notice is not None and f"by session {_OTHER}" in notice.content


def test_confirmed_own_mutation_advances_the_baseline(tmp_path, attribution) -> None:
    path = tmp_path / "a.py"
    path.write_text("one")
    visible: list[ToolResultFacts] = []
    _read(visible, path)
    before = FileVersion.of(path)
    after = _rewrite(path, "two")
    visible.append(_modified(path, before, after).as_result())
    assert FileKnowledge.from_facts(visible).latest_reads() == {str(path): after}
    assert _notice(visible) is None


def test_truncated_results_do_not_count_as_reads(tmp_path, attribution) -> None:
    """core 只记「正文被截断」，读取证据作废由文件系统按这条事实判断。"""
    path = tmp_path / "a.py"
    path.write_text("one")
    visible: list[ToolResultFacts] = []
    _read(visible, path)
    visible[0] = visible[0].model_copy(update={"content_truncated": True})
    knowledge = FileKnowledge.from_facts(visible)
    assert knowledge.latest_reads() == {}
    assert not knowledge.has_read(FileVersion.of(path))


def test_opaque_payload_round_trips_through_context_metadata(tmp_path) -> None:
    """core 把文件事实当 JSON 存放：落库再读回后，文件系统折叠出同样的已读证据。"""
    path = tmp_path / "a.py"
    path.write_text("one")
    visible: list[ToolResultFacts] = []
    _read(visible, path)
    stored = [ToolResultFacts.model_validate_json(facts.model_dump_json()) for facts in visible]
    assert FileKnowledge.from_facts(stored).latest_reads() == FileKnowledge.from_facts(
        visible
    ).latest_reads()


def test_attribution_is_bounded(tmp_path) -> None:
    registry = WriteAttribution()
    registry._LIMIT = 2
    versions = []
    for index in range(3):
        path = tmp_path / f"{index}.py"
        path.write_text("x")
        version = FileVersion.of(path)
        versions.append(version)
        registry.record(_OTHER, _modified(path, None, version))
    assert registry.writer_of(Path("unused"), versions[0]) is None
    writer = registry.writer_of(Path("unused"), versions[2])
    assert writer is not None and writer.session_id == _OTHER


def test_own_command_changes_are_not_reported_again(tmp_path, attribution) -> None:
    """命令结果里已当场告知：本会话命令改动、删除的已读文件不再报成别人改的。"""
    edited, removed = tmp_path / "edited.py", tmp_path / "removed.py"
    edited.write_text("one")
    removed.write_text("one")
    visible: list[ToolResultFacts] = []
    _read(visible, edited)
    _read(visible, removed)
    _rewrite(edited, "two")
    removed.unlink()
    attribution.record_command(_SELF, FileKnowledge.from_facts(visible).stale())
    assert _notice(visible) is None


def test_other_session_sees_a_command_as_the_likely_writer(tmp_path, attribution) -> None:
    path = tmp_path / "a.py"
    path.write_text("one")
    visible: list[ToolResultFacts] = []
    _read(visible, path)
    _rewrite(path, "two")
    attribution.record_command(_OTHER, FileKnowledge.from_facts(visible).stale())
    notice = _notice(visible)
    assert notice is not None and f"likely by a command in session {_OTHER}" in notice.content


def test_unknown_writer_mentions_own_running_background_commands(
    tmp_path, attribution, monkeypatch
) -> None:
    path = tmp_path / "a.py"
    path.write_text("one")
    visible: list[ToolResultFacts] = []
    _read(visible, path)
    _rewrite(path, "two")
    monkeypatch.setattr(notice_module.command_results, "pending", lambda _session: ["build"])
    notice = _notice(visible)
    assert notice is not None
    assert "one of your commands still running in the background" in notice.content


def test_notice_entry_carries_the_baseline_into_context(tmp_path, attribution) -> None:
    path = tmp_path / "a.py"
    path.write_text("one")
    visible: list[ToolResultFacts] = []
    _read(visible, path)
    _rewrite(path, "two")
    notice = _notice(visible)
    assert notice is not None
    _, metadata = notices_to_context([notice])
    # 基准随通知行落库：下一次对账从上下文里最近一条同类通知取它。
    assert metadata.notices[0].stale_reads == notice.stale_reads
    assert _notice(visible, metadata.notices[0].stale_reads) is None
