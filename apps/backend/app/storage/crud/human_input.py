"""Human-in-loop 请求 CRUD。"""

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from sqlalchemy import asc, select, update
from sqlalchemy.exc import IntegrityError

from app.domain.human_input.records import HumanInputRequestRecord, HumanInputResponseRecord
from app.storage.database import create_session_factory
from app.storage.model.human_input import HumanInputRequestModel, HumanInputResponseModel
from app.storage.schema import initialize_app_schema


class HumanInputStore:
    """读写通用人工输入请求和响应。"""

    def __init__(self, database_path: Path) -> None:
        """初始化人工输入仓储。"""

        self._engine = initialize_app_schema(database_path)
        self._session_factory = create_session_factory(self._engine)

    def create_request(self, run_id: str, prompt: str, schema: Dict[str, Any], status: str, step_id: Optional[str] = None) -> HumanInputRequestRecord:
        """创建人工输入请求。"""

        now = _utc_now()
        request = HumanInputRequestRecord(str(uuid4()), run_id, step_id, prompt, schema, status, now, None)
        with self._session_factory.begin() as session:
            session.add(_request_model(request))
        return request

    def list_by_status(self, status: str, run_id: Optional[str] = None) -> List[HumanInputRequestRecord]:
        """按状态列出人工输入请求。"""

        statement = select(HumanInputRequestModel).where(HumanInputRequestModel.status == status)
        if run_id is not None:
            statement = statement.where(HumanInputRequestModel.run_id == run_id)
        with self._session_factory() as session:
            rows = session.execute(statement.order_by(asc(HumanInputRequestModel.created_at))).scalars().all()
        return [_request_from_model(row) for row in rows]

    def get_request(self, request_id: str) -> HumanInputRequestRecord:
        """按标识符返回人工输入请求。"""

        with self._session_factory() as session:
            row = session.get(HumanInputRequestModel, request_id)
        if row is None:
            raise KeyError(request_id)
        return _request_from_model(row)

    def create_response(self, request_id: str, response: Dict[str, Any], idempotency_key: str) -> HumanInputResponseRecord:
        """创建人工输入响应。"""

        existing = self.get_response_by_key(idempotency_key)
        if existing is not None:
            return existing
        existing_for_request = self.get_response_by_request(request_id)
        if existing_for_request is not None:
            return existing_for_request
        self.get_request(request_id)
        now = _utc_now()
        record = HumanInputResponseRecord(str(uuid4()), request_id, response, idempotency_key, now)
        try:
            with self._session_factory.begin() as session:
                session.add(_response_model(record))
        except IntegrityError:
            concurrent = self.get_response_by_key(idempotency_key) or self.get_response_by_request(request_id)
            if concurrent is not None:
                return concurrent
            raise
        return record

    def update_request_status(self, request_id: str, status: str, responded_at: Optional[datetime] = None) -> HumanInputRequestRecord:
        """更新人工输入请求状态。"""

        with self._session_factory.begin() as session:
            result = session.execute(
                update(HumanInputRequestModel)
                .where(HumanInputRequestModel.request_id == request_id)
                .values(status=status, responded_at=_to_text(responded_at) if responded_at else None)
            )
        if result.rowcount != 1:
            raise KeyError(request_id)
        return self.get_request(request_id)

    def get_response_by_key(self, idempotency_key: str) -> Optional[HumanInputResponseRecord]:
        """按幂等键查询人工输入响应。"""

        with self._session_factory() as session:
            row = session.execute(select(HumanInputResponseModel).where(HumanInputResponseModel.idempotency_key == idempotency_key)).scalar_one_or_none()
        return _response_from_model(row) if row is not None else None

    def get_response_by_request(self, request_id: str) -> Optional[HumanInputResponseRecord]:
        """按请求标识查询人工输入响应。"""

        with self._session_factory() as session:
            row = session.execute(select(HumanInputResponseModel).where(HumanInputResponseModel.request_id == request_id)).scalar_one_or_none()
        return _response_from_model(row) if row is not None else None


def _utc_now() -> datetime:
    """返回当前 UTC datetime。"""

    return datetime.now(timezone.utc)


def _to_text(value: datetime) -> str:
    """将 datetime 序列化为 ISO-8601 文本。"""

    return value.isoformat()


def _from_text(value: str) -> datetime:
    """解析 ISO-8601 datetime 字符串。"""

    return datetime.fromisoformat(value)


def _request_model(record: HumanInputRequestRecord) -> HumanInputRequestModel:
    """将人工输入请求记录转换为 model。"""

    return HumanInputRequestModel(request_id=record.request_id, run_id=record.run_id, step_id=record.step_id, prompt=record.prompt, schema_json=json.dumps(record.schema, ensure_ascii=False, sort_keys=True), status=record.status, created_at=_to_text(record.created_at), responded_at=None)


def _request_from_model(row: HumanInputRequestModel) -> HumanInputRequestRecord:
    """将人工输入请求 model 转换为记录。"""

    return HumanInputRequestRecord(row.request_id, row.run_id, row.step_id, row.prompt, json.loads(row.schema_json), row.status, _from_text(row.created_at), _from_text(row.responded_at) if row.responded_at else None)


def _response_model(record: HumanInputResponseRecord) -> HumanInputResponseModel:
    """将人工输入响应记录转换为 model。"""

    return HumanInputResponseModel(response_id=record.response_id, request_id=record.request_id, response_json=json.dumps(record.response, ensure_ascii=False, sort_keys=True), idempotency_key=record.idempotency_key, created_at=_to_text(record.created_at))


def _response_from_model(row: HumanInputResponseModel) -> HumanInputResponseRecord:
    """将人工输入响应 model 转换为记录。"""

    return HumanInputResponseRecord(row.response_id, row.request_id, json.loads(row.response_json), row.idempotency_key, _from_text(row.created_at))
