"""CodeGraph Kernel 子系统（常驻代码智能后端）。

第一阶段仅承载 Kernel 常驻所需的 Supervisor / Client / node 解析 / 异常，
经独立目录与现有 ``tool_handler`` 文件工具范式隔离；第二阶段经 ToolSystem 接入。
"""

from app.codegraph.exceptions import (
    CodeGraphIndexingFailedError,
    CodeGraphIndexLockedError,
    CodeGraphInvalidRequestError,
    CodeGraphKernelError,
    CodeGraphKernelTimeoutError,
    CodeGraphKernelUnavailableError,
    CodeGraphNodeMissingError,
    CodeGraphProtocolIncompatibleError,
    CodeGraphToolNotAllowedError,
    CodeGraphWorkspaceNotIndexedError,
    error_from_code,
)
from app.codegraph.kernel_client import CodeGraphKernelClient
from app.codegraph.node_resolver import resolve_node_binary
from app.codegraph.protocol import (
    IndexInitResult,
    IndexStatusResult,
    IndexSyncResult,
    KernelErrorCode,
)
from app.codegraph.supervisor import (
    CodeGraphKernelSupervisor,
    KernelState,
    get_kernel_supervisor,
    set_kernel_supervisor,
)

__all__ = [
    "CodeGraphIndexLockedError",
    "CodeGraphIndexingFailedError",
    "CodeGraphInvalidRequestError",
    "CodeGraphKernelClient",
    "CodeGraphKernelError",
    "CodeGraphKernelSupervisor",
    "CodeGraphKernelTimeoutError",
    "CodeGraphKernelUnavailableError",
    "CodeGraphNodeMissingError",
    "CodeGraphProtocolIncompatibleError",
    "CodeGraphToolNotAllowedError",
    "CodeGraphWorkspaceNotIndexedError",
    "IndexInitResult",
    "IndexStatusResult",
    "IndexSyncResult",
    "KernelErrorCode",
    "KernelState",
    "error_from_code",
    "get_kernel_supervisor",
    "resolve_node_binary",
    "set_kernel_supervisor",
]
