"""Task 级变更集 API（查询累积变更、单/多文件撤销与保留）。

单一职责：把 ``change_set_service`` 的能力暴露为 HTTP 端点，只做入参校验、
异常到状态码的映射与响应投影，不承载业务规则。端点经模块级 ``@app.*``
装饰器注册到 ``app.api.app.app`` 单例上（与同目录其它域路由一致）。
"""

from fastapi import HTTPException, Query

from app.api.app import app
from app.api.schemas.request.ChangeSetActionRequest import ChangeSetActionRequest
from app.api.schemas.response.ChangeSetResponse import ChangeSetResponse
from app.service.task import change_set_service
from app.tools.tool_handler.patch.patch_apply import PatchApplyError


@app.get("/tasks/{task_id}/changes", response_model=ChangeSetResponse)
async def get_changes(
    task_id: str, checkpoint: str | None = Query(default=None)
) -> ChangeSetResponse:
    """查询某 task 的累积文件变更集。

    参数:
        task_id: 任务标识。
        checkpoint: 可选检查点 turn 标识，只返回到该 turn（含）为止的累积变更。

    返回:
        ``ChangeSetResponse``。

    异常:
        HTTPException(404): 当 checkpoint 不属于该 task 时。

    副作用:
        只读查询。
    """
    try:
        return ChangeSetResponse.from_change_set(
            change_set_service.query_change_set(task_id, checkpoint)
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
        HTTPException(404): 当任一路径没有已稳定变更时。

    副作用:
        改写 ``file_snapshots`` 的 status。
    """
    try:
        for path in request.paths:
            change_set_service.keep_file(task_id, path)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ChangeSetResponse.from_change_set(change_set_service.query_change_set(task_id))


@app.post("/tasks/{task_id}/changes/revert", response_model=ChangeSetResponse)
async def revert_changes(task_id: str, request: ChangeSetActionRequest) -> ChangeSetResponse:
    """撤销一批文件的最新变更，把它们还原到变更之前。

    参数:
        task_id: 任务标识。
        request: 含 ``paths`` 的请求体。

    返回:
        操作后的完整 ``ChangeSetResponse``。

    异常:
        HTTPException(404): 当任一路径没有已稳定变更时。
        HTTPException(409): 当反向操作应用失败（磁盘已被外部改动等）时。

    副作用:
        修改 workspace 内文件；改写 ``file_snapshots`` 的 status 与 reverted_at。
    """
    try:
        for path in request.paths:
            await change_set_service.revert_file(task_id, path)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PatchApplyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ChangeSetResponse.from_change_set(change_set_service.query_change_set(task_id))
