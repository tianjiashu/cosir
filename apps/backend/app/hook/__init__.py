"""进程内 Hook 机制（扩展点，不对外暴露）。

收口：事件枚举、上下文、结果、基类、注册表（索引+执行门面）、内置 Hook 与
拦截器实现（``HookInterceptor`` 静态方法，是所有拦截点的统一收口：
工具 Pre/PostToolUse、运行期事件、会话事件）。无配置层（决策 D3）。
"""

from app.hook.hook_base import HookBase
from app.hook.hook_context import HookContext
from app.hook.hook_event import HookDecision, HookEvent
from app.hook.hook_interceptor import HookInterceptor
from app.hook.hook_registry import (
    HookRegistry,
    get_hook_registry,
    initialize_hook_registry,
)
from app.hook.hook_result import HookResult

__all__ = [
    "HookBase",
    "HookContext",
    "HookDecision",
    "HookEvent",
    "HookInterceptor",
    "HookRegistry",
    "HookResult",
    "get_hook_registry",
    "initialize_hook_registry",
]
