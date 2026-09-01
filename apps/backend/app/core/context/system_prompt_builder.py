"""构建面向模型的系统提示词。"""

from datetime import date
from pathlib import Path
from platform import system

from app.config.settings import Settings
from app.core.agents.agent_profile import AgentProfile
from app.utils.file_utils import read_text_file


def _default_coding_rule_dir() -> str:
    """返回默认编码规则的绝对路径。"""
    return str(Path(__file__).resolve().parent / "rules" / "default-coding-rules.md")


class SystemPromptBuilder:
    """按固定分层构建本地 coding-agent 的系统提示词。

    本类为无状态工具类，所有构建逻辑均为静态方法，不持有实例状态。
    """

    @staticmethod
    def build(agent_profile: AgentProfile, workspace_root: str) -> str:
        """构建完整系统提示词文本。

        参数:
            agent_profile: 当前执行主体的 Agent 档案。
            workspace_root: 当前工作区根目录。

        返回:
            由固定 section 顺序拼接出的系统提示词；回复语言取自 ``Settings.DEFAULT_LANGUAGE``。

        异常:
            无。

        副作用:
            无。
        """
        language = Settings.DEFAULT_LANGUAGE
        coding_rule_dir = _default_coding_rule_dir()

        sections = [
            SystemPromptBuilder._agent_identity(agent_profile, workspace_root, language),
            SystemPromptBuilder._engineering_principles(coding_rule_dir),
            SystemPromptBuilder._workflow_contract(),
            SystemPromptBuilder._tool_use_policy(agent_profile),
        ]
        return "\n\n".join(sections)

    @staticmethod
    def _agent_identity(
        agent_profile: AgentProfile,
        workspace_root: str,
        language: str,
    ) -> str:
        """构建 Agent 身份 section。

        参数:
            agent_profile: 当前执行主体的 Agent 档案。
            workspace_root: 当前工作区根目录。
            language: 面向用户的语言。

        返回:
            描述身份、职责、能力与约束的 section 文本。capabilities 与 constraints
            仅在非空时追加对应行，避免空列表产生噪音行。

        异常:
            无。

        副作用:
            无。
        """

        identity_lines = [
            "<agent_identity>",
            f"你是一个运行在用户本机的 {agent_profile.role}，{agent_profile.description}。",
            f"Agent ID: {agent_profile.agent_id}",
            f"Role: {agent_profile.role}",
            f"当前所处于的操作系统: {system()}",
            (
                f"当前工作区根目录: {workspace_root},你所有的代码都在这个目录下，"
                f"且写、编辑、删除操作将被系统限制在这个目录下，"
                f"超出这个目录范围的操作将被系统拒绝"
            ),
            f"今天日期: {date.today().isoformat()}",
            f"所拥有的工具集合: {', '.join(agent_profile.allowed_tools) or 'none'}",
        ]
        identity_lines.extend(
            [
                (
                    f"你所面向的用户所使用的语言: {language},"
                    f"请使用友好的语言和用户交流。除非用户要求，不要使用emjio表情回复。"
                ),
                "</agent_identity>",
            ]
        )
        return "\n".join(identity_lines)

    @staticmethod
    def _engineering_principles(coding_rule_dir: str) -> str:
        """构建整洁代码开发原则 section。

        从 ``coding_rule_dir`` 指向的文件读取内容；文件不存在时返回兜底占位文本。

        参数:
            coding_rule_dir: 编码规则 Markdown 文件的绝对路径。

        返回:
            描述代码质量原则的 section 文本。

        异常:
            无（所有异常均在内部兜底处理）。

        副作用:
            无。
        """

        return "\n".join(
            [
                "<engineering_principles>",
                read_text_file(coding_rule_dir),
                "</engineering_principles>",
            ]
        )

    @staticmethod
    def _workflow_contract() -> str:
        """构建工作流契约 section。

        参数:
            无。

        返回:
            描述执行步骤约束的 section 文本。

        异常:
            无。

        副作用:
            无。
        """

        return "\n".join(
            [
                "<workflow_contract>",
                "- 先明确任务目标和影响范围，再定位相关代码和测试。",
                "- 需要理解代码时，优先搜索和阅读已有实现，再决定修改方案。",
                "- 修改应保持局部、可回滚、可解释，并避免牵连无关文件。",
                "- 完成代码改动后，尽可能运行相关测试、lint 或类型检查形成闭环。",
                "- 无法验证时必须明确说明原因、已完成内容和剩余风险。",
                "</workflow_contract>",
            ]
        )

    @staticmethod
    def _tool_use_policy(agent_profile: AgentProfile) -> str:
        """构建工具使用策略 section。

        参数:
            agent_profile: 当前执行主体的 Agent 档案。

        返回:
            描述允许工具和调用原则的 section 文本。

        异常:
            无。

        副作用:
            无。
        """

        allowed_tools = ", ".join(agent_profile.allowed_tools) or "none"
        return "\n".join(
            [
                "<tool_use_policy>",
                f"Allowed tools: {allowed_tools}",
                "- 工具 schema 是参数结构的唯一事实来源；调用工具时必须遵守 schema。",
                "- 文件读写、搜索、补丁和终端执行应服务于当前任务目标，不做无关探索。",
                "- 代码探索、搜索代码，codegraph工具优先级高于搜索工具",
                "- 写入、删除、补丁和命令执行属于高影响操作，必须基于已确认路径和明确目的。",
                "- 工具失败时先诊断原因并调整路线，不要重复提交同一类无效调用。",
                "</tool_use_policy>",
            ]
        )
