"""MCP 服务在这台机器上的事实：连接测试记录与 OAuth 凭据（唯一事实源）。

两者是同一类事实——「这台机器上的用户对某个服务做过什么」——所以放在同一份记录里，按作用域键 → 服务名
保存，保存配置与删除工作区时一处清理，不会只清了一半：

- **测试记录**：哪些连接定义在本机测试通过，是 Agent 能用一个服务的门槛。按定义指纹（含变量名、不含
  变量值）记成集合：连接参数一变就等于没测过；用集合而不是单个指纹，是因为配置页可以测一份还没保存的
  草稿，草稿通过不能顶掉已保存配置的记录。
- **OAuth 凭据**：动态注册得到的客户端信息与访问令牌，按服务地址（变量已展开）分开存。令牌只对授权时的
  资源地址有效（MCP 授权要求 RFC 8707 ``resource``，SDK 按 server_url 推出），所以不同地址是不同的凭据，
  不是同一条被覆盖；与测试记录用集合同理，测一份改了地址的草稿也不会抹掉已保存地址的登录。保存配置时
  裁到当前地址。手填 ``client_id`` 的服务不存客户端信息（配置就是事实源）。令牌带绝对过期时间，供重启后
  用刷新令牌静默续期（见 ``tools/mcp/oauth/provider``）。

只存在系统级 ``<DATA>/.cosir/config/mcp_machine_state.json``（受保护路径，Agent 的文件工具读不到）：
workspace 的 ``mcp_servers.json`` 可能随仓库共享，这些若写在那里，仓库作者就能预先写好「已测试」，令牌
也会随仓库外泄。文件损坏时按「什么都没做过」处理（失败即关闭）并记录错误，下一次写入会重写它。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.config.logging.logger import log
from app.service.configuration import file_store
from app.utils.path.system_cosir import system_cosir_dir, system_mcp_machine_state_file

if TYPE_CHECKING:
    from app.service.configuration.extension_scope import ExtensionScope
    from app.service.configuration.mcp.configuration import ScopedMCPServer


class OAuthRecord(BaseModel):
    """一个服务在一个地址上的授权记录。

    ``needs_authorization``：最近一次不交互地连这个地址时需要用户登录。任何一次连接成功都说明它不再成立：
    拿到新令牌时（``token_storage``）与测试通过时（:meth:`MCPMachineState.record_pass`）清掉。
    """

    model_config = ConfigDict(extra="forbid")

    client_info: dict | None = None
    tokens: dict | None = None
    expires_at: float | None = None
    needs_authorization: bool = False


class ServerMachineState(BaseModel):
    """一个服务在本机的全部事实：测试通过的定义指纹与授权记录。"""

    model_config = ConfigDict(extra="forbid")

    tested: list[str] = Field(default_factory=list)
    # 服务地址（变量已展开）→ 授权记录。
    oauth: dict[str, OAuthRecord] = Field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not self.tested and not self.oauth


class MCPMachineState(BaseModel):
    """记录文件的内容：``scopes[作用域键][服务名] = ServerMachineState``。"""

    model_config = ConfigDict(extra="forbid")

    scopes: dict[str, dict[str, ServerMachineState]] = Field(default_factory=dict)

    @classmethod
    def read(cls) -> MCPMachineState:
        path = system_mcp_machine_state_file()
        try:
            content = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return cls()
        except (OSError, UnicodeDecodeError) as exc:
            log.error(
                "mcp_machine_state_read_failed",
                extra={
                    "msg": "MCP 本机记录无法读取，按未测试、未授权处理",
                    "data": {"error_type": type(exc).__name__},
                },
            )
            return cls()
        try:
            return cls.model_validate_json(content)
        except ValidationError as exc:
            log.error(
                "mcp_machine_state_invalid",
                extra={
                    "msg": "MCP 本机记录格式无效，按未测试、未授权处理",
                    "data": {"error_type": type(exc).__name__},
                },
            )
            return cls()

    # --- 读取 ---

    def server(self, server: ScopedMCPServer) -> ServerMachineState:
        return self.scopes.get(server.scope.key, {}).get(server.name, ServerMachineState())

    def passed(self, server: ScopedMCPServer) -> bool:
        """这个服务当前的连接定义是否在本机测试通过。"""

        return server.definition_fingerprint in self.server(server).tested

    def oauth(self, server: ScopedMCPServer, server_url: str) -> OAuthRecord | None:
        """这个服务在这个地址上的授权记录。"""

        return self.server(server).oauth.get(server_url)

    # --- 写入（整份文件在锁内读改写） ---

    @classmethod
    def record_pass(cls, server: ScopedMCPServer) -> None:
        """记下一次成功连接（测试或登录授权）；同名服务其他定义的记录保留（测草稿不影响已保存配置）。

        连上了就说明这个地址眼下不需要用户登录，一并清掉它的「需要登录」标记：改用静态 token、服务不再要求
        认证、令牌已被别处续上，都不会经过「拿到新令牌」，只清那一处会让标记一直挂着。成功的连接一定能展开
        变量，这里取地址不会失败。
        """

        url = server.connection_config().url if server.config.uses_oauth else None

        def change(state: ServerMachineState) -> None:
            if server.definition_fingerprint not in state.tested:
                state.tested.append(server.definition_fingerprint)
            record = state.oauth.get(url) if url is not None else None
            if record is not None and record.needs_authorization:
                state.oauth[url] = record.model_copy(update={"needs_authorization": False})

        cls._change_server(server, change)

    @classmethod
    def revoke(cls, server: ScopedMCPServer) -> bool:
        """撤销这一份定义的测试记录（重测失败时调用）；返回是否真的撤销了一条。

        只撤销指纹相同的那一条：测试失败的是草稿时，不影响已保存配置的测试结果。
        """

        revoked = False

        def change(state: ServerMachineState) -> None:
            nonlocal revoked
            if server.definition_fingerprint in state.tested:
                state.tested.remove(server.definition_fingerprint)
                revoked = True

        cls._change_server(server, change)
        return revoked

    @classmethod
    def update_oauth(cls, server: ScopedMCPServer, server_url: str, **changes: object) -> None:
        """改写一个服务在一个地址上的授权记录；其他地址的记录不动。"""

        def change(state: ServerMachineState) -> None:
            current = state.oauth.get(server_url, OAuthRecord())
            state.oauth[server_url] = current.model_copy(update=changes)

        cls._change_server(server, change)

    @classmethod
    def keep_only(cls, scope: ExtensionScope, saved: Iterable[ScopedMCPServer]) -> None:
        """保存配置后调用：本作用域只留「仍存在的服务」的事实。

        测试记录裁到各服务的当前定义（删掉的服务、改掉的旧参数、没保存的草稿都要重测；保存的正是测过的
        草稿时立即可用）；授权记录裁到当前地址（草稿地址上的登录随之丢掉，保存前先登录的那份就是当前地址，
        保留）；删掉的服务连同授权一起丢掉（同名服务再加回来要重新授权）。
        """

        saved = list(saved)
        current = {server.name: server.definition_fingerprint for server in saved}
        urls = {server.name: _current_url(server) for server in saved}
        with file_store.locked(system_mcp_machine_state_file()):
            record = cls.read()
            names = record.scopes.get(scope.key, {})
            kept: dict[str, ServerMachineState] = {}
            for name, state in names.items():
                if name not in current:
                    continue
                url = urls[name]
                trimmed = ServerMachineState(
                    tested=[current[name]] if current[name] in state.tested else [],
                    oauth=state.oauth
                    if url is _UNKNOWN_URL
                    else {key: value for key, value in state.oauth.items() if key == url},
                )
                if not trimmed.empty:
                    kept[name] = trimmed
            if kept == names:
                return
            if kept:
                record.scopes[scope.key] = kept
            else:
                record.scopes.pop(scope.key, None)
            record._write()

    @classmethod
    def drop_scope(cls, scope: ExtensionScope) -> None:
        """丢掉一个作用域的全部事实（工作区删除后调用：同一路径再登记的工作区要重新测试、重新授权）。"""

        with file_store.locked(system_mcp_machine_state_file()):
            record = cls.read()
            if record.scopes.pop(scope.key, None) is not None:
                record._write()

    @classmethod
    def _change_server(
        cls, server: ScopedMCPServer, change: Callable[[ServerMachineState], None]
    ) -> None:
        with file_store.locked(system_mcp_machine_state_file()):
            record = cls.read()
            names = record.scopes.setdefault(server.scope.key, {})
            state = names.get(server.name, ServerMachineState()).model_copy(deep=True)
            change(state)
            if state == names.get(server.name, ServerMachineState()):
                record._prune(server.scope.key)
                return
            if state.empty:
                names.pop(server.name, None)
            else:
                names[server.name] = state
            record._prune(server.scope.key)
            record._write()

    def _prune(self, scope_key: str) -> None:
        if not self.scopes.get(scope_key):
            self.scopes.pop(scope_key, None)

    def _write(self) -> None:
        file_store.write_json(system_mcp_machine_state_file(), self, root=system_cosir_dir())


# 保存时变量还没设置、展开不出地址：判断不了哪条授权属于当前地址，整份保留，等下次保存再裁。
_UNKNOWN_URL = object()


def _current_url(server: ScopedMCPServer) -> str | object | None:
    """保存时这个服务的当前授权地址；不走 OAuth 为 ``None``（全部裁掉），展开不出为 ``_UNKNOWN_URL``。"""

    if not server.config.uses_oauth:
        return None
    try:
        return server.connection_config().url
    except ValueError:
        return _UNKNOWN_URL
