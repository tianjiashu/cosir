"""langchain_bridge 边界转换纯函数单测（本次改动涉及模块）。

覆盖 runtime_to_langchain / model_tools_to_langchain / tool_calls_from_langchain /
_tool_calls_from_metadata，它们是与本次 llm 包改动相关的转换入口，需达到 >80% 行覆盖。

注意：本次改动后 assistant 的 tool_calls 以 JSON 字符串形式存放在
``metadata["tool_calls"]``（runner 侧用 ``json.dumps(..., default=str)`` 序列化），
``runtime_to_langchain`` 通过新增的 ``_tool_calls_from_metadata`` 反序列化，并改用正确的
字段名 ``args`` / ``id``（原先错用 ``arguments`` / ``tool_call_id``）。本文件的断言据此新契约。
"""
import json

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from tests.conftest_stub_log import install_log_crud_stub

install_log_crud_stub()

from app.core.llm.langchain_bridge import (  # noqa: E402
    _tool_calls_from_metadata,
    model_tools_to_langchain,
    runtime_to_langchain,
    tool_calls_from_langchain,
)
from app.models.runtime_message import RuntimeMessage  # noqa: E402
from app.tools.schemas.tool_definition import ToolDefinition  # noqa: E402


def _noop(*_a, **_k):  # noqa: ANN
    return None


def test_runtime_to_langchain_system_and_user() -> None:
    """system / user 角色映射为对应 LangChain 消息类型。

    可能发现的缺陷：角色映射分支遗漏或错误。
    """

    msgs = [
        RuntimeMessage(role="system", content_text="sys"),
        RuntimeMessage(role="user", content_text="hi"),
    ]
    out = runtime_to_langchain(msgs)
    assert isinstance(out[0], SystemMessage)
    assert isinstance(out[1], HumanMessage)
    assert out[0].content == "sys"
    assert out[1].content == "hi"


def test_runtime_to_langchain_assistant_with_tool_calls() -> None:
    """assistant 角色带 tool_calls（JSON 字符串元数据）应映射为 AIMessage 且含正确 name/args/id。

    可能发现的缺陷：runner 以 json.dumps 存储的 tool_calls 未被反序列化；字段名仍错用
    arguments/tool_call_id 而非 args/id，导致 graph bind_tools 后无法识别调用。
    """

    msg = RuntimeMessage(
        role="assistant",
        content_text="let me call",
        metadata={
            "tool_calls": json.dumps(
                [{"name": "search", "args": {"q": "x"}, "id": "call-1"}]
            )
        },
    )
    out = runtime_to_langchain([msg])
    assert isinstance(out[0], AIMessage)
    assert len(out[0].tool_calls) == 1
    # 较新 langchain-core 的 AIMessage.tool_calls 元素会附带多余的 "type" 键，
    # 仅断言业务相关字段（name/args/id）被正确投影。
    tc = out[0].tool_calls[0]
    assert tc["name"] == "search"
    assert tc["args"] == {"q": "x"}
    assert tc["id"] == "call-1"


def test_runtime_to_langchain_assistant_tool_calls_args_not_dict() -> None:
    """assistant 的 args 非 dict 时应回退为空 dict（防御性边界）。

    可能发现的缺陷：非 dict args 直接传入导致下游解析异常。
    """

    msg = RuntimeMessage(
        role="assistant",
        content_text="x",
        metadata={"tool_calls": json.dumps([{"name": "t", "args": "bad", "id": "c1"}])},
    )
    out = runtime_to_langchain([msg])
    tc = out[0].tool_calls[0]
    assert tc["name"] == "t"
    assert tc["args"] == {}
    assert tc["id"] == "c1"


def test_runtime_to_langchain_assistant_missing_id_falls_back() -> None:
    """assistant 的 tool_calls 缺 id 时应回退到空串（args/id 字段契约）。

    可能发现的缺陷：id 缺失导致 None，LangGraph 校验失败。
    """

    msg = RuntimeMessage(
        role="assistant",
        content_text="x",
        metadata={"tool_calls": json.dumps([{"name": "t", "args": {"a": 1}}])},
    )
    out = runtime_to_langchain([msg])
    tc = out[0].tool_calls[0]
    assert tc["name"] == "t"
    assert tc["args"] == {"a": 1}
    assert tc["id"] == ""


def test_runtime_to_langchain_tool_role() -> None:
    """tool 角色映射为 ToolMessage，tool_call_id 取自元数据。"""

    msg = RuntimeMessage(
        role="tool", content_text="result", metadata={"tool_call_id": "tc-1"}
    )
    out = runtime_to_langchain([msg])
    assert isinstance(out[0], ToolMessage)
    assert out[0].tool_call_id == "tc-1"


def test_runtime_to_langchain_unknown_role_defaults_to_human() -> None:
    """未知角色应回退为 HumanMessage（防御性默认分支）。

    可能发现的缺陷：未知角色直接抛错或路由到错误类型。
    """

    msg = RuntimeMessage(role="weird", content_text="z")
    out = runtime_to_langchain([msg])
    assert isinstance(out[0], HumanMessage)
    assert out[0].content == "z"


def test_runtime_to_langchain_empty_list() -> None:
    """空消息列表应返回空列表。"""

    assert runtime_to_langchain([]) == []


def test_model_tools_to_langchain_shape() -> None:
    """工具定义应转换为 bind_tools 接受的内部函数 schema（不含 type/function 包装）。

    可能发现的缺陷：schema 被多余包装（如 {"type":"function","function":{...}}）导致
    bind_tools 收到双重包装而报错；或 name/description/parameters 字段缺失/错位。
    """

    tool = ToolDefinition(
        name="search",
        description="search web",
        permission="read",
        required_params=["q"],
        handler=_noop,
        parameters_schema={"type": "object", "properties": {"q": {"type": "string"}}},
    )
    out = model_tools_to_langchain([tool])
    assert out == [
        {
            "name": "search",
            "description": "search web",
            "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
        }
    ]


def test_model_tools_to_langchain_empty() -> None:
    """空工具列表应返回空列表（workflow 中用于跳过 bind_tools）。"""

    assert model_tools_to_langchain([]) == []


def test_tool_calls_from_langchain() -> None:
    """LangChain tool_calls 应还原为内部 ToolCall 列表。

    可能发现的缺陷：name/args/id 字段映射错位。
    """

    calls = [
        {"name": "t1", "args": {"a": 1}, "id": "id-1"},
        {"name": "t2", "args": None, "id": None},
    ]
    out = tool_calls_from_langchain(calls)
    assert out[0].tool_name == "t1"
    assert out[0].arguments == {"a": 1}
    assert out[0].call_id == "id-1"
    assert out[1].tool_name == "t2"
    assert out[1].arguments == {}
    assert out[1].call_id == ""


# ===================== 改动A：_tool_calls_from_metadata 边界 =====================


def test_tool_calls_from_metadata_valid_json_list() -> None:
    """合法 JSON 列表应被反序列化为 tool_calls 字典列表。

    可能发现的缺陷：对合法 JSON 解析失败或返回错误类型。
    """

    raw = json.dumps([{"name": "x", "args": {"a": 1}, "id": "1"}])
    out = _tool_calls_from_metadata(raw)
    assert out == [{"name": "x", "args": {"a": 1}, "id": "1"}]


def test_tool_calls_from_metadata_none_returns_empty() -> None:
    """raw 为 None 时应返回空列表（metadata 无 tool_calls 场景）。

    可能发现的缺陷：None 未被视为「无」，导致 TypeError 或异常。
    """

    assert _tool_calls_from_metadata(None) == []


def test_tool_calls_from_metadata_empty_string_returns_empty() -> None:
    """raw 为空字符串时应返回空列表（防御性边界）。

    可能发现的缺陷：空串进入 json.loads 抛异常未被捕获。
    """

    assert _tool_calls_from_metadata("") == []
    assert _tool_calls_from_metadata("   ") == []


def test_tool_calls_from_metadata_invalid_json_returns_empty() -> None:
    """raw 为非法 JSON 时应返回空列表且不抛异常（防御性边界）。

    可能发现的缺陷：非法 JSON 未被 ValueError 捕获导致崩溃。
    """

    assert _tool_calls_from_metadata("{not valid json") == []
    assert _tool_calls_from_metadata("null") == []


def test_tool_calls_from_metadata_non_list_returns_empty() -> None:
    """raw 反序列化结果非 list（如 dict/str）时应返回空列表。

    可能发现的缺陷：非 list 结果被原样返回，下游遍历时按 dict 处理出错。
    """

    assert _tool_calls_from_metadata(json.dumps({"name": "x"})) == []
    assert _tool_calls_from_metadata(json.dumps("a string")) == []


# ===================== 改动A：runtime_to_langchain assistant 异常/边界 =====================


def test_runtime_to_langchain_assistant_no_tool_calls_metadata() -> None:
    """assistant 消息无 tool_calls 元数据时应产出空 tool_calls 且为 AIMessage（不崩溃）。

    可能发现的缺陷：metadata 缺失 tool_calls 键时对 None 调用 json.loads 崩溃。
    """

    msg = RuntimeMessage(role="assistant", content_text="just text")
    out = runtime_to_langchain([msg])
    assert isinstance(out[0], AIMessage)
    assert out[0].tool_calls == []


def test_runtime_to_langchain_assistant_invalid_tool_calls_json() -> None:
    """assistant 的 tool_calls 元数据为非法 JSON 时应返回空 tool_calls 且不崩溃。

    可能发现的缺陷：非法 JSON 未被容错，导致 runtime_to_langchain 抛异常。
    """

    msg = RuntimeMessage(
        role="assistant",
        content_text="x",
        metadata={"tool_calls": "{broken"},
    )
    out = runtime_to_langchain([msg])
    assert isinstance(out[0], AIMessage)
    assert out[0].tool_calls == []


def test_runtime_to_langchain_assistant_tool_calls_non_list_json() -> None:
    """assistant 的 tool_calls 元数据为合法 JSON 但非 list 时应返回空 tool_calls。

    可能发现的缺陷：runner 误存 dict 形式时被直接遍历，逐元素缺 name 报错。
    """

    msg = RuntimeMessage(
        role="assistant",
        content_text="x",
        metadata={"tool_calls": json.dumps({"name": "x", "args": {}, "id": "1"})},
    )
    out = runtime_to_langchain([msg])
    assert isinstance(out[0], AIMessage)
    assert out[0].tool_calls == []


def test_runtime_to_langchain_assistant_tool_calls_full_shape() -> None:
    """改动A核心场景：runner 以 json.dumps([{name,args,id}]) 存储，经转换产出正确 AIMessage。

    可能发现的缺陷：字段名仍错用 arguments/tool_call_id 而非 args/id，导致下游 graph
    无法识别工具调用（模型不会进入 tools 节点 / 校验失败）。
    """

    msg = RuntimeMessage(
        role="assistant",
        content_text="thinking...",
        metadata={
            "tool_calls": json.dumps(
                [{"name": "x", "args": {"a": 1}, "id": "1"}]
            )
        },
    )
    out = runtime_to_langchain([msg])
    assert isinstance(out[0], AIMessage)
    assert len(out[0].tool_calls) == 1
    tc = out[0].tool_calls[0]
    assert tc["name"] == "x"
    assert tc["args"] == {"a": 1}
    assert tc["id"] == "1"
    assert "arguments" not in tc
    assert "tool_call_id" not in tc
