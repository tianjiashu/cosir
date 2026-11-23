"""内置浏览器工具族的共同部分。

每个浏览器工具恰好对应宿主的一个 ``browser.*`` 方法（``method``），调用经
``call_browser``、链路错误统一映射为浏览器错误观察，注册定义也相同。
子类只写参数怎么交给宿主（``execute`` 的签名就是参数模型的字段，照 ``model_dump()`` 转交；act 先把上传路径
解析成绝对路径）与结果怎么渲染。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar

from app.core.tool.schemas import ToolDefinition, ToolExecutionContext, ToolObservation
from app.core.tool.schemas.tool_grouping import TOOL_GROUP_BROWSER
from app.core.tool.tool_base import HandlerBase
from app.tools.browser_runtime.browser_call import call_browser
from app.tools.host.errors import HostCallError
from app.tools.tool_handler.browser.errors import browser_error_observation


class BrowserTool(HandlerBase):
    method: ClassVar[str]
    group: ClassVar[str] = TOOL_GROUP_BROWSER

    def call(
        self,
        execution_context: ToolExecutionContext,
        render: Callable[[Any], ToolObservation],
        **params: Any,
    ) -> ToolObservation:
        """调用本工具的宿主方法，成功交给 ``render``；链路或宿主错误直接成为错误观察。"""
        try:
            result = call_browser(self.method, execution_context, **params)
        except HostCallError as error:
            return browser_error_observation(self.name, error)
        return render(result)

    def to_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            group=self.group,
            description=self.description,
            handler=self.execute,
            args_model=self.args_model,
            # 浏览器一族只在网页任务里用、定义合计最大（约占全部内置工具定义的四成），延迟加载；总是成组用，
            # 模型一次 ``select:`` 就能取齐。
            deferred=True,
        )
