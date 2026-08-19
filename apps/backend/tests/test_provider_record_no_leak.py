"""``ProviderRecord`` 序列化不泄漏 Key 明文的单元测试。

覆盖设计文档阶段 2「ProviderRecord no-leak」契约：``to_dict()`` 面向日志 /
事件 / API 响应消费，**刻意不输出 ``api_key``**（明文仅允许在值对象与运行时
``LLMRuntimeConfig`` 之间流动）；``base_url`` 等非凭据字段正常输出。
"""

from app.models import ProviderRecord
from app.utils.datetime_utils import utc_now


def _make_provider(api_key: str = "sk-super-secret") -> ProviderRecord:
    """构造一个携带 Key 明文的 ProviderRecord（不落库）。

    参数:
        api_key: 厂商 API Key 明文（默认测试敏感值）。

    返回:
        不落库的 ProviderRecord 实例。

    异常:
        无。

    副作用:
        无。
    """

    now = utc_now()
    return ProviderRecord(
        provider_id="p-1",
        name="deepseek",
        provider_type="deepseek",
        created_at=now,
        updated_at=now,
        base_url="https://api.deepseek.com",
        api_key=api_key,
        enabled=True,
        sort_order=0,
    )


def test_to_dict_contains_no_api_key_key() -> None:
    """``to_dict()`` 字典不含 ``api_key`` 键。"""
    data = _make_provider().to_dict()
    assert "api_key" not in data


def test_to_dict_contains_no_plaintext_secret() -> None:
    """``to_dict()`` 字典任一层都不含 Key 明文。"""
    secret = "sk-super-secret"  # noqa: S105 - 测试夹具用假密钥验证 no-leak 契约
    data = _make_provider(api_key=secret).to_dict()
    rendered = str(data)
    assert secret not in rendered


def test_plaintext_accessible_via_attribute_for_service_layer() -> None:
    """Key 明文仍可通过属性访问取用（service 层内部消费通道，不进序列化）。"""
    provider = _make_provider(api_key="sk-internal-only")
    assert provider.api_key == "sk-internal-only"
    assert "sk-internal-only" not in str(provider.to_dict())
