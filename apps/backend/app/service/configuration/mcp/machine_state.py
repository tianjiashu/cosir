"""MCP 服务在这台机器上的事实：连接测试记录与 OAuth 凭据（唯一事实源）。

两者是同一类事实——「这台机器上的用户对某个服务做过什么」——所以放在同一份记录里，按作用域键 → 服务名
保存，保存配置与删除工作区时一处清理，不会只清了一半：

- **测试记录**：哪些连接定义在本机测试通过，是 Agent 能用一个服务的门槛。按定义指纹（含变量名、不含
  变量值）记成集合：连接参数一变就等于没测过；用集合而不是单个指纹，是因为配置页可以测一份还没保存的
  草稿，草稿通过不能顶掉已保存配置的记录。
- **OAuth 凭据**：动态注册得到的客户端信息与访问令牌，连同授权时的服务地址；地址一变（改了 url 或变量
  展开结果不同），旧凭据就不再交给新地址。手填 ``client_id`` 的服务不存客户端信息（配置就是事实源）。
  令牌带绝对过期时间，供重启后用刷新令牌静默续期（见 ``tools/mcp/oauth/provider``）。

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
    """一个服务的授权记录。``needs_authorization``：Agent 调用时发现授权已失效、等用户重新授权。"""

    model_config = ConfigDict(extra="forbid")

    server_url: str
    client_info: dict | None = None
    tokens: dict | None = None
    expires_at: float | None = None
    needs_authorization: bool = False


class ServerMachineState(BaseModel):
    """一个服务在本机的全部事实：测试通过的定义指纹与授权记录。"""

    model_config = ConfigDict(extra="forbid")

    tested: list[str] = Field(default_factory=list)
    oauth: OAuthRecord | None = None

    @property
    def empty(self) -> bool:
        return not self.tested and self.oauth is None


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
        """这个服务在这个地址上的授权记录；地址不同的旧记录视为没有。"""

        record = self.server(server).oauth
        return record if record is not None and record.server_url == server_url else None

    # --- 写入（整份文件在锁内读改写） ---

    @classmethod
    def record_pass(cls, server: ScopedMCPServer) -> None:
        """记下一次成功测试；同名服务其他定义的记录保留（测草稿不影响已保存配置）。"""

        def change(state: ServerMachineState) -> None:
            if server.definition_fingerprint not in state.tested:
                state.tested.append(server.definition_fingerprint)

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
        """改写一个服务的授权记录（地址变了就从空记录开始）。"""

        def change(state: ServerMachineState) -> None:
            current = state.oauth
            if current is None or current.server_url != server_url:
                current = OAuthRecord(server_url=server_url)
            state.oauth = current.model_copy(update=changes)

        cls._change_server(server, change)

    @classmethod
    def keep_only(cls, scope: ExtensionScope, saved: Iterable[ScopedMCPServer]) -> None:
        """保存配置后调用：本作用域只留「仍存在的服务」的事实。

        测试记录裁到各服务的当前定义（删掉的服务、改掉的旧参数、没保存的草稿都要重测；保存的正是测过的
        草稿时立即可用）；删掉的服务连同授权一起丢掉（同名服务再加回来要重新授权）。
        """

        current = {server.name: server.definition_fingerprint for server in saved}
        with file_store.locked(system_mcp_machine_state_file()):
            record = cls.read()
            names = record.scopes.get(scope.key, {})
            kept: dict[str, ServerMachineState] = {}
            for name, state in names.items():
                if name not in current:
                    continue
                trimmed = ServerMachineState(
                    tested=[current[name]] if current[name] in state.tested else [],
                    oauth=state.oauth,
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
