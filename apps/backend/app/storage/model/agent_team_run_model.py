"""Agent Team 执行意图与聚合运行的 SQLite 模型。"""

from sqlalchemy import JSON, CheckConstraint, ForeignKey, Index, Integer, String, Text
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Mapped, mapped_column

from app.storage.model.base import StorageBase


class AgentTeamRunModel(StorageBase):
    """``agent_team_runs`` 表：一条 Team 执行意图及其运行过程的唯一事实。

    待确认请求和已启动运行共享同一行，通过 ``status`` 表示生命周期。节点仍使用现有
    Task/ConversationRun；本表保存 Team 级输入冻结、运行游标和聚合状态。展示预览属于工具
    消息的 ``display_data``，不在本表复制。该模型
    只描述持久化结构，不负责状态迁移、节点调度、模型调用或前端 wire schema 投影；状态
    迁移由应用 service 和 Coordinator 在事务边界内完成，前端数据由 API 层投影。

    字段:
        id: 继承自 StorageBase 的自增整数主键，也是待确认方案确认、查询和恢复的唯一标识。
        team_id: 创建本次 TeamRun 时使用的 Agent Team 配置标识。
        workspace_id: TeamRun 所属 workspace 的数据库主键，用于约束资源归属和级联删除。
        parent_task_id: 发起 TeamRun 的主 Agent Task 数据库主键。
        parent_run_id: 发起 TeamRun 的主 ConversationRun 数据库主键；Team 完成或失败后，
            Coordinator 依据该标识恢复主 Run 的后续流程。
        preview_fingerprint: 根据最终确认时的 Team 配置、目标、指令和节点运行快照计算的
            稳定指纹，用于记录本次实际执行计划的内容摘要。
        goal_input: 用户或主 Agent 提供的 Team 总体目标，作为节点执行输入的一部分。
        node_instructions_json: 按节点标识保存的用户指令快照；确认后不再重新读取外部输入。
        configuration_snapshot_json: 当前确认后执行所使用的完整 Team 配置快照，避免运行时
            配置文件变化影响当前执行；待确认阶段的候选配置会在确认时以请求体重新校验并覆盖。
        status: TeamRun 生命周期状态，取值为 ``pending``、``running``、``completed``、
            ``failed`` 或 ``cancelled``。``pending`` 专门表示已创建但尚未确认的方案，
            确认后原子迁移为 ``running``。
        state_json: AgentTeamRunState 序列化后的 Team 聚合运行状态快照，包括每次节点执行、
            活动节点游标、转移历史和节点运行快照；其中运行时敏感快照仅供后端恢复，API
            投影时必须过滤。
        end_reason: Team 进入失败或取消终态时的稳定原因码；成功、运行中及未处理状态时
            为空。原因码不承载异常正文，详细诊断由结构化日志保存。
        started_at: Team 首次从 ``pending`` 进入运行阶段的 UTC 文本时间；尚未启动时为空。
        ended_at: Team 进入完成、失败或取消等终态的 UTC 文本时间；未进入终态时为空。

    持久化副作用:
        SQLAlchemy 使用该模型在主 SQLite 数据库中创建和读写 ``agent_team_runs`` 表。外键
        关联的 workspace、Task 或主 ConversationRun 删除时，本行随之级联删除；本模型不会
        自行创建线程、进程、网络连接或进程内缓存。
    """

    __tablename__ = "agent_team_runs"
    __table_args__ = (
        Index("idx_agent_team_runs_parent", "parent_task_id", "parent_run_id"),
        Index("idx_agent_team_runs_status", "status"),
        Index(
            "uq_agent_team_runs_pending_parent",
            "parent_task_id",
            "parent_run_id",
            unique=True,
            sqlite_where=sql_text("status = 'pending'"),
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'cancelled')",
            name="ck_agent_team_runs_status",
        ),
    )

    team_id: Mapped[str] = mapped_column(Text, nullable=False)
    workspace_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    parent_task_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    parent_run_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("conversation_runs.id", ondelete="CASCADE"), nullable=False
    )
    preview_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    goal_input: Mapped[str] = mapped_column(Text, nullable=False)
    node_instructions_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    configuration_snapshot_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending"
    )
    state_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    end_reason: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[str | None] = mapped_column(Text)
    ended_at: Mapped[str | None] = mapped_column(Text)
