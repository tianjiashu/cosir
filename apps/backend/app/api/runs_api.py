"""运行域端点。

包含 Durable Run State 相关的可恢复运行查询与显式恢复端点。所有端点通过
模块级 ``@app.*`` 装饰器直接注册到 ``app.api.app.app`` 单例上。
"""

from fastapi import Depends, HTTPException

from app.api.app import app
from app.api.dependencies import get_runtime
from app.core.runtime.runner import AgentRuntime


@app.get("/runs/recoverable")
async def list_recoverable_runs(runtime: AgentRuntime = Depends(get_runtime)) -> list:
    """返回可恢复或需要用户处理的运行记录。

    参数:
        runtime: 通过依赖注入的运行时单例。

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
async def resume_run(run_id: str, runtime: AgentRuntime = Depends(get_runtime)) -> dict:
    """显式恢复指定 Durable Run。

    参数:
        run_id: 来自路由的运行标识。
        runtime: 通过依赖注入的运行时单例。

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
