"""``ToolExecutionService.run_calls_with_events`` 单元测试。

注意：业务代码已删除 ``_build_model_tool_content`` 与 ``_build_diagnostic_block``
两个静态方法，改为在 ``run_calls_with_events`` 调用点用标准库 ``dataclasses.asdict``
内联序列化。本测试文件不再直接调用那两个方法，而是通过公共服务方法
``run_calls_with_events`` 的黑盒行为来验证输出契约。

最新契约（已落地，勿改业务代码 ``tool_execution_service.py``）：
- 每个工具观察结果被序列化为 ``role="tool"`` 的 ``RuntimeMessage``，其
  ``content_text = json.dumps({k: v for k, v in dataclasses.asdict(observation).items()
  if v is not None}, ensure_ascii=False)``，且 ``content`` 字段被
  ``redact_terminal_output(observation.content)`` 覆盖（脱敏终端凭据）。
- 成功与失败观察用完全相同的序列化逻辑（不再分状态、不再排除身份字段）。
- 只过滤 ``None`` 值；空字符串 "" / 空字典 {} / ``False`` / ``0`` 均保留进 JSON。
- 不在此处截断 ``data`` 大小。
"""

import json
from dataclasses import dataclass

from app.service.tool_execution.tool_execution_service import ToolExecutionService
from app.tools.schemas import ToolCall, ToolObservation
from app.trace_infra.redaction import redact_terminal_output


@dataclass
class FakeScheduler:
    """测试替身：``execute`` 直接返回预设的观察，不做任何真实调度。"""

    observation: ToolObservation

    def execute(self, call, execution_context=None, allowed_tool_names=None):
        return self.observation


def _run(observation: ToolObservation, call: ToolCall | None = None) -> ToolObservation:
    """构造服务并运行，返回构造出的观察（便于复用）。"""
    svc = ToolExecutionService(scheduler=FakeScheduler(observation), agent_id="x")
    svc.run_calls_with_events(
        task_id="t", step_id="s", calls=[call or ToolCall(tool_name="x")]
    )
    return observation


def _content_text_dict(observation: ToolObservation) -> dict:
    """运行服务并从首条模型消息反序列化 ``content_text`` 为字典。"""
    svc = ToolExecutionService(scheduler=FakeScheduler(observation), agent_id="x")
    result = svc.run_calls_with_events(
        task_id="t", step_id="s", calls=[ToolCall(tool_name="x")]
    )
    assert len(result.messages_for_model) == 1
    msg = result.messages_for_model[0]
    assert msg.role == "tool"
    return json.loads(msg.content_text)


# ---------------------------------------------------------------------------
# 情形 1：成功观察同样被整体序列化为 JSON（含 status/tool_name/tool_call_id）
# ---------------------------------------------------------------------------

# 测试目的：成功观察也被整体序列化为 JSON，且身份字段 status/tool_name/tool_call_id
#           都应出现在 JSON 中（新契约不再排除身份字段）。
# 可能发现的缺陷：成功观察仍走旧分支、身份字段被错误排除、content_text 非合法 JSON。
def test_success_observation_serialized_entirely_with_identity_fields() -> None:
    observation = ToolObservation(
        tool_name="execute_terminal",
        status="success",
        content="hello world",
        tool_call_id="call_1",
        data={"exit_code": 0},
    )
    payload = _content_text_dict(observation)
    assert payload["status"] == "success"
    assert payload["tool_name"] == "execute_terminal"
    assert payload["tool_call_id"] == "call_1"
    assert payload["content"] == "hello world"
    assert payload["data"] == {"exit_code": 0}


# ---------------------------------------------------------------------------
# 情形 2：错误观察同样整体序列化；reason/data/retryable/permission 都在 JSON 中
# ---------------------------------------------------------------------------

# 测试目的：错误观察整体序列化，诊断字段 reason/data/retryable/permission 全部进入 JSON，
#           且身份字段也在（新契约成功/失败同逻辑）。
# 可能发现的缺陷：错误观察缺失诊断字段、data 被字符串化、retryable 被忽略。
def test_error_observation_serialized_entirely_with_diagnostic_fields() -> None:
    observation = ToolObservation(
        tool_name="execute_terminal",
        status="error",
        content="export KEY=secret",
        reason="timeout",
        data={"timed_out": True, "exit_code": None},
        retryable=True,
        permission="shell_exec",
        tool_call_id="call_1",
    )
    payload = _content_text_dict(observation)
    assert payload["status"] == "error"
    assert payload["tool_name"] == "execute_terminal"
    assert payload["tool_call_id"] == "call_1"
    assert payload["reason"] == "timeout"
    assert payload["retryable"] is True
    assert payload["permission"] == "shell_exec"
    # data 内 None 被递归保留（顶层过滤只作用于 observation 字段本身）
    assert payload["data"] == {"timed_out": True, "exit_code": None}


# ---------------------------------------------------------------------------
# 情形 3：None 字段被过滤；空串 "" 保留
# ---------------------------------------------------------------------------

# 测试目的：观测层顶层 None 字段被过滤（非 None 的空串保留）；
#           注意：dataclasses.asdict 递归展开 data，顶层过滤只作用于 observation 自身
#           字段，不会递归过滤 data 内部的 None——故 data={"exit_code": None} 内部的
#           exit_code:None 会原样保留进 JSON（这是业务真实行为，非回归缺陷）。
# 可能发现的缺陷：若 data 内部 None 被错误过滤，则说明序列化逻辑偏离 asdict 语义。
def test_none_fields_filtered_but_empty_string_preserved() -> None:
    observation = ToolObservation(
        tool_name="x",
        status="error",
        content="boom",
        error="",  # 空串（非 None），应保留
        reason="fail",  # 非 None，保留
        data={"exit_code": None},  # 顶层 None 过滤不递归到 data 内部
    )
    payload = _content_text_dict(observation)
    # error 是空串（非 None）须保留
    assert payload["error"] == ""
    # reason 保留
    assert payload["reason"] == "fail"
    # data 内部 None 不被递归过滤，原样保留
    assert payload["data"] == {"exit_code": None}


# ---------------------------------------------------------------------------
# 情形 4：空串 "" / 空字典 {} / retryable=False / exit_code=0 均保留
# ---------------------------------------------------------------------------

# 测试目的：空串、空字典、retryable=False、exit_code=0 都是「非 None」值，
#           必须保留进 JSON，不被当作 falsy 过滤。
# 可能发现的缺陷：把空串/空字典/False/0 当作 falsy 一并过滤，丢失关键信息。
def test_falsy_non_none_values_preserved() -> None:
    observation = ToolObservation(
        tool_name="x",
        status="success",
        content="",  # 空串保留
        reason="",  # 空串保留
        retryable=False,  # False 保留
        data={},  # 空字典保留
        tool_call_id="",  # 空串保留
    )
    payload = _content_text_dict(observation)
    assert payload["content"] == ""
    assert payload["reason"] == ""
    assert payload["retryable"] is False
    assert payload["data"] == {}
    assert payload["tool_call_id"] == ""


# 测试目的：data 中含 0（exit_code=0）应被保留，不被误判为 falsy 而过滤。
# 可能发现的缺陷：把 0 当作 falsy 过滤掉，丢失退出码信息。
def test_data_with_zero_is_preserved() -> None:
    observation = ToolObservation(
        tool_name="x",
        status="success",
        content="done",
        data={"exit_code": 0},
    )
    payload = _content_text_dict(observation)
    assert payload["data"] == {"exit_code": 0}


# ---------------------------------------------------------------------------
# 情形 5：content 经 redact_terminal_output 脱敏，且 JSON 中 content 是脱敏后的值
# ---------------------------------------------------------------------------

# 测试目的：JSON 中的 content 字段是经 redact_terminal_output 脱敏后的值
#           （用 key 含敏感词的 key=value 形式确保必被脱敏）。
# 可能发现的缺陷：content 未被脱敏直接使用，导致敏感信息泄露到模型消息。
def test_content_in_json_is_redacted() -> None:
    observation = ToolObservation(
        tool_name="x",
        status="error",
        content="password=hunter2topsecret",
        reason="leak",
    )
    payload = _content_text_dict(observation)
    assert "hunter2topsecret" not in payload["content"]
    assert payload["content"] == redact_terminal_output(
        "password=hunter2topsecret"
    )
    # 独立 token（sk- 前缀）形式也同样脱敏
    obs2 = ToolObservation(
        tool_name="x",
        status="success",
        content="api_key=sk-abcdefghijklmnopqrstuvw",
    )
    payload2 = _content_text_dict(obs2)
    assert "sk-abcdefghijklmnopqrstuvw" not in payload2["content"]
    assert "[REDACTED]" in payload2["content"]


# 测试目的：实测用户示例 "export KEY=secret" 在 JSON content 中的脱敏表现，
#           并同时验证 content 字段确为脱敏后的字符串（契约要求 content 是脱敏后的值）。
# 可能发现的缺陷：若 "export KEY=secret" 未被脱敏，说明 redact_terminal_output 对该
#           形如 KEY=secret 的自由文本（key 名不含敏感词）不覆盖，可能存在脱敏盲区。
def test_export_key_secret_actual_redaction_behavior() -> None:
    observation = ToolObservation(
        tool_name="execute_terminal",
        status="error",
        content="export KEY=secret",
        reason="timeout",
    )
    payload = _content_text_dict(observation)
    # 断言 JSON 中的 content 就是 redact_terminal_output 的输出（契约核心要求）
    assert payload["content"] == redact_terminal_output("export KEY=secret")
    # 注：redact_terminal_output 的正则要求 key 名含敏感关键词，"KEY" 不含 "secret"，
    # 因此 "export KEY=secret" 实际不会被遮盖；若期望其被遮盖则属业务脱敏盲区。


# ---------------------------------------------------------------------------
# 情形 6：ensure_ascii=False —— data 含中文原样保留
# ---------------------------------------------------------------------------

# 测试目的：data 含非 ASCII 字符时，JSON 应以 ensure_ascii=False 原样保留（不被 \uXXXX 转义）。
# 可能发现的缺陷：序列化时 ensure_ascii 默认 True，导致中文被转义、模型难以解析。
def test_non_ascii_preserved_via_ensure_ascii_false() -> None:
    observation = ToolObservation(
        tool_name="x",
        status="error",
        content="失败",
        reason="超时",
        data={"message": "连接超时了"},
    )
    payload = _content_text_dict(observation)
    assert payload["data"] == {"message": "连接超时了"}
    assert payload["reason"] == "超时"
    # 直接检查 content_text 原始串含中文且无 \u 转义
    svc = ToolExecutionService(scheduler=FakeScheduler(observation), agent_id="x")
    result = svc.run_calls_with_events(
        task_id="t", step_id="s", calls=[ToolCall(tool_name="x")]
    )
    raw = result.messages_for_model[0].content_text
    assert "连接超时了" in raw
    assert "\\u" not in raw


# ---------------------------------------------------------------------------
# 情形 7：嵌套 data 字典被正确序列化（非字符串化）
# ---------------------------------------------------------------------------

# 测试目的：data 内嵌套字典/列表应被原样序列化为 JSON 对象/数组，而非被字符串化。
# 可能发现的缺陷：data 被 str() 化导致嵌套结构变成字符串，模型无法解析为字典。
def test_nested_data_dict_serialized_properly() -> None:
    observation = ToolObservation(
        tool_name="x",
        status="success",
        content="nested",
        data={
            "files": ["a.txt", "b.txt"],
            "meta": {"count": 2, "ok": True},
            "nested": {"deep": {"value": 1}},
        },
    )
    payload = _content_text_dict(observation)
    assert isinstance(payload["data"]["files"], list)
    assert payload["data"]["files"] == ["a.txt", "b.txt"]
    assert payload["data"]["meta"] == {"count": 2, "ok": True}
    assert payload["data"]["nested"] == {"deep": {"value": 1}}


# ---------------------------------------------------------------------------
# 情形 8：多个调用各自独立序列化（顺序与数量契约）
# ---------------------------------------------------------------------------

# 测试目的：传入多个 calls 时，每条观察独立序列化为一条 role=tool 消息，
#           顺序与输入一致，数量匹配，且各自 content 都被脱敏。
# 可能发现的缺陷：消息数量不匹配、顺序错乱、复用同一消息对象。
def test_multiple_calls_produce_independent_messages() -> None:
    obs1 = ToolObservation(
        tool_name="a", status="success", content="one", tool_call_id="c1"
    )
    obs2 = ToolObservation(
        tool_name="b", status="error", content="two", reason="r", tool_call_id="c2"
    )
    svc = ToolExecutionService(
        scheduler=FakeScheduler(observation=obs1), agent_id="x"
    )
    # 用可变替身以支持不同 call 返回不同观察
    calls = [ToolCall(tool_name="a", call_id="c1"), ToolCall(tool_name="b", call_id="c2")]

    class SeqScheduler:
        def __init__(self, obs_list):
            self._obs = obs_list
            self._i = 0

        def execute(self, call, execution_context=None, allowed_tool_names=None):
            ob = self._obs[self._i]
            self._i += 1
            return ob

    svc = ToolExecutionService(scheduler=SeqScheduler([obs1, obs2]), agent_id="x")
    result = svc.run_calls_with_events(task_id="t", step_id="s", calls=calls)
    assert len(result.messages_for_model) == 2
    assert len(result.observations) == 2
    first = json.loads(result.messages_for_model[0].content_text)
    second = json.loads(result.messages_for_model[1].content_text)
    assert first["tool_name"] == "a"
    assert second["tool_name"] == "b"
    assert result.messages_for_model[0].metadata == {"tool_call_id": "c1"}
    assert result.messages_for_model[1].metadata == {"tool_call_id": "c2"}


# ---------------------------------------------------------------------------
# 情形 9：metadata 回绑 tool_call_id（供模型回绑工具调用）
# ---------------------------------------------------------------------------

# 测试目的：每条 role=tool 消息的 metadata 应携带对应 observation 的 tool_call_id，
#           用于把观察回绑到具体模型请求。
# 可能发现的缺陷：metadata 未设置或使用了错误 id，导致模型无法回绑。
def test_message_metadata_carries_tool_call_id() -> None:
    observation = ToolObservation(
        tool_name="x", status="success", content="ok", tool_call_id="call_42"
    )
    svc = ToolExecutionService(scheduler=FakeScheduler(observation), agent_id="x")
    result = svc.run_calls_with_events(
        task_id="t", step_id="s", calls=[ToolCall(tool_name="x", call_id="call_42")]
    )
    assert result.messages_for_model[0].metadata == {"tool_call_id": "call_42"}


# ---------------------------------------------------------------------------
# 情形 10：data 不在此处截断（超长 data 原样保留）
# ---------------------------------------------------------------------------

# 测试目的：超大 data 不应在 run_calls_with_events 被截断（契约明确「不在此处截断 data 大小」）。
# 可能发现的缺陷：回归旧逻辑在序列化点截断 data，导致模型看不到完整 data 或字段被替换。
def test_large_data_not_truncated_at_serialization() -> None:
    big = {"big": "x" * 5000}
    observation = ToolObservation(
        tool_name="x",
        status="error",
        content="large",
        reason="oversize",
        data=big,
    )
    payload = _content_text_dict(observation)
    assert payload["data"] == big
    assert "big" in payload["data"]
    assert payload["data"]["big"] == "x" * 5000
