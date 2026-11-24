"""workspace 文件浏览接口；文件内容只读，浏览可见性设置由独立命令修改。"""

import asyncio
import json
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager
from typing import Annotated, TypeVar

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
from app.service.depends import get_workspace_files_service
from app.service.workspace_files import FileConflictError, WatchedPaths, WorkspaceFilesService
from app.service.workspace_files.content.image_preview import ImagePreview

Files = Annotated[WorkspaceFilesService, Depends(get_workspace_files_service)]
T = TypeVar("T")


@contextmanager
def file_errors() -> Iterator[None]:
    """文件系统错误在 HTTP 边界唯一映射；流启动后的错误通过流内 error 收敛。

    两种「不存在」带不同的错误码：``FILE_NOT_FOUND`` 是目标的一种状态（已删除、移动或从未存在），前端
    据此不开标签、展示为中性的「文件不存在」；``WORKSPACE_NOT_FOUND`` 是整个工作区不可用。
    """
    try:
        yield
    except KeyError as exc:
        raise HTTPException(404, {"code": "WORKSPACE_NOT_FOUND", "message": "工作区已不存在"}) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, {"code": "FILE_NOT_FOUND", "message": "文件不存在"}) from exc
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
    workspace_id: int,
    request: Request,
    files: Files,
    path: str,
    version: str,
    preview: ImagePreview | None = None,
) -> Response:
    """资源地址按版本寻址（版本必填），响应可长期缓存；版本过期返回 409，前端重读信息后换地址。

    ``preview`` 只用于位图（交付物的方块与大图卡），取值是固定的两种，缓存变体有界；对其他类型带它是调用写错，400。
    """
    metadata = await run_read(request, lambda: files.info(workspace_id, path))
    with file_errors():
        if metadata.version != version:
            raise FileConflictError("文件已变化，请重新加载")
        if metadata.kind not in {"image", "svg", "pdf", "audio", "video"}:
            raise ValueError("此文件使用文本或元信息预览")
        if preview is not None and metadata.kind != "image":
            raise ValueError("缩略图只用于位图")
        resolved = await asyncio.to_thread(files.path, workspace_id, path)
    if metadata.kind == "svg" and metadata.size > 8 * 1024 * 1024:
        raise HTTPException(413, "SVG 超出图片解析预算，请切换源码分段阅读")
    root = await run_read(request, lambda: files.root(workspace_id))
    return WorkspaceResourceResponse(root, resolved, metadata, preview)


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
async def watch(workspace_id: int, payload: WatchRequest, files: Files) -> StreamingResponse:
    """推送登记路径的变化：连上先推 ``ready``（前端整体重读），之后每有变化推一次 ``changed``。

    基准在 ``ready`` 之前取好（``WatchedPaths`` 构造时），前端重读与比对之间不漏变化；客户端断开时 Starlette
    取消这个协程，没有需要释放的登记。
    """
    with file_errors():
        watched = await asyncio.to_thread(
            WatchedPaths.for_request, files, workspace_id, payload.paths
        )

    async def stream() -> AsyncIterator[bytes]:
        yield line({"type": "ready"})
        async for _ in watched.changes():
            yield line({"type": "changed"})

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
