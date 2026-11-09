"""模型配置的测试辅助。

- ``model_configs``：真实 ``ModelConfigService``，只把「按 id 读到什么」换成给定函数；发起配置校验
  （``check_launch_config``）等规则仍走生产实现。
- ``create_model_config``：经真实服务写入（文件已由 conftest 隔离到用例临时目录）。
- ``model_config_record``：在内存里构造一个模型。
"""

from collections.abc import Callable
from typing import Any

from app.models.model_config_file import ModelConnectionEntry, ModelEntry
from app.models.model_config_record import ModelConfigRecord
from app.service.model_config.model_config_service import ModelConfigService


def model_configs(get: Callable[[str], Any]) -> ModelConfigService:
    service = ModelConfigService()
    service.get_config = get  # type: ignore[method-assign]
    return service


def model_config_record(
    *,
    connection_id: str = "test",
    name: str = "test",
    base_url: str = "http://127.0.0.1",
    api_key: str = "test",
    **model: Any,
) -> ModelConfigRecord:
    """在内存里构造一个模型（不写文件）；``model`` 是 ``ModelEntry`` 的字段。"""

    return ModelConfigRecord(
        ModelConnectionEntry(id=connection_id, name=name, base_url=base_url, api_key=api_key),
        ModelEntry(**model),
    )


def create_model_config(
    *, name: str, base_url: str, api_key: str, **model: Any
) -> ModelConfigRecord:
    """经真实服务建一个只挂一个模型的连接，返回这个模型（含所属连接）。"""

    from app.service.depends import get_model_config_service

    service = get_model_config_service()
    connection = service.create_connection(
        name=name, base_url=base_url, api_key=api_key, models=[ModelEntry(**model)]
    )
    return ModelConfigRecord(connection, connection.models[0])
