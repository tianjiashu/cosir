"""工具审批 SQLite 仓储。"""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Dict, Iterator, List, Optional, Tuple
from uuid import uuid4

from app.domain.approvals.records import ApprovalDecisionRecord, ApprovalRequestRecord
from app.core.runs.records import ResumeCommandRecord
from app.core.runs.state_machine import RunStateMachine
from app.storage.migrations import ensure_durable_schema


class ApprovalStore:
    """读写工具审批请求与决策。"""

    def __init__(self, database_path: Path) -> None:
        """初始化审批仓储。

        参数:
            database_path: SQLite 数据库路径。

        返回:
            无。

        异常:
            sqlite3.Error: 如果 schema 初始化失败。

        副作用:
            初始化审批相关表。
        """

        self._database_path = database_path
        self._run_state_machine = RunStateMachine()
        ensure_durable_schema(database_path)

    def create_request(
        self,
        run_id: str,
        tool_name: str,
        permission: str,
        risk_level: str,
        payload: Dict[str, Any],
        step_id: Optional[str] = None,
        tool_call_id: Optional[str] = None,
    ) -> ApprovalRequestRecord:
        """创建审批请求。

        参数:
            run_id: 所属运行标识符。
            tool_name: 工具名称。
            permission: 权限级别。
            risk_level: 风险等级。
            payload: 面向 UI 的审批详情。
            step_id: 可选步骤标识符。
            tool_call_id: 可选工具调用标识符。

        返回:
            新建审批请求。

        异常:
            sqlite3.Error: 如果写入失败。

        副作用:
            写入 approval_requests 表。
        """

        now = _utc_now()
        approval = ApprovalRequestRecord(
            approval_id=str(uuid4()),
            run_id=run_id,
            step_id=step_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            permission=permission,
            risk_level=risk_level,
            payload=payload,
            status="pending",
            created_at=now,
            decided_at=None,
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO approval_requests(
                    approval_id, run_id, step_id, tool_call_id, tool_name,
                    permission, risk_level, payload_json, status, created_at, decided_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    approval.approval_id,
                    approval.run_id,
                    approval.step_id,
                    approval.tool_call_id,
                    approval.tool_name,
                    approval.permission,
                    approval.risk_level,
                    json.dumps(approval.payload, ensure_ascii=False, sort_keys=True),
                    approval.status,
                    _to_text(approval.created_at),
                    None,
                ),
            )
        return approval

    def create_request_and_wait_run(
        self,
        run_id: str,
        tool_name: str,
        permission: str,
        risk_level: str,
        payload: Dict[str, Any],
        step_id: Optional[str] = None,
        tool_call_id: Optional[str] = None,
    ) -> ApprovalRequestRecord:
        """原子创建审批请求并将运行切换到等待状态。

        参数:
            run_id: 所属运行标识符。
            tool_name: 工具名称。
            permission: 权限级别。
            risk_level: 风险等级。
            payload: 面向 UI 的审批详情。
            step_id: 可选步骤标识符。
            tool_call_id: 可选工具调用标识符。

        返回:
            已创建的审批请求。

        异常:
            KeyError: 如果运行不存在。
            InvalidRunTransition: 如果当前运行不能进入 waiting。
            sqlite3.Error: 如果事务写入失败。

        副作用:
            在同一个 SQLite 事务中写入 approval_requests 并更新 durable_runs。
        """

        now = _utc_now()
        approval = ApprovalRequestRecord(
            approval_id=str(uuid4()),
            run_id=run_id,
            step_id=step_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            permission=permission,
            risk_level=risk_level,
            payload=payload,
            status="pending",
            created_at=now,
            decided_at=None,
        )
        with self._connect() as connection:
            run = connection.execute(
                "SELECT status FROM durable_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise KeyError(run_id)
            self._run_state_machine.ensure_transition(run["status"], "waiting")
            connection.execute(
                """
                INSERT INTO approval_requests(
                    approval_id, run_id, step_id, tool_call_id, tool_name,
                    permission, risk_level, payload_json, status, created_at, decided_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    approval.approval_id,
                    approval.run_id,
                    approval.step_id,
                    approval.tool_call_id,
                    approval.tool_name,
                    approval.permission,
                    approval.risk_level,
                    json.dumps(approval.payload, ensure_ascii=False, sort_keys=True),
                    approval.status,
                    _to_text(approval.created_at),
                    None,
                ),
            )
            connection.execute(
                """
                UPDATE durable_runs
                SET status = 'waiting', wait_reason = ?, active_step_id = ?,
                    active_wait_id = ?, interruption_reason = NULL, updated_at = ?
                WHERE run_id = ?
                """,
                ("approval", step_id, approval.approval_id, _to_text(now), run_id),
            )
        return approval

    def list_pending(self, run_id: Optional[str] = None) -> List[ApprovalRequestRecord]:
        """列出待处理审批请求。

        参数:
            run_id: 可选运行标识符；提供时只返回该运行的审批。

        返回:
            待处理审批请求列表。

        异常:
            sqlite3.Error: 如果查询失败。

        副作用:
            无。
        """

        query = "SELECT * FROM approval_requests WHERE status = 'pending'"
        params: tuple = ()
        if run_id is not None:
            query += " AND run_id = ?"
            params = (run_id,)
        query += " ORDER BY created_at ASC"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [_approval_from_row(row) for row in rows]

    def get_request(self, approval_id: str) -> ApprovalRequestRecord:
        """按标识符返回审批请求。

        参数:
            approval_id: 审批请求标识符。

        返回:
            匹配的审批请求。

        异常:
            KeyError: 如果审批请求不存在。

        副作用:
            无。
        """

        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM approval_requests WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
        if row is None:
            raise KeyError(approval_id)
        return _approval_from_row(row)

    def record_decision(
        self,
        approval_id: str,
        decision: str,
        reason: Optional[str],
        idempotency_key: str,
    ) -> ApprovalDecisionRecord:
        """记录审批决策并幂等更新审批状态。

        参数:
            approval_id: 审批请求标识符。
            decision: 决策值。
            reason: 决策原因。
            idempotency_key: 幂等键。

        返回:
            新建或已有的审批决策。

        异常:
            KeyError: 如果审批请求不存在。
            sqlite3.Error: 如果写入失败。

        副作用:
            写入 approval_decisions 表并更新 approval_requests 状态。
        """

        existing = self.get_decision_by_key(idempotency_key)
        if existing is not None:
            return existing
        existing_for_approval = self.get_decision_by_approval(approval_id)
        if existing_for_approval is not None:
            if existing_for_approval.decision != decision:
                raise ValueError("approval already decided with a different decision")
            return existing_for_approval

        self.get_request(approval_id)
        now = _utc_now()
        record = ApprovalDecisionRecord(
            decision_id=str(uuid4()),
            approval_id=approval_id,
            decision=decision,
            reason=reason,
            decided_at=now,
            idempotency_key=idempotency_key,
        )
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO approval_decisions(
                        decision_id, approval_id, decision, reason, decided_at, idempotency_key
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.decision_id,
                        record.approval_id,
                        record.decision,
                        record.reason,
                        _to_text(record.decided_at),
                        record.idempotency_key,
                    ),
                )
                connection.execute(
                    "UPDATE approval_requests SET status = ?, decided_at = ? WHERE approval_id = ?",
                    (decision, _to_text(now), approval_id),
                )
        except sqlite3.IntegrityError:
            concurrent = self.get_decision_by_key(idempotency_key)
            if concurrent is not None:
                return concurrent
            concurrent_for_approval = self.get_decision_by_approval(approval_id)
            if concurrent_for_approval is not None:
                if concurrent_for_approval.decision != decision:
                    raise ValueError("approval already decided with a different decision") from None
                return concurrent_for_approval
            raise
        return record

    def record_decision_and_enqueue_resume(
        self,
        approval_id: str,
        decision: str,
        reason: Optional[str],
        idempotency_key: str,
        action: str,
        payload: Dict[str, Any],
    ) -> Tuple[ApprovalDecisionRecord, ResumeCommandRecord]:
        """原子记录审批决策、创建恢复命令并推进运行状态。

        参数:
            approval_id: 审批请求标识符。
            decision: approved 或 denied。
            reason: 决策原因。
            idempotency_key: 审批决策幂等键。
            action: 恢复命令动作。
            payload: 恢复命令载荷。

        返回:
            审批决策记录和恢复命令记录。

        异常:
            KeyError: 如果审批请求或运行不存在。
            ValueError: 如果同一审批已存在不同决策。
            InvalidRunTransition: 如果当前运行不能进入 resuming。
            sqlite3.Error: 如果事务写入失败。

        副作用:
            在同一个 SQLite 事务中写入审批决策、恢复命令并更新运行状态。
        """

        with self._connect() as connection:
            approval_row = connection.execute(
                "SELECT * FROM approval_requests WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            if approval_row is None:
                raise KeyError(approval_id)
            approval = _approval_from_row(approval_row)
            record = self._ensure_decision(
                connection,
                approval_id=approval_id,
                decision=decision,
                reason=reason,
                idempotency_key=idempotency_key,
            )
            command = self._ensure_resume_command(
                connection,
                run_id=approval.run_id,
                action=action,
                payload=payload,
                idempotency_key=f"resume:{record.idempotency_key}",
            )
            if command.status != "applied":
                run = connection.execute(
                    "SELECT status FROM durable_runs WHERE run_id = ?",
                    (approval.run_id,),
                ).fetchone()
                if run is None:
                    raise KeyError(approval.run_id)
                self._run_state_machine.ensure_transition(run["status"], "resuming")
                connection.execute(
                    """
                    UPDATE durable_runs
                    SET status = 'resuming', wait_reason = NULL, active_step_id = NULL,
                        active_wait_id = NULL, interruption_reason = NULL, updated_at = ?
                    WHERE run_id = ?
                    """,
                    (_to_text(_utc_now()), approval.run_id),
                )
        return record, command

    def get_decision_by_approval(self, approval_id: str) -> Optional[ApprovalDecisionRecord]:
        """按审批请求标识查询审批决策。

        参数:
            approval_id: 审批请求标识符。

        返回:
            存在时返回审批决策，否则返回 None。

        异常:
            sqlite3.Error: 如果查询失败。

        副作用:
            无。
        """

        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM approval_decisions WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
        return _decision_from_row(row) if row is not None else None

    def get_decision_by_key(self, idempotency_key: str) -> Optional[ApprovalDecisionRecord]:
        """按幂等键查询审批决策。

        参数:
            idempotency_key: 审批决策幂等键。

        返回:
            存在时返回审批决策，否则返回 None。

        异常:
            sqlite3.Error: 如果查询失败。

        副作用:
            无。
        """

        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM approval_decisions WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        return _decision_from_row(row) if row is not None else None

    def _ensure_decision(
        self,
        connection: sqlite3.Connection,
        approval_id: str,
        decision: str,
        reason: Optional[str],
        idempotency_key: str,
    ) -> ApprovalDecisionRecord:
        """在当前事务内创建或复用审批决策。

        参数:
            connection: 当前 SQLite 事务连接。
            approval_id: 审批请求标识符。
            decision: 决策值。
            reason: 决策原因。
            idempotency_key: 审批决策幂等键。

        返回:
            新建或已存在的审批决策。

        异常:
            ValueError: 如果已有决策与本次决策冲突。
            sqlite3.Error: 如果写入失败。

        副作用:
            可能写入 approval_decisions 并更新 approval_requests。
        """

        row = connection.execute(
            "SELECT * FROM approval_decisions WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if row is not None:
            return _decision_from_row(row)
        row = connection.execute(
            "SELECT * FROM approval_decisions WHERE approval_id = ?",
            (approval_id,),
        ).fetchone()
        if row is not None:
            record = _decision_from_row(row)
            if record.decision != decision:
                raise ValueError("approval already decided with a different decision")
            return record

        now = _utc_now()
        record = ApprovalDecisionRecord(
            decision_id=str(uuid4()),
            approval_id=approval_id,
            decision=decision,
            reason=reason,
            decided_at=now,
            idempotency_key=idempotency_key,
        )
        try:
            connection.execute(
                """
                INSERT INTO approval_decisions(
                    decision_id, approval_id, decision, reason, decided_at, idempotency_key
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    record.decision_id,
                    record.approval_id,
                    record.decision,
                    record.reason,
                    _to_text(record.decided_at),
                    record.idempotency_key,
                ),
            )
        except sqlite3.IntegrityError:
            row = connection.execute(
                "SELECT * FROM approval_decisions WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if row is not None:
                return _decision_from_row(row)
            row = connection.execute(
                "SELECT * FROM approval_decisions WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            if row is not None:
                existing = _decision_from_row(row)
                if existing.decision != decision:
                    raise ValueError("approval already decided with a different decision") from None
                return existing
            raise
        connection.execute(
            "UPDATE approval_requests SET status = ?, decided_at = ? WHERE approval_id = ?",
            (decision, _to_text(now), approval_id),
        )
        return record

    def _ensure_resume_command(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        action: str,
        payload: Dict[str, Any],
        idempotency_key: str,
    ) -> ResumeCommandRecord:
        """在当前事务内创建或复用恢复命令。

        参数:
            connection: 当前 SQLite 事务连接。
            run_id: 被恢复的运行标识符。
            action: 恢复动作。
            payload: 恢复动作载荷。
            idempotency_key: 恢复命令幂等键。

        返回:
            新建或已存在的恢复命令。

        异常:
            sqlite3.Error: 如果写入失败。

        副作用:
            可能写入 resume_commands。
        """

        row = connection.execute(
            "SELECT * FROM resume_commands WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if row is not None:
            return _resume_command_from_row(row)
        now = _utc_now()
        command = ResumeCommandRecord(
            command_id=str(uuid4()),
            run_id=run_id,
            action=action,
            payload=payload,
            idempotency_key=idempotency_key,
            status="pending",
            created_at=now,
            applied_at=None,
        )
        try:
            connection.execute(
                """
                INSERT INTO resume_commands(
                    command_id, run_id, action, payload_json, idempotency_key,
                    status, created_at, applied_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    command.command_id,
                    command.run_id,
                    command.action,
                    json.dumps(command.payload, ensure_ascii=False, sort_keys=True),
                    command.idempotency_key,
                    command.status,
                    _to_text(command.created_at),
                    None,
                ),
            )
        except sqlite3.IntegrityError:
            row = connection.execute(
                "SELECT * FROM resume_commands WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if row is not None:
                return _resume_command_from_row(row)
            raise
        return command

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """打开 SQLite 连接。

        参数:
            无。

        返回:
            可在 with 语句中使用的 SQLite 连接迭代器，连接启用了 Row 工厂。

        异常:
            sqlite3.Error: 如果连接失败。

        副作用:
            打开数据库连接；正常退出时提交事务，异常退出时回滚事务，最终关闭连接。
        """

        connection = sqlite3.connect(self._database_path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def _utc_now() -> datetime:
    """返回审批仓储使用的当前 UTC 时间。

    参数:
        无。

    返回:
        带 UTC 时区的当前 datetime。

    异常:
        无。

    副作用:
        无。
    """

    return datetime.now(timezone.utc)


def _to_text(value: datetime) -> str:
    """将审批时间序列化为 SQLite 文本。

    参数:
        value: 待序列化的 datetime。

    返回:
        ISO-8601 格式的时间字符串。

    异常:
        无。

    副作用:
        无。
    """

    return value.isoformat()


def _from_text(value: str) -> datetime:
    """将 SQLite 时间文本解析为 datetime。

    参数:
        value: ISO-8601 格式的时间字符串。

    返回:
        解析后的 datetime。

    异常:
        ValueError: 如果 value 不是合法的 ISO-8601 时间文本。

    副作用:
        无。
    """

    return datetime.fromisoformat(value)


def _approval_from_row(row: sqlite3.Row) -> ApprovalRequestRecord:
    """从 SQLite 行构造审批请求领域记录。

    参数:
        row: approval_requests 查询返回的 SQLite 行。

    返回:
        反序列化后的审批请求记录。

    异常:
        KeyError: 如果 row 缺少必要列。
        json.JSONDecodeError: 如果 payload_json 不是合法 JSON。
        ValueError: 如果时间字段无法解析。

    副作用:
        无。
    """

    return ApprovalRequestRecord(
        approval_id=row["approval_id"],
        run_id=row["run_id"],
        step_id=row["step_id"],
        tool_call_id=row["tool_call_id"],
        tool_name=row["tool_name"],
        permission=row["permission"],
        risk_level=row["risk_level"],
        payload=json.loads(row["payload_json"]),
        status=row["status"],
        created_at=_from_text(row["created_at"]),
        decided_at=_from_text(row["decided_at"]) if row["decided_at"] else None,
    )


def _decision_from_row(row: sqlite3.Row) -> ApprovalDecisionRecord:
    """从 SQLite 行构造审批决策领域记录。

    参数:
        row: approval_decisions 查询返回的 SQLite 行。

    返回:
        反序列化后的审批决策记录。

    异常:
        KeyError: 如果 row 缺少必要列。
        ValueError: 如果时间字段无法解析。

    副作用:
        无。
    """

    return ApprovalDecisionRecord(
        decision_id=row["decision_id"],
        approval_id=row["approval_id"],
        decision=row["decision"],
        reason=row["reason"],
        decided_at=_from_text(row["decided_at"]),
        idempotency_key=row["idempotency_key"],
    )


def _resume_command_from_row(row: sqlite3.Row) -> ResumeCommandRecord:
    """从 SQLite 行构造恢复命令记录。

    参数:
        row: resume_commands 查询返回的 SQLite 行。

    返回:
        反序列化后的恢复命令记录。

    异常:
        KeyError: 如果 row 缺少必要列。
        json.JSONDecodeError: 如果 payload_json 不是合法 JSON。
        ValueError: 如果时间字段无法解析。

    副作用:
        无。
    """

    return ResumeCommandRecord(
        command_id=row["command_id"],
        run_id=row["run_id"],
        action=row["action"],
        payload=json.loads(row["payload_json"]),
        idempotency_key=row["idempotency_key"],
        status=row["status"],
        created_at=_from_text(row["created_at"]),
        applied_at=_from_text(row["applied_at"]) if row["applied_at"] else None,
    )
