"""工具的后台结果能力声明（``ToolDefinition.background``）。

声明了能力的工具在参数模型里显式带一个 ``background`` 字段（用
:meth:`BackgroundCapability.args_field` 生成，描述随能力的默认值变化），handler 把它解析后交给
``run_background_work``；普通工具不声明，不受影响。
字段显式写在参数模型里而不是注册时动态补：参数模型是工具契约的唯一事实源，注册不改写它。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import Field

BACKGROUND_FIELD = "background"


@dataclass(frozen=True)
class BackgroundCapability:
    """``default_background``：模型不填 ``background`` 时是否后台执行。

    前台等待一律可由用户转到后台：作业照常运行，结论改为回投，对作业没有区别。

    没有前台时限：前台就一直等到结论（超时、取消由作业自己收尾），不会自动转后台、也不会因此
    挂起 Agent。进入后台后，未结占位会阻止本 Run 正常完成。
    """

    default_background: bool

    def resolve_background(self, requested: bool | None) -> bool:
        return self.default_background if requested is None else requested

    def args_field(self) -> Any:
        """参数模型里 ``background: bool | None`` 的字段定义。

        strict schema 要求全部字段必填，所以用 null 表示「用工具默认值」。
        """

        default = "background" if self.default_background else "foreground"
        return Field(
            default=None,
            description=(
                "true returns at once so you can keep working; the result arrives later as a "
                "<background_result> message for this call (wait_messages pauses for it). false "
                "waits here until it finishes. null uses the default "
                f"({default})."
            ),
        )
