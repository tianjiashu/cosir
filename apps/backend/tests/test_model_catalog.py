"""``ModelCatalog`` 最大上下文窗口回填单元测试。

覆盖设计文档阶段 5 ①契约：
- deepseek 精确覆盖优先（不查 litellm 目录）；
- 未收录模型懒查 litellm 目录回填 ``max_input_tokens``；
- 目录查询失败静默回退兜底窗口（不抛）。
"""

from unittest.mock import patch

from app.core.llm.model_catalog import ModelCatalog


def test_deepseek_exact_window_used_without_catalog() -> None:
    """deepseek 精确覆盖优先：直接返回 1M，不触发 litellm 目录查询。"""
    with patch(
        "litellm.get_model_info",
        side_effect=AssertionError("deepseek should not hit litellm catalog"),
    ):
        assert ModelCatalog.max_context_window("deepseek/deepseek-v4-flash") == 1_000_000


def test_unknown_model_falls_back_to_128k_when_catalog_misses() -> None:
    """目录未收录（get_model_info 返回无 max_input_tokens）→ 回退 128k。"""
    with patch("litellm.get_model_info", return_value={}):
        assert ModelCatalog.max_context_window("deepseek/unknown-model") == 128_000


def test_unknown_model_backfilled_from_litellm_catalog() -> None:
    """目录收录未知模型（返回 max_input_tokens）→ 回填该窗口。"""
    with patch(
        "litellm.get_model_info",
        return_value={"max_input_tokens": 200_000},
    ):
        assert ModelCatalog.max_context_window("deepseek/some-200k-model") == 200_000


def test_catalog_query_failure_does_not_raise() -> None:
    """目录查询抛异常 → 静默回退 128k（不抛）。"""
    with patch("litellm.get_model_info", side_effect=RuntimeError("catalog down")):
        assert ModelCatalog.max_context_window("deepseek/whatever") == 128_000


def test_catalog_result_cached_in_process() -> None:
    """同一模型重复查询命中进程内缓存，不再重复调 litellm。"""
    calls: list[str] = []

    def fake_get_model_info(model: str) -> dict:
        calls.append(model)
        return {"max_input_tokens": 150_000}

    ModelCatalog._litellm_window_cache.pop("some-cached-model", None)
    with patch("litellm.get_model_info", side_effect=fake_get_model_info):
        assert ModelCatalog.max_context_window("deepseek/some-cached-model") == 150_000
        assert ModelCatalog.max_context_window("deepseek/some-cached-model") == 150_000
    # 第二次命中缓存，litellm 只被调一次
    assert calls == ["some-cached-model"]
    ModelCatalog._litellm_window_cache.pop("some-cached-model", None)
