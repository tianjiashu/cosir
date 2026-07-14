"""Human-in-loop 输入业务服务。"""

import logging
from typing import Any, Dict, Optional

from app.human_input.records import HumanInputRequestRecord, HumanInputResponseRecord
from app.human_input.store import HumanInputStore
from app.runs.resume import ResumeDispatcher
from app.runs.store import DurableRunStore


class HumanInputService:
    """连接人工输入请求、运行等待状态和恢复命令。"""

    def __init__(
        self,
        human_input_store: HumanInputStore,
        run_store: DurableRunStore,
        resume_dispatcher: ResumeDispatcher,
        logger: logging.Logger,
    ) -> None:
        """初始化人工输入服务。

        参数:
            human_input_store: 人工输入仓储。
            run_store: Durable Run State 仓储。
            resume_dispatcher: 恢复命令分发器。
            logger: 日志器。

        返回:
            无。

        异常:
            无。

        副作用:
            保存依赖项。
        """

        self._human_input_store = human_input_store
        self._run_store = run_store
        self._resume_dispatcher = resume_dispatcher
        self._logger = logger

    def request_input(
        self,
        run_id: str,
        prompt: str,
        schema: Dict[str, Any],
        step_id: Optional[str] = None,
    ) -> HumanInputRequestRecord:
        """创建人工输入请求并将运行标记为等待。

        参数:
            run_id: 运行标识符。
            prompt: 展示给用户的问题。
            schema: 响应结构。
            step_id: 可选步骤标识符。

        返回:
            创建的人工输入请求。

        异常:
            KeyError: 如果运行不存在。
            InvalidRunTransition: 如果运行不能进入 waiting。

        副作用:
            写入请求、更新运行状态并记录日志。
        """

        request = self._human_input_store.create_request(
            run_id=run_id,
            prompt=prompt,
            schema=schema,
            step_id=step_id,
        )
        self._run_store.mark_status(
            run_id,
            "waiting",
            wait_reason="human_input",
            active_step_id=step_id,
            active_wait_id=request.request_id,
        )
        self._logger.info("human_input_requested run_id=%s request_id=%s", run_id, request.request_id)
        return request

    def respond(
        self,
        request_id: str,
        response: Dict[str, Any],
        idempotency_key: str,
    ) -> HumanInputResponseRecord:
        """记录人工输入响应并分发恢复命令。

        参数:
            request_id: 人工输入请求标识符。
            response: 用户响应载荷。
            idempotency_key: 幂等键。

        返回:
            人工输入响应记录。

        异常:
            KeyError: 如果请求不存在。

        副作用:
            写入响应、创建恢复命令并记录日志。
        """

        request = self._human_input_store.get_request(request_id)
        record = self._human_input_store.record_response(request_id, response, idempotency_key)
        self._resume_dispatcher.dispatch(
            run_id=request.run_id,
            action="provide_human_input",
            payload={"request_id": request_id, "response": response},
            idempotency_key=f"resume:{idempotency_key}",
        )
        self._logger.info("human_input_received run_id=%s request_id=%s", request.run_id, request_id)
        return record
