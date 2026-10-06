"""ReAct graph 的动态路由契约。"""

from enum import Enum


class ReactRoute(str, Enum):
    """ReAct 图的动态下一步目标。

    枚举值对应 graph 节点名；``END`` 是图结束的路由值，由条件边转换为 LangGraph 的结束标记。
    该类型限制 graph state 可写入的动态目标，只表达控制流，不表示 Run 的业务生命周期状态；
    构造枚举不会访问文件、数据库或进程。
    """

    MODEL = "model"
    TOOLS = "tools"
    STRUCTURED_OUTPUT = "structured_output"
    AGENT_TEAM_WAIT = "agent_team_wait"
    END = "end"
