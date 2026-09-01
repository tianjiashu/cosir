"""Provider 配置响应契约测试。"""

from datetime import UTC, datetime

from app.api.schemas.response.ProviderResponse import ProviderResponse
from app.models.provider_record import ProviderRecord


def test_provider_response_maps_current_provider_record_fields() -> None:
    """响应应从当前 ProviderRecord 的 id/name 字段构造，且不要求旧 type 字段。"""

    now = datetime.now(UTC)
    response = ProviderResponse.from_record(
        ProviderRecord(id=7, name="deepseek", created_at=now, updated_at=now),
        api_key_configured=True,
    )

    assert response.provider_id == 7
    assert response.name == "deepseek"
    assert "api_key" not in response.model_dump()
