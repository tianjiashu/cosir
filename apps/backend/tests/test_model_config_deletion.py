"""模型连接配置删除后的 Run 历史事实测试。"""

from collections.abc import Iterator
from pathlib import Path

import pytest

from app.service.depends import (
    close_service_dependencies,
    get_conversation_run_crud,
    get_model_config_service,
    get_task_crud,
    get_workspace_crud,
)
from app.storage.store_engines import init_storage
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces
from app.utils import paths


@pytest.fixture
def storage(tmp_path: Path) -> Iterator[None]:
    """为模型配置删除测试提供隔离的主库与 checkpoint 路径。"""

    close_service_dependencies()
    db_dir = tmp_path / "storage"
    db_dir.mkdir()
    paths.override(
        DATABASE_FILE=db_dir / "app.sqlite3",
        CHECKPOINT_FILE=db_dir / "checkpoints.sqlite3",
        LOG_DIR=db_dir / "logs",
    )
    init_storage()
    task_runtime_spaces.close()
    yield
    task_runtime_spaces.close()
    close_service_dependencies()
    paths.reset()


def test_delete_model_config_keeps_run_history(storage) -> None:
    """删除配置后，Run 保留模型和上下文窗口快照，只清空弱引用。"""

    service = get_model_config_service()
    config = service.create_config(
        config_name="DeepSeek 删除测试",
        base_url="https://api.deepseek.com",
        api_key="secret-key",
        model_name="deepseek-chat",
        context_window_k=64,
    )
    workspace = get_workspace_crud().create("coding-agent", str(Path.cwd()))
    task = get_task_crud().create(workspace.id, "root")
    run = get_conversation_run_crud().create(
        task.id,
        "你好",
        status="failed",
        model_config_id=config.id,
        model_name=config.model_name,
        context_window_k=config.context_window_k,
    )

    service.delete_config(config.id)

    stored_run = get_conversation_run_crud().get(run.id)
    assert stored_run.model_name == "deepseek-chat"
    assert stored_run.context_window_k == 64
    assert stored_run.model_config_id is None
    assert all(item.id != config.id for item in service.list_configs())
