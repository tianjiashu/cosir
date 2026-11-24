"""Playwright 的真实文件服务夹具：所有文件都放系统临时目录，不接生产数据库。"""

import os
import sys
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.app import app
from app.parent_watch import install_parent_watch_from_env
from app.service.depends import (
    close_service_dependencies,
    get_workspace_crud,
    get_workspace_files_service,
)
from app.storage.store_engines import init_storage
from app.utils.path import system_cosir as paths


@asynccontextmanager
async def fixture_lifespan(_app: FastAPI):
    with tempfile.TemporaryDirectory(prefix="cosir-files-e2e-") as temporary:
        root = Path(temporary)
        (root / "src").mkdir()
        (root / "src" / "main.py").write_text("print('before update')\n", encoding="utf-8")
        (root / "alias").symlink_to(root / "src", target_is_directory=True)
        (root / "src" / "loop").symlink_to(root, target_is_directory=True)
        (root / "media" / "deep").mkdir(parents=True)
        Image.new("RGB", (20, 10), "red").save(root / "media" / "deep" / "photo.png")
        (root / "README.md").write_text(
            "# Local image\n\n![asset](media/deep/photo.png)", encoding="utf-8"
        )
        # 视觉回归与图片资源用例：一张在、一张不在的 Markdown 图片；一张超过缩小阈值的大图（原图尺寸与缩小图不同）。
        (root / "media" / "gallery.md").write_text(
            "# Gallery\n\n![photo](deep/photo.png)\n\n![gone](deep/gone.png)\n", encoding="utf-8"
        )
        Image.new("RGB", (4100, 4100), "teal").save(root / "media" / "huge.png")
        (root / "large.txt").write_text("中文😀 line\n" * 350000, encoding="utf-8")
        # 隔离 storage 到临时目录，避免触碰生产数据库；各接口共享同一 service 单例。
        storage_dir = Path(tempfile.mkdtemp(prefix="cosir-files-e2e-storage-"))
        paths.override(
            DATABASE_FILE=storage_dir / "app.sqlite3",
            LOG_DIR=storage_dir / "logs",
        )
        init_storage()
        # 工作区 id 由 SQLite 自增分配，与前端内存测试服务的固定 id 无关；
        # 经 health 暴露给 spec，由它把浏览器请求的 id 映射到这里。
        workspace = get_workspace_crud().create("demo", str(root))
        files = get_workspace_files_service()
        app.dependency_overrides[get_workspace_files_service] = lambda: files
        app.state.fixture_root = root
        app.state.fixture_workspace_id = workspace.id
        yield
        files.close()
        app.dependency_overrides.clear()
        close_service_dependencies()


class WatchConnections:
    """数正在进行的 ``/files/watch`` 连接：E2E 据此断言「没有登记就没有连接」。

    变化由后端按周期 stat 比对得出（``WatchedPaths``），是确定性的，夹具写完文件不必再替系统送事件；
    客户端断开时流式响应结束，下游调用返回即计数减一。
    """

    active = 0

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not scope["path"].endswith("/files/watch"):
            await self.app(scope, receive, send)
            return
        WatchConnections.active += 1
        try:
            await self.app(scope, receive, send)
        finally:
            WatchConnections.active -= 1


app.add_middleware(WatchConnections)


@app.post("/__test__/files/update")
def update_file():
    root = app.state.fixture_root
    # 模拟编辑器的原子替换而非同 inode 写入。
    replacement = root / "src" / "new.py"
    replacement.write_text("print('after update')\n", encoding="utf-8")
    replacement.replace(root / "src" / "main.py")
    return {"updated": True}


@app.post("/__test__/files/image-update")
def update_image():
    root = app.state.fixture_root
    Image.new("RGB", (40, 30), "blue").save(root / "media" / "deep" / "photo.png")
    return {"updated": True}


@app.post("/__test__/files/delete")
def delete_file():
    (app.state.fixture_root / "src" / "main.py").unlink()
    return {"deleted": True}


@app.get("/__test__/files/watch-status")
def watch_status():
    return {"subscriptions": WatchConnections.active}


@app.post("/__test__/files/link-fixture")
def link_fixture():
    root = app.state.fixture_root
    for relative, marker in [
        ("API.md", "ROOT_API"),
        ("slow.md", "SLOW_FILE"),
        ("slow-again.md", "SLOW_AGAIN"),
        ("docs/brand/API.md", "BRAND_API"),
        ("node_modules/example/API.md", "DEPENDENCY_API"),
        ("docs/brand/a%41#中文.md", "LITERAL_PATH"),
    ]:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# {marker}\nline two\nline three\n", encoding="utf-8")
    (root / "docs/brand/guide.md").write_text(
        "# Guide\n\n`./API.md` [special](a%2541%23%E4%B8%AD%E6%96%87.md)\n",
        encoding="utf-8",
    )
    return {"ready": True}


@app.post("/__test__/files/preview-fixture")
def preview_fixture():
    """按需在 previews/ 下放各类预览样本（表格、Mermaid、音视频、TIFF、几种损坏文件与看不了的文件）。

    不放进启动时的工作区：已有用例与视觉基线截到目录树，多一个目录就要重录。音视频样本是仓库里的几 KB 文件
    （无头 Chromium 的 MediaRecorder 录制；Ogg 封装它录不出来，所以没有 .ogg 样本），WAV 由标准库现写。
    """
    import shutil
    import struct
    import wave

    root = app.state.fixture_root / "previews"
    root.mkdir(exist_ok=True)
    rows = ["城市,人口,备注", '杭州,"12,200,000","多\n行备注"', "上海,24870000,", "成都,21190000,西南"]
    rows += [f"城市{index},{index * 1000},样本" for index in range(1, 301)]
    (root / "cities.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    (root / "flow.mmd").write_text("graph TD\n  Start-->Done\n", encoding="utf-8")
    (root / "broken.mmd").write_text("graph TD\n  A-->>>\n", encoding="utf-8")
    with wave.open(str(root / "tone.wav"), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"".join(struct.pack("<h", (i % 40 - 20) * 800) for i in range(8000)))
    media = Path(__file__).resolve().parent / "media"
    shutil.copy(media / "tone.webm", root / "tone.webm")
    shutil.copy(media / "clip.webm", root / "clip.webm")
    (root / "fake.mp4").write_bytes(b"not a video at all" * 64)
    scan = Image.new("I;16", (64, 32))
    for x in range(64):
        for y in range(32):
            scan.putpixel((x, y), x * 900)
    scan.save(root / "scan.tif")
    (root / "broken.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    (root / "broken.pdf").write_bytes(b"%PDF-1.7\n" + b"garbage " * 64)
    (root / "notebook.ipynb").write_text('{\n "cells": [],\n "nbformat": 4\n}\n', encoding="utf-8")
    # 内容不是文本：docx 后缀在系统可预览表里，zip 不在；两者都落到「看不了的文件」，说法与出口不同。
    (root / "report.docx").write_bytes(b"PK\x03\x04" + b"\x00" * 64)
    (root / "archive.zip").write_bytes(b"PK\x03\x04" + b"\x00" * 64)
    return {"ready": True}


@app.post("/__test__/files/reading-fixture")
def reading_fixture():
    """按需在 reading/ 下放阅读视图样本（与 preview-fixture 同理，不进启动时的目录树）。

    - ``turkish.md``：小写后长度会变的字符（``İ``）后面还有可查找的词。
    - ``segmented.md``：超过整读上限、按 512 KiB 分段的 Markdown；第一段正好由 128 行注释填满，所以第二段
      从第 129 行开始，标记段落在全文第 131 行、段内第 3 行。
    """
    root = app.state.fixture_root / "reading"
    root.mkdir(exist_ok=True)
    (root / "turkish.md").write_text("# Introduction\n\nİstanbul – budget\n", encoding="utf-8")
    block = ("<!-- " + "x" * 4086 + " -->\n") * 128
    (root / "segmented.md").write_text(
        block + "# Second segment\n\nSegmentMarker\n\n" + block * 4, encoding="utf-8"
    )
    return {"ready": True}


@app.get("/__test__/files/health")
def health():
    # pid 让拉起方确认回应的是自己刚起的进程：端口被遗留的夹具占着时，新进程绑不上端口，旧进程却照样回应。
    return {"status": "ready", "workspace_id": app.state.fixture_workspace_id, "pid": os.getpid()}


app.router.lifespan_context = fixture_lifespan
if __name__ == "__main__":
    # 与生产后端同一条存活契约：拉起方（Playwright worker）的 stdin 管道一关就退出。worker 被强杀、整轮被中断时
    # afterAll 来不及停它，没有这条它会留成孤儿、占着这个 slot 的端口，之后每一轮都报「端口被占」。
    install_parent_watch_from_env()
    # 端口由 spec 按 E2E slot 传入（apps/desktop/tests/e2e/e2e-env.ts），并行运行各用各的。
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
