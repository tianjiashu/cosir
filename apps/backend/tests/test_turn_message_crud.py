"""TurnMessageCrud 逐条增量落库与跨 turn 隔离测试。

覆盖本次「一条消息持久化一次」改造新增的 ``append_message`` / ``clear_turn_messages``：
- 单条追加不覆盖同 turn 其它行；
- 按 ``turn_id`` 隔离，历史 turn 数据不受本轮清写影响；
- 与既有 ``load_messages`` 集成验证跨轮拼装数据源完整。

隔离策略：每个用例用 ``tmp_path`` 独立主库并显式 ``reset_service_dependencies``
清理 service 层单例缓存，避免跨用例引擎 / session 工厂残留污染。
"""

from __future__ import annotations

import json

import pytest
import sqlalchemy

from app.config.settings import Settings
from app.models.runtime_message import RuntimeMessage
from app.service.depends import reset_service_dependencies
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


def _msg(role: str, text: str, **meta) -> RuntimeMessage:
    return RuntimeMessage(role=role, content_text=text, metadata=dict(meta))


def _seed_turn(turn_id: str) -> None:
    """用裸 SQL 插入最小 workspace→task→turn 引用行，仅满足外键约束（测试夹具）。"""

    with main_engine().begin() as session:
        session.execute(
            sqlalchemy.text(
                "INSERT OR IGNORE INTO workspaces(workspace_id, name, root_path, "
                "created_at, updated_at) "
                "VALUES ('ws-1', 'ws', 'x', '2020-01-01T00:00:00Z', '2020-01-01T00:00:00Z')"
            )
        )
        session.execute(
            sqlalchemy.text(
                "INSERT OR IGNORE INTO tasks(task_id, workspace_id, agent_id, input_text, "
                "title, last_message_preview, status, created_at, updated_at) "
                "VALUES ('task-1', 'ws-1', 'developer', 'i', 't', 'p', 'active', "
                "'2020-01-01T00:00:00Z', '2020-01-01T00:00:00Z')"
            )
        )
        session.execute(
            sqlalchemy.text(
                "INSERT OR IGNORE INTO turns(turn_id, task_id, input_text, status, "
                "created_at, updated_at) "
                "VALUES (:tid, 'task-1', 'i', 'pending', "
                "'2020-01-01T00:00:00Z', '2020-01-01T00:00:00Z')"
            ),
            {"tid": turn_id},
        )


def test_append_message_does_not_overwrite_sibling_rows(tmp_path):
    """单条追加只插入一行，不清空同 turn 已有消息。"""

    _setup_storage(tmp_path)
    try:
        turn_id = "turn-A"
        _seed_turn(turn_id)
        crud = TurnMessageCrud()

        crud.append_message(turn_id, _msg("user", "hello"), sequence=0)
        crud.append_message(turn_id, _msg("assistant", "hi"), sequence=1)

        loaded = crud.load_messages(turn_id)
        assert len(loaded) == 2
        assert loaded[0].role == "user" and loaded[0].content_text == "hello"
        assert loaded[1].role == "assistant" and loaded[1].content_text == "hi"
    finally:
        _teardown_storage()


def test_clear_turn_messages_is_scoped_by_turn_id(tmp_path):
    """clear_turn_messages 只删目标 turn，不影响历史 turn 的跨轮记忆。"""

    _setup_storage(tmp_path)
    try:
        crud = TurnMessageCrud()
        _seed_turn("turn-prev")
        _seed_turn("turn-cur")

        # 历史 turn 已落库（模拟上一轮跨轮记忆）
        crud.append_message("turn-prev", _msg("user", "prior"), sequence=0)
        # 本轮先落两条
        crud.append_message("turn-cur", _msg("user", "current"), sequence=0)
        crud.append_message("turn-cur", _msg("assistant", "reply"), sequence=1)

        # 本轮重跑：清掉本轮残留，历史 turn 应原样保留
        crud.clear_turn_messages("turn-cur")
        crud.append_message("turn-cur", _msg("user", "current-retried"), sequence=0)

        assert crud.load_messages("turn-prev") == [
            _msg("user", "prior")
        ]
        cur = crud.load_messages("turn-cur")
        assert len(cur) == 1
        assert cur[0].content_text == "current-retried"
    finally:
        _teardown_storage()


def test_metadata_json_roundtrip(tmp_path):
    """metadata 经 JSON 落库后能原样读回;守护 dict[str, Any] 契约(tool_calls 存为字符串)。"""

    _setup_storage(tmp_path)
    try:
        _seed_turn("turn-M")
        crud = TurnMessageCrud()
        # 真实契约：tool_calls 以 JSON 字符串存入 metadata（非 list），与读取端 json.loads 对齐
        msg = _msg("assistant", "plan", tool_calls=json.dumps([{"name": "x", "id": "1"}]))
        crud.append_message("turn-M", msg, sequence=0)

        loaded = crud.load_messages("turn-M")
        raw = loaded[0].metadata.get("tool_calls")
        assert isinstance(raw, str)  # 落库形态为字符串
        assert json.loads(raw) == [{"name": "x", "id": "1"}]  # 读回可被解析还原
    finally:
        _teardown_storage()


def test_append_message_raises_on_missing_turn_foreign_key(tmp_path):
    """target turn 不存在时 append 因外键约束失败，错误不被静默吞掉。"""

    _setup_storage(tmp_path)
    try:
        crud = TurnMessageCrud()
        # 不 seed turn，直接追加应触发外键约束 IntegrityError
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            crud.append_message(
                "turn-ghost", _msg("user", "orphan"), sequence=0
            )
    finally:
        _teardown_storage()
