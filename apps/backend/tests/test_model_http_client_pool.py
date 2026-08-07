"""共享模型 HTTP 客户端连接池的单测。

验证 ``model_http_pool`` 模块按「事件循环 + base_url」复用 ``httpx.AsyncClient`` 的
进程级缓存，以及真实生产入口（``factory.build_chat_model`` → ``DeepSeekProvider``）
确实接入共享客户端：
- 同循环、同 base_url 多次构建注入同一 AsyncClient（连接复用）；
- base_url 为 None 回落到 openai 默认端点并归一并复用；
- 不同 base_url 使用不同客户端；
- 跨事件循环不复用已失效客户端（新建而非复用绑定到旧循环的客户端）；
- ``close_shared_model_http_clients`` 能正确关闭并清空缓存。

不发起任何网络请求：``build_openai_compatible_chat_model`` 在缺 Key 时仍以
``api_key=None`` 构造 chat model；测试经 monkeypatch 注入 dummy Key 仅用于绕过
openai SDK 的凭据校验，构造不触网。
"""

import asyncio
from os import environ

import httpx
import pytest
from langchain_openai import ChatOpenAI

from app.core.llm.factory import build_chat_model
from app.core.llm.llm_provider.openai_compatible import build_openai_compatible_chat_model
from app.core.llm.model_http_pool import (
    _DEFAULT_OPENAI_BASE_URL,
    _SHARED_HTTP_CLIENTS,
    close_shared_model_http_clients,
    get_shared_model_http_client,
)
from app.core.llm.model_settings import ModelSettings


@pytest.fixture(autouse=True)
def _clear_pool(monkeypatch):
    """每个用例前后清空进程级客户端缓存并注入 dummy Key，避免串扰与凭据校验。"""

    _SHARED_HTTP_CLIENTS.clear()
    # openai SDK 在 api_key 为 None 时强制要求凭据；构造不触网，dummy 安全。
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-test-key")
    yield
    _SHARED_HTTP_CLIENTS.clear()


def _extract_async_client(model: ChatOpenAI) -> httpx.AsyncClient:
    """从 ChatOpenAI 实例取回注入的共享 AsyncClient（真实存放路径 root_async_client._client）。

    参数:
        model: 已构造的 ChatOpenAI 实例。

    返回:
        注入的 ``httpx.AsyncClient`` 实例。

    异常:
        AttributeError: 若 langchain 内部路径变更则暴露测试断言失败。
    """

    return model.root_async_client._client


async def test_shared_base_shares_client():
    """直接经共享基座：同 base_url 两次构建注入同一 AsyncClient。"""

    base_url = "https://api.deepseek.com/v1"
    m1 = build_openai_compatible_chat_model(
        "deepseek-chat",
        None,
        base_url=base_url,
        chat_model_class=ChatOpenAI,
        log_event="test_build_1",
        log_label="Test",
    )
    m2 = build_openai_compatible_chat_model(
        "deepseek-chat",
        None,
        base_url=base_url,
        chat_model_class=ChatOpenAI,
        log_event="test_build_2",
        log_label="Test",
    )
    assert _extract_async_client(m1) is _extract_async_client(m2)


async def test_none_base_url_normalizes_and_reuses():
    """base_url 为 None 回落到 openai 默认端点并归一并复用。"""

    m1 = build_openai_compatible_chat_model(
        "gpt-x",
        None,
        base_url=None,
        chat_model_class=ChatOpenAI,
        log_event="test_build_3",
        log_label="Test",
    )
    m2 = build_openai_compatible_chat_model(
        "gpt-x",
        None,
        base_url=None,
        chat_model_class=ChatOpenAI,
        log_event="test_build_4",
        log_label="Test",
    )
    assert _extract_async_client(m1) is _extract_async_client(m2)
    assert _DEFAULT_OPENAI_BASE_URL in _SHARED_HTTP_CLIENTS[id(asyncio.get_running_loop())]


async def test_different_base_url_uses_distinct_clients():
    """不同 base_url 使用不同 AsyncClient。"""

    m1 = build_openai_compatible_chat_model(
        "m",
        None,
        base_url="https://api.deepseek.com/v1",
        chat_model_class=ChatOpenAI,
        log_event="test_build_5",
        log_label="Test",
    )
    m2 = build_openai_compatible_chat_model(
        "m",
        None,
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        chat_model_class=ChatOpenAI,
        log_event="test_build_6",
        log_label="Test",
    )
    assert _extract_async_client(m1) is not _extract_async_client(m2)


async def test_factory_build_reuses_shared_client():
    """真实生产入口 factory.build_chat_model 经 DeepSeekProvider 接入共享客户端。

    绕过 fake 回退：注入 dummy Key 到自定义 api_key_env，使 factory 走真实 DeepSeek 分支。
    """

    environ["TEST_DEEPSEEK_KEY"] = "dummy"
    settings = ModelSettings(api_key_env="TEST_DEEPSEEK_KEY", base_url="https://api.deepseek.com/v1")
    model = build_chat_model("deepseek-v4-flash", model_settings=settings)
    # 断言：生产路径构造出的模型持有进程级共享客户端（同一循环同 base_url 取用一致）。
    client = _extract_async_client(model)
    reused = get_shared_model_http_client("https://api.deepseek.com/v1")
    assert client is reused
    assert not client.is_closed


async def test_cross_event_loop_does_not_reuse_stale_client():
    """跨事件循环不复用绑定到旧循环的客户端：旧循环 client 已失效时应新建。

    验证缓存按 loop id 维度隔离，避免「Event loop is closed」类跨循环故障。
    直接构造一个已关闭的旧循环对象，模拟其在缓存中持有一个失效 client，
    再在当前（主）事件循环取用同 base_url，应新建而非复用旧维度 client。
    """

    base_url = "https://api.deepseek.com/v1"

    # 模拟：旧循环（已关闭）缓存了一个 client（仅占位，不触网）
    old_loop = asyncio.new_event_loop()
    old_loop.close()
    stale = httpx.AsyncClient()
    _SHARED_HTTP_CLIENTS[id(old_loop)] = {base_url: stale}

    # 当前（主）事件循环内取用同 base_url：应新建，而非复用旧 loop 维度的 stale
    fresh = get_shared_model_http_client(base_url)
    assert fresh is not stale
    assert not fresh.is_closed


async def test_close_shared_clients_closes_and_clears():
    """close_shared_model_http_clients 关闭所有客户端并清空缓存。"""

    get_shared_model_http_client("https://api.deepseek.com/v1")
    get_shared_model_http_client("https://dashscope.aliyuncs.com/compatible-mode/v1")
    assert len(_SHARED_HTTP_CLIENTS) == 1  # 同循环：外层一个 loop 维度
    assert sum(len(v) for v in _SHARED_HTTP_CLIENTS.values()) == 2
    await close_shared_model_http_clients()
    assert len(_SHARED_HTTP_CLIENTS) == 0


async def test_deepseek_reasoning_content_preserved_after_refactor():
    """重构后 DeepSeekChatOpenAI 继承共享基类，reasoning_content 收集逻辑仍正确。

    验证：DeepSeek 接入共享构建基座后，流式 chunk 中的 ``reasoning_content`` 仍被
    写入 ``AIMessageChunk.additional_kwargs``，未被继承重构破坏。
    """

    from langchain_core.messages import AIMessageChunk

    environ["TEST_DEEPSEEK_KEY"] = "dummy"
    settings = ModelSettings(api_key_env="TEST_DEEPSEEK_KEY", base_url="https://api.deepseek.com/v1")
    model = build_chat_model("deepseek-v4-flash", model_settings=settings)
    assert isinstance(model, ChatOpenAI)

    chunk = {"choices": [{"delta": {"content": "hi", "reasoning_content": "thinking"}}]}
    generation_chunk = model._convert_chunk_to_generation_chunk(chunk, AIMessageChunk, None)
    assert generation_chunk is not None
    message = generation_chunk.message
    assert isinstance(message, AIMessageChunk)
    assert message.additional_kwargs.get("reasoning_content") == "thinking"
