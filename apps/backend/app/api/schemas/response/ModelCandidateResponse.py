from pydantic import BaseModel


class ModelCandidateResponse(BaseModel):
    """序列化 discover 候选模型响应（litellm 目录过滤产物，设计文档 §7.1）。

    参数:
        model_name: litellm 路由名（带 provider 前缀）。
        display_name: 去前缀后的展示名。
        max_context_window: litellm 已知的上下文窗口（token，预填可改）。
        supports_thinking: litellm 目录标注的推理模型标识。
        already_imported: 该厂商下是否已存在同名条目（前端置灰依据）。

    返回:
        Pydantic 响应模型。

    异常:
        无。

    副作用:
        无。
    """

    model_name: str
    display_name: str
    max_context_window: int
    supports_thinking: bool = False
    already_imported: bool = False

    @classmethod
    def from_candidate(cls, candidate) -> "ModelCandidateResponse":
        """从 ``ModelCandidate`` 服务值对象构造响应模型（鸭子类型，避免
        api 层依赖 service 内部类型注解）。

        参数:
            candidate: ``ProviderDiscoverService.ModelCandidate`` 或同字段
                形态对象。

        返回:
            与候选字段对齐的 ``ModelCandidateResponse`` 实例。

        异常:
            无。

        副作用:
            无。
        """

        return cls(
            model_name=candidate.model_id,
            display_name=candidate.display_name,
            max_context_window=candidate.max_context_window,
            supports_thinking=candidate.supports_thinking,
            already_imported=candidate.already_imported,
        )
