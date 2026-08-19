"""``runtime_events`` 表的纯 CRUD 数据访问层。

单一职责：只提供 ``runtime_events`` 单表的增查与 model↔dict 转换，不承担运行时编排。

职责边界：
- 负责：运行时事件单表写入与查询、``RuntimeEventModel``↔事件字典转换、turn 内 sequence
  原子分配。
- 不负责：事件发布 / 广播（由 ``RuntimeEventBus`` 负责）、事件构建与 payload 校验
  （由 ``RuntimeEvent`` 值对象负责）、运行时编排。

依赖约定：构造时通过 ``main_session_factory()`` 取得主库共享 session 工厂，必须在
``init_storage()`` 之后实例化；本类不创建、不释放引擎。
"""

import json
import threading
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from app.storage.model.runtime_event_model import RuntimeEventModel
from app.storage.store_engines import main_session_factory


class RuntimeEventCrud:
    """``runtime_events`` 表的纯 CRUD。

    仅负责单表增查与 model↔dict 转换，不承担运行时编排；所有方法通过共享的主库
    session 工厂访问数据库。
    """

    _sequence_lock = threading.Lock()
    _SEQUENCE_RETRY_LIMIT: int = 5

    def __init__(self) -> None:
        """绑定主库共享 session 工厂。

        参数:
            无。

        返回:
            无。

        异常:
            RuntimeError: 如果 ``init_storage`` 尚未调用（主库 session 工厂不可用）。

        副作用:
            无（仅复用已初始化的主库 session 工厂）。
        """
        self._session_factory = main_session_factory()

    def save_event(self, event_dict: dict[str, Any]) -> None:
        """写入一条运行时事件到持久化存储。

        参数:
            event_dict: 已序列化的事件字典（来自 ``RuntimeEvent.to_dict()``），
                必须包含 ``event_id``、``event_type``、``task_id``、``sequence``、
                ``payload``、``created_at`` 字段；``turn_id`` 键必含但值可为
                None（无 turn 归属的事件，sequence 默认 0）。

        返回:
            无。

        异常:
            不向上抛出。任何持久化异常均经 ``log.exception`` 记录后吞掉（事件持久化
            失败不应中断运行流）。

        副作用:
            向 ``runtime_events`` 表插入一行。
        """

        from app.config.logging.logger import log

        try:
            payload_json = json.dumps(
                event_dict.get("payload", {}), ensure_ascii=False, default=str
            )
            model = RuntimeEventModel(
                turn_id=event_dict["turn_id"],
                sequence=event_dict.get("sequence", 0),
                event_id=event_dict["event_id"],
                event_type=event_dict["event_type"],
                task_id=event_dict["task_id"],
                payload_json=payload_json,
                created_at=event_dict.get("created_at", ""),
            )
            with self._session_factory.begin() as session:
                session.add(model)
        except Exception:
            log.exception(
                "runtime_event_persist_failed",
                extra={
                    "msg": "failed to persist runtime event",
                    "data": {
                        "event_id": event_dict.get("event_id"),
                        "event_type": event_dict.get("event_type"),
                    },
                },
            )

    def save_event_with_next_sequence(self, event_dict: dict[str, Any]) -> int:
        """在同一事务内原子分配并写入下一个 turn 内运行时事件序号。

        参数:
            event_dict: 已序列化的事件字典；``turn_id`` 为 None 或空串时降级走
                ``save_event``（不分配 turn 内 sequence）。

        返回:
            实际写入的 sequence。

        异常:
            RuntimeError: 如果分配 sequence 或写入事件失败。

        副作用:
            在进程级锁保护下向 ``runtime_events`` 表插入一行。
        """

        from app.config.logging.logger import log

        turn_id = event_dict.get("turn_id")
        if not isinstance(turn_id, str) or not turn_id:
            self.save_event(event_dict)
            sequence = event_dict.get("sequence", 0)
            if not isinstance(sequence, int):
                raise RuntimeError("runtime event sequence must be an integer")
            return sequence

        for attempt in range(self._SEQUENCE_RETRY_LIMIT):
            with self._sequence_lock:
                try:
                    with self._session_factory.begin() as session:
                        value = session.execute(
                            select(func.max(RuntimeEventModel.sequence)).where(
                                RuntimeEventModel.turn_id == turn_id
                            )
                        ).scalar_one()
                        sequence = 0 if value is None else int(value) + 1
                        payload_json = json.dumps(
                            event_dict.get("payload", {}), ensure_ascii=False, default=str
                        )
                        session.add(
                            RuntimeEventModel(
                                turn_id=turn_id,
                                sequence=sequence,
                                event_id=event_dict["event_id"],
                                event_type=event_dict["event_type"],
                                task_id=event_dict["task_id"],
                                payload_json=payload_json,
                                created_at=event_dict.get("created_at", ""),
                            )
                        )
                        return sequence
                except IntegrityError:
                    log.warning(
                        "runtime_event_sequence_conflict",
                        extra={
                            "msg": "runtime event sequence conflict, retrying",
                            "data": {
                                "event_id": event_dict.get("event_id"),
                                "event_type": event_dict.get("event_type"),
                                "turn_id": turn_id,
                                "attempt": attempt + 1,
                            },
                        },
                    )
                    continue
                except Exception as exc:
                    log.exception(
                        "runtime_event_persist_with_sequence_failed",
                        extra={
                            "msg": "failed to persist runtime event with next sequence",
                            "data": {
                                "event_id": event_dict.get("event_id"),
                                "event_type": event_dict.get("event_type"),
                                "turn_id": turn_id,
                            },
                        },
                    )
                    raise RuntimeError(
                        "failed to persist runtime event with next sequence"
                    ) from exc
        log.error(
            "runtime_event_sequence_retry_exhausted",
            extra={
                "msg": "runtime event sequence retry exhausted",
                "data": {
                    "event_id": event_dict.get("event_id"),
                    "event_type": event_dict.get("event_type"),
                    "turn_id": turn_id,
                    "retry_limit": self._SEQUENCE_RETRY_LIMIT,
                },
            },
        )
        raise RuntimeError("runtime event sequence retry exhausted")

    def list_by_turn(self, turn_id: str) -> list[dict[str, Any]]:
        """按 turn_id 查询所有已持久化的运行时事件（按 sequence 升序）。

        参数:
            turn_id: 轮次标识符。

        返回:
            事件字典列表（可直接反序列化为前端 ``RuntimeEvent`` 格式），
            每项含 ``event_id``、``event_type``、``task_id``、``turn_id``、
            ``sequence``、``payload``（已从 JSON 解码）、``created_at``。
            无记录时返回空列表。

        异常:
            无。查询失败返回空列表并记日志。

        副作用:
            无（只读查询）。
        """

        from app.config.logging.logger import log

        try:
            with self._session_factory() as session:
                stmt = (
                    select(RuntimeEventModel)
                    .where(RuntimeEventModel.turn_id == turn_id)
                    .order_by(RuntimeEventModel.sequence)
                )
                rows = session.execute(stmt).scalars().all()
                return [_event_dict_from_model(row) for row in rows]
        except Exception:
            log.exception(
                "runtime_event_query_failed",
                extra={
                    "msg": "failed to query runtime events",
                    "data": {"turn_id": turn_id},
                },
            )
            return []

    def delete_by_ids(self, ids: list[str]) -> None:
        """按轮次标识批量删除运行时事件（用于任务 / 工作区级联删除）。

        参数:
            ids: 待清理事件的轮次标识列表；为空时不执行任何操作。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果删除失败。

        副作用:
            ids 为空时直接返回；对应行不存在时静默无操作。
        """

        if not ids:
            return
        with self._session_factory.begin() as session:
            session.execute(
                delete(RuntimeEventModel).where(RuntimeEventModel.turn_id.in_(ids))
            )

    def list_by_task(self, task_id: str) -> list[dict[str, Any]]:
        """按 task_id 查询所有已持久化的运行时事件（按 turn + sequence 升序）。

        用于打开任务时一次性加载该任务下所有 turn 的完整事件历史，
        重建含思考 / 工具调用 / 状态变更的 timeline。

        参数:
            task_id: 任务标识符。

        返回:
            事件字典列表（同 ``list_by_turn`` 的格式），跨 turn 按
            ``(turn_id, sequence)`` 排列。无记录时返回空列表。

        异常:
            无。查询失败返回空列表并记日志。

        副作用:
            无（只读查询）。
        """

        from app.config.logging.logger import log

        try:
            with self._session_factory() as session:
                stmt = (
                    select(RuntimeEventModel)
                    .where(RuntimeEventModel.task_id == task_id)
                    .order_by(RuntimeEventModel.turn_id, RuntimeEventModel.sequence)
                )
                rows = session.execute(stmt).scalars().all()
                return [_event_dict_from_model(row) for row in rows]
        except Exception:
            log.exception(
                "runtime_event_query_by_task_failed",
                extra={
                    "msg": "failed to query runtime events by task",
                    "data": {"task_id": task_id},
                },
            )
            return []


def _event_dict_from_model(row: RuntimeEventModel) -> dict[str, Any]:
    """把 ``RuntimeEventModel`` ORM 行转换为事件字典（供 ``list_by_turn`` / ``list_by_task`` 复用）。

    ``payload_json`` 反序列化为 dict；为空时回退为空字典。

    参数:
        row: 查询得到的 ``RuntimeEventModel`` 行。

    返回:
        与前端 ``RuntimeEvent`` 格式对齐的事件字典。

    异常:
        json.JSONDecodeError: 如果 ``payload_json`` 不是合法 JSON。

    副作用:
        无。
    """
    payload = json.loads(row.payload_json) if row.payload_json else {}
    return {
        "event_id": row.event_id,
        "event_type": row.event_type,
        "task_id": row.task_id,
        "turn_id": row.turn_id,
        "sequence": row.sequence,
        "payload": payload,
        "created_at": row.created_at,
    }
