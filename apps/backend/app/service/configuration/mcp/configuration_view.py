"""MCP 配置页看到的服务条目：可见服务加上只属于本机的状态。

服务定义来自各作用域的 ``mcp_servers.json``；「本机测试通过」与 OAuth 授权状态来自系统级本机记录
（``machine_state``），变量的值从哪来看后端进程环境与 ``Settings`` 的注入记录。这些在这里一处拼成配置页
的视图，并推出每个服务的**主状态**——用户只需要知道「Agent 能不能用、不能的话下一步做什么」，规则只此
一处，前端照着显示、不自己拼。接入层只做格式转换、不读持久化，与 Skill 的 ``configuration_view`` 同一分工。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from app.config.settings import Settings
from app.service.configuration.mcp.configuration import ScopedMCPServer
from app.service.configuration.mcp.machine_state import MCPMachineState

# 按优先级从高到低：停用了就不谈其他；缺必填变量时连不上；授权失效时 Agent 调不通；再看有没有测过。
ServerStatus = Literal["disabled", "missing_variables", "authorization_required", "untested", "available"]
# ``cosir``：值在 cosir 的系统 ``.env`` 里，配置页可以改；``shell``：值来自用户的 shell 环境，启动时优先于
# ``.env``（``Settings`` 只 ``setdefault``），配置页改不动，只能在 shell 里改。
VariableSource = Literal["unset", "cosir", "shell"]


@dataclass(frozen=True)
class MCPVariableStatus:
    """服务引用的一个变量：值从哪来（不带值），没设置时有没有默认值兜底。"""

    name: str
    source: VariableSource
    has_default: bool

    @classmethod
    def of(cls, name: str, has_default: bool) -> MCPVariableStatus:
        if not os.environ.get(name):
            return cls(name, "unset", has_default)
        return cls(name, "cosir" if Settings.is_file_loaded_value(name) else "shell", has_default)


@dataclass(frozen=True)
class MCPServerEntry:
    """配置页的一个服务。

    ``literal_fields``：工作区服务里直接写了值的 env 与 headers 字段——工作区配置随仓库共享、Agent 也读
    得到，配置页据此提醒把密钥改成变量；系统级配置在受保护的系统 ``.cosir`` 里，不提醒。
    """

    server: ScopedMCPServer
    status: ServerStatus
    variables: tuple[MCPVariableStatus, ...]
    literal_fields: tuple[str, ...]

    @classmethod
    def for_servers(cls, servers: list[ScopedMCPServer]) -> list[MCPServerEntry]:
        """为一组可见服务附上本机状态；本机记录只读一次。"""

        machine = MCPMachineState.read()
        entries = []
        for server in servers:
            variables = tuple(
                MCPVariableStatus.of(ref.name, ref.has_default)
                for ref in server.config.variable_references()
            )
            entries.append(
                cls(
                    server=server,
                    status=_status(machine, server, variables),
                    variables=variables,
                    literal_fields=(
                        tuple(server.config.literal_value_fields())
                        if server.scope.workspace_root is not None
                        else ()
                    ),
                )
            )
        return entries


def _status(
    machine: MCPMachineState, server: ScopedMCPServer, variables: tuple[MCPVariableStatus, ...]
) -> ServerStatus:
    if not server.config.enabled:
        return "disabled"
    if any(item.source == "unset" and not item.has_default for item in variables):
        return "missing_variables"
    if server.config.transport != "stdio":
        # 必填变量都在，展开不会失败；授权记录认的是实际地址。
        record = machine.oauth(server, server.connection_config().url)
        if record is not None and record.needs_authorization:
            return "authorization_required"
    return "available" if machine.passed(server) else "untested"
