"""测试公共：解锁坏导入链。

仓库存在预存坏导入 ``app/config/logging/save/sqlite_handler.py:12`` 误写为
``from app.storage.crud.log import LogStore``，而实际模块名是 ``log_crud``。这导致
``import app.core.runtime.runner`` / ``import app.api.turns_api`` 在导入期失败。

本模块在被测模块导入**之前**把 ``app.storage.crud.log`` 注入 ``sys.modules``，
仅提供 ``LogStore`` 占位类以满足模块级 import（该类不会被实例化，不影响被测行为）。
这是测试隔离手段，不修改任何业务代码。
"""

import sys
import types

_STUB_INSTALLED = "_coding_agent_stub_log_installed"


def install_log_crud_stub() -> None:
    """在 sys.modules 中注入 app.storage.crud.log 占位模块。"""

    if getattr(sys.modules.get("__main__"), _STUB_INSTALLED, False):
        return
    if "app.storage.crud.log" in sys.modules:
        return

    stub = types.ModuleType("app.storage.crud.log")

    class LogStore:  # 占位实现，仅用于解锁 import；不参与被测逻辑
        def insert_many(self, batch):  # noqa: D401
            raise NotImplementedError("stub LogStore")

    stub.LogStore = LogStore
    sys.modules["app.storage.crud.log"] = stub
    sys.modules.setdefault("__main__", types.ModuleType("__main__"))
    setattr(sys.modules["__main__"], _STUB_INSTALLED, True)
