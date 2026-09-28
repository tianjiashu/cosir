"""Provider 配置显示名称契约测试。"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.schemas.request.ProviderCreateRequest import ProviderCreateRequest
from app.api.schemas.response.ProviderResponse import ProviderResponse
from app.models.provider_record import ProviderRecord
from app.storage.model.provider_model import ProviderModel


def test_provider_model_persists_display_name_separately_from_capability_name() -> None:
    """Provider ORM 应同时保留能力名称与用户可见的配置名称。"""

    assert "display_name" in ProviderModel.__table__.c
    assert not ProviderModel.__table__.c.name.unique


def test_provider_schema_allows_same_capability_name_with_distinct_display_names() -> None:
    """同一能力名称可以落库多个配置，但显示名称不能重复。"""

    engine = create_engine("sqlite:///:memory:")
    ProviderModel.__table__.create(engine)

    with Session(engine) as session:
        session.add_all(
            [
                ProviderModel(name="deepseek", display_name="DeepSeek 官方"),
                ProviderModel(name="deepseek", display_name="DeepSeek 中转"),
            ]
        )
        session.commit()
        assert session.scalar(select(ProviderModel).where(ProviderModel.name == "deepseek"))

        session.add(ProviderModel(name="deepseek", display_name="DeepSeek 官方"))
        with pytest.raises(IntegrityError):
            session.commit()


def test_provider_create_request_accepts_display_name() -> None:
    """创建请求应校验并保留 display_name，而 name 仍是能力注册表名称。"""

    request = ProviderCreateRequest(
        name="deepseek",
        display_name="DeepSeek 官方",
        base_url="https://api.example.com",
        api_key="test-key",
    )

    assert request.name == "deepseek"
    assert request.display_name == "DeepSeek 官方"


def test_provider_response_exposes_display_name() -> None:
    """Provider API 响应应返回用户可见名称，且不改变能力名称。"""

    now = datetime.now(UTC)
    record = ProviderRecord(
        id=1,
        name="deepseek",
        display_name="DeepSeek 官方",
        created_at=now,
        updated_at=now,
    )

    response = ProviderResponse.from_record(record, api_key_configured=True)

    assert response.name == "deepseek"
    assert response.display_name == "DeepSeek 官方"
