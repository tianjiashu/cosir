"""工具调用和执行记录 CRUD。"""

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Dict, Optional
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.storage.database import create_session_factory
from app.storage.model.tool_execution import ToolCallModel, ToolExecutionModel
from app.storage.schema import initialize_app_schema
from app.tools.execution.records import ToolCallRecord, ToolExecutionRecord


class ToolExecutionStore:
    """读写工具调用和执行记录。"""

    def __init__(self, database_path: Path) -> None:
        """初始化工具执行仓储。

        参数:
            database_path: SQLite 数据库路径。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 如果 schema 初始化失败。

        副作用:
            初始化主库 schema。
        """

        self._engine = initialize_app_schema(database_path)
        self._session_factory = create_session_factory(self._engine)

    def create_tool_call(
        self,
        run_id: str,
        tool_name: str,
        arguments: Dict[str, Any],
        permission: str,
        status: str,
        idempotency_key: str,
        step_id: Optional[str] = None,
    ) -> ToolCallRecord:
        """创建幂等工具调用记录。"""

        existing = self.get_tool_call_by_key(idempotency_key)
        if existing is not None:
            return existing
        now = _utc_now()
        call = ToolCallRecord(str(uuid4()), run_id, step_id, tool_name, arguments, permission, status, idempotency_key, now, now)
        try:
            with self._session_factory.begin() as session:
                session.add(_tool_call_model(call))
        except IntegrityError:
            concurrent = self.get_tool_call_by_key(idempotency_key)
            if concurrent is not None:
                return concurrent
            raise
        return call

    def update_tool_call_status(self, tool_call_id: str, status: str) -> ToolCallRecord:
        """更新工具调用状态。"""

        with self._session_factory.begin() as session:
            result = session.execute(update(ToolCallModel).where(ToolCallModel.tool_call_id == tool_call_id).values(status=status, updated_at=_to_text(_utc_now())))
        if result.rowcount == 0:
            raise KeyError(tool_call_id)
        return self.get_tool_call(tool_call_id)

    def get_tool_call(self, tool_call_id: str) -> ToolCallRecord:
        """按标识符返回工具调用记录。"""

        with self._session_factory() as session:
            row = session.get(ToolCallModel, tool_call_id)
        if row is None:
            raise KeyError(tool_call_id)
        return _tool_call_from_model(row)

    def get_tool_call_by_key(self, idempotency_key: str) -> Optional[ToolCallRecord]:
        """按幂等键查询工具调用记录。"""

        with self._session_factory() as session:
            row = session.execute(select(ToolCallModel).where(ToolCallModel.idempotency_key == idempotency_key)).scalar_one_or_none()
        return _tool_call_from_model(row) if row is not None else None

    def create_execution(
        self,
        tool_call_id: str,
        status: str,
        effect_status: str,
        completed_at: Optional[datetime] = None,
        artifact_id: Optional[str] = None,
        error: Optional[str] = None,
    ) -> ToolExecutionRecord:
        """创建工具执行记录。"""

        now = _utc_now()
        record = ToolExecutionRecord(str(uuid4()), tool_call_id, status, effect_status, artifact_id, error, now, completed_at)
        with self._session_factory.begin() as session:
            session.add(
                ToolExecutionModel(
                    execution_id=record.execution_id,
                    tool_call_id=record.tool_call_id,
                    status=record.status,
                    effect_status=record.effect_status,
                    artifact_id=record.artifact_id,
                    error=record.error,
                    started_at=_to_text(record.started_at),
                    completed_at=_to_text(record.completed_at) if record.completed_at else None,
                )
            )
        return record


def _utc_now() -> datetime:
    """返回当前 UTC datetime。"""

    return datetime.now(timezone.utc)


def _to_text(value: datetime) -> str:
    """将 datetime 序列化为 ISO-8601 文本。"""

    return value.isoformat()


def _from_text(value: str) -> datetime:
    """解析 ISO-8601 datetime 字符串。"""

    return datetime.fromisoformat(value)


def _tool_call_model(call: ToolCallRecord) -> ToolCallModel:
    """将工具调用记录转换为 model。"""

    return ToolCallModel(tool_call_id=call.tool_call_id, run_id=call.run_id, step_id=call.step_id, tool_name=call.tool_name, arguments_json=json.dumps(call.arguments, ensure_ascii=False, sort_keys=True), permission=call.permission, status=call.status, idempotency_key=call.idempotency_key, created_at=_to_text(call.created_at), updated_at=_to_text(call.updated_at))


def _tool_call_from_model(row: ToolCallModel) -> ToolCallRecord:
    """将工具调用 model 转换为记录。"""

    return ToolCallRecord(row.tool_call_id, row.run_id, row.step_id, row.tool_name, json.loads(row.arguments_json), row.permission, row.status, row.idempotency_key, _from_text(row.created_at), _from_text(row.updated_at))
