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
- ``to_definition() -> ToolDefinition``: 返回可注册到 ``ToolRegistry`` 的工具定义。

后端不承载任何渲染职责：handler 只产出模型继续工作所需的 ``content``，以及客户端
展示所需的 ``display_data`` 和必要的内部 ``artifact_data``。成功结果应保持最小；错误
通过 ``error`` 描述事实、``reason`` 描述下一步、``retryable`` 仅提示模型是否可在
修正后再次调用，不触发自动重试。
"""

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from pydantic import BaseModel

from app.core.tools.schemas import ToolDefinition, ToolObservation


class HandlerBase(ABC):
    """工具 handler 抽象基类。

    本类定义所有内置工具的共有接口契约。子类必须声明类级元数据并实现
    ``execute`` / ``to_definition`` 两个实例方法。

    **调用约定**：
    ``ToolHandlerRunner`` 通过 ``handler(**arguments, execution_context=ec)`` 调用 execute，
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

    # --- 工具元数据（每个子类必须重新声明） ---
    # name / description / args_model 声明为实例可写属性而非 ClassVar：多数单用途工具
    # 以类属性形式固化；按实例承载多个定义的 handler 需要为前三项按实例赋值。
    # mypy 禁止用实例变量覆盖父类 ClassVar，故此处放开前三个。
    # permission / timeout_seconds / risk_level 在工具族内始终为类级，保持 ClassVar。
    name: str
    description: str
    permission: ClassVar[str]
    args_model: type[BaseModel]
    timeout_seconds: ClassVar[float]
    risk_level: ClassVar[str]

    @abstractmethod
    def execute(self, *args: Any, **kwargs: Any) -> ToolObservation:
        """执行工具逻辑，返回结构化观测结果。

        子类必须实现此方法。具体参数签名按工具需要声明，但必须接受
        ``execution_context`` 关键字参数（``ToolExecutionContext | None``），
        由 :class:`ToolHandlerRunner` 在调用时强制注入。

        参数:
            args / kwargs: 由 :class:`ToolHandlerRunner` 按工具的参数模型
                解包后传入的关键字参数。

        返回:
            成功或失败均归一化为 ``ToolObservation``。成功时 ``content`` 可以为空；
            失败时 ``error`` 只描述事实，``reason`` 只描述下一步动作。

        异常:
            不主动抛出；失败路径均归一化为 ``ToolObservation(status="error")``。

        副作用:
            各子类自述。
        """
        ...

    def to_definition_if_avaliable(self) -> ToolDefinition | None:
        if self.avaliable():
            return self.to_definition()
        return None

    @abstractmethod
    def to_definition(self) -> ToolDefinition:
        """返回当前工具的可注册定义。

        子类必须返回一个包含完整元数据的 ``ToolDefinition`` 实例，其
        ``handler`` 回调指向本实例的 ``execute`` 方法，``display`` 为纯静态的
        ``ToolDisplayHints`` 声明（不含任何渲染函数）。

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

    def avaliable(self) -> bool:
        return True
