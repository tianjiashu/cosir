"""延迟工具目录：一个 Session 现在能加载的全部工具，内置与外部两类合在一处。

``tool_search`` 检索它，``DeferredToolsSource`` 把名字列给模型，两者看到的是同一份。读取时现算、不存不注册：
内置的取 Session 工具面固化的定义（不变），外部的每次问外部工具端口（只读本机记录，按文件签名缓存），所以
不存在要和配置文件同步的第二份事实。

外部目录读不出来时，内置部分照常可用，读不出来的原因放在 ``unavailable``，由消费方告诉模型。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from openai.types.shared_params import FunctionDefinition

from app.core.tool.external_tools import ExternalCatalogUnavailable, external_tools
from app.models.external_tool import ExternalToolDefinition


@dataclass(frozen=True)
class DeferredEntry:
    """目录里的一个工具：``function`` 是 ``tool_search`` 原样交给模型的定义（加载后也只在那条结果里）；
    ``external`` 是外部工具的定义快照（加载记录存它），内置工具为 ``None``。"""

    function: FunctionDefinition
    external: ExternalToolDefinition | None = None

    @property
    def name(self) -> str:
        return self.function["name"]

    @property
    def description(self) -> str:
        return self.function.get("description", "")


@dataclass(frozen=True)
class DeferredCatalog:
    entries: tuple[DeferredEntry, ...]
    unavailable: str | None = None

    @classmethod
    def read(cls, builtin: Sequence[FunctionDefinition], workspace_root: str) -> DeferredCatalog:
        """内置在前（工具面顺序），外部在后（按名字排序）；外部工具名带 ``mcp__`` 前缀，不会与内置撞名。"""
        entries = [DeferredEntry(function) for function in builtin]
        try:
            external = external_tools().catalog(workspace_root)
        except ExternalCatalogUnavailable as exc:
            return cls(tuple(entries), str(exc))
        entries.extend(DeferredEntry(cls._external_function(d), d) for d in external)
        return cls(tuple(entries))

    @staticmethod
    def _external_function(definition: ExternalToolDefinition) -> FunctionDefinition:
        """外部工具交给模型的定义：参数原样，不加任何平台字段，键顺序 description、name、parameters。

        不带 ``strict``：外部 schema（如 MCP 的 ``inputSchema``）一般不满足 strict 的要求（全部字段必填、
        ``additionalProperties: false``），参数由准入门禁在本地按 JSON Schema 校验。
        """
        return FunctionDefinition(
            description=definition.description,
            name=definition.name,
            parameters=definition.parameters,
        )
