"""单次模型请求（step）内的可变记账；只存在于当前驱动，不写入 Run。"""

from dataclasses import dataclass, field


@dataclass
class StepContext:
    """一次模型请求内累积的运行期状态。

    与 ``RuntimeContext`` 的区别在生命周期：``RuntimeContext`` 覆盖整次驱动（多个 step），
    本类由 ``model_node`` 在进入流式循环前重建。因此这里持有的集合天然只覆盖本请求，
    「累积快照去重」与「取消收口范围」可以共用同一份记账，无需再维护请求内的 id 列表。

    字段可变且跨节点传递时按引用共享；需要新请求时整体替换实例，不做逐字段清理。
    """

    # 本请求已向前端投影过 ``ToolCallCreatedEvent`` 的调用（``call_id`` → 工具名）。SDK 快照是
    # 累积的，同一调用会在后续每帧重复出现，记一次即可。
    projected_tool_calls: dict[str, str] = field(default_factory=dict)
