"""模型厂商领域服务聚合包。

对外统一暴露 provider 配置管理相关的领域服务：
- ``ProviderService``：厂商配置 CRUD 与状态聚合。
"""

from app.service.provider.connection_test_result import ConnectionTestResult
from app.service.provider.provider_service import ProviderService

__all__ = [
    "ConnectionTestResult",
    "ProviderService",
]
