"""Turn orchestration service.

单一职责：编排轮次的创建与管理——创建轮次时同步更新所属任务的
最新轮次 ID 和消息预览；并透传每轮消息轨迹的读写（跨轮记忆）。

职责边界：
- 负责：轮次创建（含任务最新轮次更新）、轮次查询与状态更新、消息轨迹读写透传。
- 不负责：直接 SQL 操作（委托给 ``TurnCrud``/``TaskCrud``/``TurnMessageCrud``）。
"""

from app.config.logging.logger import log
from app.models import RuntimeMessage, TurnRecord
from app.service import depends as service_depends
from app.utils.datetime_utils import preview


class TurnService:
    """Orchestrate turn creation, queries, status management, and message store."""

    def __init__(self) -> None:
        """初始化轮次 service。

        参数:
            无。

        返回:
            无。

        异常:
            RuntimeError: 如果 storage 尚未初始化。

        副作用:
            从 service 依赖入口取得 CRUD 单例并保存引用。
        """

        self._task = service_depends.get_task_crud()
        self._turn = service_depends.get_turn_crud()
        self._message = service_depends.get_turn_message_crud()

    def create_turn(
        self,
        task_id: str,
        input_text: str,
        status: str = "pending",
        agent_id: str | None = None,
    ) -> TurnRecord:
        """Create a turn and update the parent task's latest turn info.

        参数:
            task_id: 所属任务标识。
            input_text: 本轮用户输入文本。
            status: 初始状态，默认 ``"pending"``。
            agent_id: 可选，本轮回绑定的 agent 标识；为 None 时回退到任务默认归属。
        """

        if not isinstance(input_text, str) or not input_text.strip():
            raise ValueError("input_text must be a non-empty string")
        turn = self._turn.create(task_id, input_text, status, agent_id=agent_id)
        self._task.update_latest_turn(task_id, turn.turn_id, preview(input_text))
        return turn

    def get_turn(self, turn_id: str) -> TurnRecord:
        return self._turn.get(turn_id)

    def list_turns_for_task(self, task_id: str) -> list[TurnRecord]:
        return self._turn.list_by_task(task_id)

    def get_turn_for_task(self, task_id: str) -> TurnRecord:
        return self._turn.get_latest_turn(task_id)

    def get_latest_turn(self, task_id: str) -> TurnRecord:
        return self._turn.get_latest_turn(task_id)

    def update_turn_status(
        self, turn_id: str, status: str, end_reason: str | None = None
    ) -> TurnRecord:
        return self._turn.update_status(turn_id, status, end_reason)

    def cancel_turn_if_active(self, turn_id: str, end_reason: str) -> TurnRecord | None:
        """Cancel a pending/running turn atomically.

        参数:
            turn_id: 待取消的 turn 标识。
            end_reason: 取消原因。

        返回:
            成功取消时返回更新后的 TurnRecord；turn 已处于非 active 状态时返回 None。

        异常:
            KeyError: 如果指定 turn 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果底层更新失败。

        副作用:
            条件满足时更新 turn 状态为 cancelled。
        """

        return self._turn.cancel_if_active(turn_id, end_reason)

    def complete_turn_if_running(self, turn_id: str, response_text: str) -> TurnRecord | None:
        """Complete a running turn and persist its response atomically.

        参数:
            turn_id: 待完成的 turn 标识。
            response_text: Agent 最终回复文本。

        返回:
            成功完成时返回更新后的 TurnRecord；turn 已不是 running 时返回 None。

        异常:
            KeyError: 如果指定 turn 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果底层更新失败。

        副作用:
            条件满足时更新 turn 状态和回复文本。
        """

        return self._turn.complete_if_running(turn_id, response_text)

    def fail_turn_if_running(
        self, turn_id: str, end_reason: str | None = None
    ) -> TurnRecord | None:
        """Fail a running turn atomically.

        参数:
            turn_id: 待失败落定的 turn 标识。
            end_reason: 可选失败原因。

        返回:
            成功失败落定时返回更新后的 TurnRecord；turn 已不是 running 时返回 None。

        异常:
            KeyError: 如果指定 turn 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果底层更新失败。

        副作用:
            条件满足时更新 turn 状态。
        """

        return self._turn.fail_if_running(turn_id, end_reason)

    def update_turn_response(self, turn_id: str, response_text: str | None) -> TurnRecord:
        """把轮次的 Agent 回复文本落库，供历史接口直接读取。"""

        return self._turn.update_response(turn_id, response_text)

    def has_turn_status(self, turn_id: str | None, status: str) -> bool:
        """Return whether the turn currently has the requested status.

        参数:
            turn_id: 目标轮次标识，允许为 ``None``（调用方缺陷时按非目标状态处理）。
            status: 待比对的状态字符串（如 ``cancelled`` / ``running``）。

        返回:
            轮次存在且状态匹配时返回 True；轮次不存在或状态不符时返回 False。

        异常:
            无。``turn_id`` 为 ``None`` 属于调用方缺陷，记 warn 后返回 False 而非抛错，
            避免取消检测在非法输入下静默失效；turn 已删除/不存在按「非目标状态」处理
            （``KeyError`` 转 ``False``），防止取消检测因 ``KeyError`` 被上层吞掉而失效，
            导致已取消的 turn 仍继续进入工具执行。
        """

        if turn_id is None:
            log.warning(
                "turn_status_null_id",
                extra={
                    "msg": "has_turn_status 收到空 turn_id，按非目标状态处理（调用方缺陷）",
                    "data": {"turn_id": None, "status": status},
                },
            )
            return False
        try:
            return self._turn.get(turn_id).status == status
        except KeyError:
            return False

    def claim_pending_turn(self, turn_id: str) -> bool:
        return self._turn.claim_pending(turn_id)

    def load_turn_messages(self, turn_id: str) -> list[RuntimeMessage]:
        """Load a turn's ordered message trajectory; empty list if none stored."""

        return self._message.load_messages(turn_id)

    def append_turn_message(
        self, turn_id: str, message: RuntimeMessage, sequence: int
    ) -> None:
        """Incremental single-row append of one runtime message (cross-turn memory).

        参数:
            turn_id: 目标轮次标识。
            message: 单条模型无关的运行时消息（用户提问 / 模型回复 / 工具观察）。
            sequence: 轮内自增序号，由编排层 ``RuntimeOperations`` 维护。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 写入失败（透传给调用方）。

        副作用:
            在 ``turn_messages`` 表追加一行，不影响同 turn 已有行（与 ``clear_turn_messages``
            的整轮清空语义互补，组合实现 turn 重跑幂等）。
        """

        self._message.append_message(turn_id, message, sequence)

    def clear_turn_messages(self, turn_id: str) -> None:
        """Delete all stored messages for a turn (used before re-running a turn).

        参数:
            turn_id: 目标轮次标识。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 删除失败（透传底层 CRUD 异常）。

        副作用:
            删除 ``turn_messages`` 表中该 turn 的全部行；仅清本 turn，不影响其它 turn。
        """

        self._message.clear_turn_messages(turn_id)
