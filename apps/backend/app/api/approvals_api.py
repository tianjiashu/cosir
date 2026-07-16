"""审批域端点。

包含工具权限审批的查询与决策端点。所有端点通过模块级 ``@app.*`` 装饰器直接
注册到 ``app.api.app.app`` 单例上。
"""

from fastapi import Depends, HTTPException

from app.api.app import app
from app.api.dependencies import get_runtime
from app.api.schemas import ApprovalDecisionRequest
from app.core.runtime.runner import AgentRuntime


@app.get("/tasks/{task_id}/approvals")
async def list_task_approvals(task_id: str, runtime: AgentRuntime = Depends(get_runtime)) -> list:
    """返回任务关联的待处理审批请求。

    参数:
        task_id: 来自路由的任务标识。
        runtime: 通过依赖注入的运行时单例。

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
async def decide_approval(
    approval_id: str,
    payload: ApprovalDecisionRequest,
    runtime: AgentRuntime = Depends(get_runtime),
) -> dict:
    """处理用户审批决策。

    参数:
        approval_id: 来自路由的审批请求标识。
        payload: 审批决策请求体。
        runtime: 通过依赖注入的运行时单例。

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
