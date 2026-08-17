"""单个 Agent 的模型覆盖配置值对象。

单一职责：只承载该 Agent 的模型覆盖配置（生成参数 + 可选 api_base 覆盖），不参与模型构建。
所有字段均为可选覆盖项：未提供（``None``）时，由全局运行配置（``app.config.settings``
模块级静态变量）与模型注册表提供缺省值，``ModelSettings`` 仅覆盖显式给出的字段。
"""

from dataclasses import dataclass
from typing import Any

_FIELDS = (
    "temperature",
    "top_p",
    "max_tokens",
    "thinking",
    "base_url",
    "api_key_env",
)


@dataclass(frozen=True)
class ModelSettings:
    """单个 Agent 的模型覆盖配置值对象。

    职责边界：
    - 负责：声明 Agent 级别的模型覆盖项（采样参数、thinking 模式、可选 api_base 覆盖
      与 Key 来源环境变量）。
    - 不负责：模型构建（交给 ``core.llm.factory.build_chat_model``）、全局默认值
      （交给 ``app.config.settings`` 模块级静态变量 / 模型注册表）、字段校验语义
      （仅做「是否提供」的覆盖判断）。

    说明:
        上下文窗口上限不在此处（亦无 Agent 级覆盖需求）：模型最大窗口归 ``ModelCatalog``，
        全局软上限归 ``Settings.CONTEXT_WINDOW_TOKENS``，两者 min 即实际上限
        （见 ``context_window_resolver.resolve_context_window``）。不为假想需求预留覆盖字段。
    """

    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    thinking: bool | None = None
    # 可选 api_base 覆盖：None 时不传，由 litellm 按模型前缀内置解析端点。
    base_url: str | None = None
    api_key_env: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 可序列化字典（仅含非 ``None`` 字段）。

        参数:
            无。

        返回:
            只包含显式覆盖字段的字典。

        异常:
            无。

        副作用:
            无。
        """

        return {k: getattr(self, k) for k in _FIELDS if getattr(self, k) is not None}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ModelSettings":
        """从字典重建，忽略未知键（前向兼容）。

        参数:
            data: 原始字典（可能含未来版本新增字段）。

        返回:
            重建的 ``ModelSettings`` 实例。

        异常:
            无。

        副作用:
            无。
        """

        return cls(**{k: data[k] for k in _FIELDS if k in data})
