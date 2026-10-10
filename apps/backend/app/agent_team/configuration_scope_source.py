"""Agent Team 配置文件来源：作用域键 → 配置目录 → 已校验配置映射。

单一职责：把「某个作用域对应的 ``.cosir/agent-teams`` 目录」读成 ``team_id → 配置`` 映射。
不做索引、不缓存、不做作用域可见性合并——那些属于 ``AgentTeamConfigurationRegistry``。

不负责：注册表索引、作用域覆盖规则、配置文件写入与删除。

目录级读取与符号链接策略复用 ``app.utils.scope_config_directory``，与 Agent profile 来源共用
同一套严格度（见该模块说明）。

失败策略（坏配置不影响运行）：目录不存在视为「没有配置」（不记日志）；布局不合法（不是目录、
符号链接目录、越界符号链接）与单个文件无效一律降级为跳过并写日志，**不向读取方抛异常**——
Team 配置在每次运行前解析，不能让一个坏文件把用户的操作打断。
"""

from __future__ import annotations

from pathlib import Path

from app.agent_team.configuration.agent_team_configuration import AgentTeamConfiguration
from app.config.logging.logger import log
from app.utils.json_utils import read_json_object
from app.utils.path.system_cosir import system_agent_team_config_dir
from app.utils.path.workspace_cosir import workspace_agent_team_config_dir
from app.utils.scope_config_directory import ScopeDirectoryError, read_scope_json_files
from app.utils.workspace_scope import SYSTEM_SCOPE


class AgentTeamConfigurationScopeSource:
    """系统级与 workspace 级 Agent Team JSON 配置的来源。

    目录映射是唯一口径：系统作用域固定取 ``system_agent_team_config_dir()``，workspace
    作用域取 ``<workspace>/.cosir/agent-teams``（``app.utils.path`` 是拼接 ``.cosir`` 的
    唯一位置）。
    """

    def directory(self, scope: str) -> Path:
        """返回作用域对应的 Team 配置目录。

        参数:
            scope: 系统作用域哨兵或归一化后的 workspace 作用域键。

        返回:
            该作用域的 ``agent-teams`` 配置目录；不校验存在性、不创建目录。

        异常:
            无。

        副作用:
            无（纯路径拼接）。
        """

        if scope == SYSTEM_SCOPE:
            return system_agent_team_config_dir()
        return workspace_agent_team_config_dir(scope)

    def load(self, scope: str) -> dict[str, AgentTeamConfiguration]:
        """读取作用域配置目录中的全部合法 Team 配置。

        参数:
            scope: 系统作用域哨兵或归一化后的 workspace 作用域键。

        返回:
            以 ``team_id`` 为键的已校验配置；目录不存在或全部文件无效时返回空映射。

        异常:
            无：目录级读取失败降级为空映射并写 ``team_configuration_scope_load_failed``
            warning 日志；单个文件无效写 ``agent_team_config_invalid`` error 日志后跳过
            （与既有事件名保持一致，便于日志连续排查）。

        副作用:
            读取配置目录及文件；不创建目录、不修改文件系统、不写缓存。
        """

        directory = self.directory(scope)
        try:
            paths = read_scope_json_files(directory)
        except ScopeDirectoryError as exc:
            self._log_scope_load_failed(scope, directory, reason=f"{type(exc).__name__}: {exc}")
            return {}

        loaded: dict[str, AgentTeamConfiguration] = {}
        for path in paths:
            try:
                configuration = AgentTeamConfiguration.model_validate(read_json_object(path))
            except Exception as exc:
                log.error(
                    "agent_team_config_invalid",
                    extra={
                        "msg": "Agent Team 配置无效，已跳过该文件",
                        "data": {
                            "path": str(path),
                            "error_type": type(exc).__name__,
                            "error": str(exc)[:500],
                        },
                    },
                )
                continue
            loaded[configuration.team_id] = configuration
        return loaded

    @staticmethod
    def _log_scope_load_failed(scope: str, directory: Path, *, reason: str) -> None:
        """记录目录级装载失败，供降级后的复盘定位。

        参数:
            scope: 失败的作用域键。
            directory: 该作用域对应的配置目录。
            reason: 失败原因摘要（异常类型与消息，或布局判定结论）。

        返回:
            无。

        异常:
            无。

        副作用:
            写 ``team_configuration_scope_load_failed`` warning 日志（含 scope、directory、
            reason 截断 500 字符）。
        """

        log.warning(
            "team_configuration_scope_load_failed",
            extra={
                "msg": "Team 配置目录不可用，该作用域按无配置降级",
                "data": {
                    "scope": scope,
                    "directory": str(directory),
                    "reason": reason[:500],
                },
            },
        )
