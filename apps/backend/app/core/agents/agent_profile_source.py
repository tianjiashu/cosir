"""Agent profile 配置文件来源：作用域键 → 配置文件 → profile 列表与签名。

单一职责：把一个作用域的配置文件读成已校验的 profile 列表，并给出这些文件的签名，供注册表判断要不要
重读。系统作用域除了 ``.cosir/agents`` 目录，还包括主 Agent：它的预设正文来自
``main_agent_system_prompt.md``、步数上限来自 ``Settings``，一并算进签名，所以手动改了文件或在配置页
改了步数，下一次读取就生效。

不负责：注册表索引、重复/冲突裁决、配置文件写入。
目录级读取与符号链接策略复用 ``app.utils.scope_config_directory``（见该模块说明）。

失败策略（本模块是「坏配置不影响运行」的落点之一）：目录不存在视为「没有配置」；目录级问题
（失效符号链接、目录本身是符号链接、路径不是目录、读取失败）与单个文件无效一律降级为空/跳过
并写日志，**不向读取方抛异常**——Agent 读取路径（resolve/list）遍布运行期热点，不能让一个坏
配置文件或坏目录把 Run 打断。
"""

from __future__ import annotations

from collections.abc import Hashable
from pathlib import Path

from app.config.logging.logger import log
from app.config.settings import Settings
from app.core.agents.agent_profile import AgentProfile
from app.utils.file_signature import directory_entries, stat_signature
from app.utils.path.system_cosir import system_agent_config_dir, system_main_agent_prompt_file
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

    def signature(self, scope: str) -> Hashable:
        """该作用域全部配置文件的签名：配置目录与其下的 JSON；系统作用域另加主 Agent 的提示词文件与步数。"""

        files = directory_entries(self.directory(scope), ".json")
        if scope != SYSTEM_SCOPE:
            return stat_signature(files)
        return (
            stat_signature([*files, system_main_agent_prompt_file()]),
            Settings.MAIN_AGENT_MAX_STEPS,
        )

    def load(self, scope: str) -> list[AgentProfile]:
        """读取作用域配置目录中的全部合法 profile。

        参数:
            scope: 系统作用域哨兵或归一化后的 workspace 作用域键。

        返回:
            按文件名排序读取、逐个通过 ``AgentProfile.vaild_agent_profile`` 的 profile；系统作用域
            的第一个是主 Agent（排在文件之前，同名文件因此顶替不了它）。目录不存在、目录级失败或
            全部文件无效时文件部分为空。

        异常:
            无：目录级问题在此降级为空列表，并写 ``agent_profile_scope_load_failed``
            warning 日志（含 scope、directory、异常类型与消息）；单个文件无效由
            ``AgentProfile.vaild_agent_profile`` 以 ``agent_profile_config_invalid``
            error 日志留痕后跳过。

        副作用:
            读取配置目录及文件（系统作用域另读主 Agent 提示词）；不修改文件系统、不写入任何缓存。
        """

        profiles: list[AgentProfile] = [_main_agent()] if scope == SYSTEM_SCOPE else []
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
            return profiles

        for path in paths:
            profile = AgentProfile.vaild_agent_profile(path)
            if profile is None:
                continue
            profiles.append(profile)
        return profiles


def _main_agent() -> AgentProfile:
    """由代码维护结构、由用户文件提供预设正文的主 Agent；正文读取失败时为空（不注入 ``<agent_layer>``）。"""

    # 延迟导入：配置 service 在模块级回引 ``app.config.configuration``，顶层导入会成环。
    from app.core.agents.define_agents import main_agent
    from app.service.configuration.instructions.main_agent_prompt_service import (
        MainAgentPromptConfigurationService,
    )

    # 只读：文件缺失按「未配置」处理。空白文件由 lifespan 启动时建好；这里若顺手建文件，签名会在读完后
    # 立刻变化，下一次读取白白重读一遍。
    prompt = (
        MainAgentPromptConfigurationService().read().content
        if system_main_agent_prompt_file().exists()
        else ""
    )
    return main_agent(system_prompt=prompt, max_steps=Settings.MAIN_AGENT_MAX_STEPS)
