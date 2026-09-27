"""厂商删除与 Run 模型路由事实分层（``conversation_runs.provider_id`` 的 SET NULL）行为测试。

覆盖两条契约：
1. 厂商被历史 Run 引用时仍可删除，Run 行与 ``model_name`` 保留，``provider_id`` 置空；
2. ``DELETE /providers/{id}`` 把存储层外键违约映射为 409 + 可读 ``detail``。
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from app.api.providers_api import delete_provider as delete_provider_endpoint
from app.service.depends import (
    close_service_dependencies,
    get_conversation_run_crud,
    get_provider_service,
    get_task_crud,
    get_workspace_crud,
)
from app.storage.store_engines import init_storage
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces
from app.utils import paths
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError


@pytest.fixture
def storage(tmp_path: Path) -> Iterator[None]:
    """为厂商删除测试提供隔离的主库与 checkpoint 路径。"""

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


def test_delete_provider_clears_run_reference_and_keeps_model_name(storage) -> None:
    """被历史 Run 引用的厂商可以删除，Run 行保留且只失去 provider 弱引用。"""

    provider = get_provider_service().create_provider(
        name="deepseek",
        base_url="https://api.deepseek.com",
        api_key="secret-key",
    )
    workspace = get_workspace_crud().create("coding-agent", str(Path.cwd()))
    task = get_task_crud().create(workspace.id, "root")
    run = get_conversation_run_crud().create(
        task.id,
        "你好",
        status="failed",
        provider_id=provider.id,
        model_name="deepseek-flash",
    )

    get_provider_service().delete_provider(provider.id)

    stored_run = get_conversation_run_crud().get(run.id)
    # Run 行本身与其模型名文本属于历史事实，不随厂商配置消失。
    assert stored_run.model_name == "deepseek-flash"
    assert stored_run.provider_id is None
    # 厂商确实已被删除（删除不被历史行挡住）。
    assert all(item.id != provider.id for item in get_provider_service().list_providers())


@pytest.mark.asyncio
async def test_delete_provider_endpoint_maps_integrity_error_to_conflict() -> None:
    """存储层外键违约映射为 409 与可读提示，而不是冒泡成 500。"""

    class _FailingProviderService:
        @staticmethod
        def delete_provider(provider_id: int) -> None:
            raise IntegrityError(
                "DELETE FROM providers",
                {"id": provider_id},
                Exception("FOREIGN KEY constraint failed"),
            )

    with pytest.raises(HTTPException) as excinfo:
        await delete_provider_endpoint(7, _FailingProviderService())

    assert excinfo.value.status_code == 409
    assert isinstance(excinfo.value.detail, str)
    assert excinfo.value.detail
