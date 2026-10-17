"""workspace 文件浏览接口；文件内容只读，浏览可见性设置由独立命令修改。"""

import asyncio
import json
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager
from typing import Annotated, TypeVar

import anyio
from fastapi import Depends, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse

from app.api.schemas.request.SearchRequest import SearchRequest
from app.api.schemas.request.TextSearchRequest import TextSearchRequest
from app.api.schemas.request.VisibilityRequest import VisibilityRequest
from app.api.schemas.request.WatchRequest import WatchRequest
from app.api.schemas.response.BrowserVisibilityResponse import BrowserVisibilityResponse
from app.api.schemas.response.DirectoryResponse import DirectoryResponse
from app.api.schemas.response.FileInfoResponse import FileInfoResponse
from app.api.schemas.response.TextResponse import TextResponse
from app.api.schemas.response.VisibilityChangeResponse import VisibilityChangeResponse
from app.api.schemas.response.WorkspaceResourceResponse import WorkspaceResourceResponse
from app.app import app
from app.service.depends import get_workspace_file_watcher, get_workspace_files_service
from app.service.workspace_files import (
    FileConflictError,
    WorkspaceFileWatcher,
    WorkspaceFilesService,
)

Files = Annotated[WorkspaceFilesService, Depends(get_workspace_files_service)]
Watcher = Annotated[WorkspaceFileWatcher, Depends(get_workspace_file_watcher)]
T = TypeVar("T")


@contextmanager
def file_errors() -> Iterator[None]:
    """文件系统错误在 HTTP 边界唯一映射；流启动后的错误通过流内 error 收敛。"""
    try:
        yield
    except (KeyError, FileNotFoundError) as exc:
        raise HTTPException(404, "文件或工作区已不存在") from exc
    except PermissionError as exc:
        raise HTTPException(403, "无法访问文件，或链接目标位于工作区之外") from exc
    except FileConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except UnicodeError as exc:
        raise HTTPException(422, "文本解码失败，请选择其他编码") from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(422, "文件无法读取，请刷新后重试") from exc


async def run_read(request: Request, operation: Callable[[], T]) -> T:
    """同步磁盘读取移出事件循环；长搜索另有协作取消，普通读取有界。"""
    with file_errors():
        return await asyncio.to_thread(operation)


async def run_cancellable_read(request: Request, operation: Callable[[threading.Event], T]) -> T:
    """HTTP 断开显式取消目录扫描或旧编码定位，后台线程不会继续遍历整盘。"""
    cancelled = threading.Event()
    work = asyncio.create_task(run_read(request, lambda: operation(cancelled)))
    try:
        while not work.done():
            await asyncio.wait({work}, timeout=0.1)
            if await request.is_disconnected():
                raise HTTPException(499, "文件请求已取消")
        return await work
    finally:
        cancelled.set()
        if not work.done():
            work.cancel()


@app.get("/workspaces/{workspace_id}/files/visibility")
async def visibility(workspace_id: int, files: Files) -> BrowserVisibilityResponse:
    with file_errors():
        return BrowserVisibilityResponse.from_domain(
            await asyncio.to_thread(files.visibility, workspace_id)
        )


@app.patch("/workspaces/{workspace_id}/files/visibility")
async def change_visibility(
    workspace_id: int, payload: VisibilityRequest, files: Files
) -> VisibilityChangeResponse:
    with file_errors():
        return VisibilityChangeResponse.from_domain(
            await asyncio.to_thread(
                files.change_visibility,
                workspace_id,
                payload.paths,
                ignored=payload.ignored,
            )
        )


@app.get("/workspaces/{workspace_id}/files/directory")
async def directory(
    workspace_id: int,
    request: Request,
    files: Files,
    path: str = "",
    cursor: str | None = None,
    limit: int = Query(500, ge=1, le=2000),
) -> DirectoryResponse:
    return DirectoryResponse.from_domain(
        await run_cancellable_read(
            request, lambda cancelled: files.directory(workspace_id, path, cursor, limit, cancelled)
        )
    )


@app.get("/workspaces/{workspace_id}/files/info")
async def info(workspace_id: int, request: Request, files: Files, path: str) -> FileInfoResponse:
    return FileInfoResponse.from_domain(
        await run_read(request, lambda: files.info(workspace_id, path))
    )


@app.get("/workspaces/{workspace_id}/files/text")
async def text(
    workspace_id: int,
    request: Request,
    files: Files,
    path: str,
    offset: int = Query(0, ge=0),
    encoding: str = "utf-8",
    version: str | None = None,
    tail: bool = False,
    before: int | None = Query(None, ge=0),
) -> TextResponse:
    return TextResponse.from_domain(
        await run_cancellable_read(
            request,
            lambda cancelled: files.text(
                workspace_id, path, offset, encoding, version, tail, before, cancelled
            ),
        )
    )


@app.get("/workspaces/{workspace_id}/files/resource", response_model=None)
async def resource(
    workspace_id: int, request: Request, files: Files, path: str, version: str | None = None
) -> Response:
    metadata = await run_read(request, lambda: files.info(workspace_id, path))
    with file_errors():
        if version and metadata.version != version:
            raise FileConflictError("文件已变化，请重新加载")
        if metadata.kind not in {"image", "svg", "pdf"}:
            raise ValueError("此文件使用文本或元信息预览")
        resolved = await asyncio.to_thread(files.path, workspace_id, path)
    if metadata.kind == "svg" and metadata.size > 8 * 1024 * 1024:
        raise HTTPException(413, "SVG 超出图片解析预算，请切换源码分段阅读")
    root = await run_read(request, lambda: files.root(workspace_id))
    return WorkspaceResourceResponse(root, resolved, metadata)


def line(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False) + "\n").encode()


@app.post("/workspaces/{workspace_id}/files/search")
async def search(
    workspace_id: int, payload: SearchRequest, request: Request, files: Files
) -> StreamingResponse:
    await run_read(request, lambda: files.root(workspace_id))
    cancelled = threading.Event()
    iterator = files.search_pages(
        workspace_id, payload.query, payload.include_directories, payload.offset, 1000, cancelled
    )

    async def stream() -> AsyncIterator[bytes]:
        try:
            while not await request.is_disconnected():
                batch = await asyncio.to_thread(next, iterator, None)
                if batch is None:
                    break
                yield line(batch)
        except (OSError, ValueError):
            yield line({"error": "搜索期间工作区或浏览配置不可用，请检查后重试"})
        finally:
            cancelled.set()

    return StreamingResponse(
        stream(), media_type="application/x-ndjson", headers={"Cache-Control": "no-store"}
    )


@app.post("/workspaces/{workspace_id}/files/watch")
async def watch(
    workspace_id: int, payload: WatchRequest, request: Request, watcher: Watcher
) -> StreamingResponse:
    with file_errors():
        subscription = await anyio.to_thread.run_sync(
            watcher.subscribe, workspace_id, payload.paths, asyncio.get_running_loop()
        )

    async def stream() -> AsyncIterator[bytes]:
        try:
            yield line({"type": "ready", "polling": subscription.polling})
            last_reconcile = time.monotonic()
            while not subscription.closed:
                try:
                    await asyncio.wait_for(subscription.changed.wait(), timeout=2)
                    await asyncio.sleep(0.15)
                    subscription.changed.clear()
                    yield line({"type": "changed"})
                except TimeoutError:
                    # 周期失效也覆盖原生监听丢事件、目录删除重建、休眠恢复。
                    polling = await asyncio.to_thread(watcher.check_health, subscription)
                    reconcile = polling or time.monotonic() - last_reconcile >= 15
                    if reconcile:
                        last_reconcile = time.monotonic()
                    yield line(
                        {"type": "reconcile" if reconcile else "heartbeat", "polling": polling}
                    )
            yield line({"type": "closed"})
        finally:
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(watcher.unsubscribe, subscription)

    return StreamingResponse(
        stream(), media_type="application/x-ndjson", headers={"Cache-Control": "no-store"}
    )


@app.post("/workspaces/{workspace_id}/files/find-text")
async def find_text(
    workspace_id: int, payload: TextSearchRequest, request: Request, files: Files
) -> StreamingResponse:
    cancelled = threading.Event()
    iterator = await run_read(
        request,
        lambda: files.find_text_pages(
            workspace_id,
            payload.path,
            payload.query,
            payload.encoding,
            payload.version,
            payload.offset,
            500,
            cancelled,
        ),
    )

    async def stream() -> AsyncIterator[bytes]:
        try:
            while not await request.is_disconnected():
                batch = await asyncio.to_thread(next, iterator, None)
                if batch is None:
                    break
                yield line(batch)
        except (OSError, UnicodeError, ValueError, FileConflictError) as exc:
            yield line({"error": str(exc), "complete": True, "matches": []})
        finally:
            cancelled.set()

    return StreamingResponse(
        stream(), media_type="application/x-ndjson", headers={"Cache-Control": "no-store"}
    )
