"""Agent Team 运行表的单表 CRUD。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.agent_team.configuration.agent_team_configuration import AgentTeamConfiguration
from app.agent_team.state.agent_team_run_state import AgentTeamRunState
from app.models.enums.agent_team_run_status import AgentTeamRunStatus
from app.storage.model.agent_team_run_model import AgentTeamRunModel
from app.storage.store_engines import main_session_factory
from app.storage.write_transaction import begin_immediate
from app.utils.datetime_utils import to_text, utc_now


class AgentTeamRunCrud:
    """只负责 ``agent_team_runs`` 单表读写，不编排状态迁移。

    本类遵循 storage/crud 层约定：传入外部 ``Session`` 时复用调用方事务，不自行提交；
    未传入时为单次 CRUD 操作创建本地事务。待确认生命周期由
    ``AgentTeamRunService`` 编排，运行中的节点流转和终态规则由 ``AgentTeamCoordinator``
    负责。
    """

    def __init__(self) -> None:
        self._session_factory = main_session_factory()

    def create(
        self,
        *,
        configuration: AgentTeamConfiguration,
        workspace_id: int,
        parent_task_id: int,
        parent_run_id: int,
        preview_fingerprint: str,
        goal: str,
        node_runtime_snapshots: dict[str, dict[str, Any]],
        session: Session | None = None,
    ) -> AgentTeamRunModel:
        """创建一条待确认 TeamRun，并保存当前候选执行输入和运行快照。

        预览文档不属于 TeamRun 持久化事实，只由工具结果的 ``display_data`` 承载。
        用户确认时，应用 service 会使用前端提交的最终配置重新生成并覆盖执行快照。
        """

        row = AgentTeamRunModel(
            team_id=configuration.team_id,
            workspace_id=workspace_id,
            parent_task_id=parent_task_id,
            parent_run_id=parent_run_id,
            preview_fingerprint=preview_fingerprint,
            goal_input=goal,
            configuration_snapshot_json=configuration.model_dump(mode="json"),
            status=AgentTeamRunStatus.PENDING.value,
            state_json=AgentTeamRunState.initial(node_runtime_snapshots).to_json(),
        )
        if session is None:
            with begin_immediate(self._session_factory) as owned_session:
                owned_session.add(row)
                owned_session.flush()
        else:
            session.add(row)
            session.flush()
        return row

    def get_by_id(
        self,
        run_id: int,
        *,
        session: Session | None = None,
    ) -> AgentTeamRunModel:
        """按 StorageBase 自增主键读取 TeamRun。"""

        def query(target: Session) -> AgentTeamRunModel:
            row = target.get(AgentTeamRunModel, run_id)
            if row is None:
                raise KeyError(run_id)
            return row

        if session is not None:
            return query(session)
        with self._session_factory() as owned_session:
            return query(owned_session)

    def find_by_node_run_id(self, node_run_id: int) -> AgentTeamRunModel | None:
        """从持久化的节点引用中找到拥有该 ConversationRun 的活动 Team。"""

        with self._session_factory() as session:
            rows = session.scalars(
                select(AgentTeamRunModel).where(
                    AgentTeamRunModel.status == AgentTeamRunStatus.RUNNING.value,
                )
            ).all()
            for row in rows:
                state = AgentTeamRunState.model_validate(row.state_json)
                if state.execution_for_run(node_run_id) is not None:
                    return row
        return None

    def list_active(self) -> list[AgentTeamRunModel]:
        """读取所有已进入运行阶段且尚未进入 Team 终态的运行。

        ``pending`` 只表示等待用户确认，不属于可恢复执行集合；它只能由确认入口或显式
        取消入口处理。
        """

        with self._session_factory() as session:
            return list(
                session.scalars(
                    select(AgentTeamRunModel).where(
                        AgentTeamRunModel.status == AgentTeamRunStatus.RUNNING.value,
                    )
                ).all()
            )

    def list_pending_confirmations(self) -> list[AgentTeamRunModel]:
        """读取尚未确认的 TeamRun。

        返回:
            按数据库查询顺序返回所有 ``pending`` TeamRun；无记录时返回空列表。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 查询主库失败。

        副作用:
            打开一次主库只读 session。
        """

        with self._session_factory() as session:
            return list(
                session.scalars(
                    select(AgentTeamRunModel).where(
                        AgentTeamRunModel.status == AgentTeamRunStatus.PENDING.value,
                    )
                ).all()
            )

    def update_status_if_in(
        self,
        team_run_db_id: int,
        target_status: str,
        allowed_statuses: tuple[str, ...],
        *,
        state: AgentTeamRunState | None = None,
        end_reason: str | None = None,
        started: bool = False,
        ended: bool = False,
        configuration_snapshot_json: str | None = None,
        preview_fingerprint: str | None = None,
        goal_input: str | None = None,
        session: Session | None = None,
    ) -> AgentTeamRunModel | None:
        """仅在当前状态属于白名单时原子更新 TeamRun。

        ``allowed_statuses`` 参与 SQL 的 ``WHERE`` 条件，因此并发收尾、取消或节点提交
        不会覆盖已经写入的其他状态。返回 ``None`` 表示记录不存在，或当前状态已经不在
        白名单内；调用方必须把它当作状态已被其他路径处理，而不是继续写入。

        参数:
            team_run_db_id: ``StorageBase.id`` 主键。
            target_status: 条件满足后写入的目标状态。
            allowed_statuses: 允许被迁移的当前状态集合。
            state: 可选的 AgentTeamRunState 聚合状态快照。
            end_reason: 可选的失败或取消原因码；不承载异常正文或展示文案。
            started: 是否补写首次启动时间。
            ended: 是否补写终态时间。
            session: 可选外部事务；传入时复用且不自行提交。

        返回:
            条件更新成功后的模型；条件不满足时返回 ``None``。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 数据库更新失败时向上抛出。
        """

        if session is None:
            with begin_immediate(self._session_factory) as owned_session:
                return self._update_status_if_in_session(
                    owned_session,
                    team_run_db_id,
                    target_status,
                    allowed_statuses,
                    state=state,
                    end_reason=end_reason,
                    started=started,
                    ended=ended,
                    configuration_snapshot_json=configuration_snapshot_json,
                    preview_fingerprint=preview_fingerprint,
                    goal_input=goal_input,
                    )
        return self._update_status_if_in_session(
            session,
            team_run_db_id,
            target_status,
            allowed_statuses,
            state=state,
            end_reason=end_reason,
            started=started,
            ended=ended,
            configuration_snapshot_json=configuration_snapshot_json,
            preview_fingerprint=preview_fingerprint,
            goal_input=goal_input,
        )

    @staticmethod
    def _update_status_if_in_session(
        session: Session,
        team_run_db_id: int,
        target_status: str,
        allowed_statuses: tuple[str, ...],
        *,
        state: AgentTeamRunState | None,
        end_reason: str | None,
        started: bool,
        ended: bool,
        configuration_snapshot_json: str | None,
        preview_fingerprint: str | None,
        goal_input: str | None,
    ) -> AgentTeamRunModel | None:
        """在调用方事务中执行 TeamRun 的条件状态更新。"""

        values: dict[str, object] = {"status": target_status}
        if state is not None:
            values["state_json"] = state.to_json()
        if end_reason is not None:
            values["end_reason"] = end_reason
        if ended:
            values["ended_at"] = to_text(utc_now())
        if configuration_snapshot_json is not None:
            values["configuration_snapshot_json"] = configuration_snapshot_json
        if preview_fingerprint is not None:
            values["preview_fingerprint"] = preview_fingerprint
        if goal_input is not None:
            values["goal_input"] = goal_input

        result = session.execute(
            update(AgentTeamRunModel)
            .where(
                AgentTeamRunModel.id == team_run_db_id,
                AgentTeamRunModel.status.in_(allowed_statuses),
            )
            .values(**values)
        )
        if not result.rowcount:
            return None
        session.flush()
        row = session.get(AgentTeamRunModel, team_run_db_id)
        if row is None:
            return None
        session.refresh(row)
        if started and row.started_at is None:
            row.started_at = to_text(utc_now())
            session.flush()
        return row
