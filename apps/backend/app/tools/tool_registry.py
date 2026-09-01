"""In-memory registry for tool definitions."""

import threading
from collections.abc import Iterable, Mapping
from typing import Any

from app.config.logging.logger import log
from app.tools.schemas.tool_definition import ToolDefinition


class ToolRegistry:
    """线程安全的工具定义注册目录（纯读侧）。

    职责边界：只负责工具定义的注册（register/deregister）、查询
    （get_* 系列）与导出（schema/权限投影），即充当运行期的"工具
    定义目录"。所有工具的实际执行统一由 ``ToolScheduler.execute``
    编排，本类不调用、也不持有任何执行逻辑——职责严格止于"定义"。

    线程安全：注册表的读写均经 ``RLock`` 保护，可在多 worker 场景下
    并发查询；任何变更都会递增 ``generation`` 计数器，供调用方做缓存失效。
    """

    def __init__(self, definitions: Iterable[ToolDefinition] = ()) -> None:
        """初始化工具注册表。

        参数:
            definitions: 可选的工具定义集合，构造时逐个注册；为空则得到空注册表。

        返回:
            无。

        异常:
            无（register 内部的类型错误会向上抛出，详见 register）。

        副作用:
            创建内部字典、可重入锁与代计数器；若传入 definitions 则立即注册它们。
        """

        self._tool_definitions: dict[str, ToolDefinition] = {}
        self._lock = threading.RLock()
        self._generation = 0
        for definition in definitions:
            self.register(definition)

    def register(self, definition: ToolDefinition) -> None:
        """注册一个工具定义。

        参数:
            definition: 待注册的工具定义；会先经 ``normalized()`` 归一化后存入。

        返回:
            无。

        异常:
            TypeError: 当 definition 不是 ``ToolDefinition`` 实例时抛出。

        副作用:
            同名工具已存在时记录警告日志并跳过，不改变 generation；
            否则写入定义并使 generation 自增 1。
        """

        if not isinstance(definition, ToolDefinition):
            raise TypeError(f"expected ToolDefinition, got {type(definition).__name__}")
        normalized = definition.normalized()
        with self._lock:
            if normalized.name in self._tool_definitions:
                log.warning(
                    "tool_already_registered",
                    extra={"data": {"name": normalized.name}},
                )
                return
            self._tool_definitions[normalized.name] = normalized
            self._generation += 1

    def deregister(self, name: str) -> None:
        """按名称移除一个已注册的工具定义。

        参数:
            name: 工具名称。

        返回:
            无。

        异常:
            无。

        副作用:
            仅当 name 存在时才删除定义并使 generation 自增 1；不存在时静默无操作。
        """

        with self._lock:
            if name in self._tool_definitions:
                del self._tool_definitions[name]
                self._generation += 1

    def get_tool_definition(self, name: str) -> ToolDefinition | None:
        """按名称查询工具定义。

        参数:
            name: 工具名称。

        返回:
            命中时返回对应的 ``ToolDefinition``；未命中时返回 None。

        异常:
            无。

        副作用:
            无（只读，持锁查询）。
        """

        with self._lock:
            return self._tool_definitions.get(name)

    def get_schema(self, name: str) -> Mapping[str, Any] | None:
        """查询单个工具的参数 schema 投影。

        参数:
            name: 工具名称。

        返回:
            命中时返回该工具的 ``parameters_schema``；未命中时返回 None。

        异常:
            无。

        副作用:
            无（经 get_tool_definition 只读查询）。
        """

        tool_definition = self.get_tool_definition(name)
        return tool_definition.parameters_schema if tool_definition else None

    def get_all_tool_names(self) -> list[str]:
        """返回全部已注册工具名称。

        参数:
            无。

        返回:
            按字典序排序的工具名称列表。

        异常:
            无。

        副作用:
            无（只读，持锁查询后排序）。
        """

        with self._lock:
            return sorted(self._tool_definitions.keys())

    def get_all_definitions(self) -> list[ToolDefinition]:
        """返回全部已注册工具定义。

        参数:
            无。

        返回:
            按名称字典序排序的 ``ToolDefinition`` 列表。

        异常:
            无。

        副作用:
            无（只读，持锁查询）。
        """

        with self._lock:
            return [self._tool_definitions[name] for name in sorted(self._tool_definitions)]

    def get_tools_by_permission(self, permission: str) -> list[ToolDefinition]:
        """按权限标签筛选工具定义。

        参数:
            permission: 权限标签。

        返回:
            所有 ``permission`` 字段等于给定标签的工具定义列表（不保证顺序）。

        异常:
            无。

        副作用:
            无（只读，持锁查询）。
        """

        with self._lock:
            return [
                definition
                for definition in self._tool_definitions.values()
                if definition.permission == permission
            ]

    def get_permissions(self) -> set[str]:
        """返回全部工具用到的权限标签集合。

        参数:
            无。

        返回:
            去重后的权限标签集合。

        异常:
            无。

        副作用:
            无（只读，持锁查询）。
        """

        with self._lock:
            return {definition.permission for definition in self._tool_definitions.values()}

    @property
    def generation(self) -> int:
        """返回注册表代计数器。

        参数:
            无。

        返回:
            当前 generation 值；每次成功注册或移除定义后自增。

        异常:
            无。

        副作用:
            无（只读）。
        """

        return self._generation
