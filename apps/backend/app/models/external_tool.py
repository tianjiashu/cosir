"""外部工具（MCP）的定义快照：模型可见定义加执行时找回服务用的定位。

外部工具一律延迟加载（见 ``core/tool/external_tools``）。快照挂在 ``tool_search`` 结果行的 metadata 上
（``ToolResultFacts.loaded_external_tools``），是「这个 Session 已加载哪些外部工具」的唯一持久化事实：随上下文继承
（分叉）、移出（撤回即卸载）、重建（恢复、重启），不另存。存的是加载那一刻的定义，之后服务里的定义变了，
要再加载一次才换成新版本。
"""

from pydantic import BaseModel, ConfigDict


class ExternalToolDefinition(BaseModel):
    """``name`` 是模型调用用的名字，在目录里唯一；``parameters`` 是服务给的业务参数 JSON Schema，原样保存
    （不满足 strict，按非 strict 导出，本地按 JSON Schema 校验）。``reference`` 只给实现读：执行时按它找回
    服务内部的身份（MCP 的作用域、服务与原工具名），不从 ``name`` 反解。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str
    description: str
    parameters: dict[str, object]
    reference: dict[str, str]
