"""工具审批 CRUD。"""

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from sqlalchemy import asc, delete, select, update
from sqlalchemy.exc import IntegrityError

from app.domain.approvals.records import ApprovalDecisionRecord, ApprovalRequestRecord
from app.storage.database import create_session_factory
from app.storage.model.approval import ApprovalDecisionModel, ApprovalRequestModel
from app.storage.schema import initialize_app_schema


class ApprovalStore:
    """读写工具审批请求与决策。"""

    def __init__(self, database_path: Path) -> None:
        """初始化审批仓储。"""

        self._engine = initialize_app_schema(database_path)
        self._session_factory = create_session_factory(self._engine)

    def create_request(self, run_id: str, tool_name: str, permission: str, risk_level: str, payload: Dict[str, Any], status: str, step_id: Optional[str] = None, tool_call_id: Optional[str] = None) -> ApprovalRequestRecord:
        """创建审批请求。"""

        approval = ApprovalRequestRecord(str(uuid4()), run_id, step_id, tool_call_id, tool_name, permission, risk_level, payload, status, _utc_now(), None)
        with self._session_factory.begin() as session:
            session.add(_approval_model(approval))
        return approval

    def list_by_status(self, status: str, run_id: Optional[str] = None) -> List[ApprovalRequestRecord]:
        """按状态列出审批请求。"""

        statement = select(ApprovalRequestModel).where(ApprovalRequestModel.status == status)
        if run_id is not None:
            statement = statement.where(ApprovalRequestModel.run_id == run_id)
        with self._session_factory() as session:
            rows = session.execute(statement.order_by(asc(ApprovalRequestModel.created_at))).scalars().all()
        return [_approval_from_model(row) for row in rows]

    def delete_by_run_ids(self, run_ids: List[str]) -> None:
        """删除运行集合下的审批请求与决策。"""

        if not run_ids:
            return
        with self._session_factory.begin() as session:
            approval_ids = [
                row[0]
                for row in session.execute(
                    select(ApprovalRequestModel.approval_id).where(ApprovalRequestModel.run_id.in_(tuple(run_ids)))
                ).all()
            ]
            if approval_ids:
                session.execute(delete(ApprovalDecisionModel).where(ApprovalDecisionModel.approval_id.in_(approval_ids)))
                session.execute(delete(ApprovalRequestModel).where(ApprovalRequestModel.approval_id.in_(approval_ids)))

    def get_request(self, approval_id: str) -> ApprovalRequestRecord:
        """按标识符返回审批请求。"""

        with self._session_factory() as session:
            row = session.get(ApprovalRequestModel, approval_id)
        if row is None:
            raise KeyError(approval_id)
        return _approval_from_model(row)

    def create_decision(self, approval_id: str, decision: str, reason: Optional[str], idempotency_key: str) -> ApprovalDecisionRecord:
        """创建审批决策。"""

        existing = self.get_decision_by_key(idempotency_key)
        if existing is not None:
            return existing
        existing_for_approval = self.get_decision_by_approval(approval_id)
        if existing_for_approval is not None:
            return existing_for_approval
        self.get_request(approval_id)
        now = _utc_now()
        record = ApprovalDecisionRecord(str(uuid4()), approval_id, decision, reason, now, idempotency_key)
        try:
            with self._session_factory.begin() as session:
                session.add(_decision_model(record))
        except IntegrityError:
            concurrent = self.get_decision_by_key(idempotency_key) or self.get_decision_by_approval(approval_id)
            if concurrent is not None:
                return concurrent
            raise
        return record

    def update_request_status(self, approval_id: str, status: str, decided_at: Optional[datetime] = None) -> ApprovalRequestRecord:
        """更新审批请求状态。"""

        with self._session_factory.begin() as session:
            result = session.execute(
                update(ApprovalRequestModel)
                .where(ApprovalRequestModel.approval_id == approval_id)
                .values(status=status, decided_at=_to_text(decided_at) if decided_at else None)
            )
        if result.rowcount != 1:
            raise KeyError(approval_id)
        return self.get_request(approval_id)

    def get_decision_by_approval(self, approval_id: str) -> Optional[ApprovalDecisionRecord]:
        """按审批请求标识查询审批决策。"""

        with self._session_factory() as session:
            row = session.execute(select(ApprovalDecisionModel).where(ApprovalDecisionModel.approval_id == approval_id)).scalar_one_or_none()
        return _decision_from_model(row) if row is not None else None

    def get_decision_by_key(self, idempotency_key: str) -> Optional[ApprovalDecisionRecord]:
        """按幂等键查询审批决策。"""

        with self._session_factory() as session:
            row = session.execute(select(ApprovalDecisionModel).where(ApprovalDecisionModel.idempotency_key == idempotency_key)).scalar_one_or_none()
        return _decision_from_model(row) if row is not None else None


def _utc_now() -> datetime:
    """返回当前 UTC datetime。"""

    return datetime.now(timezone.utc)


def _to_text(value: datetime) -> str:
    """将 datetime 序列化为 ISO-8601 文本。"""

    return value.isoformat()


def _from_text(value: str) -> datetime:
    """解析 ISO-8601 datetime 字符串。"""

    return datetime.fromisoformat(value)


def _approval_model(record: ApprovalRequestRecord) -> ApprovalRequestModel:
    """将审批请求记录转换为 model。"""

    return ApprovalRequestModel(approval_id=record.approval_id, run_id=record.run_id, step_id=record.step_id, tool_call_id=record.tool_call_id, tool_name=record.tool_name, permission=record.permission, risk_level=record.risk_level, payload_json=json.dumps(record.payload, ensure_ascii=False, sort_keys=True), status=record.status, created_at=_to_text(record.created_at), decided_at=None)


def _approval_from_model(row: ApprovalRequestModel) -> ApprovalRequestRecord:
    """将审批请求 model 转换为记录。"""

    return ApprovalRequestRecord(row.approval_id, row.run_id, row.step_id, row.tool_call_id, row.tool_name, row.permission, row.risk_level, json.loads(row.payload_json), row.status, _from_text(row.created_at), _from_text(row.decided_at) if row.decided_at else None)


def _decision_model(record: ApprovalDecisionRecord) -> ApprovalDecisionModel:
    """将审批决策记录转换为 model。"""

    return ApprovalDecisionModel(decision_id=record.decision_id, approval_id=record.approval_id, decision=record.decision, reason=record.reason, decided_at=_to_text(record.decided_at), idempotency_key=record.idempotency_key)


def _decision_from_model(row: ApprovalDecisionModel) -> ApprovalDecisionRecord:
    """将审批决策 model 转换为记录。"""

    return ApprovalDecisionRecord(row.decision_id, row.approval_id, row.decision, row.reason, _from_text(row.decided_at), row.idempotency_key)
