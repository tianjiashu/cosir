"""``runtime_events`` 表的纯 CRUD。

仅负责单表读写与 model↔dict 转换，不承担运行时编排；所有操作通过共享的主库
session 工厂访问数据库。

参数:
    无。

异常:
    无。

副作用:
    对 ``runtime_events`` 表执行增查操作。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.storage.model.runtime_event_model import RuntimeEventModel
from app.storage.store_engines import main_session_factory


class RuntimeEventCrud:
    """``runtime_events`` 表的纯 CRUD。

    参数:
        无。

    返回:
        无。

    异常:
        无。

    副作用:
        通过主库 session 执行 runtime_events 的增查操作。
    """

    def save_event(self, event_dict: dict[str, Any]) -> None:
        """写入一条运行时事件到持久化存储。

        参数:
            event_dict: 已序列化的事件字典（来自 ``RuntimeEvent.to_dict()``），
                必须包含 ``event_id``、``event_type``、``task_id``、``turn_id``、
                ``sequence``、``payload``、``created_at`` 字段。

        返回:
            无。

        异常:
            无。写入失败仅记日志，不向上抛出（事件持久化失败不应中断运行流）。

        副作用:
            向 ``runtime_events`` 表插入一行。
        """

        from app.config.logging.logger import log

        try:
            payload_json = __import__("json").dumps(
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
            with main_session_factory()() as session:
                session.add(model)
                session.commit()
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

        import json

        from app.config.logging.logger import log

        try:
            with main_session_factory()() as session:
                stmt = (
                    select(RuntimeEventModel)
                    .where(RuntimeEventModel.turn_id == turn_id)
                    .order_by(RuntimeEventModel.sequence)
                )
                rows = session.execute(stmt).scalars().all()
                result = []
                for row in rows:
                    payload = json.loads(row.payload_json) if row.payload_json else {}
                    result.append(
                        {
                            "event_id": row.event_id,
                            "event_type": row.event_type,
                            "task_id": row.task_id,
                            "turn_id": row.turn_id,
                            "sequence": row.sequence,
                            "payload": payload,
                            "created_at": row.created_at,
                        }
                    )
                return result
        except Exception:
            log.exception(
                "runtime_event_query_failed",
                extra={
                    "msg": "failed to query runtime events",
                    "data": {"turn_id": turn_id},
                },
            )
            return []

    def delete_by_turn_ids(self, turn_ids: list[str]) -> None:
        """按轮次标识批量删除运行时事件（用于任务 / 工作区级联删除）。

        参数:
            turn_ids: 待清理事件的轮次标识列表；为空时不执行任何操作。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果删除失败。

        副作用:
            turn_ids 非空时从 ``runtime_events`` 表删除匹配的行。
        """

        from sqlalchemy import delete

        if not turn_ids:
            return
        with main_session_factory().begin() as session:
            session.execute(
                delete(RuntimeEventModel).where(RuntimeEventModel.turn_id.in_(turn_ids))
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

        import json

        from app.config.logging.logger import log

        try:
            with main_session_factory()() as session:
                stmt = (
                    select(RuntimeEventModel)
                    .where(RuntimeEventModel.task_id == task_id)
                    .order_by(RuntimeEventModel.turn_id, RuntimeEventModel.sequence)
                )
                rows = session.execute(stmt).scalars().all()
                result = []
                for row in rows:
                    payload = json.loads(row.payload_json) if row.payload_json else {}
                    result.append(
                        {
                            "event_id": row.event_id,
                            "event_type": row.event_type,
                            "task_id": row.task_id,
                            "turn_id": row.turn_id,
                            "sequence": row.sequence,
                            "payload": payload,
                            "created_at": row.created_at,
                        }
                    )
                return result
        except Exception:
            log.exception(
                "runtime_event_query_by_task_failed",
                extra={
                    "msg": "failed to query runtime events by task",
                    "data": {"task_id": task_id},
                },
            )
            return []
