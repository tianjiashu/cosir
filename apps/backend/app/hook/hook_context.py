"""Hook 触发上下文值对象。

单一职责：承载「一次 Hook 触发时传给 Hook 的数据」这一载体。它是全 7 类事件
共用的扁平容器——非工具事件下 `tool_*` 字段恒为 ``None``（取舍说明见
``Hook机制技术方案.md`` §5.2）。``metadata`` 作为通用扩展位，供未来事件族
新增字段时无需改签名。

本对象为不可变 dataclass，由触发方构造后只读传入 Hook。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.tools.schemas import ToolObservation
from app.hook.hook_event import HookEvent


@dataclass(frozen=True)
class HookContext:
    """一次 Hook 触发的上下文。

    属性:
        event: 触发时机（必填，索引键）。
        tool_name: 工具名；非 ``PRE_TOOL_USE`` / ``POST_TOOL_USE`` 时为 ``None``。
        tool_arguments: 工具入参（dict 形式）；前置 Hook 时为原始入参，供改写。
        tool_observation: 工具执行结果；仅 ``POST_TOOL_USE`` 有值，其它为 ``None``。
        session_id: 会话标识（桌面一次性后端进程对应一个 session）。
        workspace_id: 工作区标识；缺工作区上下文时为 ``None``。
        task_id: 任务标识；非任务上下文时为 ``None``。
        run_id: 轮次标识；非轮次上下文时为 ``None``。
        metadata: 通用扩展位，供未来事件族携带额外字段，默认空 dict。

    关于「通用契约含工具专有字段」的取舍：``tool_name`` / ``tool_arguments`` /
    ``tool_observation`` 是工具类事件专有，非工具事件下恒为 ``None``。这是有意选择，
    理由与拆分阈值见 ``Hook机制技术方案.md`` §5.2——若工具专有字段增至 6 个以上，
    或出现第二个事件族的专有字段簇，则按事件族拆分 Context。

    ``metadata`` 必须用 ``field(default_factory=dict)``，禁止裸 ``= {}``（可变默认值反模式）。
    """

    event: HookEvent
    tool_name: str | None = None
    tool_arguments: dict | None = None
    tool_observation: ToolObservation | None = None
    session_id: str | None = None
    workspace_id: int | None = None
    task_id: str | None = None
    run_id: str | None = None
    metadata: dict = field(default_factory=dict)

    @classmethod
    def from_locatable(
        cls,
        event: HookEvent,
        locatable: object,
        turn: object | None = None,
        tool_name: str | None = None,
        tool_arguments: dict | None = None,
        tool_observation: ToolObservation | None = None,
    ) -> HookContext:
        """从「可定位对象」构造 HookContext，统一提取位置标识字段。

        工具拦截的 ``ToolExecutionContext``、运行期事件的 ``task`` 记录，均带
        ``workspace_id`` / ``task_id`` / ``run_id`` 定位字段，但字段宿主不同。
        本工厂统一以 ``getattr`` 安全提取，避免各触发点重复写三连 ``getattr``。

        参数:
            event: 触发时机（必填，索引键）。
            locatable: 提供 ``workspace_id`` / ``task_id`` 的可定位对象
                （``ToolExecutionContext`` 或任务记录）。缺字段按 ``None`` 处理。
            turn: 可选轮次记录，提供 ``run_id``（运行期事件传 ``turn``，
                ``ToolExecutionContext`` 自身已带 ``run_id`` 时不必再传）。
            tool_name: 工具名；仅工具类事件非 ``None``。
            tool_arguments: 工具入参；仅 ``PRE_TOOL_USE`` 提供。
            tool_observation: 工具结果；仅 ``POST_TOOL_USE`` 提供。

        返回:
            已填充定位字段与工具字段的 ``HookContext``。

        异常:
            无。

        副作用:
            无。
        """
        run_id = getattr(turn, "id", None) if turn is not None else None
        return cls(
            event=event,
            tool_name=tool_name,
            tool_arguments=tool_arguments,
            tool_observation=tool_observation,
            workspace_id=getattr(locatable, "workspace_id", None),
            task_id=getattr(locatable, "task_id", None),
            run_id=run_id or getattr(locatable, "run_id", None),
        )

    def __post_init__(self) -> None:
        """防御性校验：保证 ``metadata`` 永远是独立的 dict 实例。

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            当 ``metadata`` 为 ``None`` 时重置为空 dict（配合 ``default_factory`` 使用，
            避免任何路径传入 ``None`` 导致后续写入污染其它对象）。
        """
        if self.metadata is None:
            object.__setattr__(self, "metadata", {})
