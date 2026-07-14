"""项目内访问 LangGraph 的窄门面。"""

from pathlib import Path
from typing import Any, Callable, Optional

from app.core.runs.checkpointer import build_sqlite_checkpointer
from app.core.runs.graph_builder import build_react_like_graph
from app.core.runs.invoke import invoke_graph


class LangGraphRuntime:
    """封装 graph 构建、checkpointer 装配与调用入口。"""

    def __init__(self, database_path: Path, graph_factory: Callable[[Any], Any]) -> None:
        """初始化 LangGraph runtime facade。

        参数:
            database_path: LangGraph checkpointer 使用的 SQLite 数据库路径。
            graph_factory: 接收 checkpointer 并返回 compiled graph 的工厂。

        返回:
            无。

        异常:
            LangGraphCheckpointerUnavailable: 如果缺少官方 SQLite checkpointer。

        副作用:
            初始化 LangGraph checkpointer，并构建 graph。
        """

        self._checkpointer = build_sqlite_checkpointer(database_path)
        self._graph = build_react_like_graph(lambda: graph_factory(self._checkpointer))

    def invoke(
        self,
        thread_id: str,
        input_value: Optional[Any] = None,
        resume_value: Optional[Any] = None,
    ) -> Any:
        """调用或恢复 LangGraph graph。

        参数:
            thread_id: Durable Run 绑定的 thread_id。
            input_value: 初次调用输入。
            resume_value: 恢复 interrupt 的值。

        返回:
            LangGraph graph 返回值。

        异常:
            ValueError: 如果输入和恢复值同时提供。

        副作用:
            推进 LangGraph graph 状态。
        """

        return invoke_graph(
            self._graph,
            thread_id,
            input_value=input_value,
            resume_value=resume_value,
        )

    def close(self) -> None:
        """关闭 LangGraph Runtime 持有的底层资源。

        参数:
            无。

        返回:
            无。

        异常:
            sqlite3.Error: 如果 checkpointer 底层连接关闭失败。

        副作用:
            关闭 LangGraph SQLite checkpointer 的数据库连接。
        """

        self._checkpointer.close()
