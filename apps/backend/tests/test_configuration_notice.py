"""配置对账：Agent 看到的配置与当前配置不同时投净差异通知，基准随通知推进。

覆盖：没变化不说；文档只发 diff（diff 不比全文短时发全文）；改回去不说；不适用的来源不比较；新来源告知一次；
目录只报增删改（含从「不可用」恢复）；Skill 更新提醒重新加载；基准取上下文里最近一条通知（没有就取系统行），
撤回移出通知后回退；工作区指令文件每次驱动定位一次；投影缓存按文件签名重读。每次请求前的接线见
``request_notices``；节点其余判定的单测用替身上下文并关掉对账。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from app.core.agents.define_agents import main_agent
from app.core.context.context_entry import ContextEntry
from app.core.context.prompt_sources.cache import ProjectionCache
from app.core.context.prompt_sources.registry import DEFERRED_TOOLS, SKILLS
from app.core.context.prompt_sources.source import ItemDiff, SourceItem, text_change
from app.core.context.prompt_sources.sources.deferred_tools import UNAVAILABLE_KEY
from app.core.context.runtime_message_context_manager import RuntimeMessageContextManager
from app.core.tool.schemas.tool_names import TOOL_SEARCH
from app.core.workflow.message_queue.message_bodies.notice import NoticeBody, notices_to_context
from app.models.message_context.message_metadata.system_message_metadata import (
    PromptSourceBaseline,
    PromptSourceEntry,
    SystemMessageMetadata,
)
from app.utils.file_signature import stat_signature
from app.utils.path import system_cosir as paths
from app.utils.workspace_instruction import find_workspace_instruction_file


@pytest.fixture(autouse=True)
def redirect_system_cosir(tmp_path: Path):
    """系统级 ``.cosir`` 指向临时目录：全局 AGENTS.md 与 MCP 配置都从这里读。"""
    paths.override(DATA_DIR=tmp_path / "data")
    yield
    paths.reset()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    return root


_AGENT = main_agent(system_prompt="Main preset.")


def _manager(
    workspace: Path, baseline: PromptSourceBaseline, tool_names: tuple[str, ...] = ()
) -> RuntimeMessageContextManager:
    """只装配对账要用的字段；基准放在系统行上，工作区指令文件按「驱动开始时」定位一次。"""
    manager = object.__new__(RuntimeMessageContextManager)
    manager.workspace = SimpleNamespace(root_path=str(workspace))
    manager.tool_names = tool_names
    manager._entries = []
    manager._system_entry = ContextEntry(
        {"role": "system", "content": "system"},
        None,
        0,
        metadata=SystemMessageMetadata(prompt_sources=baseline),
        turn=0,
    )
    manager._workspace_instruction = find_workspace_instruction_file(workspace)
    return manager


def configuration_change_notice(
    workspace: Path, baseline: PromptSourceBaseline, tool_names: tuple[str, ...] = ()
) -> NoticeBody | None:
    return _manager(workspace, baseline, tool_names).configuration_change_notice(_AGENT)


def _write_agents_md(workspace: Path, text: str) -> None:
    (workspace / "AGENTS.md").write_text(text, encoding="utf-8")


def _baseline_now(workspace: Path, tool_names: tuple[str, ...] = ()) -> PromptSourceBaseline:
    """对一个「什么都没看到」的基准对账一次，取告知后的基准，作为「Agent 已看到当前配置」。"""
    notice = configuration_change_notice(workspace, {}, tool_names)
    assert notice is not None and notice.prompt_sources is not None
    return notice.prompt_sources


_LONG_RULES = "\n".join(f"rule {index}: keep this" for index in range(40))


def test_no_change_means_no_notice(workspace: Path) -> None:
    _write_agents_md(workspace, "rule v1")
    baseline = _baseline_now(workspace)

    assert configuration_change_notice(workspace, baseline) is None


def test_changed_workspace_rules_are_sent_as_a_diff(workspace: Path) -> None:
    """文档只发 diff：没改的行不重复，Agent 结合已看到的旧版本还原新版本。"""

    _write_agents_md(workspace, _LONG_RULES)
    baseline = _baseline_now(workspace)
    _write_agents_md(workspace, _LONG_RULES.replace("rule 20: keep this", "rule 20: changed"))

    notice = configuration_change_notice(workspace, baseline)

    assert notice is not None
    assert notice.notice_kind == "configuration_changed"
    assert "workspace AGENTS.md (<workspace_layer>) changed" in notice.content
    assert "-rule 20: keep this" in notice.content
    assert "+rule 20: changed" in notice.content
    # diff 只带上下文两行，远处没改的行不出现。
    assert "rule 1: keep this" not in notice.content
    # 只说变了的来源：语言、预设、全局指令没变，不出现。
    assert "language" not in notice.content
    assert "agent preset" not in notice.content
    # 告知后的基准对上当前配置：下一步不再重复。
    assert notice.prompt_sources is not None
    assert configuration_change_notice(workspace, notice.prompt_sources) is None


def test_text_change_falls_back_to_full_text_when_shorter() -> None:
    assert text_change("a", "b") == "It now reads:\nb"
    assert text_change("", "new").startswith("It now reads:")


def test_switching_to_another_agents_md_names_both_paths(workspace: Path) -> None:
    (workspace / "pkg").mkdir()
    (workspace / "pkg" / "AGENTS.md").write_text(_LONG_RULES, encoding="utf-8")
    baseline = _baseline_now(workspace)
    _write_agents_md(workspace, _LONG_RULES + "\nroot only")

    notice = configuration_change_notice(workspace, baseline)

    assert notice is not None
    assert "now loaded from ./AGENTS.md (was ./pkg/AGENTS.md)" in notice.content
    assert "+root only" in notice.content


def test_changed_and_reverted_says_nothing(workspace: Path) -> None:
    """只比净差异：在两次对账之间改了又改回去，什么都不说。"""

    _write_agents_md(workspace, "rule v1")
    baseline = _baseline_now(workspace)
    _write_agents_md(workspace, "rule version 2")
    _write_agents_md(workspace, "rule v1")

    assert configuration_change_notice(workspace, baseline) is None


def test_removed_workspace_rules_are_announced(workspace: Path) -> None:
    _write_agents_md(workspace, "rule v1")
    baseline = _baseline_now(workspace)
    (workspace / "AGENTS.md").unlink()

    notice = configuration_change_notice(workspace, baseline)

    assert notice is not None
    assert "workspace AGENTS.md (<workspace_layer>) is gone" in notice.content


def test_agents_md_location_is_fixed_within_a_drive(workspace: Path) -> None:
    """定位每次驱动做一次：驱动中途新建的 AGENTS.md 等下一次驱动；已定位文件的内容改动下一步就生效。"""

    _write_agents_md(workspace, "rule v1")
    baseline = _baseline_now(workspace)
    manager = _manager(workspace, baseline)
    (workspace / "AGENTS.md").unlink()
    (workspace / "agents.md").write_text("rule v1", encoding="utf-8")
    # 大小写不敏感的文件系统上两者是同一个文件；只在区分大小写时验证「沿用定位」。
    if not (workspace / "AGENTS.md").exists():
        assert manager.configuration_change_notice(_AGENT) is not None  # 定位到的文件没了
    _write_agents_md(workspace, "rule version 2")
    notice = manager.configuration_change_notice(_AGENT)
    assert notice is not None and "rule version 2" in notice.content


def test_sources_outside_the_session_are_not_compared(workspace: Path) -> None:
    """没有 tool_search 的 Session 不比较延迟工具目录：基准里的旧条目也不会被说成「已删除」。"""

    baseline = _baseline_now(workspace)
    stale = {
        **baseline,
        "deferred_tools": {"mcp__old__x": PromptSourceEntry(fingerprint="fp", text="mcp__old__x")},
    }

    assert configuration_change_notice(workspace, stale) is None


def test_a_source_missing_from_the_baseline_is_told_once(workspace: Path) -> None:
    """基准里没有的来源（例如后来才登记的）按「之前什么都没看到」比较，把现有内容告知一次。"""

    baseline = _baseline_now(workspace)
    without_language = {key: value for key, value in baseline.items() if key != "reply_language"}

    notice = configuration_change_notice(workspace, without_language)

    assert notice is not None
    assert "language setting changed" in notice.content


def test_deferred_catalog_change_lists_only_what_changed(workspace: Path) -> None:
    baseline = _baseline_now(workspace, (TOOL_SEARCH,))
    announced = {
        **baseline,
        "deferred_tools": {"mcp__old__x": PromptSourceEntry(fingerprint="fp", text="mcp__old__x")},
    }

    notice = configuration_change_notice(workspace, announced, (TOOL_SEARCH,))

    assert notice is not None
    assert "no longer available. Do not search for them" in notice.content
    assert "mcp__old__x\n\nThis is ambient context" in notice.content
    assert "now available" not in notice.content


def test_catalog_readable_again_but_empty_says_so() -> None:
    """之前看到「目录不可用」，现在修好了但没有条目：要说清目录现在为空，不能只说「变了」。"""

    previous = {UNAVAILABLE_KEY: PromptSourceEntry(fingerprint="x", text="unavailable")}

    deferred = DEFERRED_TOOLS.describe(ItemDiff.between(previous, {}), {}, previous)
    skills = SKILLS.describe(ItemDiff.between(previous, {}), {}, previous)

    assert "readable again; no new deferred tools are available." in deferred
    assert "readable again; no Skills are available." in skills


def test_updated_skill_asks_to_reload_and_shows_new_description() -> None:
    old = PromptSourceEntry(fingerprint="old", text="- review: d1")
    items = {"review": SourceItem("new", "- review: d2")}

    text = SKILLS.describe(ItemDiff.between({"review": old}, items), items, {"review": old})

    assert "Description changed:\n    - review: d2" in text
    assert "load it again" in text


def test_notice_entry_carries_the_baseline_into_context(workspace: Path) -> None:
    """通知合成进 context 时带上基准：下一次对账（以及重启、分叉之后）从上下文里读到它。"""

    _write_agents_md(workspace, "rule v1")
    notice = configuration_change_notice(workspace, {})
    assert notice is not None

    _message, metadata = notices_to_context([notice])

    assert metadata.notices[0].prompt_sources == notice.prompt_sources


def _entry(text: str) -> PromptSourceEntry:
    return PromptSourceEntry(fingerprint=text, text=text)


def _notice_entry(baseline: PromptSourceBaseline | None, sequence: int) -> ContextEntry:
    body = NoticeBody(
        content="x",
        notice_kind="configuration_changed" if baseline is not None else "tool_availability",
        prompt_sources=baseline,
        banned_tools=None if baseline is not None else [],
    )
    message, metadata = notices_to_context([body])
    return ContextEntry(message, 1, sequence, metadata=metadata, turn=1)


def test_baseline_falls_back_to_the_system_prompt_then_follows_notices(workspace: Path) -> None:
    system = {"reply_language": {"language": _entry("a")}}
    later = {"reply_language": {"language": _entry("b")}}
    manager = _manager(workspace, system)
    manager._entries = [_notice_entry(None, 1)]

    # 上下文里没有配置变化通知：基准是系统提示词生成时的。
    assert manager.prompt_source_baseline == system

    manager._entries.append(_notice_entry(later, 2))
    assert manager.prompt_source_baseline == later


def test_cache_reloads_only_when_the_signature_changes(tmp_path: Path) -> None:
    cache = ProjectionCache()
    target = tmp_path / "AGENTS.md"
    target.write_text("v1", encoding="utf-8")
    loads: list[int] = []

    def load() -> dict[str, SourceItem]:
        loads.append(1)
        return {}

    cache.get("rules", "ws", stat_signature([target]), load)
    cache.get("rules", "ws", stat_signature([target]), load)
    assert len(loads) == 1
    target.write_text("version 2", encoding="utf-8")
    cache.get("rules", "ws", stat_signature([target]), load)
    assert len(loads) == 2


def test_cache_drops_a_deleted_workspace(tmp_path: Path) -> None:
    cache = ProjectionCache()
    loads: list[int] = []
    cache.get("rules", str(tmp_path), "sig", lambda: loads.append(1) or {})

    cache.drop_scope(str(tmp_path))
    cache.get("rules", str(tmp_path), "sig", lambda: loads.append(2) or {})

    assert loads == [1, 2]


def test_signature_marks_missing_paths(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"

    assert stat_signature([missing]) == ((str(missing), None, None),)


def test_skill_edit_is_seen_without_waiting(tmp_path: Path, workspace: Path, monkeypatch) -> None:
    """手动改了 SKILL.md：下一次对账就看到，不靠失效通知。"""

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    skill = workspace / ".agents" / "skills" / "review"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: review\ndescription: v1\n---\n", encoding="utf-8")
    baseline = _baseline_now(workspace, ("skill",))

    (skill / "SKILL.md").write_text(
        "---\nname: review\ndescription: version two\n---\n", encoding="utf-8"
    )
    notice = configuration_change_notice(workspace, baseline, ("skill",))

    assert notice is not None
    assert "version two" in notice.content
    assert "load it again" in notice.content


def _sqlite_manager(store, run) -> RuntimeMessageContextManager:
    context = RuntimeMessageContextManager(
        current_session_id=store.session.id,
        agent=_AGENT,
        tool_names=(),
        context_store=store.context,
        workspace=store.workspace,
    )
    context.begin_run(run)
    return context


def test_baseline_round_trips_and_withdrawal_reverts_it(canonical_store) -> None:
    """真实库往返：通知与本批其余通知合成一行写入后，新的上下文管理器读回告知后的基准、不再重复；
    撤回把这一行移出上下文后，基准回退到系统行，下一次对账重新告知。"""

    store = canonical_store
    workspace = Path(store.workspace.root_path)
    _write_agents_md(workspace, _LONG_RULES)
    run = store.runs.create(store.session.id, status="running")

    first = _sqlite_manager(store, run)
    assert first.configuration_change_notice(_AGENT) is None

    _write_agents_md(workspace, _LONG_RULES + "\nnew rule")
    notice = first.configuration_change_notice(_AGENT)
    assert notice is not None and "+new rule" in notice.content
    other = NoticeBody(content="tools", notice_kind="tool_availability", banned_tools=[])
    message, metadata = notices_to_context([other, notice])
    first.add_message(message, metadata=metadata, project_to_transport=False)
    row_id = max(row.id for row in store.contexts.get(store.session.id))
    assert first.prompt_source_baseline == notice.prompt_sources

    # 重启后（新的管理器从库重建）基准仍是告知后的：不再重复同一条变化。
    assert _sqlite_manager(store, run).configuration_change_notice(_AGENT) is None

    with store.factory.begin() as db:
        store.contexts.exclude_rows(store.session.id, frozenset({row_id}), db)
    again = _sqlite_manager(store, run).configuration_change_notice(_AGENT)
    assert again is not None and "+new rule" in again.content
