"""用于任务与 SSE 端点的 FastAPI 应用工厂。"""

from contextlib import asynccontextmanager
from typing import AsyncIterator

from app.api.dependencies import build_runtime
from app.api.sse import format_sse_event
from app.core.runtime.runner import AgentRuntime


def create_app(runtime: AgentRuntime = None):
    """创建 FastAPI 应用。

    参数:
        runtime: 可选的运行时依赖。省略时会构建默认的本地运行时。

    返回:
        配置了任务、事件、SSE 和取消路由的 FastAPI 应用。

    异常:
        RuntimeError: 当当前环境未安装 FastAPI 时抛出。

    副作用:
        构建运行时依赖并配置日志。
    """

    try:
        from fastapi import FastAPI, HTTPException
        from fastapi.responses import StreamingResponse
        from pydantic import BaseModel, field_validator
    except ImportError as exc:
        raise RuntimeError(
            "FastAPI is required to run the backend API. Install project dependencies first."
        ) from exc

    runtime = runtime or build_runtime()

    @asynccontextmanager
    async def lifespan(_app):
        """管理 FastAPI 应用生命周期并在关闭时释放运行时资源。

        参数:
            _app: FastAPI 应用实例，当前只用于满足 lifespan 协议。

        生成:
            应用运行期间的控制权。

        异常:
            无。Runtime 内部会记录关闭失败。

        副作用:
            关闭 Runtime 持有的 LangGraph checkpointer 等资源。
        """

        try:
            yield
        finally:
            runtime.close()

    app = FastAPI(title="coding-agent backend", lifespan=lifespan)

    class CreateTaskRequest(BaseModel):
        """校验任务创建请求体。

        参数:
            text: 非空的纯文本任务输入。
            session_id: 可选的会话标识。

        返回:
            Pydantic 请求模型。

        异常:
            ValueError: 当 ``text`` 为空白时抛出。

        副作用:
            无。
        """

        text: str
        session_id: str = None

        @field_validator("text")
        @classmethod
        def text_must_not_be_blank(cls, value: str) -> str:
            """校验任务文本不为空白。

            参数:
                value: 从请求体解析出的文本值。

            返回:
                校验通过时返回原始文本值。

            异常:
                ValueError: 当文本为空白时抛出。

            副作用:
                无。
            """

            if not value.strip():
                raise ValueError("text must not be blank")
            return value

    class ApprovalDecisionRequest(BaseModel):
        """校验审批决策请求体。

        参数:
            decision: 审批决策，必须是 approved 或 denied。
            reason: 可选的人类可读原因。
            idempotency_key: 前端生成的幂等键。

        返回:
            Pydantic 请求模型。

        异常:
            ValueError: 当 decision 或 idempotency_key 非法时抛出。

        副作用:
            无。
        """

        decision: str
        reason: str = None
        idempotency_key: str

        @field_validator("decision")
        @classmethod
        def decision_must_be_supported(cls, value: str) -> str:
            """校验审批决策值。

            参数:
                value: 从请求体解析出的审批决策。

            返回:
                校验通过的审批决策。

            异常:
                ValueError: 如果决策不是 approved 或 denied。

            副作用:
                无。
            """

            if value not in {"approved", "denied"}:
                raise ValueError("decision must be approved or denied")
            return value

        @field_validator("idempotency_key")
        @classmethod
        def idempotency_key_must_not_be_blank(cls, value: str) -> str:
            """校验幂等键不为空。

            参数:
                value: 从请求体解析出的幂等键。

            返回:
                校验通过的幂等键。

            异常:
                ValueError: 如果幂等键为空白。

            副作用:
                无。
            """

            if not value.strip():
                raise ValueError("idempotency_key must not be blank")
            return value

    @app.post("/tasks")
    async def create_task(payload: CreateTaskRequest) -> dict:
        """根据纯文本输入创建任务。

        参数:
            payload: 包含 text 与可选 session_id 的请求体。

        返回:
            创建后的任务状态。

        异常:
            HTTPException: 当输入文本缺失或为空白时抛出。

        副作用:
            在运行时存储中创建任务状态。
        """

        try:
            task = runtime.create_task(
                input_text=payload.text,
                session_id=payload.session_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return task.to_dict()

    @app.get("/health")
    async def get_health() -> dict:
        """返回后端健康状态与当前模型配置摘要。

        参数:
            无。

        返回:
            不含 secret 原文的健康状态字典。

        异常:
            无。

        副作用:
            无。
        """

        return runtime.backend_health()

    @app.get("/tasks/{task_id}")
    async def get_task(task_id: str) -> dict:
        """返回任务状态。

        参数:
            task_id: 来自路由的任务标识。

        返回:
            已存储的任务状态。

        异常:
            HTTPException: 当任务不存在时抛出。

        副作用:
            无。
        """

        try:
            return runtime.get_task(task_id).to_dict()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="task not found") from exc

    @app.get("/tasks/{task_id}/events")
    async def list_events(task_id: str) -> list:
        """返回任务的运行时事件。

        参数:
            task_id: 来自路由的任务标识。

        返回:
            该任务的有序事件列表。

        异常:
            HTTPException: 当任务不存在时抛出。

        副作用:
            无。
        """

        try:
            return [event.to_dict() for event in runtime.list_events(task_id)]
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="task not found") from exc

    @app.get("/tasks/{task_id}/checkpoints")
    async def list_checkpoints(task_id: str) -> list:
        """返回任务的 checkpoint。

        参数:
            task_id: 来自路由的任务标识。

        返回:
            该任务的有序 checkpoint 列表。

        异常:
            HTTPException: 当任务不存在时抛出。

        副作用:
            无。
        """

        try:
            return [checkpoint.to_dict() for checkpoint in runtime.list_checkpoints(task_id)]
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="task not found") from exc

    @app.get("/runs/recoverable")
    async def list_recoverable_runs() -> list:
        """返回可恢复或需要用户处理的运行记录。

        参数:
            无。

        返回:
            可恢复运行记录列表。

        异常:
            HTTPException: 当 Durable Run State 未配置时抛出。

        副作用:
            无。该接口只读取可恢复运行，不消费恢复命令。
        """

        try:
            return runtime.list_recoverable_runs()
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/runs/{run_id}/resume")
    async def resume_run(run_id: str) -> dict:
        """显式恢复指定 Durable Run。

        参数:
            run_id: 来自路由的运行标识。

        返回:
            恢复后的运行记录。

        异常:
            HTTPException: 当运行不存在或恢复能力未配置时抛出。

        副作用:
            重新排队该 run 遗留命令，消费恢复命令，并同步任务与运行状态。
        """

        try:
            return runtime.resume_run(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/tasks/{task_id}/approvals")
    async def list_task_approvals(task_id: str) -> list:
        """返回任务关联的待处理审批请求。

        参数:
            task_id: 来自路由的任务标识。

        返回:
            待处理审批请求列表。

        异常:
            HTTPException: 当任务不存在或审批服务不可用时抛出。

        副作用:
            无。
        """

        try:
            return runtime.list_pending_approvals(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="task not found") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/approvals/{approval_id}/decision")
    async def decide_approval(approval_id: str, payload: ApprovalDecisionRequest) -> dict:
        """处理用户审批决策。

        参数:
            approval_id: 来自路由的审批请求标识。
            payload: 审批决策请求体。

        返回:
            审批决策摘要。

        异常:
            HTTPException: 当审批不存在、请求非法或服务不可用时抛出。

        副作用:
            写入审批决策并创建恢复命令。
        """

        try:
            return runtime.decide_approval(
                approval_id=approval_id,
                decision=payload.decision,
                reason=payload.reason,
                idempotency_key=payload.idempotency_key,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="approval not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/tasks/{task_id}/stream")
    async def stream_task(task_id: str):
        """通过 SSE 流式返回任务的运行时事件。

        参数:
            task_id: 来自路由的任务标识。

        返回:
            发送 Server-Sent Events 的 StreamingResponse。

        异常:
            HTTPException: 当任务不存在时抛出。

        副作用:
            运行任务，并在流式推送事件期间修改任务状态。
        """

        try:
            runtime.get_task(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="task not found") from exc

        return StreamingResponse(
            _sse_events(runtime, task_id),
            media_type="text/event-stream",
        )

    @app.post("/tasks/{task_id}/cancel")
    async def cancel_task(task_id: str) -> dict:
        """将任务标记为已取消。

        参数:
            task_id: 来自路由的任务标识。

        返回:
            更新后的任务状态。

        异常:
            HTTPException: 当任务不存在时抛出。

        副作用:
            在运行时存储中更新任务状态。
        """

        try:
            task = runtime.cancel_task(task_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="task not found") from exc
        return task.to_dict()

    return app


async def _sse_events(runtime: AgentRuntime, task_id: str) -> AsyncIterator[str]:
    """将运行时事件转换为 SSE 传输格式字符串。

    参数:
        runtime: 产生任务事件的运行时。
        task_id: 待运行的任务标识。

    生成:
        SSE 格式的事件字符串。

    异常:
        KeyError: 当任务在流式开始前消失时抛出。

    副作用:
        执行运行时任务并存储产生的事件。
    """

    async for event in runtime.run_task(task_id):
        yield format_sse_event(event)
