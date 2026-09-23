"""Conversation Run 生命周期状态机。

单一职责：作为 Run 状态迁移的唯一入口——以「状态白名单 + 原子条件更新」作为幂等闸门，
仅在条件更新真正命中后才发布 ``RunStatusChangedEvent``，随后回读并返回最新记录；同时提供
Run 记录读取与「task 是否已有 active run」查询。

职责边界：
- 负责：``pending`` / ``running`` / 终态之间的条件迁移及其事件发布、Run 记录查询。
- 不负责：命令输入解析与附件处理、Run 创建与编辑的用例编排（见
  ``ConversationRunService``）；崩溃恢复中「Run 终态 + 未闭合工具调用」的多事实收口同样
  由 ``ConversationRunService`` 编排。
- 不拥有跨表事务：各迁移方法在 CRUD 的条件更新内自成一次写入；仅 ``has_active_run``
  接受调用方传入的外部 ``session``，此时事务提交与事件发布由调用方负责。

持久化事实归 ``ConversationRunCrud``；本模块只表达「一次合法状态迁移 + 其对应事件」。
"""
from __future__ import annotations

from typing import cast

from sqlalchemy.orm import Session

from app.assistant_transport.event import RunStatusChangedEvent
from app.assistant_transport.event.dispatch import dispatch_conversation_event
from app.config.constant import Constant
from app.core.workflows.conversation_run_usage_stats import ConversationRunUsageStats
from app.models import (
    ConversationRunError,
    ConversationRunRecord,
    ConversationRunStatus,
)
from app.models.conversation_run_failure import (
    run_failure_message,
)
from app.models.conversation_run_usage import ConversationRunUsage
from app.service import depends as service_depends
from app.storage.store_engines import main_session_factory


def usage_payload(
    usage_stats: ConversationRunUsageStats | None,
) -> ConversationRunUsage | None:
    """把运行时 token 累加器转成 Run 行可持久化的六键字典。

    参数:
        usage_stats: 运行期累加器；``None`` 表示本次不写用量列（例如崩溃恢复）。

    返回:
        ``ConversationRunUsage`` 字典或 ``None``。

    异常:
        无；累加器自身保证返回可 JSON 序列化的整数/None 字段。

    副作用:
        无。
    """

    if usage_stats is None:
        return None
    return cast("ConversationRunUsage | None", usage_stats.to_dict())


def terminal_error(
    status: ConversationRunStatus, end_reason: str | None
) -> ConversationRunError | None:
    """把终态状态映射为受控的持久化错误契约。

    参数:
        status: 目标终态；``completed`` 表示成功终止，不写错误契约。
        end_reason: 领域侧终态原因；仅当它是合法标识符（``isidentifier()``）时才作为
            ``code`` 落库，避免把自由文本写进受控字段。

    返回:
        非 ``completed`` 终态返回受控的 ``ConversationRunError``；``completed`` 返回 ``None``。
        ``message`` 由失败 code 目录统一产出（provider 无关的通用文案），未登记的 code
        回退通用失败文案，保证 UI 始终拿得到可展示文本。

    异常:
        无。

    副作用:
        无。
    """

    if status is ConversationRunStatus.COMPLETED:
        return None
    code = end_reason if isinstance(end_reason, str) and end_reason.isidentifier() else None
    if code is None:
        code = (
            Constant.Run.RUN_FAILURE_CODE_CANCELLED
            if status is ConversationRunStatus.CANCELLED
            else Constant.Run.RUN_FAILURE_CODE_UNKNOWN
        )
    return ConversationRunError(code=code, message=run_failure_message(code))


class ConversationRunStateService:
    """Run 状态迁移与查询的唯一入口。"""

    def __init__(self) -> None:
        """初始化状态 service：装配 run CRUD 与会话工厂。

        参数:
            无。

        返回:
            无。

        异常:
            RuntimeError: 如果 storage 尚未初始化。

        副作用:
            从 service 依赖入口取得 CRUD 单例并保存引用；不读写任何 run 状态。
        """

        self._run = service_depends.get_conversation_run_crud()
        self._session_factory = main_session_factory()

    def has_active_run(self, task_id: int, session: Session | None = None) -> bool:
        """检查该 task 是否已存在 ``pending`` / ``running`` 的 run。

        这是「一个 task 同一时刻只允许一个 active run」这条不变量的读侧表达。注意它
        **不是** Task 操作闸门的重复：闸门只把并发操作串行化，并不阻止两条不同
        ``command_id`` 的请求依次各建一个 active run。

        参数:
            task_id: 任务标识。
            session: 可选，调用方已开启的事务会话；为 None 时本方法自建只读会话。

        返回:
            该 task 存在 ``pending`` 或 ``running`` run 时返回 True，否则 False。

        异常:
            无；底层会话或查询失败按 SQLAlchemy 语义向上传播。

        副作用:
            无（仅只读查询）。
        """

        statuses = (ConversationRunStatus.PENDING.value, ConversationRunStatus.RUNNING.value)
        if session is not None:
            return self._run.has_run_in_status(task_id, statuses, session)
        with self._session_factory() as owned_session:
            return self._run.has_run_in_status(task_id, statuses, owned_session)

    def get_run(self, run_id: int) -> ConversationRunRecord:
        """按标识读取单个 Run 记录。

        参数:
            run_id: Conversation Run 标识。

        返回:
            对应的 ``ConversationRunRecord``。

        异常:
            KeyError: run 不存在。

        副作用:
            无。
        """

        return self._run.get(run_id)

    def list_runs_for_task(self, task_id: int) -> list[ConversationRunRecord]:
        """按创建顺序读取某 task 的全部 Run 记录。

        参数:
            task_id: 任务标识。

        返回:
            该 task 的 ``ConversationRunRecord`` 列表；无记录时返回空列表。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 查询失败。

        副作用:
            无。
        """

        return self._run.list_by_task(task_id)

    def resume_cancelled_run(self, run_id: int) -> ConversationRunRecord | None:
        """恢复任意 cancelled run 为 ``running``，清空上一轮终态字段。

        本方法只表达「哪些迁移合法」（``cancelled`` → ``running``）与事件发布；清空上一轮
        终态字段由 ``update_status_if_in`` 的 ``clear_terminal_fields`` 完成。

        ``checkpoint_thread_id`` 保持原值：``resume`` 执行模式下 workflow 传
        ``input_state=None`` 让 LangGraph 从该线程的**既有 checkpoint** 继续，换新线程会让
        续跑落到一个没有 checkpoint 的空线程上。

        参数:
            run_id: 待恢复的 Conversation Run 标识。

        返回:
            恢复后的记录；run 当前不是 ``cancelled`` 时返回 None。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 写入失败。run 不存在时按零行更新静默返回 None
                （存在性由调用方按需预检）。

        副作用:
            条件更新 run 为 ``running``（清空上一轮终态字段，``checkpoint_thread_id``
            保持不变）并发布 RUNNING 状态事件；条件不满足时不做任何写入。
        """

        record = self._run.update_status_if_in(
            run_id,
            ConversationRunStatus.RUNNING.value,
            (ConversationRunStatus.CANCELLED.value,),
            clear_terminal_fields=True,
        )
        if record is None:
            return None
        dispatch_conversation_event(
            RunStatusChangedEvent(
                task_id=record.task_id,
                run_id=run_id,
                status=ConversationRunStatus.RUNNING,
            ),
        )
        return record

    def claim_pending_run(self, run_id: int) -> ConversationRunRecord | None:
        """认领一个待执行的 Conversation Run（``pending`` → ``running``）。

        参数:
            run_id: 待认领的运行标识。

        返回:
            认领成功时返回更新后的 ``ConversationRunRecord``；run 已不是 ``pending``
            （终态、或已被其它执行器认领为 ``running``）时返回 None。

        异常:
            KeyError: run 不存在。
            sqlalchemy.exc.SQLAlchemyError: 写入失败。

        副作用:
            pending run 成功迁移时发布一次 RUNNING 状态事件；未命中时不写库、不发事件。
        """

        row = self._run.update_status_if_in(
            run_id=run_id,
            target_status=ConversationRunStatus.RUNNING.value,
            allowed_statuses=(ConversationRunStatus.PENDING.value,),
        )
        if row is not None:
            dispatch_conversation_event(
                RunStatusChangedEvent(
                    task_id=row.task_id,
                    run_id=run_id,
                    status=ConversationRunStatus.RUNNING,
                ),
            )
            return self._run.get(run_id)

        return None

    def complete_run_if_running(
        self,
        run_id: int,
        final_output: str | None = None,
        usage_stats: ConversationRunUsageStats | None = None,
    ) -> ConversationRunRecord | None:
        """Complete a running Conversation Run atomically.

        业务语义：仅 ``running`` 可进入 ``completed`` 终态并落库回复文本；约束收敛在
        本方法（service 层），CRUD 层只做通用的「状态白名单 + 原子更新」。Agent 对该
        轮次的最终回答文本通过 ``final_output`` 一并写入，供快速检索与审计。

        参数:
            run_id: 待完成的 Conversation Run 标识。
            final_output: 可选，Agent 对该轮次的最终回答文本；为 None 时不修改该列。
            usage_stats: 可选，运行用量统计，终态时按六键契约落库并发布事件。

        返回:
            成功完成时返回更新后的 ConversationRunRecord；run 已不是 running 时返回 None。

        异常:
            KeyError: 如果指定 run 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果底层更新失败。

        副作用:
            条件满足时更新 run 状态为 completed（并可选写入 final_output 与用量），随后
            发布 COMPLETED 状态事件。
        """

        record = self._run.update_status_if_in(
            run_id,
            ConversationRunStatus.COMPLETED.value,
            (ConversationRunStatus.RUNNING.value,),
            None,
            final_output=final_output,
            usage=usage_payload(usage_stats),
            error=None,
        )
        if record is None:
            return None
        dispatch_conversation_event(
            RunStatusChangedEvent(
                task_id=record.task_id,
                run_id=run_id,
                status=ConversationRunStatus.COMPLETED,
                usage_stats=usage_stats,
            ),
        )
        return record

    def fail_run_if_running(
        self,
        run_id: int,
        end_reason: str | None = None,
        final_output: str | None = None,
        usage_stats: ConversationRunUsageStats | None = None,
    ) -> ConversationRunRecord | None:
        """Fail a running Conversation Run atomically.

        业务语义：仅 ``running`` 可进入 ``failed`` 终态；约束收敛在本方法（service 层），
        CRUD 层只做通用的「状态白名单 + 原子更新」。终态同时写入 ``final_output``，使复用
        同一工作流的子 Agent 即便失败，主 Agent 也能从委派结果中感知其终态输出。

        参数:
            run_id: 待失败落定的 Conversation Run 标识。
            end_reason: 可选失败原因。
            final_output: 可选，随终态一并写入的失败说明文本，供委派场景主 Agent 感知。
            usage_stats: 可选，运行用量统计，终态时按六键契约落库并发布事件。

        返回:
            成功失败落定时返回更新后的 ConversationRunRecord；run 已不是 running 时返回 None。

        异常:
            KeyError: 如果指定 run 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果底层更新失败。

        副作用:
            条件满足时更新 run 状态为 failed（并可选写入 end_reason 与 final_output），
            随后发布 FAILED 状态事件；事件同时携带同一份受控错误契约，使前端无需额外
            查询即可展示失败原因。
        """

        error = terminal_error(ConversationRunStatus.FAILED, end_reason)
        record = self._run.update_status_if_in(
            run_id,
            ConversationRunStatus.FAILED.value,
            (ConversationRunStatus.RUNNING.value,ConversationRunStatus.PENDING.value),
            end_reason,
            final_output=final_output,
            usage=usage_payload(usage_stats),
            error=error,
        )
        if record is None:
            return None
        dispatch_conversation_event(
            RunStatusChangedEvent(
                task_id=record.task_id,
                run_id=run_id,
                status=ConversationRunStatus.FAILED,
                end_reason=end_reason,
                usage_stats=usage_stats,
                error=error,
            ),
        )
        return record

    def cancel_run_if_running(
        self,
        run_id: int,
        end_reason: str = "user_cancelled",
        final_output: str | None = None,
        usage_stats: ConversationRunUsageStats | None = None,
    ) -> ConversationRunRecord | None:
        """将 active Run 标记 cancelled，并发布其 Transport 展示状态。

        终态同时写入 ``final_output``，使复用同一工作流的子 Agent 即便被取消，主 Agent
        也能从委派结果中感知其已产出（或被中断）的内容，而非仅看到一个空终态。

        参数:
            run_id: 待取消的 Conversation Run 标识。
            end_reason: 稳定的取消原因。
            final_output: 可选，随终态一并写入的取消说明/部分输出文本，供委派场景主 Agent 感知。
            usage_stats: 可选，运行用量统计，终态时按六键契约落库并发布事件。

        返回:
            成功落定时返回更新后的 ConversationRunRecord；run 已是终态时返回 None。

        异常:
            KeyError: run 不存在。
            sqlalchemy.exc.SQLAlchemyError: 写入失败。

        副作用:
            条件满足时将 ``pending`` / ``running`` 更新为 ``cancelled``（并可选写入
            end_reason、final_output 与用量），随后发布 CANCELLED 状态事件；事件同时携带
            同一份受控错误契约，使前端无需额外查询即可展示终止原因。
        """

        error = terminal_error(ConversationRunStatus.CANCELLED, end_reason)
        record = self._run.update_status_if_in(
            run_id,
            ConversationRunStatus.CANCELLED.value,
            (ConversationRunStatus.PENDING.value, ConversationRunStatus.RUNNING.value),
            end_reason,
            final_output=final_output,
            usage=usage_payload(usage_stats),
            error=error,
        )
        if record is None:
            return None
        dispatch_conversation_event(
            RunStatusChangedEvent(
                task_id=record.task_id,
                run_id=run_id,
                status=ConversationRunStatus.CANCELLED,
                end_reason=end_reason,
                usage_stats=usage_stats,
                error=error,
            ),
        )
        return record

    def cancel_run_for_startup_recovery(
        self, run_id: int, end_reason: str = "runtime_restarted"
    ) -> ConversationRunRecord | None:
        """Converge an orphaned Run without invoking the live observer.

        Startup recovery runs before process-local Child Agent sessions and waiters are
        available.  It therefore uses the same conditional canonical write but deliberately
        omits live notifications and follow-up scheduling.
        """

        record = self._run.update_status_if_in(
            run_id,
            ConversationRunStatus.CANCELLED.value,
            (ConversationRunStatus.PENDING.value, ConversationRunStatus.RUNNING.value),
            end_reason,
            error=terminal_error(ConversationRunStatus.CANCELLED, end_reason),
        )
        return record
