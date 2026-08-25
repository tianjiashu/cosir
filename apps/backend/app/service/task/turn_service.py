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
        task_id: int,
        input_text: str,
        agent_id: str | None = None,
        status: str = "pending",
        product_id: str | None = None,
        model_id: str | None = None,
        thinking: bool | None = None,
        reasoning_effort: str | None = None,
        paths: list[str] | None = None,
    ) -> TurnRecord:
        """Create a turn and update the parent task's latest turn info.

        参数:
            task_id: 所属任务标识。
            input_text: 本轮用户输入文本。
            status: 初始状态，默认 ``"pending"``。
            agent_id: 可选，本轮回绑定的 agent 标识；为 None 时回退到默认 ``"developer"``。
            model_id: 可选，本 turn 请求的模型名（litellm 路由名）；None 表示用户
                未选择模型（设计阶段 1.5：5 个内置 profile 不再内置默认模型，前端优先
                校验、后端兜底报错）。
            thinking: 可选，是否开启思考模式；None 表示用户未指定。
            reasoning_effort: 可选，思考努力等级（low/high/max）；None 表示用户未指定。
            paths: 可选，本轮涉及的文件路径集合（JSON 文本存储），None 表示无文件涉及。

        返回:
            新创建的 ``TurnRecord``。

        异常:
            ValueError: 如果 ``input_text`` 为空或全空白。
            ModelNotConfiguredError: 如果请求模型未选择 / 未收录 / 归属厂商禁用 /
                Key 未配置（设计 §6.4 两段式 ① service 期预解析，由 API 层捕获为
                HTTP 422；child 委派路径无 HTTP 上下文，由 ``delegation_executor``
                捕获并把 delegation 置 failed）。
            sqlalchemy.exc.SQLAlchemyError: 如果底层写入失败。

        副作用:
            向 ``turns`` 表插入一行（``model_name`` 落库为解析后的最终模型名
            ——``LLMRuntimeConfig.model_name``，取自 DB ``models.model_name``，
            保证时间线可追溯到真实模型，D11）；更新所属任务最新轮次信息。
        """


        turn = self._turn.create(
            task_id,
            input_text,
            status,
            agent_id=agent_id,
            product_id=product_id,
            model_id=model_id,
            thinking=thinking,
            reasoning_effort=reasoning_effort,
            paths=paths,
        )
        return turn

    def get_turn(self, turn_id: int) -> TurnRecord:
        return self._turn.get(turn_id)

    def list_turns_for_task(self, task_id: int) -> list[TurnRecord]:
        return self._turn.list_by_task(task_id)

    def cancel_turn_if_active(self, turn_id: int, end_reason: str) -> TurnRecord | None:
        """Cancel a pending/running turn atomically.

        业务语义：仅 ``pending`` / ``running`` 可进入 ``cancelled`` 终态；该约束收敛在
        本方法（service 层），CRUD 层只做通用的「状态白名单 + 原子更新」。

        参数:
            turn_id: 待取消的 turn 标识。
            end_reason: 取消原因。

        返回:
            成功取消时返回更新后的 TurnRecord；turn 已处于非 active 状态时返回 None。

        异常:
            KeyError: 如果指定 turn 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果底层更新失败。

        副作用:
            条件满足时更新 turn 状态为 cancelled 并写入 end_reason。
        """

        return self._turn.update_status_if_in(
            turn_id,
            target_status="cancelled",
            allowed_statuses=("pending", "running"),
            end_reason=end_reason,
        )

    def complete_turn_if_running(self, turn_id: int, response_text: str) -> TurnRecord | None:
        """Complete a running turn and persist its response atomically.

        业务语义：仅 ``running`` 可进入 ``completed`` 终态并落库回复文本；约束收敛在
        本方法（service 层），CRUD 层只做通用的「状态白名单 + 原子更新」。

        参数:
            turn_id: 待完成的 turn 标识。
            response_text: Agent 最终回复文本。

        返回:
            成功完成时返回更新后的 TurnRecord；turn 已不是 running 时返回 None。

        异常:
            KeyError: 如果指定 turn 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果底层更新失败。

        副作用:
            条件满足时更新 turn 状态为 completed 并写入 response_text。
        """

        return self._turn.update_status_if_in(
            turn_id,
            target_status="completed",
            allowed_statuses=("running",),
            response_text=response_text,
        )

    def fail_turn_if_running(
        self, turn_id: int, end_reason: str | None = None
    ) -> TurnRecord | None:
        """Fail a running turn atomically.

        业务语义：仅 ``running`` 可进入 ``failed`` 终态；约束收敛在本方法（service 层），
        CRUD 层只做通用的「状态白名单 + 原子更新」。

        参数:
            turn_id: 待失败落定的 turn 标识。
            end_reason: 可选失败原因。

        返回:
            成功失败落定时返回更新后的 TurnRecord；turn 已不是 running 时返回 None。

        异常:
            KeyError: 如果指定 turn 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果底层更新失败。

        副作用:
            条件满足时更新 turn 状态为 failed（并可选写入 end_reason）。
        """

        return self._turn.update_status_if_in(
            turn_id,
            target_status="failed",
            allowed_statuses=("running",),
            end_reason=end_reason,
        )

    def has_turn_status(self, turn_id: int | None, status: str) -> bool:
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

    def claim_pending_turn(self, turn_id: int) -> bool:
        """以乐观锁方式抢占 pending turn 为 running。

        业务语义：仅 ``pending`` 可抢占为 ``running``，该约束收敛在本方法（service 层），
        CRUD 层只做通用的「状态白名单 + 原子更新」。返回 ``bool`` 表示本次是否成功抢占，
        供上层（runner / delegation executor）判断「是否由我执行该 turn」。

        参数:
            turn_id: 待抢占的 turn 标识。

        返回:
            抢占成功（本次确实把 pending 更新为 running）返回 True；turn 已被他人抢占或
            非 pending 状态返回 False。

        异常:
            KeyError: 如果指定 turn 不存在。
            sqlalchemy.exc.SQLAlchemyError: 如果底层更新失败。

        副作用:
            条件满足时更新 turn 状态为 running。
        """

        return (
            self._turn.update_status_if_in(
                turn_id,
                target_status="running",
                allowed_statuses=("pending",),
            )
            is not None
        )

    def load_turn_messages(self, turn_id: int) -> list[RuntimeMessage]:
        """Load a turn's ordered message trajectory; empty list if none stored."""

        return self._message.load_messages(turn_id)

    def append_turn_message(
        self,
        turn_id: int,
        message: RuntimeMessage,
        sequence: int,
        in_context: bool = True,
    ) -> None:
        """Incremental single-row append of one runtime message (cross-turn memory).

        参数:
            turn_id: 目标轮次标识。
            message: 单条模型无关的运行时消息（用户提问 / 模型回复 / 工具观察）。
            sequence: 轮内自增序号，由编排层 ``RuntimeOperations`` 维护。
            in_context: 是纳入模型在上下文内。（默认 True）。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 写入失败（透传给调用方）。

        副作用:
            在 ``turn_messages`` 表追加一行，不影响同 turn 已有行（与 ``clear_turn_messages``
            的整轮清空语义互补，组合实现 turn 重跑幂等）。
        """

        self._message.append_message(
            turn_id, message, sequence, in_context=in_context
        )

    def clear_turn_messages(self, turn_id: int) -> None:
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
