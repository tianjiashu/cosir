"""内置 Hook 集合。

单一职责：承载首版内置 Hook（当前 ``ToolAuditHook``）与其播种入口 ``bootstrap_hooks``。
无配置层（决策 D3），内置 Hook 在启动期硬编码注册；其余 6 类事件首版无内置实现，
空订阅列表下 ``fire`` 零开销放行。
"""

from app.hook.builtins.bootstrap_hooks import bootstrap_hooks
from app.hook.builtins.tool_audit_hook import ToolAuditHook

__all__ = ["ToolAuditHook", "bootstrap_hooks"]
