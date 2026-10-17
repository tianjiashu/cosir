"""Playwright 的真实文件服务夹具：所有文件都放系统临时目录，不接生产数据库。"""

import sys
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.app import app
from app.service.depends import (
    close_service_dependencies,
    get_workspace_crud,
    get_workspace_file_watcher,
    get_workspace_files_service,
)
from app.service.workspace_files.file_watch import WorkspaceFileWatcher
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
        (root / "large.txt").write_text("中文😀 line\n" * 350000, encoding="utf-8")
        # 隔离 storage 到临时目录，避免触碰生产数据库；watcher 与 api 共享同一 service 单例。
        storage_dir = Path(tempfile.mkdtemp(prefix="cosir-files-e2e-storage-"))
        paths.override(
            DATABASE_FILE=storage_dir / "app.sqlite3",
            CHECKPOINT_FILE=storage_dir / "checkpoints.sqlite3",
            LOG_DIR=storage_dir / "logs",
        )
        init_storage()
        get_workspace_crud().create("demo", str(root))
        files = get_workspace_files_service()
        watcher = WorkspaceFileWatcher()
        app.dependency_overrides[get_workspace_files_service] = lambda: files
        app.dependency_overrides[get_workspace_file_watcher] = lambda: watcher
        app.state.fixture_root = root
        app.state.fixture_watcher = watcher
        yield
        watcher.close()
        files.close()
        app.dependency_overrides.clear()
        close_service_dependencies()


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
    watcher = app.state.fixture_watcher
    return {"subscriptions": len(watcher._subscribers), "watches": len(watcher._watches)}


@app.get("/__test__/files/health")
def health():
    return {"status": "ready"}


app.router.lifespan_context = fixture_lifespan
if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8766, log_level="warning")
