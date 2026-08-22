"""content_to_text 归一逻辑单元测试。

锁定与 langchain_core.BaseMessage.text 一致的口径：只取字符串块与 type=='text'
的文本块，忽略其它块类型，text 非字符串时跳过。防止回归到旧 _content_to_text
「不校验 type 混入多模态副文本」的漂移。
"""

from app.utils.message_content import content_to_text


def test_str_passthrough():
    """字符串 content 原样返回。"""
    assert content_to_text("hello") == "hello"


def test_list_of_strings_joined():
    """纯字符串列表拼接。"""
    assert content_to_text(["a", "b", "c"]) == "abc"


def test_text_block_extracted():
    """type=='text' 的文本块被提取。"""
    assert content_to_text([{"type": "text", "text": "hi"}]) == "hi"


def test_mixed_string_and_text_block():
    """字符串块与 text 块混合拼接。"""
    content = ["prefix", {"type": "text", "text": "mid"}, "suffix"]
    assert content_to_text(content) == "prefixmidsuffix"


def test_non_text_block_ignored():
    """非 text 类型块（如 image_url）被忽略，不混入文本。"""
    content = [
        {"type": "text", "text": "visible"},
        {"type": "image_url", "text": "must-not-appear", "image_url": {"url": "x"}},
    ]
    assert content_to_text(content) == "visible"


def test_non_string_text_skipped():
    """text 字段非字符串时跳过，避免 join 报错。"""
    content = [{"type": "text", "text": 123}, {"type": "text", "text": "ok"}]
    assert content_to_text(content) == "ok"


def test_missing_text_key_in_text_block_ignored():
    """type=='text' 但无 text 键的块跳过。"""
    content = [{"type": "text"}, {"type": "text", "text": "keep"}]
    assert content_to_text(content) == "keep"


def test_unknown_block_type_with_text_ignored():
    """未知类型块即使含 text 键也不提取（严格按 type=='text' 过滤）。"""
    content = [{"type": "non_standard", "text": "leak"}]
    assert content_to_text(content) == ""


def test_empty_list_returns_empty():
    """空列表返回空串。"""
    assert content_to_text([]) == ""


def test_other_types_return_empty():
    """非 str/list 类型返回空串。"""
    assert content_to_text(None) == ""
    assert content_to_text(42) == ""


def test_last_chunk_carries_all_content_merged_extraction():
    """末 chunk 一次性给 content 时，合并后整体提取仍能捕获正文。

    锁定 L4 加固：output_text 改为从合并后的 ai_message.content 统一提取，与 _has_content
    同源。即使流式阶段前面 chunk 无文本 delta（text 为空未触发 MODEL_OUTPUT_DELTA），
    只要合并后 content 非空，逐 chunk 提取拼接与整体提取等价，正文必被捕获。
    """
    from langchain_core.messages import AIMessageChunk

    empty_chunk = AIMessageChunk(content="", id="c1")
    text_chunk = AIMessageChunk(content="完整正文", id="c2")
    merged = empty_chunk + text_chunk
    assert content_to_text(merged.content) == "完整正文"
