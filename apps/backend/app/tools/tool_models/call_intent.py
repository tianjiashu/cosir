"""调用意图：参数一眼看不懂的工具（终端命令、页面脚本）自己声明的展示参数。

其余工具的参数本身就说清了在做什么（路径、查询、网址），调用行按「动词 · 目标」展示即可，不为每次调用
多要一句话，所以这不是平台统一注入的字段：需要的工具在参数模型里用 :func:`intent_field` 声明，前端工具目录
按工具名从 ``args.intent`` 取标题。意图只服务展示：执行、授权与日志不读它，发给宿主的参数也不带它（导出宿主
schema 时按 :data:`INTENT_FIELD` 去掉）。

放在用它的参数模型旁边（终端、页面脚本都是 ``app/tools`` 的工具）；``core/tool`` 的工具要声明意图时，依赖方向
不允许从这里导入，届时再把它移回 ``core/tool``。
"""

from typing import Any

from pydantic import Field

INTENT_FIELD = "intent"
# 后台标签从命令首行回退时的截断长度；意图本身不限长，超长不拒绝调用，展示端自行截断。
MAX_INTENT_LENGTH = 160

# 写法对照 Claude Code Bash 的 description 说明。
_DESCRIPTION = (
    "One short sentence in the user's language saying what this call does and why, in plain words "
    "the user understands without reading the arguments. It is the call's title in the interface: "
    "do not echo code, flags or file paths, and do not claim progress, success or a result."
)


def intent_field(guidance: str = "") -> Any:
    """意图字段：缺省为空串而不是必填，不强制 strict 的厂商漏写时照常执行，标题退回工具动词。"""
    return Field(default="", description=f"{_DESCRIPTION} {guidance}".rstrip())
