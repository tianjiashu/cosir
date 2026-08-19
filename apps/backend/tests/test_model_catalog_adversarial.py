"""ModelCatalog 对抗性边界测试（独立测试 Agent 新增）。

目标：挖掘 ``max_context_window`` 在目录回填边界下的缺陷。
- litellm 返回非 dict / 缺 max_input_tokens / 非 int / 非正数窗口；
- 进程内缓存被污染时对结果的影响（None 缓存）；
- deepseek 前缀剥离精确命中；
- 目录查询失败回退兜底且缓存 None。
"""

from unittest.mock import patch

from app.core.llm.model_catalog import ModelCatalog


def _reset(model: str) -> None:
    ModelCatalog._litellm_window_cache.pop(model, None)


def test_catalog_info_non_dict_returns_fallback() -> None:
    """litellm 返回非 dict（如 list/None）→ 回退 128k（不抛）。"""
    for bad in (None, [], "string", 42):
        _reset("some-non-dict-model")
        with patch("litellm.get_model_info", return_value=bad):
            assert ModelCatalog.max_context_window("deepseek/some-non-dict-model") == 128_000


def test_catalog_info_missing_max_input_tokens_returns_fallback() -> None:
    """litellm 返回 dict 但缺 max_input_tokens → 回退 128k。"""
    _reset("no-tokens-model")
    with patch("litellm.get_model_info", return_value={"something_else": 999}):
        assert ModelCatalog.max_context_window("deepseek/no-tokens-model") == 128_000


def test_catalog_info_non_int_window_returns_fallback() -> None:
    """max_input_tokens 为字符串/浮点 → 回退 128k（isinstance int 校验）。"""
    for bad in ("200000", 200000.0):
        _reset("bad-window-model")
        with patch("litellm.get_model_info", return_value={"max_input_tokens": bad}):
            assert ModelCatalog.max_context_window("deepseek/bad-window-model") == 128_000


def test_catalog_info_bool_window_accepted_as_int() -> None:
    """max_input_tokens 为 bool → 因 bool 是 int 子类被当作 1 窗口（观察性边界）。"""
    _reset("bool-window-model")
    with patch("litellm.get_model_info", return_value={"max_input_tokens": True}):
        assert ModelCatalog.max_context_window("deepseek/bool-window-model") == 1


def test_catalog_info_non_positive_window_returns_fallback() -> None:
    """max_input_tokens 为 0 或负数 → 回退 128k（>0 校验）。"""
    for bad in (0, -1):
        _reset("non-pos-model")
        with patch("litellm.get_model_info", return_value={"max_input_tokens": bad}):
            assert ModelCatalog.max_context_window("deepseek/non-pos-model") == 128_000


def test_catalog_lookup_exception_cached_as_none() -> None:
    """目录查询抛异常 → 结果（None）被缓存，二次查询不再调 litellm（不抛）。

    设计契约（model_catalog.py 第 67-68 行 docstring）："重复查询不再打日志 /
    不再调 litellm"。本用例锁定该行为，若失败说明 None 结果未被有效缓存。
    """
    calls: list[str] = []
    _reset("boom-model")

    def side_effect(model: str) -> dict:
        calls.append(model)
        raise RuntimeError("boom")

    with patch("litellm.get_model_info", side_effect=side_effect):
        assert ModelCatalog.max_context_window("deepseek/boom-model") == 128_000
        assert ModelCatalog.max_context_window("deepseek/boom-model") == 128_000
    # 契约要求第二次命中缓存，litellm 只调一次
    assert calls == ["boom-model"]
    _reset("boom-model")


def test_catalog_miss_result_not_repeatedly_re_queried() -> None:
    """目录未收录（返回 None）→ 二次查询同样不应再调 litellm（缓存契约）。"""
    calls: list[str] = []
    _reset("miss-model")

    def side_effect(model: str) -> dict:
        calls.append(model)
        return {}

    with patch("litellm.get_model_info", side_effect=side_effect):
        assert ModelCatalog.max_context_window("deepseek/miss-model") == 128_000
        assert ModelCatalog.max_context_window("deepseek/miss-model") == 128_000
    assert calls == ["miss-model"]
    _reset("miss-model")


def test_exact_deepseek_window_not_affected_by_catalog() -> None:
    """deepseek 精确窗口优先：即便目录回填了不同值也不受影响。"""
    _reset("deepseek-v4-flash")
    with patch("litellm.get_model_info", return_value={"max_input_tokens": 10}):
        assert ModelCatalog.max_context_window("deepseek/deepseek-v4-flash") == 1_000_000


def test_normalized_bare_model_with_nested_prefix() -> None:
    """多段前缀的裸名剥离：provider/a/b → b（rsplit 取末段）。"""
    _reset("b")
    with patch("litellm.get_model_info", return_value={"max_input_tokens": 64_000}):
        assert ModelCatalog.max_context_window("custom/a/b") == 64_000
    _reset("b")


def test_deepseek_window_via_cache_pollution_still_exact() -> None:
    """即使缓存里被写入错误的 deepseek-v4-flash 值，精确覆盖仍优先。"""
    ModelCatalog._litellm_window_cache["deepseek-v4-flash"] = 1
    try:
        with patch("litellm.get_model_info", side_effect=AssertionError("must not call")):
            assert ModelCatalog.max_context_window("deepseek/deepseek-v4-flash") == 1_000_000
    finally:
        _reset("deepseek-v4-flash")
