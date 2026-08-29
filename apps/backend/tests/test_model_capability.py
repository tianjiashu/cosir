"""``ModelCapability.get_capability`` 回归测试。

覆盖 D-review 发现的字段读取 bug：
- ``effort_map`` 位于 ``supports_reasoning_effort`` 子对象内，必须逐层读取而非读顶层；
- 未命中模型应回退保守默认、不抛异常；
- ``supports_reasoning_effort.supported=False`` 时不应被 dict 整体误判为 True；
- ``effort_map`` 的键/值映射应被正确还原。

所有用例使用的模型名均来自 ``model_capabilities.json`` 真实键，期望值与 JSON 一致。
"""

from app.llm_provider.capability.model_capability import ModelCapability


def test_effort_map_read_from_nested_object() -> None:
    """``effort_map`` 必须来自 ``supports_reasoning_effort.effort_map`` 而非顶层。"""

    cap = ModelCapability.get_capability("deepseek-v4-flash-vision-exp")

    assert cap.reasoning_effort.supported is True
    assert cap.reasoning_effort.effort_map == {"low": "low", "high": "high", "max": "max"}


def test_nonempty_effort_map_preserved() -> None:
    """支持的模型其 effort_map 非空映射应原样还原（kimi-k3: 1:1 映射）。"""

    cap = ModelCapability.get_capability("kimi-k3")

    assert cap.reasoning_effort.supported is True
    assert cap.reasoning_effort.effort_map == {"low": "low", "high": "high", "max": "max"}


def test_supported_false_not_misjudged_by_dict() -> None:
    """``supports_reasoning_effort.supported=False`` 不应因 dict 恒真而被误判为 True。"""

    cap = ModelCapability.get_capability("GLM-5")

    assert cap.reasoning_effort.supported is False
    assert cap.reasoning_effort.effort_map == {}


def test_unknown_model_returns_conservative_default_without_raise() -> None:
    """未命中模型应回退保守默认且永不抛异常。"""

    cap = ModelCapability.get_capability("no-such-model")

    assert cap.model_name == "no-such-model"
    assert cap.context_window == 0
    assert cap.max_output_tokens == 0
    assert cap.supports_thinking is False
    assert cap.supports_image is False
    assert cap.supports_video is False
    assert cap.need_reasoning_content is False
    assert cap.reasoning_effort.supported is False
    assert cap.reasoning_effort.effort_map == {}
    # 未知模型 image_limit 也应返回空保守默认（零值 + 空格式列表），不抛不 None
    assert cap.image_limit.supported_formats == []
    assert cap.image_limit.single_image_inline_max_bytes == 0
    assert cap.image_limit.max_images_per_request == 0


def test_image_limit_parsed_from_json() -> None:
    """``image_limit`` 子对象应从 JSON 正确解析为 ``ImageLimitCapability``。"""

    cap = ModelCapability.get_capability("deepseek-v4-flash-vision-exp")

    limit = cap.image_limit
    assert limit.supported_formats == ["jpeg", "png", "gif", "webp"]
    assert limit.external_url_max_chars == 8192
    assert limit.request_body_max_bytes == 50331648
    assert limit.single_image_inline_max_bytes == 33554432
    assert limit.single_image_file_id_max_bytes == 67108864
    assert limit.max_images_per_request == 600
    assert limit.request_total_max_bytes_inline_only == 67108864
    assert limit.request_total_max_bytes_with_file_id == 209715200
    assert limit.max_image_side_px == 8192
    assert limit.max_image_side_px_when_many == 4096
    assert limit.many_image_threshold == 15


def test_image_limit_missing_returns_default() -> None:
    """未声明 ``image_limit`` 的模型（如 GLM-5）应返回空保守默认，不抛不误判。"""

    cap = ModelCapability.get_capability("GLM-5")

    assert cap.image_limit.supported_formats == []
    assert cap.image_limit.single_image_inline_max_bytes == 0
    assert cap.image_limit.many_image_threshold == 0


def test_basic_fields_parsed_correctly() -> None:
    """基础能力字段应从 JSON 正确解析（含缺失的 need_reasoning_content 回退 False）。"""

    cap = ModelCapability.get_capability("GLM-5")

    assert cap.context_window == 204800
    assert cap.max_output_tokens == 131072
    assert cap.supports_thinking is True
    assert cap.supports_image is False
    assert cap.supports_video is False
    # GLM 系列 JSON 无该字段，应保守回退 False 而非 None/报错
    assert cap.need_reasoning_content is False
