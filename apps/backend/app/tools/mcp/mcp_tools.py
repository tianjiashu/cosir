"""MCP 工具：外部工具的实现（core 端口 ``external_tools``），目录取自本机记录，调用经连接池。

目录 = 本工作区可用的服务（系统级与 workspace 级合并、启用、本机测试通过，见 ``available_servers``）各自
在测试通过时拿到的工具与服务说明（``MCPMachineState.catalog``）。每次模型请求前的配置对账都会读它，所以只读
本机文件、不连接服务，并按这几个文件的签名缓存：签名没变只做几次 stat。工具与说明同一次读出、同一份缓存。

给模型的名字是 ``mcp__<服务>__<工具>``（前缀 :data:`PREFIX` 也是前端工具目录认 MCP 一族的依据）：模型 API 的
函数名只允许 ``[A-Za-z0-9_-]``、最长 64 个字符，服务名与工具名都是外部写的，所以要换字符、可能要截断；截断或
换字符后可能撞名。撞名或超长的名字改成截断后加 6 位哈希（按服务与原工具名算，重启后不变）。名字只用于展示与
寻址目录，执行时按定义快照里的 ``reference`` 找回原名，不从名字反解。

调用：连接由进程级 ``MCPConnectionPool`` 常驻持有，在工具线程里把协程投到池的事件循环上，经 ``wait_for_call``
等待并轮询取消信号与调用时限（执行器只在调用前检查一次取消，也不做硬超时）。取消或超时只撤销这次请求，
连接保留供下次复用。观察的工具名是模型可见名（``mcp__服务__工具``），展示数据里是服务与原工具名。

工具描述前加 ``[MCP server <名字>]``：描述是服务写的，进了请求的 ``tools`` 参数、不在 untrusted 块里，系统提示词
据这个前缀告诉模型它是外部服务写的说明（规则在 ``SystemPromptBuilder`` 的外部内容段落）。
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from app.config.logging.logger import log
from app.core.tool.external_tools import ExternalCatalogUnavailable
from app.core.tool.results.external_content import ExternalContent
from app.core.tool.results.tool_cancelled import tool_cancelled
from app.core.tool.results.tool_error import tool_error
from app.core.tool.results.tool_success import tool_success
from app.core.tool.schemas import ToolExecutionContext, ToolObservation
from app.models.external_tool import ExternalInstructions, ExternalToolDefinition
from app.service import depends as service_depends
from app.service.configuration.extension_scope import ExtensionScope
from app.service.configuration.mcp.configuration import (
    MCPConfigurationError,
    ScopedMCPServer,
    available_servers,
)
from app.service.configuration.mcp.machine_state import MCPMachineState, MCPToolEntry
from app.tools.cancellable_wait import (
    STOPPED_IN_FLIGHT_REASON,
    CallDeadlineExceeded,
    ToolCallStopped,
    wait_for_call,
)
from app.tools.display.mcp_display import build_mcp_call_display_data
from app.tools.mcp.call_result import MCPCallResult
from app.tools.mcp.client import MCPToolArgumentsError, MCPToolNotFoundError
from app.tools.mcp.failure import MCPFailure
from app.tools.mcp.result_images import StoredResultImages
from app.utils.file_signature import FileSignature, stat_signature
from app.utils.workspace_scope import normalize_scope_path

# 模型可见名的前缀与长度规则（见模块说明）。
PREFIX = "mcp__"
_MAX_NAME_LENGTH = 64
_HASH_LENGTH = 6
_INVALID = re.compile(r"[^A-Za-z0-9_-]")

# 单次调用（含必要的建连）的时限；执行器不做硬超时，由这里自己守。
CALL_TIMEOUT_SECONDS = 90.0
# 单个工具描述与单个服务说明的上限：够说明用法，又不让一个服务的长文档撑大每次请求。
_MAX_DESCRIPTION_CHARS = 2000
_MAX_INSTRUCTIONS_CHARS = 2000


# 一次读出的目录：工具定义与服务说明（服务名唯一：两层同名时 workspace 覆盖系统级）。
_Loaded = tuple[tuple[ExternalToolDefinition, ...], tuple[ExternalInstructions, ...]]


class MCPExternalTools:
    """进程级单例，lifespan 注入 core。缓存按工作区键，多线程读取由锁保护。"""

    def __init__(self) -> None:
        self._cache: dict[str, tuple[FileSignature, _Loaded]] = {}
        self._lock = threading.Lock()

    def catalog(self, workspace_root: str) -> tuple[ExternalToolDefinition, ...]:
        return self._cached(workspace_root)[0]

    def instructions(self, workspace_root: str) -> tuple[ExternalInstructions, ...]:
        return self._cached(workspace_root)[1]

    def _cached(self, workspace_root: str) -> _Loaded:
        key = normalize_scope_path(workspace_root)
        # 签名在读盘之前取：读盘期间文件又变了，下一次签名对不上会再读一次。
        signature = stat_signature(self._catalog_files(workspace_root))
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None and cached[0] == signature:
                return cached[1]
            loaded = self._load(workspace_root)
            self._cache[key] = (signature, loaded)
            return loaded

    def execute(
        self,
        definition: ExternalToolDefinition,
        arguments: dict[str, Any],
        execution_context: ToolExecutionContext,
    ) -> ToolObservation:
        """按快照里的作用域与服务名找回服务（不从模型可见名反解），用原工具名调用。"""

        reference = definition.reference
        try:
            servers = available_servers(execution_context.workspace_root)
        except MCPConfigurationError as exc:
            return tool_error(
                definition.name,
                f"{exc}. The local MCP configuration cannot be read, so this tool cannot be called.",
                status_hint="配置有误",
            )
        server = next(
            (
                item
                for item in servers
                if item.scope.key == reference["scope"] and item.name == reference["server"]
            ),
            None,
        )
        if server is None:
            return tool_error(
                definition.name,
                f"MCP server {reference['server']} is no longer available: it was removed, disabled, "
                "or has not passed a connection test on this machine. Do not call its tools again.",
                status_hint="服务不可用",
            )
        return self._call(definition.name, server, reference["tool"], arguments, execution_context)


    def _call(
        self,
        model_name: str,
        server: ScopedMCPServer,
        tool_name: str,
        arguments: dict[str, Any],
        execution_context: ToolExecutionContext,
    ) -> ToolObservation:
        """调用 ``server`` 上的 ``tool_name``（原名）；失败一律归一成错误观察，不抛异常。

        参数已按加载时的定义快照校验过；连接池还会按服务当前声明的 schema 再校验一次（服务可能已改了 schema），
        不合格时请求不出本机。
        """

        pool = service_depends.get_mcp_connection_pool()
        try:
            result = MCPCallResult.from_sdk(
                wait_for_call(
                    pool.submit(pool.call_tool(server, tool_name, arguments)),
                    execution_context,
                    CALL_TIMEOUT_SECONDS,
                )
            )
        except ToolCallStopped:
            return tool_cancelled(model_name, reason=STOPPED_IN_FLIGHT_REASON)
        except CallDeadlineExceeded:
            # 只有等待本身的时限：建连超时等 future 里冒出的 TimeoutError 落到下面的通用分支归类。
            self._log_call_failure(server, tool_name, execution_context, "timeout")
            return tool_error(
                model_name,
                f"MCP call timed out after {CALL_TIMEOUT_SECONDS:.0f} seconds; the request was "
                "abandoned but the server may still have acted on it. Check its state before "
                "retrying.",
                status_hint="调用超时",
            )
        except MCPToolArgumentsError as exc:
            self._log_call_failure(server, tool_name, execution_context, "invalid_arguments", error=str(exc))
            return tool_error(
                model_name,
                f"{exc}. The request was not sent. The server's current input schema differs from the one "
                f"you loaded; fix the arguments to match it and call again: "
                f"{json.dumps(exc.schema, ensure_ascii=False)}",
                status_hint="参数无效",
            )
        except MCPToolNotFoundError as exc:
            self._log_call_failure(server, tool_name, execution_context, "tool_not_found")
            return tool_error(
                model_name,
                f"{exc}. The server no longer has this tool; search for its tools again with tool_search.",
                status_hint="工具不存在",
            )
        except Exception as exc:
            failure = MCPFailure.from_exception(exc, server)
            self._log_call_failure(server, tool_name, execution_context, failure.kind, error=failure.detail)
            return tool_error(model_name, failure.for_model(), status_hint=failure.status_hint())

        if result.is_error:
            self._log_call_failure(server, tool_name, execution_context, "server_reported_error")
            # 报错正文也是服务器给的，同样进 untrusted 块。
            external = ExternalContent()
            reported = (
                external.block("mcp_result", result.text, server.name)
                if result.text
                else "The MCP server reported a tool call failure without details."
            )
            return tool_error(
                model_name,
                external.close(
                    f"The MCP server reported a tool call failure:\n{reported}\n"
                    "Check the arguments and the server's response before deciding whether to retry."
                ),
                status_hint="工具报错",
            )
        return self._success(model_name, server, tool_name, result, execution_context)


    @staticmethod
    def _success(
        model_name: str,
        server: ScopedMCPServer,
        tool_name: str,
        result: MCPCallResult,
        execution_context: ToolExecutionContext,
    ) -> ToolObservation:
        """成功结果：服务器给的文字进 untrusted 块（``ExternalContent``），图片说明是本仓写的、在块外；
        图片存进工作区附件（``StoredResultImages``），经工具图片通道交给模型。

        MCP 工具只是有时带图，不能像专门看图的工具那样由门禁整个拒绝；模型看不了图时由 observe 统一改写成
        说明（``withhold_unviewable_images``），这里不判断模型能力。
        """

        images = StoredResultImages.store(
            result.images,
            workspace_id=execution_context.workspace_id,
            name=f"mcp-{server.name}",
            log_data={
                "session_id": execution_context.session_id,
                "server_name": server.name,
                "tool_name": tool_name,
            },
        )
        external = ExternalContent()
        text = external.block("mcp_result", result.text, server.name) if result.text else ""
        return tool_success(
            model_name,
            external.close("\n".join(part for part in [text, *images.notes] if part)),
            display_data=build_mcp_call_display_data(
                server_name=server.name,
                tool_name=tool_name,
                scope=server.scope.kind,
                images=images.asset_ids,
            ),
            images=images.paths,
        )


    @staticmethod
    def _log_call_failure(
        server: ScopedMCPServer,
        tool_name: str,
        execution_context: ToolExecutionContext,
        reason: str,
        *,
        error: str | None = None,
    ) -> None:
        """调用失败的唯一日志出口：执行器不记录返回的错误观察，这里不记就查不到。"""

        log.warning(
            "mcp_tool_call_failed",
            extra={
                "msg": "MCP 工具调用失败",
                "data": {
                    "session_id": execution_context.session_id,
                    "tool_call_id": execution_context.tool_call_id,
                    "scope": server.scope.key,
                    "server_name": server.name,
                    "tool_name": tool_name,
                    "reason": reason,
                    "error": error,
                },
            },
        )

    # --- 目录 ---

    @classmethod
    def _load(cls, workspace_root: str) -> _Loaded:
        try:
            servers = available_servers(workspace_root)
        except MCPConfigurationError as exc:
            log.warning(
                "mcp_external_catalog_unavailable",
                extra={"msg": "MCP 配置读取失败，延迟工具目录不可用", "data": {"error": str(exc)}},
            )
            raise ExternalCatalogUnavailable(
                "The MCP tool catalog is unavailable because the local MCP configuration could not be "
                "read. Tell the user to fix it in the MCP settings."
            ) from exc
        machine = MCPMachineState.read()
        catalogs = [(server, machine.catalog(server)) for server in servers]
        entries = [(server, entry) for server, catalog in catalogs for entry in catalog.tools]
        names = cls._model_tool_names((server.name, entry.name) for server, entry in entries)
        definitions = [
            cls._definition(names[(server.name, entry.name)], server, entry) for server, entry in entries
        ]
        instructions = [
            ExternalInstructions(source=server.name, text=text)
            for server, catalog in catalogs
            if (text := cls._clip(catalog.instructions.strip(), _MAX_INSTRUCTIONS_CHARS))
        ]
        return (
            tuple(sorted(definitions, key=lambda d: d.name)),
            tuple(sorted(instructions, key=lambda i: i.source)),
        )

    @staticmethod
    def _clip(text: str, limit: int) -> str:
        return text if len(text) <= limit else text[: limit - 1] + "…"

    @classmethod
    def _definition(
        cls, name: str, server: ScopedMCPServer, entry: MCPToolEntry
    ) -> ExternalToolDefinition:
        description = cls._clip(" ".join(entry.description.split()), _MAX_DESCRIPTION_CHARS)
        return ExternalToolDefinition(
            name=name,
            description=f"[MCP server {server.name}] {description}".rstrip(),
            parameters=entry.input_schema,
            reference={"scope": server.scope.key, "server": server.name, "tool": entry.name},
        )

    @staticmethod
    def _catalog_files(workspace_root: str) -> list[Path]:
        """决定目录的全部文件：两层服务配置，加上记着测试结果与工具目录的本机记录。"""

        return [
            ExtensionScope.system().mcp_servers_file,
            ExtensionScope.workspace(workspace_root).mcp_servers_file,
            MCPMachineState.file(),
        ]

    # --- 模型可见名 ---

    @classmethod
    def _model_tool_names(cls, tools: Iterable[tuple[str, str]]) -> dict[tuple[str, str], str]:
        """``(服务名, 原工具名)`` → 模型可见名；同一批里名字唯一。"""

        pairs = list(tools)
        plain = {pair: f"{PREFIX}{cls._clean(pair[0])}__{cls._clean(pair[1])}" for pair in pairs}
        counts = Counter(plain.values())
        return {
            pair: name if counts[name] == 1 and len(name) <= _MAX_NAME_LENGTH else cls._hashed(name, pair)
            for pair, name in plain.items()
        }

    @staticmethod
    def _clean(text: str) -> str:
        return _INVALID.sub("_", text)

    @staticmethod
    def _hashed(name: str, pair: tuple[str, str]) -> str:
        digest = hashlib.sha256(f"{pair[0]}\0{pair[1]}".encode()).hexdigest()[:_HASH_LENGTH]
        return f"{name[: _MAX_NAME_LENGTH - _HASH_LENGTH - 1]}_{digest}"
