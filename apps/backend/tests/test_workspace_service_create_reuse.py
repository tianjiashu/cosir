"""针对 ``WorkspaceService.create_workspace`` 的「复用或创建」语义的单元测试。

覆盖：同路径复用、跨平台大小写不敏感复用、新建分支、.cosir 元数据初始化（含降级）、
非法 root_path 校验，以及同类的 list/get/create_task/delete 等配套行为以达成覆盖率。
测试彼此独立，均依赖 ``storage`` fixture 隔离数据库。
"""

import shutil
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.config.settings import Settings
from app.service.depends import (
    close_service_dependencies,
    get_task_crud,
    get_workspace_crud,
    get_workspace_service,
)
from app.storage.model.workspace_model import WorkspaceModel
from app.storage.store_engines import init_storage, main_session_factory
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces


@pytest.fixture
def storage(tmp_path: Path):
    """为创建/复用测试提供隔离的主库和 checkpoint 路径（复用既有模式）。"""

    close_service_dependencies()
    db_dir = tmp_path / "storage"
    db_dir.mkdir()
    Settings.override(
        DATABASE_FILE=db_dir / "app.sqlite3",
        CHECKPOINT_FILE=db_dir / "checkpoints.sqlite3",
        LOG_DIR=db_dir / "logs",
    )
    init_storage()
    task_runtime_spaces.close()
    yield
    task_runtime_spaces.close()
    close_service_dependencies()


def _count() -> int:
    """统计 workspaces 表当前行数。"""
    with main_session_factory()() as session:
        return int(session.scalar(select(func.count()).select_from(WorkspaceModel)) or 0)


def _make_dir(tmp_path: Path, name: str) -> Path:
    """在 tmp_path 下创建一个空目录并返回其路径。"""
    directory = tmp_path / name
    directory.mkdir()
    return directory


# ---------------------------------------------------------------------------
# 1. 复用返回同一条记录
# ---------------------------------------------------------------------------
def test_reuse_returns_same_record(storage, tmp_path) -> None:
    # 目的：验证同路径二次 create 命中复用、保留既有 name、不重复插入。潜在缺陷：重复插入或覆盖 name。
    service = get_workspace_service()
    root = _make_dir(tmp_path, "ws_reuse")

    first = service.create_workspace("first", str(root))
    second = service.create_workspace("second", str(root))

    assert second.id == first.id
    assert second.name == "first"
    assert _count() == 1


# ---------------------------------------------------------------------------
# 2. 新路径新建
# ---------------------------------------------------------------------------
def test_new_path_creates_new_record(storage, tmp_path) -> None:
    # 目的：验证不同路径 create 得到新 id 且 DB 行数 +1。潜在缺陷：误判为复用或计数错误。
    service = get_workspace_service()
    root_a = _make_dir(tmp_path, "ws_a")
    root_b = _make_dir(tmp_path, "ws_b")

    first = service.create_workspace("a", str(root_a))
    assert _count() == 1

    second = service.create_workspace("b", str(root_b))
    assert second.id != first.id
    assert _count() == 2


# ---------------------------------------------------------------------------
# 3. 跨平台大小写不敏感复用
# ---------------------------------------------------------------------------
def test_case_insensitive_reuse(storage, tmp_path) -> None:
    # 目的：验证大小写不同但解析后等价的路径在大小写不敏感 FS 上命中复用。潜在缺陷：漏判等价路径。
    service = get_workspace_service()
    project = _make_dir(tmp_path, "Project")

    # 探测文件系统是否大小写不敏感：创建 "Project" 后 "project" 是否存在。
    if not (tmp_path / "project").exists():
        pytest.skip("filesystem is case-sensitive; skipping case-insensitive reuse check")

    first = service.create_workspace("first", str(project))
    second = service.create_workspace("second", str(tmp_path / "project"))

    assert second.id == first.id
    assert second.name == "first"
    assert _count() == 1


# ---------------------------------------------------------------------------
# 4. 新建分支创建 .cosir
# ---------------------------------------------------------------------------
def test_new_branch_creates_cosir_dirs(storage, tmp_path) -> None:
    # 目的：验证新建分支在根目录生成 .cosir 与 .cosir/Attachment 目录。潜在缺陷：目录未创建。
    service = get_workspace_service()
    root = _make_dir(tmp_path, "ws_cosir")

    service.create_workspace("ws", str(root))

    cosir = root / ".cosir"
    attachment = cosir / "Attachment"
    assert cosir.is_dir()
    assert attachment.is_dir()


# ---------------------------------------------------------------------------
# 5. 复用分支重建缺失的 .cosir
# ---------------------------------------------------------------------------
def test_reuse_branch_rebuilds_missing_cosir(storage, tmp_path) -> None:
    # 目的：验证复用分支在 .cosir 被删后仍会重建并复用同一条记录。潜在缺陷：复用分支忽略 .cosir 重建。
    service = get_workspace_service()
    root = _make_dir(tmp_path, "ws_missing_cosir")

    first = service.create_workspace("ws", str(root))
    shutil.rmtree(root / ".cosir")
    assert not (root / ".cosir").exists()

    second = service.create_workspace("other", str(root))

    assert second.id == first.id
    assert (root / ".cosir").is_dir()
    assert (root / ".cosir" / "Attachment").is_dir()
    assert _count() == 1


# ---------------------------------------------------------------------------
# 6. .cosir 初始化失败降级
# ---------------------------------------------------------------------------
def test_cosir_init_failure_downgrades(storage, tmp_path) -> None:
    # 目的：验证 .cosir 已被同名文件占位导致 mkdir 失败时仍返回 record 且不抛错、不重复插入。潜在缺陷：向上抛异常。
    service = get_workspace_service()
    root = _make_dir(tmp_path, "ws_cosir_file")
    (root / ".cosir").write_text("x")  # 同名文件占位

    record = service.create_workspace("ws", str(root))

    assert record is not None
    assert record.id > 0
    assert _count() == 1


# ---------------------------------------------------------------------------
# 7. 非法 root_path 抛 ValueError
# ---------------------------------------------------------------------------
def test_invalid_root_path_raises_value_error(storage, tmp_path) -> None:
    # 目的：验证相对路径 / 不存在目录 / 已存在文件三种非法 root_path 均抛 ValueError。潜在缺陷：校验缺失或类型不符。
    service = get_workspace_service()

    # (a) 相对路径
    with pytest.raises(ValueError):
        service.create_workspace("a", "relative/path")

    # (b) 不存在的目录
    with pytest.raises(ValueError):
        service.create_workspace("b", str(tmp_path / "does_not_exist"))

    # (c) 已存在的文件路径（非目录）
    a_file = tmp_path / "afile.txt"
    a_file.write_text("x")
    with pytest.raises(ValueError):
        service.create_workspace("c", str(a_file))


# ---------------------------------------------------------------------------
# 配套用例：覆盖 WorkspaceService 其余行为以满足覆盖率
# ---------------------------------------------------------------------------
def test_name_whitespace_falls_back_to_dir_name(storage, tmp_path) -> None:
    # 目的：验证新建分支 name 为空白时回退为根目录名。潜在缺陷：空白 name 被原样写入或抛错。
    service = get_workspace_service()
    root = _make_dir(tmp_path, "ws_fallback")

    record = service.create_workspace("   ", str(root))

    assert record.name == "ws_fallback"
    assert _count() == 1


def test_get_workspace_returns_record(storage, tmp_path) -> None:
    # 目的：验证 get_workspace 按 id 返回既有记录。潜在缺陷：返回错误记录或抛错。
    service = get_workspace_service()
    root = _make_dir(tmp_path, "ws_get")

    created = service.create_workspace("ws", str(root))
    fetched = service.get_workspace(created.id)

    assert fetched.id == created.id
    assert fetched.name == "ws"
    assert fetched.root_path == created.root_path


def test_get_workspace_missing_raises_keyerror(storage, tmp_path) -> None:
    # 目的：验证 get_workspace 对不存在 id 抛 KeyError。潜在缺陷：返回 None 或错误异常类型。
    service = get_workspace_service()

    with pytest.raises(KeyError):
        service.get_workspace(987654)


def test_list_workspaces(storage, tmp_path) -> None:
    # 目的：验证 list_workspaces 返回全部已创建工作区。潜在缺陷：遗漏记录或返回顺序/类型错误。
    service = get_workspace_service()
    root_a = _make_dir(tmp_path, "ws_list_a")
    root_b = _make_dir(tmp_path, "ws_list_b")

    service.create_workspace("a", str(root_a))
    service.create_workspace("b", str(root_b))

    workspaces = service.list_workspaces()
    assert len(workspaces) == 2
    names = {ws.name for ws in workspaces}
    assert names == {"a", "b"}


def test_create_task_creates_task(storage, tmp_path) -> None:
    # 目的：验证 create_task 在写闸门内为 workspace 创建 task 并返回记录。潜在缺陷：未创建或异常类型错误。
    service = get_workspace_service()
    root = _make_dir(tmp_path, "ws_task")

    workspace = service.create_workspace("ws", str(root))
    task = service.create_task(workspace.id, "my task")

    assert task.id is not None
    assert task.id > 0
    assert get_task_crud().list_ids_by_workspace(workspace.id) == [task.id]


def test_delete_workspace_removes_record(storage, tmp_path) -> None:
    # 目的：验证 delete_workspace 级联删除 task 并移除 workspace 记录。潜在缺陷：记录残留或事务未提交。
    service = get_workspace_service()
    root = _make_dir(tmp_path, "ws_delete")

    workspace = service.create_workspace("ws", str(root))
    get_task_crud().create(workspace.id, "root")

    service.delete_workspace(workspace.id)

    assert _count() == 0
    assert get_task_crud().list_ids_by_workspace(workspace.id) == []
    with pytest.raises(KeyError):
        get_workspace_crud().get(workspace.id)
