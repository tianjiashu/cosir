"""MCP 服务的本机测试记录：哪些作用域里的哪些服务、以哪些连接配置测试通过。

为什么单独存、且只存系统级（``<DATA>/.cosir/config/mcp_tested_servers.json``）：「测试通过」是
「这台机器上的用户看过并跑通了这条命令」的本机事实，是 Agent 能用一个服务的门槛。workspace 的
``mcp_servers.json`` 可能随仓库共享，若测试结果也写在那里，仓库作者就能预先写好「已测试」，
让未经本机测试的命令直接交给 Agent。

记录按作用域键 → 服务名 → 测试通过的连接指纹集合保存：连接参数一变，指纹对不上，就等于没测过。
用集合而不是单个指纹，是因为配置页可以测一份还没保存的草稿——草稿测试通过不能顶掉已保存配置的
记录。保存时把本作用域的记录裁到「仍存在的服务的当前配置」，删掉的服务再加回来、或残留的草稿
指纹都必须重新测试；工作区删除时整份作用域记录一起丢掉。

文件损坏时按「什么都没测过」处理（失败即关闭）并记录错误，下一次写入会重写它。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.config.logging.logger import log
from app.service.configuration.file_store import ConfigurationFileStore
from app.utils.path.system_cosir import system_cosir_dir, system_mcp_tested_servers_file

if TYPE_CHECKING:
    from app.service.configuration.extension_scope import ExtensionScope
    from app.service.configuration.mcp_configuration import ScopedMCPServer


class MCPTestedServers(BaseModel):
    """测试记录文件的内容：``scopes[作用域键][服务名] = [测试通过的连接指纹, ...]``。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    scopes: dict[str, dict[str, list[str]]] = Field(default_factory=dict)

    @classmethod
    def read(cls) -> MCPTestedServers:
        path = system_mcp_tested_servers_file()
        try:
            content = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return cls()
        except (OSError, UnicodeDecodeError) as exc:
            log.error(
                "mcp_tested_servers_read_failed",
                extra={
                    "msg": "MCP 测试记录无法读取，按全部未测试处理",
                    "data": {"error_type": type(exc).__name__},
                },
            )
            return cls()
        try:
            return cls.model_validate_json(content)
        except ValidationError as exc:
            log.error(
                "mcp_tested_servers_invalid",
                extra={
                    "msg": "MCP 测试记录格式无效，按全部未测试处理",
                    "data": {"error_type": type(exc).__name__},
                },
            )
            return cls()

    def passed(self, server: ScopedMCPServer) -> bool:
        """这个服务当前的连接配置是否在本机测试通过。"""

        return server.fingerprint in self.scopes.get(server.scope.key, {}).get(server.name, [])

    @classmethod
    def record_pass(cls, server: ScopedMCPServer) -> None:
        """记下一次成功测试；同名服务其他配置的记录保留（测草稿不影响已保存配置）。"""

        with ConfigurationFileStore.locked(system_mcp_tested_servers_file()):
            record = cls.read()
            fingerprints = record.scopes.setdefault(server.scope.key, {}).setdefault(server.name, [])
            if server.fingerprint not in fingerprints:
                fingerprints.append(server.fingerprint)
                record._write()

    @classmethod
    def revoke(cls, server: ScopedMCPServer) -> bool:
        """撤销这一份连接配置的测试记录（重测失败时调用）；返回是否真的撤销了一条。

        只撤销指纹相同的那一条：测试失败的是草稿时，不影响已保存配置的测试结果。
        """

        with ConfigurationFileStore.locked(system_mcp_tested_servers_file()):
            record = cls.read()
            names = record.scopes.get(server.scope.key, {})
            fingerprints = names.get(server.name, [])
            if server.fingerprint not in fingerprints:
                return False
            fingerprints.remove(server.fingerprint)
            record._prune_empty(server.scope.key)
            record._write()
            return True

    @classmethod
    def keep_only(cls, scope: ExtensionScope, saved: Iterable[ScopedMCPServer]) -> None:
        """保存配置后调用：本作用域只保留「仍存在的服务的当前配置」的测试记录。

        删掉的服务、改掉的旧参数、没保存的草稿留下的指纹都在这里清掉；保存的正是测过的那份草稿时，
        它的指纹被保留，保存后立即可用。
        """

        current = {server.name: server.fingerprint for server in saved}
        with ConfigurationFileStore.locked(system_mcp_tested_servers_file()):
            record = cls.read()
            names = record.scopes.get(scope.key, {})
            kept = {
                name: [fingerprint]
                for name, fingerprint in current.items()
                if fingerprint in names.get(name, [])
            }
            if kept == names:
                return
            record.scopes[scope.key] = kept
            record._prune_empty(scope.key)
            record._write()

    @classmethod
    def drop_scope(cls, scope: ExtensionScope) -> None:
        """丢掉一个作用域的全部测试记录（工作区删除后调用：同一路径再登记的工作区要重新测试）。"""

        with ConfigurationFileStore.locked(system_mcp_tested_servers_file()):
            record = cls.read()
            if record.scopes.pop(scope.key, None) is not None:
                record._write()

    def _prune_empty(self, scope_key: str) -> None:
        names = self.scopes.get(scope_key)
        if names is None:
            return
        for name in [name for name, fingerprints in names.items() if not fingerprints]:
            del names[name]
        if not names:
            del self.scopes[scope_key]

    def _write(self) -> None:
        serialized = json.dumps(self.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"
        ConfigurationFileStore.write_text_atomic(
            system_mcp_tested_servers_file(), serialized, root=system_cosir_dir()
        )
