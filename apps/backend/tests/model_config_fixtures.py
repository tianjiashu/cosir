"""不触达存储的模型配置服务：真实 ``ModelConfigService``，只把它读配置的 crud 换成给定函数。

发起配置校验（``check_launch_config``）等规则仍走生产实现，测试只决定「按 id 读到什么」。
"""

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from app.service.model_config.model_config_service import ModelConfigService


def model_configs(get: Callable[[int], Any]) -> ModelConfigService:
    service = ModelConfigService.__new__(ModelConfigService)
    service._crud = SimpleNamespace(get=get)  # type: ignore[assignment]
    return service
