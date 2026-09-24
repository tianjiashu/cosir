"""Tool system package。

``ToolSystem`` 采用**惰性导出**（PEP 562）：任何 ``app.core.tools.<子模块>`` 的导入都会先执行本
``__init__``，若在此处 eager 导入 ``tool_system``，纯常量模块（如 ``schemas.tool_names``）的导入就
会被放大成「整条装配链」加载；该链上的 ``terminal_session`` / ``context_listener`` 等模块又会反向
导入 ``app.assistant_transport.event``，使该包在自身初始化中途被回头导入而抛 ImportError
（后端启动即失败）。惰性导出把装配推迟到真正访问 ``ToolSystem`` 时，保留
``from app.core.tools import ToolSystem`` 这一对外契约不变。
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.tools.tool_system import ToolSystem

__all__ = ["ToolSystem"]


def __getattr__(name: str) -> Any:
    """按需导出 ``ToolSystem``（PEP 562 惰性模块属性）。

    参数:
        name: 被访问的模块属性名。

    返回:
        ``name`` 为 ``"ToolSystem"`` 时返回该类型；首次访问时导入并写回模块命名空间，
        后续访问走常规属性查找。

    异常:
        AttributeError: 访问本模块未定义的其它属性时抛出。

    副作用:
        首次访问 ``ToolSystem`` 时导入 ``app.core.tools.tool_system``（触发工具系统装配）。
    """

    if name == "ToolSystem":
        from app.core.tools.tool_system import ToolSystem

        globals()[name] = ToolSystem
        return ToolSystem
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
