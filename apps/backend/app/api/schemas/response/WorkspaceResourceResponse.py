"""资源响应持有已校验句柄；PDF 的单段 Range 读取不会再次沿路径打开文件。"""

import os
import re
from collections.abc import AsyncIterator
from pathlib import Path

import anyio
from fastapi import HTTPException
from starlette.responses import Response, StreamingResponse
from starlette.types import Receive, Scope, Send

from app.service.workspace_files.file_content import version_of
from app.service.workspace_files.file_access import regular_file
from app.service.workspace_files.image_preview import image_preview
from app.service.workspace_files.file_models import FileInfo


class WorkspaceResourceResponse(Response):
    """同一响应读同一个句柄；仅支持单段 Range，多段按 HTTP 语义退回完整响应。"""

    def __init__(self, root: Path, path: Path, info: FileInfo) -> None:
        super().__init__()
        self.root, self.path, self.info = root, path, info

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        from app.api.workspace_files_api import file_errors

        manager = regular_file(self.path, self.root)
        with file_errors(), anyio.CancelScope(shield=True):
            source = await anyio.to_thread.run_sync(manager.__enter__)
        try:
            stat = await anyio.to_thread.run_sync(os.fstat, source.fileno())
            if version_of(stat) != self.info.version:
                raise HTTPException(409, "文件已变化，请重新加载")
            headers = {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
                "Accept-Ranges": "bytes",
                "ETag": f'"{self.info.version}"',
            }
            if self.info.kind == "image":
                with file_errors():
                    thumbnail = await anyio.to_thread.run_sync(image_preview, source)
                if thumbnail is not None:
                    headers["X-Cosir-Image-Preview"] = "thumbnail"
                    await Response(thumbnail, media_type="image/png", headers=headers)(
                        scope, receive, send
                    )
                    return
            request_headers = dict(scope["headers"])
            start, end, status = 0, stat.st_size, 200
            range_value = request_headers.get(b"range", b"").decode("latin1")
            if_range = request_headers.get(b"if-range")
            if not if_range or if_range.decode("latin1") == headers["ETag"]:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_value)
                if match and any(match.groups()):
                    first, last = match.groups()
                    start = int(first) if first else max(0, end - int(last))
                    end = min(end, int(last) + 1) if first and last else end
                    if start >= end:
                        await Response(
                            status_code=416,
                            headers={**headers, "Content-Range": f"bytes */{stat.st_size}"},
                        )(scope, receive, send)
                        return
                    status = 206
                    headers["Content-Range"] = f"bytes {start}-{end - 1}/{stat.st_size}"
            headers["Content-Length"] = str(end - start)
            await anyio.to_thread.run_sync(source.seek, start)

            async def chunks() -> AsyncIterator[bytes]:
                remaining = end - start
                while remaining:
                    chunk = await anyio.to_thread.run_sync(source.read, min(256 * 1024, remaining))
                    if not chunk:
                        raise OSError("资源读取期间文件被截断")
                    remaining -= len(chunk)
                    yield chunk

            await StreamingResponse(
                chunks(), status_code=status, media_type=self.info.mime, headers=headers
            )(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(manager.__exit__, None, None, None)
