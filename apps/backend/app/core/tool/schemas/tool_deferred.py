"""工具的延迟结果能力声明（``ToolDefinition.deferred``）。

声明了能力的工具在参数模型里显式带一个 ``background`` 字段（用 :func:`background_field` 生成，描述随
能力的默认值与前台上限变化），handler 把它解析后交给 ``run_deferred``；普通工具不声明，不受影响。
字段显式写在参数模型里而不是注册时动态补：参数模型是工具契约的唯一事实源，注册不改写它。设计见
``docs/plan/deferred-tool-result-design.md``「新增工具如何接入」。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import Field

BACKGROUND_FIELD = "background"


@dataclass(frozen=True)
class DeferredCapability:
    """``default_background``：模型不填 ``background`` 时是否后台执行；``foreground_limit_s``：前台
    等待上限，到点自动转后台（``None`` 不设上限）；``detachable``：前台等待能否被用户转到后台。"""

    default_background: bool
    foreground_limit_s: float | None
    detachable: bool = True

    def resolve_background(self, requested: bool | None) -> bool:
        return self.default_background if requested is None else requested


def background_field(capability: DeferredCapability) -> Any:
    """参数模型里 ``background: bool | None`` 的字段定义。

    strict schema 要求全部字段必填，所以用 null 表示「用工具默认值」。
    """

    default = "background" if capability.default_background else "foreground"
    limit = (
        f" Foreground waits over {capability.foreground_limit_s:g}s move to the background "
        "automatically."
        if capability.foreground_limit_s is not None
        else ""
    )
    return Field(
        default=None,
        description=(
            "true returns at once so you can keep working; the result arrives later as a "
            "<background_result> message for this call (wait_messages pauses for it). null uses "
            f"the default ({default}).{limit}"
        ),
    )
