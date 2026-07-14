"""针对工作流步骤控制的测试。"""

import asyncio
import unittest
from unittest.mock import patch

from app.core.workflows.step_controller import (
    LangGraphStepController,
    MIN_SAFE_LANGGRAPH_VERSION,
    _version_at_least,
)


class LangGraphStepControllerTests(unittest.TestCase):
    """校验工作流步骤决定使用 LangGraph 控制器边界。"""

    def test_next_step_advances_until_limit(self) -> None:
        """校验步骤决定会递增，直到达到最大步骤数。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果继续决定不正确。

        副作用:
            在已安装时可能执行一个已编译的 LangGraph。
        """

        controller = LangGraphStepController()

        first = asyncio.run(controller.next_step(step_count=0, max_steps=1))
        second = asyncio.run(controller.next_step(step_count=1, max_steps=1))

        self.assertTrue(first.can_continue)
        self.assertEqual(first.step_count, 1)
        self.assertFalse(second.can_continue)
        self.assertEqual(second.step_count, 1)

    def test_missing_langgraph_uses_fallback(self) -> None:
        """校验缺失 LangGraph 时使用确定性回退逻辑。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果回退模式未产生正确的决定。

        副作用:
            临时打补丁修改已安装的 LangGraph 版本检测。
        """

        with patch(
            "app.core.workflows.step_controller._installed_langgraph_version",
            return_value=None,
        ):
            controller = LangGraphStepController()
            decision = asyncio.run(controller.next_step(step_count=0, max_steps=1))

        self.assertFalse(controller.uses_langgraph)
        self.assertTrue(decision.can_continue)

    def test_unsafe_langgraph_version_uses_fallback(self) -> None:
        """校验不安全的 LangGraph 版本不会被使用。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果使用了不安全的 LangGraph 版本。

        副作用:
            临时打补丁修改已安装的 LangGraph 版本检测。
        """

        with patch(
            "app.core.workflows.step_controller._installed_langgraph_version",
            return_value=(0, 6, 11),
        ):
            controller = LangGraphStepController()

        self.assertFalse(controller.uses_langgraph)

    def test_version_comparison_accepts_safe_langgraph_baseline(self) -> None:
        """校验最低安全 LangGraph 版本检查。

        参数:
            无。

        返回:
            无。

        异常:
            AssertionError: 如果版本比较不正确。

        副作用:
            无。
        """

        self.assertTrue(_version_at_least((1, 0, 10), MIN_SAFE_LANGGRAPH_VERSION))
        self.assertTrue(_version_at_least((1, 1, 0), MIN_SAFE_LANGGRAPH_VERSION))
        self.assertFalse(_version_at_least((0, 6, 11), MIN_SAFE_LANGGRAPH_VERSION))


if __name__ == "__main__":
    unittest.main()
