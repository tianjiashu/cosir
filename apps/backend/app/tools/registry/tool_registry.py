"""内存工具注册表。

参考 hermes-agent 的 registry 设计，但适配本项目的 ToolDefinition 契约：

- 以 ``ToolDefinition`` 为单一事实来源，不再用 ToolEntry 包装层。
- 支持构造时批量注册与运行时增量 ``register``。
- 提供 OpenAI/DeepSeek 协议格式的工具定义导出、按 name/permission 查询，
  以及基础 ``dispatch``（直接调用 handler，异常处理交由上层 executor）。

线程安全：所有读写在 ``RLock`` 保护下进行；``generation`` 计数器供上层缓存失效。
"""

import logging
import threading
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set

from app.tools.schemas.tool_definition import ToolDefinition

logger = logging.getLogger("coding_agent.backend")


class ToolRegistry:
    """消费 ToolDefinition 的内存注册表。"""

    def __init__(self, definitions: Iterable[ToolDefinition] = ()) -> None:
        """初始化注册表并批量注册初始工具定义。

        参数:
            definitions: 初始 ToolDefinition 列表（可空）。

        返回:
            无。

        异常:
            无。

        副作用:
            写入内部字典。
        """
        self._tool_definitions: Dict[str, ToolDefinition] = {}
        self._lock = threading.RLock()
        self._generation = 0
        for definition in definitions:
            self.register(definition)

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------
    def register(self, definition: ToolDefinition) -> None:
        """注册一个工具定义。

        参数:
            definition: 已构造好的 ToolDefinition。

        返回:
            无。

        异常:
            TypeError: 当 definition 不是 ToolDefinition 时抛出。

        副作用:
            写入内部字典并递增 generation；重名仅告警并跳过。
        """
        if not isinstance(definition, ToolDefinition):
            raise TypeError(
                f"expected ToolDefinition, got {type(definition).__name__}"
            )
        with self._lock:
            if definition.name in self._tool_definitions:
                logger.warning(
                    "tool_already_registered",
                    extra={"data": {"name": definition.name}},
                )
                return
            self._tool_definitions[definition.name] = definition
            self._generation += 1

    def deregister(self, name: str) -> None:
        """按名称移除一个工具。

        参数:
            name: 工具名。

        返回:
            无。

        异常:
            无。

        副作用:
            从内部字典删除（若存在）并递增 generation。
        """
        with self._lock:
            if name in self._tool_definitions:
                del self._tool_definitions[name]
                self._generation += 1

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def get_tool_definition(self, name: str) -> Optional[ToolDefinition]:
        """按名称返回工具定义，不存在返回 None。"""
        with self._lock:
            return self._tool_definitions.get(name)


    def get_schema(self, name: str) -> Optional[Mapping[str, Any]]:
        """返回工具的参数模式，绕过可用性过滤。

        参数:
            name: 工具名。

        返回:
            parameters_schema 字典；工具不存在时返回 None。

        异常:
            无。

        副作用:
            无。
        """
        tool_definition = self.get_tool_definition(name)
        return tool_definition.parameters_schema if tool_definition else None

    def get_all_tool_names(self) -> List[str]:
        """返回所有已注册工具名（按名称排序）。"""
        with self._lock:
            return sorted(self._tool_definitions)

    def get_all_definitions(self) -> List[ToolDefinition]:
        """返回所有已注册工具定义（按名称排序）。"""
        with self._lock:
            return [self._tool_definitions[name] for name in sorted(self._tool_definitions)]

    def get_tools_by_permission(self, permission: str) -> List[ToolDefinition]:
        """返回指定权限级别下的所有工具定义。

        参数:
            permission: 权限级别（如 ``safe_read``）。

        返回:
            匹配该权限的 ToolDefinition 列表。

        异常:
            无。

        副作用:
            无。
        """
        with self._lock:
            return [
                entry
                for entry in self._tool_definitions.values()
                if entry.permission == permission
            ]

    def get_permissions(self) -> Set[str]:
        """返回所有出现的权限级别集合。"""
        with self._lock:
            return {entry.permission for entry in self._tool_definitions.values()}

    # ------------------------------------------------------------------
    # OpenAI 协议导出
    # ------------------------------------------------------------------
    def get_openai_definitions(
        self, tool_names: Optional[Set[str]] = None
    ) -> List[Dict[str, Any]]:
        """返回 OpenAI/DeepSeek 协议格式的工具定义列表。

        参数:
            tool_names: 需要导出的工具名集合；为 None 时导出所有
                ``visible_by_default`` 为真的工具。

        返回:
            ``[{"type": "function", "function": {"name", "description", "parameters"}}, ...]``

        异常:
            无。

        副作用:
            无。
        """
        with self._lock:
            entries = list(self._tool_definitions.values())
        result: List[Dict[str, Any]] = []
        for entry in sorted(entries, key=lambda e: e.name):
            if tool_names is not None:
                if entry.name not in tool_names:
                    continue
            elif not entry.visible_by_default:
                continue
            result.append(
                {
                    "type": "function",
                    "function": {
                        "name": entry.name,
                        "description": entry.description,
                        "parameters": entry.parameters_schema,
                    },
                }
            )
        return result

    # ------------------------------------------------------------------
    # 调度
    # ------------------------------------------------------------------
    def dispatch(self, name: str, arguments: Mapping[str, Any]) -> Any:
        """按名称执行工具 handler。

        参数:
            name: 工具名。
            arguments: 模型传入的关键字参数字典。

        返回:
            handler 的执行结果（类型由具体工具决定，通常 str）。

        异常:
            KeyError: 当工具未注册时抛出，由上层决定如何转成错误观察。

        副作用:
            执行 handler（副作用由具体工具定义）。
        """
        entry = self.get_tool_definition(name)
        if entry is None:
            raise KeyError(f"unknown tool: {name}")
        return entry.handler(**arguments)

    @property
    def generation(self) -> int:
        """返回当前 generation 计数器，供上层缓存失效判断。"""
        return self._generation
