"""Agent profile 配置文件来源：作用域键 → 配置目录 → profile 列表。

单一职责：把「某个作用域对应的 ``.cosir/agents`` 目录」读成已校验的 profile 列表。
不做索引、不缓存、不做作用域可见性合并——那些属于 ``AgentProfileRegistry``。

不负责：注册表索引、重复/冲突裁决、配置文件写入，以及「该作用域是否已被装载」的判定。
目录级读取与符号链接策略复用 ``app.utils.scope_config_directory``，与 Team 配置来源共用同一
套严格度（见该模块说明）。

失败策略（本模块是「坏配置不影响运行」的落点之一）：目录不存在视为「没有配置」；目录级问题
（失效符号链接、目录本身是符号链接、路径不是目录、读取失败）与单个文件无效一律降级为空/跳过
并写日志，**不向读取方抛异常**——Agent 读取路径（resolve/list）遍布运行期热点，不能让一个坏
配置文件或坏目录把 Run 打断。
"""

from __future__ import annotations

from pathlib import Path

from app.config.logging.logger import log
from app.core.agents.agent_profile import AgentProfile
from app.utils.path.system_cosir import system_agent_config_dir
from app.utils.path.workspace_cosir import workspace_agent_config_dir
from app.utils.scope_config_directory import ScopeDirectoryError, read_scope_json_files
from app.utils.workspace_scope import SYSTEM_SCOPE


class AgentProfileScopeSource:
    """系统级与 workspace 级 Agent JSON 配置的来源。

    目录映射是唯一口径：系统作用域固定取 ``system_agent_config_dir()``，workspace 作用域
    取 ``<workspace>/.cosir/agents``（``app.utils.path`` 是拼接 ``.cosir`` 的唯一位置）。
    """

    def directory(self, scope: str) -> Path:
        """返回作用域对应的配置目录。

        参数:
            scope: 系统作用域哨兵或归一化后的 workspace 作用域键。

        返回:
            该作用域的 ``agents`` 配置目录；不校验存在性、不创建目录。

        异常:
            无。

        副作用:
            无（纯路径拼接）。
        """

        if scope == SYSTEM_SCOPE:
            return system_agent_config_dir()
        return workspace_agent_config_dir(scope)

    def load(self, scope: str) -> list[AgentProfile]:
        """读取作用域配置目录中的全部合法 profile。

        参数:
            scope: 系统作用域哨兵或归一化后的 workspace 作用域键。

        返回:
            按文件名排序读取、逐个通过 ``AgentProfile.vaild_agent_profile`` 的 profile；
            目录不存在、目录级失败或全部文件无效时返回空列表。

        异常:
            无：目录级问题在此降级为空列表，并写 ``agent_profile_scope_load_failed``
            warning 日志（含 scope、directory、异常类型与消息）；单个文件无效由
            ``AgentProfile.vaild_agent_profile`` 以 ``agent_profile_config_invalid``
            error 日志留痕后跳过。

        副作用:
            读取配置目录及文件；不修改文件系统、不写入任何缓存。
        """

        directory = self.directory(scope)
        try:
            paths = read_scope_json_files(directory)
        except ScopeDirectoryError as exc:
            log.warning(
                "agent_profile_scope_load_failed",
                extra={
                    "msg": "Agent 配置目录不可用，该作用域按无配置降级",
                    "data": {
                        "scope": scope,
                        "directory": str(directory),
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:500],
                    },
                },
            )
            return []

        profiles: list[AgentProfile] = []
        for path in paths:
            profile = AgentProfile.vaild_agent_profile(path)
            if profile is None:
                continue
            profiles.append(profile)
        return profiles
