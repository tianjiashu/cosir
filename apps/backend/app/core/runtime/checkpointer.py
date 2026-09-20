"""LangGraph checkpointer 工厂。

Workflow 编排层强依赖 LangGraph（见 ``AGENTS.md`` 不可变决议）：``StateGraph`` 负责
编排、``AsyncSqliteSaver`` 负责 checkpoint 真实落盘。checkpoint 数据库文件路径来自固定路径常量
``app.config.paths.CHECKPOINT_FILE``，本模块只负责产出 checkpointer；未来如需替换为其他后端
（如 PostgreSQL），只需修改 ``build_checkpointer`` 与 ``paths.CHECKPOINT_FILE`` 两处。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.storage.store_engines import checkpoint_path as _engine_checkpoint_path


@asynccontextmanager
async def build_checkpointer() -> AsyncIterator[AsyncSqliteSaver]:
    """构造并产出 AsyncSqliteSaver checkpointer。

    ``AsyncSqliteSaver`` 底层经 aiosqlite 直连数据库文件，不接受 SQLAlchemy 引擎；因此
    直接复用 ``app.storage.store_engines.checkpoint_path`` 提供的文件路径，连接生命周期由
    LangGraph 的 ``from_conn_string`` 上下文管理器负责释放。产出对象需以 ``async with``
    方式使用。

    生成:
        已配置好、可直接传给 ``graph.compile(checkpointer=...)`` 的 AsyncSqliteSaver。

    异常:
        RuntimeError: 如果 ``init_storage`` 尚未调用。
    """

    async with AsyncSqliteSaver.from_conn_string(_engine_checkpoint_path()) as saver:
        yield saver
