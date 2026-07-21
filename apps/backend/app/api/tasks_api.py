"""任务领域端点。

包含任务查询、事件与 checkpoint 取消端点。所有端点通过模块级 ``@app.*`` 装饰器
直接注册到 ``app.api.app.app`` 单例中。

分层约定：任务查询与轮次列表直接依赖 ``TaskService``；事件回放、取消与健康检查
属于运行时执行 / 生命周期职责，仍依赖 ``AgentRuntime``。
"""

from fastapi import Depends, HTTPException

from app.api.app import app
from app.api.dependencies import get_runtime, get_task_service
from app.core.runtime.runner import AgentRuntime
from app.service.task.task_service import TaskService


@app.get("/health")
async def get_health(runtime: AgentRuntime = Depends(get_runtime)) -> dict:
    """返回后端健康状态与当前模型配置摘要。

    参数:
        runtime: 通过依赖注入的运行时单例。

    返回:
        不含 secret 原文的健康状态字典。

    异常:
        无。

    副作用:
        无。
    """

    return runtime.backend_health()


@app.get("/tasks/{task_id}")
async def get_task(
    task_id: str,
    task_service: TaskService = Depends(get_task_service),
) -> dict:
    """返回任务状态。

    参数:
        task_id: 来自路由的任务标识。
        task_service: 通过依赖注入的任务 service。

    返回:
        已存储的任务状态。

    异常:
        HTTPException: 当任务不存在时抛出。

    副作用:
        无。
    """

    try:
        return task_service.get_task(task_id).to_dict()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
