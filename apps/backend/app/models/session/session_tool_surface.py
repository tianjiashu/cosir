"""Session 的工具面：创建时固化的全部模型工具定义，以及其中哪些延迟加载。

工具面是 Session 的事实，创建时按 Agent 选出的工具、Session 类型与派生深度定下（``session_tool_scope``），
之后不变；分叉原样复制，请求前缀才接得上来源的缓存。定义都是 strict function 定义；延迟加载的那几个
（``ToolDefinition.deferred``）不进每次请求，模型经 ``tool_search`` 取进上下文后才带上，所以固化的就是
加载时交给模型的那一份，同一 Session 里前后一致。外部工具（MCP）不在这里：它们来自进程外、运行期会变，
目录每次现读（``core/tool/external_tools``）。
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SessionToolSurface(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 全部工具的模型定义，按工具注册顺序。
    schemas: list[dict[str, Any]] = Field(default_factory=list)
    # 其中延迟加载的工具名。
    deferred: list[str] = Field(default_factory=list)
