"""factory 单元测试：确保构建出的 ChatLiteLLM 默认开启真正的逐 token 流式。

回归防护：build_chat_model 曾缺失 streaming=True，导致 LangChain 的 astream 退化成
ainvoke + 单次 yield，每个模型 step 只产出 1 个整块 chunk，前端无流式感。
"""

from app.llm_provider.factory import build_chat_model


def test_build_chat_model_enables_streaming() -> None:
    """构建的模型必须 streaming=True，否则 astream 不会逐 token 流式。"""

    model = build_chat_model("deepseek/deepseek-v4-flash")

    assert getattr(model, "streaming", False) is True
