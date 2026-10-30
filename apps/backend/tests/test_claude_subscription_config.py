"""订阅连接配置不存 token，明确路由到原生 Claude provider。"""

import pytest
from pydantic import ValidationError

from app.api.schemas.request.ModelConfigCreateRequest import ModelConfigCreateRequest
from app.core.agents.resolved_model import resolve_model
from app.core.llm_provider.model_factory import build_chat_model
from app.models.model_config_record import ModelConfigRecord


def test_subscription_config_accepts_local_login_without_api_key():
    request = ModelConfigCreateRequest(
        provider="claude_subscription",
        config_name="Claude 订阅",
        base_url="https://api.anthropic.com",
        api_key="",
        model_name="claude-sonnet-4-6",
        context_window_k=200,
    )
    record = ModelConfigRecord(**request.model_dump())
    resolved = resolve_model(record)
    assert resolved.provider == "claude_subscription"
    assert resolved.api_key == ""


def test_subscription_config_rejects_other_origins_and_stored_tokens():
    for base_url, key in [
        ("https://elsewhere.example", ""),
        ("https://api.anthropic.com", "do-not-store"),
    ]:
        with pytest.raises(ValidationError):
            ModelConfigCreateRequest(
                provider="claude_subscription",
                config_name="Claude",
                base_url=base_url,
                api_key=key,
                model_name="claude-sonnet-4-6",
                context_window_k=200,
            )


@pytest.mark.asyncio
async def test_factory_routes_subscription_without_reading_credentials_at_construction():
    from app.core.llm_provider.claude.model import ClaudeChatModel

    record = ModelConfigRecord(
        provider="claude_subscription",
        config_name="Claude",
        base_url="https://api.anthropic.com",
        api_key="",
        model_name="claude-sonnet-4-6",
        context_window_k=200,
    )
    model = build_chat_model(resolve_model(record))
    try:
        assert isinstance(model, ClaudeChatModel)
    finally:
        await model.close()


def test_existing_model_table_gets_provider_without_losing_rows(tmp_path):
    from sqlalchemy import create_engine, text

    from app.storage.store_engines import initialize_app_schema

    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.sqlite3'}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE model_configs (id INTEGER PRIMARY KEY, config_name TEXT)"
        )
        connection.exec_driver_sql(
            "INSERT INTO model_configs (id, config_name) VALUES (7, 'existing')"
        )
    initialize_app_schema(engine)
    initialize_app_schema(engine)
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT id, config_name, provider FROM model_configs")
        ).one() == (7, "existing", "openai")
    engine.dispose()


def test_subscription_config_crud_round_trip_and_partial_update(isolated_storage):
    from app.storage.crud.model_config_crud import ModelConfigCrud

    crud = ModelConfigCrud()
    record = crud.create(
        provider="claude_subscription",
        config_name="Claude",
        base_url="https://api.anthropic.com",
        api_key="",
        model_name="claude-sonnet-4-6",
        context_window_k=200,
    )
    assert crud.get(record.id).provider == "claude_subscription"
    assert crud.get(record.id).api_key == ""
    with pytest.raises(ValueError):
        crud.update(record.id, base_url="https://other.example")
    updated = crud.update(record.id, config_name="Claude personal")
    assert updated.provider == "claude_subscription" and updated.api_key == ""


@pytest.mark.parametrize(
    "credential",
    [
        b"not-json",
        b'{"claudeAiOauth":{"accessToken":"secret","expiresAt":1,"scopes":["user:inference"]}}',
        b'{"claudeAiOauth":{"accessToken":"secret","expiresAt":9999999999999,"scopes":[]}}',
    ],
)
def test_invalid_credentials_fail_without_exposing_token(credential):
    from app.core.llm_provider.claude.claude_code_login import parse_local_login
    from app.core.llm_provider.claude.errors import ClaudeAPIError

    with pytest.raises(ClaudeAPIError) as captured:
        parse_local_login(credential)
    assert captured.value.status_code == 401
    assert "secret" not in str(captured.value)
