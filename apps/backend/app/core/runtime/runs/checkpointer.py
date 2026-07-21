"""LangGraph checkpointer 工厂。

Workflow 编排层强依赖 LangGraph（见 ``AGENTS.md`` 不可变决议）：``StateGraph`` 负责
编排、``SqliteSaver`` 负责 checkpoint 真实落盘。checkpoint 异步引擎由
``app.storage.engines`` 统一创建与释放（路径来自 ``BackendSettings.checkpoint_file``），
本模块只负责产出 checkpointer；未来如需替换为其他后端（如 PostgreSQL），只需修改
``build_checkpointer`` 与 ``engines`` 两处。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.storage.store_engines import checkpoint_async_engine, checkpoint_path as _engine_checkpoint_path


def checkpoint_path() -> str:
    """返回 checkpoint sqlite 文件路径（来自 ``BackendSettings.checkpoint_file``）。

    返回:
        checkpoint 数据库文件绝对路径字符串。

    异常:
        RuntimeError: 如果 ``init_storage`` 尚未调用。
    """

    return _engine_checkpoint_path()


@asynccontextmanager
async def build_checkpointer() -> AsyncIterator[AsyncSqliteSaver]:
    """构造并产出 AsyncSqliteSaver checkpointer。

    复用 ``app.storage.engines`` 提供的进程级 checkpoint 异步引擎；产出对象需以
    ``async with`` 方式使用，确保 LangGraph 在 graph 执行结束后释放其持有的连接。

    生成:
        已配置好、可直接传给 ``graph.compile(checkpointer=...)`` 的 AsyncSqliteSaver。

    异常:
        RuntimeError: 如果 ``init_storage`` 尚未调用。
    """

    async with AsyncSqliteSaver.from_engine(checkpoint_async_engine()) as saver:
        yield saver
