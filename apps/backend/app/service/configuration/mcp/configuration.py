"""MCP 服务配置的数据契约、读取规则与 Agent 可用视图。

只描述「连哪些服务、怎么连、哪些对 Agent 可用」；建立连接与会话由 ``app/tools/mcp/`` 负责。
放在 service 层：配置页（``configuration_service``）与 Agent 侧（``tools/mcp`` 的连接池与外部工具目录）都读它。

两个作用域（系统级、workspace 级）各有一份 ``mcp_servers.json``，只放连接定义；「本机测试通过」
不在这里，而在系统级的本机记录（``mcp/machine_state``）里，workspace 文件随仓库共享也带不进来。
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    model_validator,
)

from app.service.configuration import file_store
from app.service.configuration.extension_scope import ExtensionScope
from app.service.configuration.mcp.machine_state import MCPMachineState
from app.service.configuration.mcp.variable_references import (
    MissingVariablesError,
    VariableReference,
    find_references,
    has_reference,
    is_reserved,
    substitute,
)
from app.utils.workspace_scope import SYSTEM_SCOPE, normalize_scope_path

DUPLICATE_SERVER_NAME_MESSAGE = "MCP 服务名称必须唯一"
# 校验上下文键：正在校验的是变量已展开的建连配置（见 ``MCPServerConfig.expanded``）。
_EXPANDED = "expanded"


class MCPOAuthConfig(BaseModel):
    """远程服务的 OAuth 参数；整块可省略：省略时自动注册客户端（动态注册），回调端口随机。

    服务不支持动态注册时（如 GitHub 的远程 MCP 服务）要手填预先注册的 ``client_id``；回调地址固定为
    ``http://127.0.0.1:<callback_port>/callback``，在服务方登记应用时填它。``client_secret`` 只接受
    变量引用（``${VAR}``），配置文件里不出现密钥原文。
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    client_id: str = ""
    client_secret: str = ""
    callback_port: int | None = Field(default=None, ge=1024, le=65535)
    scope: str = ""

    @model_validator(mode="after")
    def validate_client(self, info: ValidationInfo) -> MCPOAuthConfig:
        if self.client_secret and not self.client_id:
            raise ValueError("填写 client_secret 时必须同时填写 client_id")
        # 「只能写成引用」约束的是落盘定义；展开出的建连配置里本来就是值。
        expanded = bool(info.context and info.context.get(_EXPANDED))
        if self.client_secret and not expanded and not has_reference(self.client_secret):
            raise ValueError("client_secret 只能写成环境变量引用，如 ${GITHUB_CLIENT_SECRET}")
        return self


class MCPServerConfig(BaseModel):
    """一个 MCP 服务的连接定义（落盘的原样内容，可含环境变量引用，见 ``variable_references``）。

    command、args、env 的值、url、headers 的值与 OAuth 客户端凭据允许引用变量；env 与 headers 的键、名称与
    传输方式不允许。
    建连前经 :meth:`expanded` 换成实际值，落盘与测试记录始终认原样定义。
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(min_length=1)
    transport: Literal["stdio", "sse", "streamable_http"] = "stdio"
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    oauth: MCPOAuthConfig | None = None
    enabled: bool = True

    @model_validator(mode="after")
    def validate_transport_configuration(self) -> MCPServerConfig:
        """要求所选传输具备对应连接入口、不带另一种传输的字段，且不引用宿主契约变量。

        url 整个来自变量时协议要等展开后才知道：展开出的配置会再经这里校验一次。
        """

        if self.transport == "stdio" and not self.command.strip():
            raise ValueError("stdio MCP 服务必须配置 command")
        if self.transport != "stdio" and not self.url.strip():
            raise ValueError("HTTP MCP 服务必须配置 url")
        if (
            self.transport != "stdio"
            and not has_reference(self.url)
            and not self.url.casefold().startswith(("http://", "https://"))
        ):
            raise ValueError("HTTP MCP 服务 url 必须使用 http 或 https")
        stray = self._fields_of_other_transport()
        if stray:
            kind = "stdio" if self.transport == "stdio" else "HTTP"
            raise ValueError(f"{kind} MCP 服务不使用 {'、'.join(stray)}，请删除这些字段")
        reserved = sorted({ref.name for ref in self._references() if is_reserved(ref.name)})
        if reserved:
            raise ValueError(f"MCP 服务不能引用 cosir 内部变量：{', '.join(reserved)}")
        return self

    @property
    def uses_oauth(self) -> bool:
        """远程服务且请求头没有自带 ``Authorization`` 时走 OAuth（服务不要求认证时 OAuth 也不会触发）。

        自带 ``Authorization``（静态 token，常写成 ``Bearer ${TOKEN}``）是用户选定的认证方式，401 只说明
        token 不对，不该转去走授权流程。建连（``oauth/provider``）与配置页状态都按这一条判断。
        """

        return self.transport != "stdio" and not any(
            key.lower() == "authorization" for key in self.headers
        )

    def variable_references(self) -> list[VariableReference]:
        """引用到的变量，按首次出现排序；同名多处引用时，只要有一处没有默认值就算必填。"""

        merged: dict[str, bool] = {}
        for reference in self._references():
            merged[reference.name] = merged.get(reference.name, True) and reference.has_default
        return [VariableReference(name, has_default) for name, has_default in merged.items()]

    def literal_value_fields(self) -> list[str]:
        """直接写了值、没有引用变量的 env 与 headers 字段（如 ``env.API_KEY``）。

        工作区配置随仓库共享、Agent 也读得到，这些字段里若是密钥就会外泄；配置页据此提醒改成变量。
        """

        return [
            f"{prefix}.{key}"
            for prefix, values in (("env", self.env), ("headers", self.headers))
            for key, value in values.items()
            if value and not has_reference(value)
        ]

    def expanded(self, environ: Mapping[str, str]) -> MCPServerConfig:
        """把引用换成 ``environ`` 里的值，得到建连用的配置。

        异常:
            MissingVariablesError: 引用的变量未设置且没有默认值。
            ValueError: 展开后的配置不合法（如 url 变量展开后不是 http(s)）。
        """

        missing: list[str] = []
        data = self.model_dump()
        data["command"] = substitute(self.command, environ, missing)
        data["args"] = [substitute(arg, environ, missing) for arg in self.args]
        data["env"] = {key: substitute(value, environ, missing) for key, value in self.env.items()}
        data["url"] = substitute(self.url, environ, missing)
        data["headers"] = {
            key: substitute(value, environ, missing) for key, value in self.headers.items()
        }
        if self.oauth is not None:
            data["oauth"]["client_id"] = substitute(self.oauth.client_id, environ, missing)
            data["oauth"]["client_secret"] = substitute(self.oauth.client_secret, environ, missing)
        if missing:
            raise MissingVariablesError(missing)
        return MCPServerConfig.model_validate(data, context={_EXPANDED: True})

    def _fields_of_other_transport(self) -> list[str]:
        """填了值、却属于另一种传输的字段。

        每种传输只带自己的字段（stdio：command、args、env；HTTP：url、headers、oauth），所以展开、变量引用与连接
        指纹都不必按传输挑字段：留着另一种传输的值，会让 HTTP 服务因为旧的 stdio 参数报缺变量，改了用不上的值也会
        让「测试通过」失效。
        """

        if self.transport == "stdio":
            present = {"url": bool(self.url), "headers": bool(self.headers), "oauth": self.oauth is not None}
        else:
            present = {"command": bool(self.command), "args": bool(self.args), "env": bool(self.env)}
        return [name for name, filled in present.items() if filled]

    def _references(self) -> list[VariableReference]:
        texts = [self.command, *self.args, *self.env.values(), self.url, *self.headers.values()]
        if self.oauth is not None:
            texts += [self.oauth.client_id, self.oauth.client_secret]
        return [reference for text in texts for reference in find_references(text)]


class MCPConfiguration(BaseModel):
    """一个作用域 ``mcp_servers.json`` 的顶层结构：只有服务列表。

    服务名唯一是本模型的不变量：读文件与保存都经它构造，规则只在这一处。
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    servers: list[MCPServerConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_names(self) -> MCPConfiguration:
        names = [server.name for server in self.servers]
        if len(names) != len(set(names)):
            raise ValueError(DUPLICATE_SERVER_NAME_MESSAGE)
        return self

    @classmethod
    def read(cls, scope: ExtensionScope) -> MCPConfiguration:
        """读取一个作用域的 MCP 配置；尚未创建配置文件时视为空服务目录。

        异常:
            MCPConfigurationError: 配置文件无法读取或格式无效（含服务名称重复）。
        """

        return file_store.read_json_model(
            scope.mcp_servers_file,
            cls,
            error=MCPConfigurationError,
            label="MCP 配置",
            event="mcp_configuration",
            log_data={"scope": scope.key},
        )

    @classmethod
    def of(cls, servers: list[MCPServerConfig]) -> MCPConfiguration:
        """由界面提交的服务列表构造配置；违反不变量时给出可直接展示的原因。

        异常:
            MCPConfigurationError: 服务名称重复。
        """

        try:
            return cls(servers=servers)
        except ValidationError as exc:
            raise MCPConfigurationError(DUPLICATE_SERVER_NAME_MESSAGE) from exc


class MCPConfigurationError(ValueError):
    """MCP 配置文件无法解析或违反配置契约。"""


@dataclass(frozen=True)
class ScopedMCPServer:
    """一个 MCP 服务的一次具体使用：连接定义、定义它的作用域，以及在哪个工作区里用。

    两个维度不能混：``scope`` 是定义在哪（决定配置文件、本机测试记录与授权归谁），``used_in`` 是在哪个
    工作区里用（决定进程在哪运行、告诉服务的 roots、连接归谁）。系统级服务在工作区 A、B 里用是两次不同
    的使用：各自一条连接，stdio 进程在各自的工作区根目录运行，服务状态也不在工作区之间串用；测试记录与
    令牌仍按定义作用域共用一份。``used_in`` 为 ``None`` 只出现在系统级配置页（不在任何工作区里）。

    ``overrides`` 表示 workspace 层的服务遮住了系统层的同名服务。
    """

    scope: ExtensionScope
    config: MCPServerConfig
    overrides: bool = False
    used_in: Path | None = None

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def cwd(self) -> Path | None:
        """stdio 子进程的工作目录：在哪个工作区里用就在哪运行；不在工作区里时沿用后端进程目录。"""

        return self.used_in

    @property
    def pool_key(self) -> tuple[str, str, str]:
        """连接身份：定义作用域 + 使用的工作区 + 服务名。"""

        used = normalize_scope_path(self.used_in) if self.used_in is not None else SYSTEM_SCOPE
        return (self.scope.key, used, self.config.name)

    @property
    def definition_fingerprint(self) -> str:
        """原样定义（含变量名，不含变量值）的标识：本机测试记录认它。

        用户批准的是「这个服务连哪、会读哪些变量」，所以换 Key 不用重测，改了变量名或改了引用位置要重测。
        """

        return _fingerprint(self.config)

    def connection_config(self) -> MCPServerConfig:
        """建连用的配置：变量按当前进程环境展开（每次现取，换 Key 后下一次建连即生效）。

        异常:
            MissingVariablesError: 引用的变量未设置且没有默认值。
        """

        return self.config.expanded(os.environ)

    def connection_fingerprint(self) -> str:
        """实际连接参数（变量已展开）的标识：连接池认它，变量值一变就重建连接。

        异常:
            MissingVariablesError: 引用的变量未设置且没有默认值。
        """

        return _fingerprint(self.connection_config())


def visible_servers(workspace_root: str | Path | None) -> list[ScopedMCPServer]:
    """某个工作区（``None`` 为系统级）看得到的服务：workspace 层同名覆盖系统层。

    顺序：未被覆盖的系统服务按配置顺序在前，workspace 服务按配置顺序在后。
    """

    system = ExtensionScope.system()
    used_in = Path(workspace_root) if workspace_root is not None else None
    system_servers = [
        ScopedMCPServer(system, item, used_in=used_in)
        for item in MCPConfiguration.read(system).servers
    ]
    if workspace_root is None:
        return system_servers
    workspace = ExtensionScope.workspace(workspace_root)
    workspace_configs = MCPConfiguration.read(workspace).servers
    workspace_names = {item.name for item in workspace_configs}
    system_names = {item.name for item in system_servers}
    return [server for server in system_servers if server.name not in workspace_names] + [
        ScopedMCPServer(workspace, item, overrides=item.name in system_names, used_in=used_in)
        for item in workspace_configs
    ]


def available_servers(workspace_root: str | Path | None) -> list[ScopedMCPServer]:
    """对 Agent 可用的服务：可见、启用，并且当前连接配置在本机测试通过。"""

    machine = MCPMachineState.read()
    return [
        server
        for server in visible_servers(workspace_root)
        if server.config.enabled and machine.passed(server)
    ]


def _fingerprint(server: MCPServerConfig) -> str:
    """按连接参数生成稳定标识；启停开关不参与（启停不需要重新测试，也不需要重建连接）。"""

    connection = server.model_dump(mode="json", exclude={"enabled"})
    serialized = json.dumps(connection, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
