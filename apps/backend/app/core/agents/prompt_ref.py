"""Prompt 引用值对象（纯数据类，第一部分仅作为接缝，不消费）。

本模块属于 core 层，零外部依赖：不 import Langfuse 或任何 prompt-infra。
它是「第一部分：子 Agent 改造」与「第二部分：接入 prompt 管理」之间的接缝——
第一部分只定义该数据结构，第二部分才引入 PromptResolver / LocalPromptProvider
去读取 ``fallback_path`` 并接入 Langfuse。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PromptRef:
    """描述一个 Agent profile 所关联的 prompt 引用。

    纯数据类：仅承载 prompt 的寻址与变量元信息，不负责加载或渲染。
    第一部分的 AgentProfile 仅存储该引用（``prompt_ref: PromptRef | None``），
    SystemPromptBuilder 在现阶段仍直接消费 ``description``/``capabilities``/``constraints``，
    不读取本引用。

    参数:
        name: prompt 的稳定标识（必填，非空）。第二部分用作 PromptRegistry 的 key。
        label: 人类可读的 prompt 名称，用于前端展示或调试。
        fallback_path: 本地兜底 prompt 文件路径（绝对或相对 workspace）；第二部分消费。
        variables_schema: 该 prompt 期望的变量名到类型说明的映射，供 UI/校验使用。

    返回:
        不可变的 prompt 引用值对象。

    异常:
        ValueError: ``name`` 为空或仅含空白时抛出。

    副作用:
        无。
    """

    name: str
    label: str
    fallback_path: str | None = None
    variables_schema: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """校验必填字段。

        参数:
            无。

        返回:
            无。

        异常:
            ValueError: ``name`` 为空或仅含空白。

        副作用:
            无。
        """

        if not self.name or not self.name.strip():
            raise ValueError("PromptRef.name must be a non-empty string")
