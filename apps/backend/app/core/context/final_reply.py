"""一个 Run 的最终回复由哪几条模型回复组成：模型节点（``final_output``、状态事件）与冷重建共用这一条规则。

最终回复是 Run 正常结束时的最后一条回复，**加上**紧挨在它前面、同一回合里只调用了「属于回复本身」的工具
（``ToolDefinition.part_of_reply``：回复下方的按钮、交付卡）的连续几条回复。模型常把答案和这类调用写在同一条
回复里、调用之后只补一句收尾；只认最后一条会把答案当成过程折叠，父 Agent 收到的汇报也只剩那句收尾。

只选回复，不判断 Run 结局：截断、取消、空输出是否构成最终回复由调用方按结局判断。
"""

from collections.abc import Sequence
from dataclasses import dataclass

from openai.types.chat import ChatCompletionMessageParam


@dataclass(frozen=True)
class CommittedResponse:
    """一条已提交的模型回复：上下文序号、回合与它调用的工具名。"""

    sequence: int
    turn: int
    tool_names: tuple[str, ...]

    @classmethod
    def from_message(cls, sequence: int, turn: int, message: ChatCompletionMessageParam) -> "CommittedResponse":
        calls = message.get("tool_calls") or []
        return cls(sequence, turn, tuple(call["function"]["name"] for call in calls))


def final_reply_sequences(responses: Sequence[CommittedResponse], reply_tools: frozenset[str]) -> list[int]:
    """按序号升序返回最终回复所在的回复序号；最后一条回复带非回复类调用（Run 没在回复上结束）时为空。

    ``responses`` 是这个 Run 已提交的模型回复（按序号升序）。
    """

    if not responses or responses[-1].tool_names:
        return []
    last = responses[-1]
    chosen = [last.sequence]
    for response in reversed(responses[:-1]):
        if response.turn != last.turn or not response.tool_names:
            break
        if not all(name in reply_tools for name in response.tool_names):
            break
        chosen.append(response.sequence)
    return sorted(chosen)
