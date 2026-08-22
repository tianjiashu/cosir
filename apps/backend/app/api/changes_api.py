"""Task 级变更集 API（查询累积变更、单/多文件撤销与保留）。

单一职责：把 ``service.task.change_set`` 的能力暴露为 HTTP 端点，只做入参校验、
异常到状态码的映射与响应投影，不承载业务规则。端点经模块级 ``@app.*``
装饰器注册到 ``app.api.app.app`` 单例上（与同目录其它域路由一致）。
"""

from fastapi import HTTPException, Query

from app.api.schemas.request.ChangeSetActionRequest import ChangeSetActionRequest
from app.api.schemas.response.ChangeSetResponse import ChangeSetResponse
from app.app import app
from app.service.task.change_set import (
    ChangeSetConflictError,
    keep_file,
    query_change_set,
    revert_file,
)
from app.tools.tool_handler.patch.patch_apply import PatchApplyError


@app.get("/tasks/{task_id}/changes", response_model=ChangeSetResponse)
async def get_changes(
    task_id: str,
    checkpoint: str | None = Query(default=None),
    include_running: bool = Query(default=True),
) -> ChangeSetResponse:
    """查询某 task 的累积文件变更集。

    参数:
        task_id: 任务标识。
        checkpoint: 可选检查点 turn 标识，只返回到该 turn（含）为止的累积变更。
        include_running: 是否纳入运行中（``stable=0``）变更；默认 True，使工具执行中
            产生的变更可被实时展示与撤销。置 False 可回到「仅已稳定」的旧视图。

    返回:
        ``ChangeSetResponse``。

    异常:
        HTTPException(404): 当 checkpoint 不属于该 task 时。

    副作用:
        只读查询。
    """
    try:
        return ChangeSetResponse.from_change_set(
            query_change_set(task_id, checkpoint, include_running=include_running)
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/tasks/{task_id}/changes/keep", response_model=ChangeSetResponse)
async def keep_changes(task_id: str, request: ChangeSetActionRequest) -> ChangeSetResponse:
    """把一批文件的最新变更标记为「保留」。

    参数:
        task_id: 任务标识。
        request: 含 ``paths`` 的请求体。

    返回:
        操作后的完整 ``ChangeSetResponse``，供前端直接替换本地状态。

    异常:
        HTTPException(404): 当任一路径没有任何变更快照（含运行中）时。
        HTTPException(409): 当某快照 ``status`` 已被并发改态（CAS miss，lost update 防护）时。

    副作用:
        改写 ``file_snapshots`` 的 status。
    """
    try:
        for path in request.paths:
            keep_file(task_id, path)
    except ChangeSetConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ChangeSetResponse.from_change_set(
        query_change_set(task_id, include_running=True)
    )


@app.post("/tasks/{task_id}/changes/revert", response_model=ChangeSetResponse)
async def revert_changes(task_id: str, request: ChangeSetActionRequest) -> ChangeSetResponse:
    """撤销一批文件的最新变更，把它们还原到变更之前。

    参数:
        task_id: 任务标识。
        request: 含 ``paths`` 的请求体。

    返回:
        操作后的完整 ``ChangeSetResponse``。

    异常:
        HTTPException(404): 当任一路径没有任何变更快照时。
        HTTPException(409): 当反向操作应用失败（磁盘已被外部改动等）时。
        HTTPException(409): 当某快照 ``status`` 已被并发改态（CAS miss，lost update 防护）时。

    副作用:
        修改 workspace 内文件；改写 ``file_snapshots`` 的 status 与 reverted_at；
        对每个成功撤销的文件广播 ``file_change_updated`` 事件。
    """
    try:
        for path in request.paths:
            await revert_file(task_id, path)
    except ChangeSetConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PatchApplyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ChangeSetResponse.from_change_set(
        query_change_set(task_id, include_running=True)
    )
