"""工具「请求用户作出决定」的领域契约（human-in-the-loop 的请求侧）。

本模块承载请求的语义与形状：谁被问（``request_id`` / ``kind``）、问什么（``prompt``）、允许哪些
决定（``decisions``）、以及供用户编辑的草稿（``draft_schema`` / ``draft``）。它**不**承载「用户
答了什么」（那是 :class:`UserDecision`），也不承载前端渲染细节。

它是**工作流控制流事实**而不是展示数据：``wait_user`` 节点据此决定是否挂起图、问用户什么；
因此它挂在 :class:`ToolObservation` 上，而不是混进 ``display_data``（后者只服务前端渲染）。
投影给前端的载荷由 ``project_user_input_request`` 组装。

为什么用 ``@dataclass`` 而不是 pydantic：本类型会作为 ``ToolObservation`` 的字段随观察经
``dataclasses.asdict`` 投影进 workflow state（LangGraph checkpoint 只接受可 JSON 序列化的值），
而 ``asdict`` 会递归 dataclass、却**不会**转换 pydantic 模型（只做深拷贝），放 pydantic 会让
checkpoint 落盘失败。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.tools.schemas.user_decision import UserDecisionKind

# 工具未在请求里声明允许集合时的默认值：「批准 / 驳回」是最小通用交互，工具想让用户可放弃时
# 显式加 ``abort``。
DEFAULT_DECISIONS: tuple[UserDecisionKind, ...] = (
    UserDecisionKind.APPROVE,
    UserDecisionKind.REJECT,
)

# 投影到前端 tool part 载荷时承载本请求的键（与 ``tool_ui_display_contract.md`` 一致）。
USER_INPUT_REQUEST_KEY = "user_input_request"


@dataclass(frozen=True)
class UserInputRequest:
    """一次「要求用户先作出决定才能继续」的请求。

    字段:
        request_id: 本 Run 内唯一、对前端稳定的请求标识；用户提交的决定按它匹配回请求。
            ``tool_call_id`` 不进请求——它由观察本身携带，不需要在请求里重复。
        kind: 提问工具自定义的请求种类（如 ``agent_team_review``）；框架不解释其含义，只透传前端。
        prompt: 受控短文案，向用户说明要决定什么。
        decisions: 本请求接受的决定子集；提交未声明的决定会被拒绝（不静默忽略）。
        draft_schema: 前端据此选择草稿渲染器；空串表示本请求没有可编辑草稿。
        draft: 供用户编辑的结构化草稿；形状由 ``draft_schema`` 定义，框架只透传。

    异常:
        无（构造不做校验；从外部形状还原时的校验见 :meth:`from_dict`）。
    """

    request_id: str
    kind: str
    prompt: str = ""
    decisions: tuple[UserDecisionKind, ...] = DEFAULT_DECISIONS
    draft_schema: str = ""
    draft: dict[str, Any] = field(default_factory=dict)

    def accepts(self, kind: UserDecisionKind) -> bool:
        """判断本请求是否接受某个决定。

        参数:
            kind: 待校验的决定种类。

        返回:
            工具声明了该决定时为 ``True``，否则为 ``False``（调用方应据此拒绝外部提交的决定，
            不得静默忽略）。

        异常:
            无。

        副作用:
            无（纯函数）。
        """

        return kind in self.decisions

    @classmethod
    def from_dict(cls, raw: object) -> UserInputRequest:
        """从 state 里的投影形状还原请求并校验。

        为什么需要它：观察经 ``dataclasses.asdict`` 进 checkpoint 后是普通 dict（元组字段保持元组、
        枚举元素保持枚举），工作流每次派生都要把它还原成类型化值。校验必须在这一处显式失败，
        不能让畸形声明静默退化成「本次没有待决请求」——那会表现为「用户根本没被问到」，
        且没有任何痕迹。

        参数:
            raw: 观察投影里的 ``user_input_request`` 值。

        返回:
            校验通过的请求。

        异常:
            ValueError: 声明不是对象、缺 ``request_id`` / ``kind``、``decisions`` 不是非空列表
                或含未声明的决定种类。

        副作用:
            无（纯函数）。
        """

        if not isinstance(raw, dict):
            raise ValueError("待决请求声明必须是对象")
        request_id = raw.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("待决请求必须携带非空 request_id")
        kind = raw.get("kind")
        if not isinstance(kind, str) or not kind:
            raise ValueError("待决请求必须携带非空 kind")
        return cls(
            request_id=request_id,
            kind=kind,
            prompt=_text(raw.get("prompt")),
            decisions=_decisions(raw.get("decisions")),
            draft_schema=_text(raw.get("draft_schema")),
            draft=_mapping(raw.get("draft")),
        )

    def to_request_payload(self) -> dict[str, Any]:
        """把请求投影为对外载荷（interrupt 载荷与前端 tool part 载荷共用同一种形状）。

        参数:
            self: 待投影的请求。

        返回:
            只含前端渲染与用户决定所需事实的普通 dict；**不含** ``tool_call_id`` 等图内部关联键，
            避免把图内部标识当成对外契约。

        异常:
            无。

        副作用:
            无（纯函数）。
        """

        return {
            "request_id": self.request_id,
            "request_kind": self.kind,
            "prompt": self.prompt,
            "decisions": [kind.value for kind in self.decisions],
            "draft_schema": self.draft_schema,
            "draft": dict(self.draft),
        }


def _decisions(raw: object) -> tuple[UserDecisionKind, ...]:
    """解析工具声明的允许决定集合；缺省用通用默认值，非法值直接报错。

    接受 ``list`` 与 ``tuple``：``dataclasses.asdict`` 投影出来的正是元组（元素仍是
    ``UserDecisionKind`` 这个 ``str`` 子类）。
    """

    if raw is None:
        return DEFAULT_DECISIONS
    if not isinstance(raw, list | tuple) or not raw:
        raise ValueError("待决请求声明的 decisions 必须是非空列表")
    resolved: list[UserDecisionKind] = []
    for item in raw:
        if not isinstance(item, str):
            raise ValueError("待决请求声明的 decisions 每一项都必须是字符串")
        try:
            resolved.append(UserDecisionKind(item))
        except ValueError as exc:
            raise ValueError(f"未知的决定种类声明：{item}") from exc
    return tuple(dict.fromkeys(resolved))


def _mapping(raw: object) -> dict[str, Any]:
    """把可选的映射字段归一为普通 dict；非映射取空 dict（草稿缺失是合法情况）。"""

    return dict(raw) if isinstance(raw, dict) else {}


def _text(raw: object) -> str:
    """把可选的文本字段归一为 str；非字符串取空串。"""

    return raw if isinstance(raw, str) else ""


__all__ = [
    "DEFAULT_DECISIONS",
    "USER_INPUT_REQUEST_KEY",
    "UserInputRequest",
]
