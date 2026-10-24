"""工具执行入口的调用值对象。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolCall:
    """模型请求的一次工具调用。

    ``arguments`` 是模型产出的原始参数串（可能是残缺 JSON），JSON 解码与 schema 校验都只在
    ``ToolAccessGate`` 一处发生，调用方不预解析：畸形参数与其它准入拒绝走同一条观察路径。
    """

    tool_name: str
    arguments: str = ""
    call_id: str = ""
