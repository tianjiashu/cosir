"""工具 handler 抽象基类。

所有内置工具 handler 必须继承 ``HandlerBase`` 并提供以下契约：

**必须实现（类级常量）**：

- ``name``: 工具名称，与 ``ToolDefinition.name`` 对应。
- ``description``: 面向模型的工具描述。
- ``permission``: 权限标签（如 ``safe_read`` / ``file_write``）。
- ``args_model``: Pydantic 参数校验模型（``type[BaseModel]``）。
- ``timeout_seconds``: 执行超时（秒）。
- ``risk_level``: 风险等级（``"low"`` / ``"medium"`` / ``"high"``）。

**必须实现（实例方法）**：

- ``execute(**kwargs) -> ToolObservation``: 工具执行入口。具体参数按工具需要声明，
  但必须接受 ``execution_context`` 关键字参数，由执行链在调用时强制注入。
- ``render_summary(derived) -> str``: 把补齐派生字段的参数字典投影为折叠态摘要文本
  （执行前渲染）。
- ``render_result_summary(data) -> str | None``: 把 ``ToolObservation.data`` 投影为
  执行后结果摘要；返回 ``None`` 时前端降级为执行前摘要。
- ``to_definition() -> ToolDefinition``: 返回可注册到 ``ToolRegistry`` 的工具定义。
"""

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from pydantic import BaseModel

from app.tools.schemas import ToolDefinition, ToolObservation


class HandlerBase(ABC):
    """工具 handler 抽象基类。

    本类定义所有内置工具的共有接口契约。子类必须声明类级元数据并实现
    ``execute`` / ``render_summary`` / ``render_result_summary`` /
    ``to_definition`` 四个实例方法。

    **调用约定**：
    ``ToolExecutor`` 通过 ``handler(**arguments, execution_context=ec)`` 调用 execute，
    其中 ``arguments`` 是已通过 pydantic 校验的参数字典。子类的 execute 签名可以
    按工具需要声明具体参数，但最终的一个关键字参数必须是
    ``execution_context: ToolExecutionContext | None = None``。

    参数:
        无（子类无需调用 ``super().__init__()``；HandlerBase 本身无实例状态，
        仅定义接口契约）。

    返回:
        ``HandlerBase`` 子类的实例。

    异常:
        无。

    副作用:
        无。
    """

    # --- 类级元数据（每个子类必须重新声明） ---
    name: ClassVar[str]
    description: ClassVar[str]
    permission: ClassVar[str]
    args_model: ClassVar[type[BaseModel]]
    timeout_seconds: ClassVar[float]
    risk_level: ClassVar[str]

    @abstractmethod
    def execute(self, *args: Any, **kwargs: Any) -> ToolObservation:
        """执行工具逻辑，返回结构化观测结果。

        子类必须实现此方法。具体参数签名按工具需要声明，但必须接受
        ``execution_context`` 关键字参数（``ToolExecutionContext | None``），
        由 :class:`ToolExecutor` 在调用时强制注入。

        参数:
            args / kwargs: 由 :class:`ToolExecutor` 按工具的参数模型
                解包后传入的关键字参数。

        返回:
            成功或失败均归一化为 ``ToolObservation``。

        异常:
            不主动抛出；失败路径均归一化为 ``ToolObservation(status="error")``。

        副作用:
            各子类自述。
        """
        ...

    @abstractmethod
    def render_request_summary(self, arguments: dict[str, Any]) -> str:
        """将补齐派生字段的参数字典投影为折叠态摘要文本（执行前渲染）。


        参数:
            arguments: 已通过 pydantic 校验的参数字典。

        返回:
            用于前端折叠态展示的单行摘要。
        """
        ...

    @abstractmethod
    def render_result_summary(
        self,
        display_data: dict[str, Any],
    ) -> str | list[dict[str, Any]] | dict[str, Any] | None:
        """将 ``ToolObservation.data`` 投影为执行后结果摘要。

        参数:
            data: ``ToolObservation.data`` 结构化载荷。子类须自行处理缺失键
                （通过 ``data.get()`` 或直接索引——直接索引抛 ``KeyError`` 时
                会被 ``ToolDisplayHints.render_result`` 捕获并降级为 ``None``）。

        返回:
            结果摘要文本；返回 ``None`` 或空串时前端降级为执行前摘要。
        """
        ...

    @abstractmethod
    def to_definition(self) -> ToolDefinition:
        """返回当前工具的可注册定义。

        子类必须返回一个包含完整元数据的 ``ToolDefinition`` 实例，其
        ``handler`` 回调指向本实例的 ``execute`` 方法，
        ``display.render_summary`` / ``display.render_result_summary`` 分别指向
        本实例的 ``render_summary`` / ``render_result_summary`` 方法。

        参数:
            无。

        返回:
            可直接注册到 ``ToolRegistry`` 的 ``ToolDefinition``。

        异常:
            无。

        副作用:
            无。
        """
        ...
