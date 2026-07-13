"""针对模型上下文预算校验的测试。"""

import unittest

from app.context.budget import (
    ContextBudgetExceeded,
    measure_message_chars,
    validate_context_budget,
)
from app.models.base import RuntimeMessage


class ContextBudgetTests(unittest.TestCase):
    """校验第一版的模型上下文预算检查。"""

    def test_measure_message_chars_counts_text_and_string_metadata(self) -> None:
        """校验预算测量包含有用的、面向模型的文本。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果测量的大小不正确。

        副作用:
            无。
        """

        messages = [
            RuntimeMessage(
                role="tool",
                content_text="hello",
                metadata={"tool_name": "read_file", "count": 3},
            )
        ]

        self.assertEqual(measure_message_chars(messages), len("helloread_file"))

    def test_validate_context_budget_allows_equal_budget(self) -> None:
        """校验恰好处于上限的消息集会被接受。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果等大小上下文被拒绝。

        副作用:
            无。
        """

        validate_context_budget([RuntimeMessage(role="user", content_text="abcd")], 4)

    def test_validate_context_budget_rejects_oversized_context(self) -> None:
        """校验过大的消息文本会在服务商 I/O 之前失败。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果接受了过大的上下文。

        副作用:
            无。
        """

        with self.assertRaises(ContextBudgetExceeded):
            validate_context_budget([RuntimeMessage(role="user", content_text="abcde")], 4)

    def test_validate_context_budget_rejects_invalid_limit(self) -> None:
        """校验非正的限制会被显式拒绝。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果接受了非法的限制。

        副作用:
            无。
        """

        with self.assertRaises(ValueError):
            validate_context_budget([], 0)


if __name__ == "__main__":
    unittest.main()
