"""LangGraph SQLite checkpointer 装配边界。"""

from dataclasses import dataclass
import os
from pathlib import Path
import sqlite3
from typing import Any


class LangGraphCheckpointerUnavailable(RuntimeError):
    """表示当前环境缺少 LangGraph SQLite checkpointer 依赖。"""


@dataclass
class ManagedSqliteCheckpointer:
    """持有 LangGraph SQLite checkpointer 及其底层连接。

    参数:
        saver: LangGraph 官方 SQLite checkpointer 实例。
        connection: checkpointer 使用的 SQLite 连接。

    返回:
        可代理 LangGraph checkpointer 方法且支持 close 的包装对象。

    异常:
        无。

    副作用:
        保存一个需要显式关闭的 SQLite 连接引用。
    """

    saver: Any
    connection: sqlite3.Connection

    def __getattr__(self, name: str) -> Any:
        """将未知属性代理给 LangGraph 官方 checkpointer。

        参数:
            name: 被访问的属性名称。

        返回:
            官方 checkpointer 上的同名属性。

        异常:
            AttributeError: 如果官方 checkpointer 也不存在该属性。

        副作用:
            无。
        """

        return getattr(self.saver, name)

    def close(self) -> None:
        """关闭 LangGraph checkpointer 底层 SQLite 连接。

        参数:
            无。

        返回:
            无。

        异常:
            sqlite3.Error: 如果关闭连接失败。

        副作用:
            关闭 SQLite 连接，释放文件句柄。
        """

        self.connection.close()


def build_sqlite_checkpointer(database_path: Path) -> ManagedSqliteCheckpointer:
    """创建 LangGraph 官方 SQLite checkpointer。

    参数:
        database_path: 用于保存 LangGraph graph state 的 SQLite 文件。

    返回:
        带 close 生命周期的 LangGraph SQLite checkpointer 包装对象。

    异常:
        LangGraphCheckpointerUnavailable: 如果当前环境未安装官方 SQLite checkpointer。
        sqlite3.Error: 如果 SQLite 连接无法打开。

    副作用:
        打开 SQLite 连接，LangGraph checkpointer 可能初始化自身表结构。
    """

    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
    except ModuleNotFoundError as exc:
        raise LangGraphCheckpointerUnavailable(
            "缺少 langgraph SQLite checkpointer；请安装 langgraph-checkpoint-sqlite 或项目依赖。"
        ) from exc

    os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path, check_same_thread=False)
    return ManagedSqliteCheckpointer(saver=SqliteSaver(connection), connection=connection)


def build_thread_config(thread_id: str) -> dict:
    """构建 LangGraph 调用所需的 thread_id 配置。

    参数:
        thread_id: Durable Run 绑定的 LangGraph thread_id。

    返回:
        可传给 LangGraph invoke 的配置字典。

    异常:
        ValueError: 如果 thread_id 为空。

    副作用:
        无。
    """

    if not thread_id:
        raise ValueError("thread_id must not be blank")
    return {"configurable": {"thread_id": thread_id}}
