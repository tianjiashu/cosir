"""core 对「外部工具」的端口：依赖倒置，core 不导入 ``app.tools``。

外部工具是定义来自进程外的工具（MCP 服务是唯一入口）：每个工作区不同、运行期随配置与服务变化，参数是外部
给的 JSON Schema（不满足 strict、整包传参）。它们一律延迟加载：不进 Session 的基础 schema，模型先在
提示词里看到名字目录（``DeferredToolsSource``），用 ``tool_search`` 把定义取进上下文
（结果行 metadata 的 ``facts.loaded_external_tools``），之后按名字直接调用、由准入放行（``RequestTools``）。延迟机制归 core
（请求工具集 ``RequestTools``，检索与目录在 ``tool_search`` 的同族子包 ``core/tool/handler/deferred/``）；端口只回答两件事：现在有哪些外部工具、怎样执行一次调用。

延迟加载的内置工具不经这个端口：它们在 ``ToolDefinition.deferred`` 上声明、随 Session 工具面固化，与外部工具
在 ``DeferredCatalog`` 里合成一份目录。实现由 lifespan 装配时注入
（测试由 conftest 注入），未注入就调用属于装配缺陷，直接报错。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from app.models.external_tool import ExternalToolDefinition

if TYPE_CHECKING:
    from app.core.tool.schemas import ToolExecutionContext, ToolObservation


class ExternalCatalogUnavailable(Exception):
    """目录读不出来（如本机配置损坏）；消息是给模型看的英文说明，由目录来源投影成一条「目录不可用」。"""


class ExternalTools(Protocol):
    """core 用到的外部工具能力；实现是 MCP 工具目录（``tools/mcp/external_tools``）。"""

    def catalog(self, workspace_root: str) -> tuple[ExternalToolDefinition, ...]:
        """这个工作区现在可加载的外部工具，按名字排序、名字唯一。

        每次模型请求前的配置对账都会读它，所以必须廉价：只读本机记录、不连接外部服务。

        异常:
            ExternalCatalogUnavailable: 目录读不出来。
        """
        ...

    def execute(
        self,
        definition: ExternalToolDefinition,
        arguments: dict[str, Any],
        execution_context: ToolExecutionContext,
    ) -> ToolObservation:
        """执行一次已通过准入与参数校验的调用；在工作线程里调用，可以阻塞。

        ``definition`` 是加载那一刻的快照，实现据 ``reference`` 找回内部身份；已经没有它（服务下线、
        工具被删）时返回错误观察，不抛异常。``arguments`` 是整包业务参数，不展开成关键字
        参数：外部参数名可能撞上执行器自己的参数。
        """
        ...


_tools: ExternalTools | None = None


def set_external_tools(tools: ExternalTools | None) -> None:
    """装配期注入实现；``None`` 解除（进程关闭后）。"""
    global _tools
    _tools = tools


def external_tools() -> ExternalTools:
    """取已注入的实现。

    异常:
        RuntimeError: 尚未装配。
    """
    if _tools is None:
        raise RuntimeError("external tools are not assembled")
    return _tools
