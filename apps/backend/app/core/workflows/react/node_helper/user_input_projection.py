"""human-in-the-loop 的纯投影函数（观察 ↔ 待决请求 ↔ 用户决定）。

本模块是「工具要求用户先作出决定才能继续」这一横切关注点的**唯一纯函数收口**：从工具观察派生
待决请求、把外部提交的恢复值解析为领域决定、把决定写回观察。它不接触 ``RuntimeConfig``、不读写
DB、不发事件、不持有跨调用状态，因此可脱离 graph 单独测试。

设计取舍（不明显但重要）：

- **待决请求不持久化，每次从观察派生**：请求的唯一载体是观察上的 ``user_input_request`` 声明
  （``ToolObservation.user_input_request`` 在 state 里的投影），「还有没有待决请求」就等于
  「观察上还有没有这个声明」。这样不存在「请求列表」这份可与观察分叉的第二事实，也就不需要任何
  合并 / 叠加逻辑。
- **决定一旦被消费就清掉观察上的声明**：无论批准还是驳回。否则下一次进入 ``wait_user`` 会重新把
  它派生为待决，用户已经答过的问题会被再问一遍；批准那条尤其容易踩，因为它的真实结果要等重执行
  产出后才替换掉旧观察。
- **驳回/放弃的观察状态取 ``cancelled`` 而非 ``error``**：用户驳回不是工具失败。若按
  ``error`` 且 ``retryable=False`` 结算，会累加 ``tool_error_count``，使用户连续驳回几次后
  触发 ``TOOL_ERROR_LIMIT`` 把 Run 判为失败。取消语义既不累加计数，也与「该动作没有执行」
  的事实一致；用户意见与后续指示放进 ``reason``（模型消息对 cancelled 只取 reason）。
- **未声明与未匹配一律报错，不静默忽略**：外部提交的决定若指向本批未声明的请求，或使用了
  该请求未声明的决定种类，说明前端/调用方与工具契约已经分叉，必须上抛让整轮失败可排查；
  静默丢弃会造成「用户以为批了、图却继续挂起」这类无日志的假成功。校验在
  ``wait_user`` 节点内进行（它同时需要请求对象来判断接受哪些决定）。
"""

from __future__ import annotations

import json
from typing import Any

from app.core.tools.schemas import (
    EXECUTING_DECISION_KINDS,
    USER_INPUT_REQUEST_KEY,
    UserDecision,
    UserDecisionKind,
    UserInputRequest,
)

# 驳回/放弃时写进观察的稳定短文案：cancelled 终态的模型消息只消费 ``reason``，
# 因此「不要再执行」的指示必须放在 reason 里，而不是 content。
_REJECT_REASON_PREFIX = (
    "the user rejected this tool call and did not approve execution; "
    "revise the proposal with the user input below before asking again."
)
_ABORT_REASON_PREFIX = "the user aborted this tool call; do not retry it."


def extract_request(observations: list[dict[str, Any]]) -> UserInputRequest | None:
    """从本批工具观察派生唯一的「要求用户先作出决定」请求。

    判据只有一个：观察上带 ``user_input_request`` 声明。已批准调用的声明在决定被消费时就清掉了，
    重执行产出的新观察也不带声明，因此本函数天然只返回**尚未作答**的请求。

    参数:
        observations: ``tools`` 节点对本批 ``ToolObservation`` 的 ``dataclasses.asdict``
            投影列表；只读，不修改入参。

    返回:
        唯一待决请求；没有声明时返回 ``None``。

    异常:
        ValueError: 声明的形状不合法（缺 ``request_id`` / ``kind``、``decisions`` 非法），或
            本批观察带有多个待决请求，违反单 HIL 调用契约。

    副作用:
        无（纯函数）。
    """

    found: UserInputRequest | None = None
    for observation in observations:
        request = _request_of(observation)
        if request is None:
            continue
        if found is not None:
            raise ValueError("同一批工具结果只能包含一个待决请求")
        found = request
    return found


def request_call_id(observations: list[dict[str, Any]], request_id: str) -> str:
    """查找待决请求所属的 ``tool_call_id``。

    观察是「请求」与「调用」的接合点：请求本身不带 ``tool_call_id``（它是图内部标识，不进对外
    契约），因此「批准后要重开哪条调用」必须由观察给出。

    参数:
        observations: 本批观察摘要，与 :func:`extract_request` 同一份。
        request_id: 需要关联到工具调用的待决请求标识。

    返回:
        声明该请求的观察所关联的 ``tool_call_id``。

    异常:
        ValueError: 声明了请求的观察缺少 ``tool_call_id``——没有它就无法把批准关联回调用，
            必须显式失败，而不是让「用户批了却什么都没执行」静默发生。
        KeyError: 本批观察没有声明该 ``request_id``。

    副作用:
        无（纯函数）。
    """

    for observation in observations:
        request = _request_of(observation)
        if request is None or request.request_id != request_id:
            continue
        call_id = observation.get("tool_call_id")
        if not isinstance(call_id, str) or not call_id:
            raise ValueError("待决请求缺少 tool_call_id，无法关联调用与结果")
        return call_id
    raise KeyError(request_id)


def parse_resume_decision(resume_value: object) -> UserDecision | None:
    """把外部恢复值解析为单个领域决定。

    这是进程外输入（HTTP → LangGraph resume）的**唯一解析点**：恢复值可能为空，因此在此做形状
    校验并上抛，不把宽容解析扩散到节点内部。

    参数:
        resume_value: ``interrupt`` 的返回值；缺省/无决定时为 ``None`` 或 ``{"decision": None}``。

    返回:
        已校验的领域决定；无决定时返回 ``None``（调用方据此重新挂起）。

    异常:
        ValueError: 恢复值不是预期结构，或决定缺少 ``request_id`` / 合法 ``decision``。

    副作用:
        无（纯函数）。
    """

    if resume_value is None:
        return None
    if not isinstance(resume_value, dict):
        raise ValueError("恢复值必须是包含 decision 的对象")
    raw = resume_value.get("decision")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("恢复值的 decision 必须是对象")
    request_id = raw.get("request_id")
    kind = raw.get("decision")
    if not isinstance(request_id, str) or not request_id:
        raise ValueError("决定必须携带非空 request_id")
    if not isinstance(kind, str):
        raise ValueError("决定必须携带 decision")
    try:
        resolved_kind = UserDecisionKind(kind)
    except ValueError as exc:
        raise ValueError(f"未知的决定种类：{kind}") from exc
    return UserDecision(
        request_id=request_id,
        kind=resolved_kind,
        data=_mapping(raw.get("data")),
    )


def apply_decision_to_observations(
    observations: list[dict[str, Any]], decision: UserDecision | None
) -> list[dict[str, Any]]:
    """把用户决定写回观察：清掉待决声明，并把「驳回 / 放弃」改写为取消终态。

    清声明的两个理由：它是「尚未作答」的唯一标记，留着会被下一次派生重新当成待决；而驳回 / 放弃
    的决定语义要折叠成模型可见的终态，不能再挂着待决声明。

    参数:
        observations: 本批观察摘要列表（第一遍工具执行结果）。
        decision: 本次收到的单个决定，按 ``request_id`` 关联到观察上的声明。调用方（``wait_user``）
            已经校验过「决定指向的请求存在且接受该决定种类」，因此这里只做写回。

    返回:
        改写后的观察列表；决定为空或没有命中观察时**原样返回入参**（同一对象），使调用方可以
        据此跳过无意义的 state 写入。

    异常:
        无。批准只清声明、不改写状态：那条调用的真实结果由重执行产出，并会按 ``tool_call_id``
        覆盖旧观察。

    副作用:
        无；改写副本，不修改入参中的观察。
    """

    if decision is None:
        return observations
    amended: list[dict[str, Any]] = []
    changed = False
    for observation in observations:
        request = _request_of(observation)
        if request is None or request.request_id != decision.request_id:
            amended.append(observation)
            continue
        changed = True
        updated = dict(observation)
        updated[USER_INPUT_REQUEST_KEY] = None
        if decision.kind not in EXECUTING_DECISION_KINDS:
            _rewrite_as_cancelled(updated, decision)
        amended.append(updated)
    return amended if changed else observations


def _rewrite_as_cancelled(updated: dict[str, Any], decision: UserDecision) -> None:
    """把一条观察就地改写为「用户未批准」的取消终态（入参是待写回的副本）。

    参数:
        updated: 已经复制好的观察（调用方不再使用原对象）。
        decision: 用户作出的驳回 / 放弃决定；其 ``data`` 是用户意见与后续指示。

    返回:
        无。

    异常:
        无。

    副作用:
        就地修改 ``updated``：``status`` / ``reason`` / ``content`` / ``error`` / ``retryable``
        与 ``display_data`` 全部改为取消语义，保留原展示键之外只追加用户决定。
    """

    user_input = json.dumps(decision.data, ensure_ascii=False) if decision.data else ""
    prefix = (
        _REJECT_REASON_PREFIX
        if decision.kind is UserDecisionKind.REJECT
        else _ABORT_REASON_PREFIX
    )
    updated["status"] = "cancelled"
    updated["reason"] = f"{prefix}\nuser input (JSON): {user_input}" if user_input else prefix
    # cancelled 观察由 settle 映射为 cancelled 终态事件；不能保留 success 的 content，
    # 否则模型消息会与实际终态分叉。
    updated["content"] = ""
    updated["error"] = ""
    updated["retryable"] = False
    updated["display_data"] = _rejected_display_data(updated, decision)


def _rejected_display_data(
    observation: dict[str, Any], decision: UserDecision
) -> dict[str, Any]:
    """构造被驳回/放弃调用的展示数据：保留原展示键并标注用户决定。"""

    raw = observation.get("display_data")
    display_data = dict(raw) if isinstance(raw, dict) else {}
    display_data["status"] = (
        "rejected" if decision.kind is UserDecisionKind.REJECT else "aborted"
    )
    display_data["user_decision"] = decision.kind.value
    if decision.data:
        display_data["user_input"] = dict(decision.data)
    return display_data


def _request_of(observation: object) -> UserInputRequest | None:
    """还原一条观察上声明的待决请求；没有声明时返回 ``None``。

    本模块内「观察 → 请求」的唯一解析点：形状校验收口在
    :meth:`UserInputRequest.from_dict`，三个使用方（派生请求、建立关联、写回决定）共用它，
    避免各处各自按字段名解析而漂移。

    参数:
        observation: 单条观察摘要（可能是任意类型的值，非映射按无声明处理）。

    返回:
        校验通过的请求，或 ``None``（该观察没有声明待决请求）。

    异常:
        ValueError: 声明存在但形状不合法（见 :meth:`UserInputRequest.from_dict`）。

    副作用:
        无（纯函数）。
    """

    if not isinstance(observation, dict):
        return None
    raw = observation.get(USER_INPUT_REQUEST_KEY)
    if raw is None:
        return None
    return UserInputRequest.from_dict(raw)


def _mapping(raw: object) -> dict[str, Any]:
    """把可选的映射字段归一为普通 dict；非映射取空 dict。"""

    return dict(raw) if isinstance(raw, dict) else {}
