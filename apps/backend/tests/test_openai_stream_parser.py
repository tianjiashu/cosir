"""针对 OpenAI 兼容流式响应解析的测试。"""

import json
import os
import unittest

from app.models.base import ModelToolDefinition, RuntimeMessage
from app.models.openai_compatible import (
    OpenAICompatibleModelConfig,
    OpenAICompatibleStreamingAdapter,
    _build_request_payload,
    _to_provider_tool,
)
from app.models.openai_stream_parser import parse_openai_sse_lines


class OpenAIStreamParserTests(unittest.TestCase):
    """校验 OpenAI 兼容的流式分块会变为运行时增量。"""

    def test_parse_text_and_done_chunks(self) -> None:
        """校验文本分块与 DONE 标记会产生文本与最终增量。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果解析器输出与期望的增量不匹配。

        副作用:
            无。
        """

        lines = [
            _data({"choices": [{"delta": {"content": "hello"}, "finish_reason": None}]}),
            _data({"choices": [{"delta": {"content": " world"}, "finish_reason": "stop"}]}),
        ]

        deltas = list(parse_openai_sse_lines(lines))

        self.assertEqual([delta.text for delta in deltas[:2]], ["hello", " world"])
        self.assertTrue(deltas[-1].is_final)

    def test_parse_streamed_tool_call(self) -> None:
        """校验流式工具调用分片会产生一个 ToolCall 增量。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果工具名或参数被错误解析。

        副作用:
            无。
        """

        lines = [
            _data(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "id": "call_read_1",
                                        "index": 0,
                                        "function": {
                                            "name": "read_",
                                            "arguments": "{\"path\"",
                                        },
                                    }
                                ]
                            },
                            "finish_reason": None,
                        }
                    ]
                }
            ),
            _data(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "function": {
                                            "name": "file",
                                            "arguments": ": \"note.txt\"}",
                                        },
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                }
            ),
        ]

        deltas = list(parse_openai_sse_lines(lines))

        self.assertEqual(len(deltas), 1)
        self.assertEqual(deltas[0].tool_call.tool_name, "read_file")
        self.assertEqual(deltas[0].tool_call.arguments, {"path": "note.txt"})
        self.assertEqual(deltas[0].tool_call.call_id, "call_read_1")

    def test_rejects_non_object_tool_arguments(self) -> None:
        """校验工具调用参数必须能被解析为一个 JSON 对象。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果数组参数被当作工具调用接受。

        副作用:
            无。
        """

        lines = [
            _data(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "function": {
                                            "name": "read_file",
                                            "arguments": "[\"note.txt\"]",
                                        },
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                }
            ),
        ]

        with self.assertRaisesRegex(ValueError, "JSON object"):
            list(parse_openai_sse_lines(lines))

    def test_adapter_reports_missing_api_key(self) -> None:
        """校验当服务商 API Key 缺失时适配器会清晰地失败。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果缺失的 API Key 未抛出 RuntimeError。

        副作用:
            临时移除测试用的 API Key 环境变量。
        """

        env_name = "CODING_AGENT_TEST_MISSING_KEY"
        os.environ.pop(env_name, None)
        adapter = OpenAICompatibleStreamingAdapter(
            OpenAICompatibleModelConfig(
                base_url="https://example.invalid/v1",
                api_key_env=env_name,
                model="test-model",
            )
        )

        with self.assertRaisesRegex(RuntimeError, "missing API key"):
            async def collect() -> list:
                """为缺失 Key 的请求收集适配器增量。

                参数:
                    无。

                返回:
                    收集到的模型增量。

                异常:
                    RuntimeError: 当 API Key 缺失时按预期抛出。

                副作用:
                    无。
                """

                return [
                    delta
                    async for delta in adapter.stream(
                        [RuntimeMessage(role="user", content_text="hello")]
                    )
                ]

            import asyncio

            asyncio.run(collect())

    def test_openai_tool_payload_uses_function_schema(self) -> None:
        """校验模型工具被序列化为 OpenAI 兼容的函数型工具。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果工具载荷格式不正确。

        副作用:
            无。
        """

        payload = _to_provider_tool(
            ModelToolDefinition(
                name="read_file",
                description="Read a project file.",
                parameters_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            )
        )

        self.assertEqual(payload["type"], "function")
        self.assertEqual(payload["function"]["name"], "read_file")
        self.assertEqual(payload["function"]["parameters"]["required"], ["path"])

    def test_request_payload_includes_tools_when_available(self) -> None:
        """校验 OpenAI 请求载荷会暴露面向模型的工具模式。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果工具被从服务商载荷中遗漏。

        副作用:
            无。
        """

        payload = _build_request_payload(
            OpenAICompatibleModelConfig(
                base_url="https://example.invalid/v1",
                api_key_env="TEST_KEY",
                model="test-model",
            ),
            [RuntimeMessage(role="user", content_text="read note")],
            [
                ModelToolDefinition(
                    name="read_file",
                    description="Read a project file.",
                    parameters_schema={"type": "object", "properties": {}},
                )
            ],
        )

        self.assertEqual(payload["model"], "test-model")
        self.assertTrue(payload["stream"])
        self.assertEqual(payload["tool_choice"], "auto")
        self.assertTrue(payload["parallel_tool_calls"])
        self.assertEqual(payload["tools"][0]["function"]["name"], "read_file")

    def test_request_payload_disables_deepseek_thinking_by_default(self) -> None:
        """校验 DeepSeek 请求会显式关闭 thinking 模式。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果 payload 未带 thinking=disabled。

        副作用:
            无。
        """

        payload = _build_request_payload(
            OpenAICompatibleModelConfig(
                base_url="https://api.deepseek.com",
                api_key_env="TEST_KEY",
                model="deepseek-v4-flash",
            ),
            [RuntimeMessage(role="user", content_text="hello")],
            None,
        )

        self.assertEqual(payload["thinking"], {"type": "disabled"})

    def test_request_payload_omits_thinking_for_non_deepseek_provider(self) -> None:
        """校验非 DeepSeek 端点不会收到供应商私有的 thinking 字段。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果 payload 为通用兼容端点错误地附带了 thinking。

        副作用:
            无。
        """

        payload = _build_request_payload(
            OpenAICompatibleModelConfig(
                base_url="https://example.invalid/v1",
                api_key_env="TEST_KEY",
                model="test-model",
            ),
            [RuntimeMessage(role="user", content_text="hello")],
            None,
        )

        self.assertNotIn("thinking", payload)

    def test_parallel_tool_calls_are_rejected(self) -> None:
        """校验第一版解析器会拒绝并发的服务商工具调用。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果接受了多个工具调用。

        副作用:
            无。
        """

        lines = [
            _data(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "index": 0,
                                        "function": {
                                            "name": "read_file",
                                            "arguments": "{\"path\": \"a.txt\"}",
                                        },
                                    },
                                    {
                                        "id": "call_2",
                                        "index": 1,
                                        "function": {
                                            "name": "read_file",
                                            "arguments": "{\"path\": \"b.txt\"}",
                                        },
                                    },
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                }
            )
        ]

        deltas = list(parse_openai_sse_lines(lines))

        self.assertEqual([item.tool_call.call_id for item in deltas], ["call_1", "call_2"])

    def test_request_payload_serializes_tool_exchange_messages(self) -> None:
        """校验助手工具调用与工具观测会保留匹配的 id。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果服务商消息 id 或参数不正确。

        副作用:
            无。
        """

        payload = _build_request_payload(
            OpenAICompatibleModelConfig(
                base_url="https://example.invalid/v1",
                api_key_env="TEST_KEY",
                model="test-model",
            ),
            [
                RuntimeMessage(role="user", content_text="read note"),
                RuntimeMessage(
                    role="assistant",
                    content_text="",
                    metadata={
                        "tool_call_id": "call_read_1",
                        "tool_name": "read_file",
                        "tool_arguments_json": "{\"path\": \"note.txt\"}",
                    },
                ),
                RuntimeMessage(
                    role="tool",
                    content_text="note content",
                    metadata={
                        "tool_call_id": "call_read_1",
                        "tool_name": "read_file",
                    },
                ),
            ],
            [],
        )

        assistant_message = payload["messages"][1]
        tool_message = payload["messages"][2]
        self.assertEqual(assistant_message["tool_calls"][0]["id"], "call_read_1")
        self.assertEqual(
            assistant_message["tool_calls"][0]["function"]["arguments"],
            "{\"path\": \"note.txt\"}",
        )
        self.assertEqual(tool_message["tool_call_id"], "call_read_1")

    def test_tool_message_requires_tool_call_id(self) -> None:
        """校验格式错误的工具消息会在服务商 I/O 之前失败。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果接受了缺少 id 的工具消息。

        副作用:
            无。
        """

        with self.assertRaisesRegex(ValueError, "tool_call_id"):
            _build_request_payload(
                OpenAICompatibleModelConfig(
                    base_url="https://example.invalid/v1",
                    api_key_env="TEST_KEY",
                    model="test-model",
                ),
                [RuntimeMessage(role="tool", content_text="orphan observation")],
                [],
            )


def _data(payload: dict) -> str:
    """序列化一行 SSE data。

    参数:
        payload: 一行 SSE 的 JSON 载荷。

    返回:
        SSE ``data:`` 行。

    异常:
        TypeError: 如果载荷不可被 JSON 序列化。

    副作用:
        无。
    """

    return f"data: {json.dumps(payload)}"


if __name__ == "__main__":
    unittest.main()
