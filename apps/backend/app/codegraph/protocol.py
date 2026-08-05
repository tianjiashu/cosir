"""CodeGraph Kernel stdio JSON-line 协议的 Python 侧常量与类型镜像。

单一职责：承载后端 Client / Supervisor 所需的协议常量与数据结构定义，与
``third_party/workspace_event/src/agent-kernel/protocol.ts`` 保持字段一致（该文件为
单一事实来源）。之所以在 Python 侧镜像而非共享，是因为前后端分属不同语言、
agent-kernel 经 tsc 编译为独立 dist，不暴露类型给 Python 侧。

不负责：JSON 序列化细节（归 Client）、进程管理（归 Supervisor）。
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any


class KernelErrorCode(str, Enum):
    """与 agent-kernel protocol.ts KernelErrorCode 一一对应。"""

    PROTOCOL_INCOMPATIBLE = "PROTOCOL_INCOMPATIBLE"
    TIMEOUT = "TIMEOUT"
    KERNEL_UNAVAILABLE = "KERNEL_UNAVAILABLE"
    WORKSPACE_NOT_INDEXED = "WORKSPACE_NOT_INDEXED"
    TOOL_NOT_ALLOWED = "TOOL_NOT_ALLOWED"
    INVALID_REQUEST = "INVALID_REQUEST"
    INDEXING_FAILED = "INDEXING_FAILED"
    INDEX_LOCKED = "INDEX_LOCKED"
    INTERNAL = "INTERNAL"


#: 当前后端期望兼容的协议版本（与 protocol.ts PROTOCOL_VERSION 对齐）。
PROTOCOL_VERSION = "1.1.0"

#: 握手方法名。
METHOD_HELLO = "kernel.hello"
METHOD_PING = "kernel.ping"
METHOD_SHUTDOWN = "kernel.shutdown"

#: 索引生命周期方法名（写入型）。
METHOD_INDEX_STATUS = "codegraph_status"
METHOD_INDEX_INIT = "codegraph_init"
METHOD_INDEX_SYNC = "codegraph_sync"


@dataclass(frozen=True)
class HelloResult:
    """``kernel.hello`` 响应（字段对齐 protocol.ts HelloResult）。"""

    protocol_version: str
    kernel_version: str
    codegraph_version: str
    capabilities: list[str]
    platform: str


@dataclass(frozen=True)
class PingResult:
    """``kernel.ping`` 响应（字段对齐 protocol.ts PingResult）。"""

    ok: bool
    uptime_ms: int
    active_workspaces: int


@dataclass(frozen=True)
class KernelErrorObject:
    """协议层错误对象（字段对齐 protocol.ts KernelErrorObject）。"""

    code: KernelErrorCode
    message: str
    retryable: bool


@dataclass(frozen=True)
class QueryResult:
    """查询方法响应（第一版直接透传上游 ToolResult 文本）。"""

    content: list[dict[str, Any]]
    is_error: bool


#: 归一化索引状态（与 protocol.ts IndexState 一致）。
IndexState = str


@dataclass(frozen=True)
class IndexStatusResult:
    """``codegraph_status`` 响应（字段对齐 protocol.ts IndexStatusPayload）。"""

    state: IndexState
    last_indexed_at: int | None


@dataclass(frozen=True)
class IndexInitResult:
    """``codegraph_init`` 响应（字段对齐 protocol.ts IndexInitPayload）。"""

    state: IndexState
    files_indexed: int
    duration_ms: int


@dataclass(frozen=True)
class IndexSyncResult:
    """``codegraph_sync`` 响应（字段对齐 protocol.ts IndexSyncPayload）。"""

    state: IndexState
    files_added: int
    files_modified: int
    files_removed: int
    duration_ms: int
