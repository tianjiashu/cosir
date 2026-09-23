"""单个 Child Agent 落 graph state 的稳定引用契约。

``ReactGraphState.child_agents`` 的 value 类型：键为触发委派的 ``tool_call_id``，值为本结构。
字段来自 ``delegate_task`` 展示数据（``display_data.kind == "delegation-result"``）的 allowlist
投影，全部为 JSON 可序列化基本类型；session、mailbox、executor 与 child context 等进程内对象
都不进 checkpoint。workflow 从 checkpoint 恢复时用这些引用识别「该委派已创建过 child」，
避免重放时重复创建子任务与子 Run。

value 显式声明为 ``TypedDict`` 后，Pydantic 会在 state 构造与 checkpoint 反序列化时按契约校验，
并剔除未声明的键——「哪些字段允许落 checkpoint」因此成为类型事实，而不是只写在写入方的白名单
注释里。本模块不 import 任何 app 内模块，避免与 ``state`` 互相引用成环。
"""

from typing import NotRequired

from typing_extensions import TypedDict


class ChildAgentCheckpoint(TypedDict):
    """单个 Child Agent 落 checkpoint 的稳定引用（``child_agents`` 的 value）。

    Attributes:
        child_task_id: 子任务标识。
        child_run_id: 子 Run 标识。
        child_agent_id: 子 Agent 标识。
        title: 子任务标题。
        role: 子 Agent 角色；未配置角色时该键缺失。
        status: 子 Run 状态。
    """

    child_task_id: int
    child_run_id: int
    child_agent_id: str
    title: str
    status: str
    role: NotRequired[str]
