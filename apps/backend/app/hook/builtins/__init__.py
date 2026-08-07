"""内置 Hook 集合。

单一职责：承载首版内置 Hook（``FileSnapshotHook`` / ``CodeGraphIndexPrepareHook``）
与其播种入口 ``bootstrap_hooks``。无配置层（决策 D3），内置 Hook 在启动期硬编码
注册；未订阅的事件下 ``fire`` 零开销放行。
"""

from app.hook.builtins.bootstrap_hooks import bootstrap_hooks
from app.hook.builtins.codegraph_index_prepare_hook import (
    CodeGraphIndexPrepareHook,
)
from app.hook.builtins.file_snapshot_hook import FileSnapshotHook

__all__ = [
    "CodeGraphIndexPrepareHook",
    "FileSnapshotHook",
    "bootstrap_hooks",
]
