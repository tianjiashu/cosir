"""RuntimeOperations 逐条落库门面测试。

验证本次「一条消息持久化一次」改造在编排层门面的职责收口：
- ``append_runtime_message`` 自动维护 turn 内自增序号，无需调用方传 sequence；
- ``reset_message_sequence`` 清掉本轮残留并将序号归零（崩溃重跑幂等）；
- 落库经由构造注入的 ``TurnMessageCrud`` 收口，节点不直接接触存储层；
- 端到端：``_ai_to_runtime_message`` → ``append`` → ``load`` → ``runtime_to_langchain``
  的 tool_calls 往返完整（修复孤立 ToolMessage 协议错误的核心防线）。

隔离策略：每个用例用 ``tmp_path`` 独立主库；拆卸时必须 ``reset_service_dependencies``
清掉 ``get_turn_message_crud`` 的 lru_cache，否则带旧引擎 session 工厂的 CRUD 单例会
跨用例污染（旧实例指向旧库，写入时外键约束失败）。
"""

from __future__ import annotations

from types import SimpleNamespace

import sqlalchemy

from app.config.settings import Settings
from app.core.llm.langchain_bridge import runtime_to_langchain
from app.core.runtime.runtime_operations import RuntimeOperations
from app.models.runtime_message import RuntimeMessage
from app.service.depends import reset_service_dependencies
from app.service.task.turn_service import TurnService
from app.storage.crud.turn_message_crud import TurnMessageCrud
from app.storage.store_engines import close_storage, init_storage, main_engine


def _setup_storage(tmp_path) -> None:
    """用临时目录初始化主库（tmp_path 已是唯一路径，天然隔离）。"""

    Settings.override(
        DATABASE_FILE=tmp_path / "app.db",
        LOG_DATABASE_FILE=tmp_path / "log.db",
        CHECKPOINT_FILE=tmp_path / "checkpoint.db",
    )
    init_storage()


def _teardown_storage() -> None:
    """释放引擎并清空 service 层缓存单例，避免跨用例污染。"""

    close_storage()
    reset_service_dependencies()
    Settings.override(DATABASE_FILE=None, LOG_DATABASE_FILE=None, CHECKPOINT_FILE=None)


def _seed_turn(turn_id: str) -> None:
    """用裸 SQL 插入最小 workspace→task→turn 引用行，仅满足外键约束（测试夹具）。"""

    with main_engine().begin() as conn:
        conn.execute(
            sqlalchemy.text(
                "INSERT OR IGNORE INTO workspaces(workspace_id, name, root_path, "
                "created_at, updated_at) "
                "VALUES ('ws-1', 'ws', 'x', '2020-01-01T00:00:00Z', '2020-01-01T00:00:00Z')"
            )
        )
        conn.execute(
            sqlalchemy.text(
                "INSERT OR IGNORE INTO tasks(task_id, workspace_id, agent_id, input_text, "
                "title, last_message_preview, status, created_at, updated_at) "
                "VALUES ('task-1', 'ws-1', 'developer', 'i', 't', 'p', 'active', "
                "'2020-01-01T00:00:00Z', '2020-01-01T00:00:00Z')"
            )
        )
        conn.execute(
            sqlalchemy.text(
                "INSERT OR IGNORE INTO turns(turn_id, task_id, input_text, status, "
                "created_at, updated_at) "
                "VALUES (:tid, 'task-1', 'i', 'pending', "
                "'2020-01-01T00:00:00Z', '2020-01-01T00:00:00Z')"
            ),
            {"tid": turn_id},
        )


def _make_operations(turn_id: str) -> RuntimeOperations:
    """轻量构造：注入真实 ``TurnService`` 门面，仅绕开 ``ToolExecutionService`` 重依赖。

    不绕过落库链路本身——``_turn_store``（``TurnService`` 经其消息门面落库）与
    ``_current_turn`` / ``_message_sequence`` 均为生产同款字段，端到端验证
    append → TurnService 门面 → 存储层 → load。
    """

    ops = RuntimeOperations.__new__(RuntimeOperations)
    ops._current_turn = SimpleNamespace(turn_id=turn_id)
    ops._message_sequence = 0
    ops._turn_service = TurnService()
    return ops


def test_append_runtime_message_assigns_increasing_sequence(tmp_path):
    """逐条追加按 0、1、2 自增落库，不重复不跳号。"""

    _setup_storage(tmp_path)
    try:
        _seed_turn("turn-seq")
        ops = _make_operations("turn-seq")
        ops.append_runtime_message(RuntimeMessage(role="user", content_text="a"))
        ops.append_runtime_message(RuntimeMessage(role="assistant", content_text="b"))
        ops.append_runtime_message(RuntimeMessage(role="tool", content_text="c"))

        loaded = TurnMessageCrud().load_messages("turn-seq")
        assert [m.content_text for m in loaded] == ["a", "b", "c"]
    finally:
        _teardown_storage()


def test_reset_message_sequence_clears_turn_and_renumbers(tmp_path):
    """reset 清掉本轮残留并归零，后续追加从 0 重新编号。"""

    _setup_storage(tmp_path)
    try:
        _seed_turn("turn-reset")
        ops = _make_operations("turn-reset")
        ops.append_runtime_message(RuntimeMessage(role="user", content_text="old-1"))
        ops.append_runtime_message(RuntimeMessage(role="assistant", content_text="old-2"))

        ops.reset_message_sequence()
        ops.append_runtime_message(RuntimeMessage(role="user", content_text="new-1"))

        loaded = TurnMessageCrud().load_messages("turn-reset")
        assert len(loaded) == 1
        assert loaded[0].content_text == "new-1"
    finally:
        _teardown_storage()


def test_reset_message_sequence_is_idempotent(tmp_path):
    """连续两次 reset 仍只清空一次，不产生残留或异常（崩溃重跑幂等）。"""

    _setup_storage(tmp_path)
    try:
        _seed_turn("turn-idem")
        ops = _make_operations("turn-idem")
        ops.append_runtime_message(RuntimeMessage(role="user", content_text="x"))
        ops.reset_message_sequence()
        ops.reset_message_sequence()

        loaded = TurnMessageCrud().load_messages("turn-idem")
        assert loaded == []
        assert ops._message_sequence == 0
    finally:
        _teardown_storage()


def test_reset_message_sequence_noop_without_turn(tmp_path):
    """未绑定 current_turn 时 reset 是安全 no-op（仅记 warning，不抛错）。"""

    _setup_storage(tmp_path)
    try:
        ops = RuntimeOperations.__new__(RuntimeOperations)
        ops._current_turn = None
        ops._message_sequence = 3
        ops._turn_service = TurnService()
        ops.reset_message_sequence()

        assert ops._message_sequence == 3  # 未复位（无 turn 不动作）
    finally:
        _teardown_storage()


def test_append_is_noop_without_current_turn(tmp_path):
    """未绑定 current_turn 时不落库、不抛错（仅记 warning）。"""

    _setup_storage(tmp_path)
    try:
        ops = RuntimeOperations.__new__(RuntimeOperations)
        ops._current_turn = None
        ops._message_sequence = 0
        ops._turn_service = TurnService()
        ops.append_runtime_message(RuntimeMessage(role="user", content_text="x"))

        # 没有任何 turn 写入，load 任意 turn 都应空
        assert TurnMessageCrud().load_messages("any-turn") == []
    finally:
        _teardown_storage()


def test_end_to_end_tool_calls_roundtrip(tmp_path):
    """核心防线：assistant tool_calls 经落库 → 读回 → 重建 LangChain 消息后完整保留。

    若 ``_ai_to_runtime_message`` 把 tool_calls 存成 list 而非 JSON 字符串，读取端
    ``_tool_calls_from_metadata`` 的 ``json.loads`` 会失败并静默返回空，导致下一轮出现
    孤立 ToolMessage 触发 OpenAI 协议校验失败。本用例阻止该回归。
    """

    _setup_storage(tmp_path)
    try:
        from langchain_core.messages import AIMessage

        from app.core.workflows.nodes.model_node import _ai_to_runtime_message

        _seed_turn("turn-e2e")
        ops = _make_operations("turn-e2e")

        source = AIMessage(
            content="let me check",
            tool_calls=[{"name": "read_file", "args": {"path": "x"}, "id": "call-1"}],
        )
        runtime_msg = _ai_to_runtime_message(source)
        ops.append_runtime_message(runtime_msg)
        # 配对 tool 观察消息（含 tool_call_id 供 runtime_to_langchain 配对）
        ops.append_runtime_message(
            RuntimeMessage(
                role="tool",
                content_text="file content",
                metadata={"tool_call_id": "call-1"},
            )
        )

        loaded = TurnMessageCrud().load_messages("turn-e2e")
        restored = runtime_to_langchain(loaded)
        assistant = next(m for m in restored if getattr(m, "tool_calls", None))
        # LangChain 重建会附带 "type": "tool_call" 等协议字段，比对核心三字段即可
        assert assistant.tool_calls[0]["name"] == "read_file"
        assert assistant.tool_calls[0]["args"] == {"path": "x"}
        assert assistant.tool_calls[0]["id"] == "call-1"
    finally:
        _teardown_storage()
