"""模型配置服务：文件是唯一事实源（手改立即生效、写坏时拒写）、Key 只存一份、删除后 Run 保留原 id。

模型配置文件由 conftest 的 ``isolate_scope_config_dirs`` 隔离到用例临时目录。
"""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.configuration import models as models_api
from app.app import app
from app.models.errors.input_errors import UserInputRejectedError
from app.models.model_config_file import ModelEntry
from app.service.depends import (
    close_service_dependencies,
    get_conversation_run_crud,
    get_model_config_service,
    get_task_crud,
    get_workspace_crud,
)
from app.storage.store_engines import init_storage
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces
from app.utils.path import system_cosir as paths
from tests.model_config_fixtures import create_model_config


@pytest.fixture
def storage(tmp_path: Path) -> Iterator[None]:
    """需要 Run 记录的用例用的隔离主库。"""

    close_service_dependencies()
    db_dir = tmp_path / "storage"
    db_dir.mkdir()
    paths.override(DATABASE_FILE=db_dir / "app.sqlite3", LOG_DIR=db_dir / "logs")
    init_storage()
    task_runtime_spaces.close()
    yield
    task_runtime_spaces.close()
    close_service_dependencies()
    paths.reset()


def _entry(name: str, window: int = 8, **capabilities: bool) -> ModelEntry:
    return ModelEntry(model_name=name, context_window_k=window, **capabilities)


def _run_for(model_config_id: str) -> int:
    workspace = get_workspace_crud().create("coding-agent", str(Path.cwd()))
    task = get_task_crud().create(workspace.id, "root")
    return (
        get_conversation_run_crud()
        .create(task.id, status="failed", model_config_id=model_config_id)
        .id
    )


def test_connection_and_models_are_written_in_file_order_and_duplicates_conflict() -> None:
    service = get_model_config_service()
    connection = service.create_connection(
        name="Packy Code",
        base_url="https://api.example.com/v1",
        api_key="secret-key",
        models=[_entry("qwen-a", 128), _entry("qwen-b", 64, supports_image=True)],
    )
    service.add_models(connection.id, [_entry("qwen-c")])

    with pytest.raises(FileExistsError, match="qwen-a"):
        service.add_models(connection.id, [_entry("qwen-d"), _entry("qwen-a")])
    with pytest.raises(FileExistsError):
        service.create_connection(name="Packy Code", base_url="u", api_key="k", models=[])

    assert connection.id == "packy-code"
    assert [config.id for config in service.list_configs()] == [
        "packy-code/qwen-a",
        "packy-code/qwen-b",
        "packy-code/qwen-c",
    ]
    saved = json.loads(service.path.read_text(encoding="utf-8"))
    assert [model["model_name"] for model in saved["connections"][0]["models"]] == [
        "qwen-a",
        "qwen-b",
        "qwen-c",
    ]


def test_key_lives_once_on_the_connection_and_renaming_keeps_ids() -> None:
    service = get_model_config_service()
    connection = service.create_connection(
        name="Packy", base_url="u", api_key="old", models=[_entry("a"), _entry("b")]
    )

    service.update_connection(connection.id, api_key="new", name="Renamed")

    configs = service.list_configs()
    assert {config.connection.api_key for config in configs} == {"new"}
    assert [config.id for config in configs] == ["packy/a", "packy/b"]


def test_update_and_delete_model_by_name_with_slashes() -> None:
    service = get_model_config_service()
    connection = service.create_connection(
        name="Router", base_url="u", api_key="k", models=[_entry("deepseek-ai/V3")]
    )

    updated = service.update_model(connection.id, "deepseek-ai/V3", supports_image=True)
    assert updated.model.supports_image is True
    with pytest.raises(KeyError):
        service.update_model(connection.id, "missing", supports_image=True)

    service.delete_model(connection.id, "deepseek-ai/V3")
    assert service.list_configs() == []
    assert [c.id for c in service.read().connections] == ["router"]


def test_hand_edited_file_takes_effect_on_the_next_read() -> None:
    service = get_model_config_service()
    service.path.parent.mkdir(parents=True, exist_ok=True)
    service.path.write_text(
        json.dumps(
            {
                "connections": [
                    {
                        "id": "local",
                        "name": "Local",
                        "base_url": "http://127.0.0.1/v1",
                        "api_key": "k",
                        "models": [{"model_name": "llama", "context_window_k": 32}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    config = service.get_config("local/llama")

    assert (config.model.context_window_k, config.model.supports_thinking) == (32, False)
    assert service.exists("local/llama") and not service.exists("local/other")


def test_broken_file_is_reported_with_location_and_never_overwritten() -> None:
    service = get_model_config_service()
    service.path.parent.mkdir(parents=True, exist_ok=True)
    broken = (
        '{"connections": [{"id": "x", "name": "X", "base_url": "u", "api_key": "k",'
        ' "models": [{"model_name": "m", "context_window_k": 0}]}]}'
    )
    service.path.write_text(broken, encoding="utf-8")

    with pytest.raises(ValueError, match=r"connections\[0\]\.models\[0\]\.context_window_k"):
        service.read()
    with pytest.raises(ValueError):
        service.create_connection(name="Y", base_url="u", api_key="k", models=[_entry("m")])
    with pytest.raises(UserInputRejectedError) as rejected:
        service.check_launch_config("x/m", None)

    assert rejected.value.code == "MODEL_CONFIG_FILE_INVALID"
    assert not service.exists("x/m")
    assert service.path.read_text(encoding="utf-8") == broken


def test_deleting_models_keeps_the_run_id_and_the_model_comes_back_under_the_same_id(
    storage,
) -> None:
    """没有外键：删除后 Run 保留原 id；加回同名模型时 id 相同，引用自动接上。"""

    service = get_model_config_service()
    config = create_model_config(
        name="DeepSeek", base_url="u", api_key="k", model_name="chat", context_window_k=64
    )
    run_id = _run_for(config.id)

    service.delete_connection(config.connection.id)

    assert get_conversation_run_crud().get(run_id).model_config_id == "deepseek/chat"
    with pytest.raises(KeyError):
        service.get_config("deepseek/chat")
    with pytest.raises(UserInputRejectedError):
        service.check_launch_config("deepseek/chat", None)
    restored = create_model_config(
        name="DeepSeek", base_url="u", api_key="k", model_name="chat", context_window_k=64
    )
    assert restored.id == get_conversation_run_crud().get(run_id).model_config_id


def test_http_routes_return_written_connections_and_map_errors() -> None:
    """写接口返回写入后的连接；重名 409、未知连接 404；模型名带 ``/`` 也能寻址。"""

    assert models_api  # 导入即注册路由
    client = TestClient(app)
    root = "/configuration/model-connections"
    model = {"model_name": "org/qwen-a", "context_window_k": 128, "supports_image": True}

    created = client.post(
        root,
        json={"name": "packy", "base_url": "https://x.test/v1/", "api_key": "k", "models": [model]},
    )
    assert created.status_code == 200
    body = created.json()
    assert body["base_url"] == "https://x.test/v1"
    assert [(m["config_id"], m["supports_image"]) for m in body["models"]] == [
        ("packy/org/qwen-a", True)
    ]

    added = client.post(
        f"{root}/packy/models", json={"models": [{"model_name": "qwen-b", "context_window_k": 64}]}
    )
    assert [m["model_name"] for m in added.json()["models"]] == ["org/qwen-a", "qwen-b"]
    assert client.post(f"{root}/packy/models", json={"models": [model]}).status_code == 409
    assert client.put(f"{root}/missing", json={"name": "x"}).status_code == 404

    updated = client.put(f"{root}/packy/models/org/qwen-a", json={"context_window_k": 256})
    assert updated.json()["context_window_k"] == 256
    assert client.delete(f"{root}/packy/models/org/qwen-a").status_code == 200

    listed = client.get(root).json()
    assert listed["path"].endswith("models.json")
    assert [len(connection["models"]) for connection in listed["connections"]] == [1]


def test_http_reports_a_broken_file_as_400_with_its_location() -> None:
    service = get_model_config_service()
    service.path.parent.mkdir(parents=True, exist_ok=True)
    service.path.write_text('{"connections": [', encoding="utf-8")

    response = TestClient(app).get("/configuration/model-connections")

    assert response.status_code == 400
    message = response.json()["detail"]["message"]
    assert "models.json" in message and "无效" in message
