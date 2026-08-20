"""非法工具调用（``invalid_tool_calls``）的纯决策与修复消息构造。

单一职责：对 LangChain 返回的 ``invalid_tool_calls`` 给出「修复 / 忽略」结论（纯决策），
并构造要求模型重试的结构化修复提示。本模块只做**纯函数**转换，不写日志、不发事件、
不读写上下文，供 ``model_node`` 在非法工具调用双轨消费（REPAIR / IGNORE）时调用。

与兄弟模块的分工：
- ``InvalidToolOutcome`` 枚举表达处置结论；三个模块级纯函数承载判定 / 决策 / 消息构造。
- 不负责：把修复消息写进 ``RuntimeContextManager``（由 ``model_node`` 按情形 a/b 决定落库/延后）、
  ``invalid_tool_calls`` 的解析（归 ``chunk_assembler``）。
"""

import re
from enum import Enum
from typing import Any

from app.utils.trace_infra.redaction import redact_terminal_output

# 非法工具调用参数预览截断长度
INVALID_TOOL_ARGS_PREVIEW_CHARS = 500

# 非法工具调用摘要数量上限
INVALID_TOOL_CALL_SUMMARY_LIMIT = 5

# 修复提示整体字符预算上限（超出整体截断并加末尾说明）
INVALID_TOOL_CALL_TOTAL_BUDGET_CHARS = 2000


class InvalidToolOutcome(str, Enum):
    """``invalid_tool_calls`` 的处置结论，由决策函数返回，调用方只做薄执行。

    - ``REPAIR``：非法调用命中已注册工具名（视为真实调用意图，仅字段非法），需把
      修复提示喂回模型重试（情形 a 随 pending_tool_calls 延后到 tools 节点写上下文；
      情形 b 无合法工具时由 model_node 直接回写 RuntimeContextManager 并回流 model 节点）。
    - ``IGNORE``：非法调用未命中任何已注册工具名，视为解析噪声，仅记 warning，
      不修复、不阻塞。
    """

    REPAIR = "repair"
    IGNORE = "ignore"


def invalid_tool_call_mention_tool_name(
    invalid_tool_call: dict[str, Any],
    available_tool_names: set[str],
) -> str | None:
    """从非法工具调用片段中提取命中可用工具名的工具名。

    判定优先级（避免脆弱的子串 ``in`` 匹配误中无关文本，如用户 prompt 中
    "请读取并搜索文件" 里的 ``search`` / ``read`` / ``file``）：

    1. **精确 name 优先**：若 ``invalid_tool_call.get("name")`` 精确等于某个
       ``available_tool_name``，直接返回该 name（最可靠）。
    2. **词边界正则兜底**：否则对每个可用工具名，用 ``\\b<re.escape(name)>\\b``
       在 ``str(invalid_tool_call)`` 上搜索；命中词边界（即作为独立 token 出现，
       而非其他单词的子串）即返回该 name。
    3. 两者都不命中返回 ``None``。

    参数:
        invalid_tool_call: LangChain 返回的 invalid_tool_call（未必是合法 JSON，
            故全程以 ``dict`` + ``str`` 形式兼容处理）。
        available_tool_names: 当前模型可见的工具名集合，通常来自
            ``RuntimeOperations.model_tools``。

    返回:
        命中的可用工具名（``str``）；完全未命中任何工具名时返回 ``None``。

    异常:
        无。

    副作用:
        无。
    """
    if not invalid_tool_call or not available_tool_names:
        return None

    if isinstance(invalid_tool_call, dict):
        # 1. 精确 name 优先：最可靠，直接等于某已注册工具名。
        exact_name = invalid_tool_call.get("name")
        if isinstance(exact_name, str) and exact_name in available_tool_names:
            return exact_name

    # 2. 词边界正则兜底：作为独立 token 出现才算命中，避免子串误中。
    invalid_tool_call_str = str(invalid_tool_call)
    for tool_name in available_tool_names:
        if re.search(rf"\b{re.escape(tool_name)}\b", invalid_tool_call_str):
            return tool_name
    return None


def build_invalid_tool_call_repair_message(
    repair_datas: list[dict[str, Any]],
) -> str:
    """构造要求模型修复非法工具调用的结构化英文提示文本。

    面向模型、纯英文。返回单条 ``str``（不是消息对象），由调用方自行包装为
    ``SystemMessage`` 写进 ``RuntimeContextManager``。结构：

    - 顶部一句总领：说明上次非法工具调用未执行、请重试、只发严格合法 tool_calls。
    - 每个 repair 条目（受 ``INVALID_TOOL_CALL_SUMMARY_LIMIT`` 限条）输出：
      ``## <tool_name>`` + ``name`` / ``args`` 预览（经 ``redact_terminal_output``
      脱敏后截断到 ``INVALID_TOOL_ARGS_PREVIEW_CHARS``、超出加 ``...[truncated]``）/
      ``error``（若有）。``args`` 预览在脱敏后再截断，确保 secret 不进上下文。
    - 整体字符预算受 ``INVALID_TOOL_CALL_TOTAL_BUDGET_CHARS`` 约束：逐条拼接，一旦
      累计超预算即停止追加并附末尾截断说明，保证不超过预算且每条仍含可定位的
      ``tool_name`` 与 ``error`` 关键字段。

    参数:
        repair_datas: 待修复的非法调用明细列表，每项形如
            ``{"tool_name": <命中工具名>,
            "invalid_tool_call": <LangChain invalid_tool_call>}``。

    返回:
        结构化英文提示 ``str``，可直接包装为 ``SystemMessage`` 注入模型上下文。

    异常:
        无（对所有字段做 ``get`` / ``str`` 容错，解析失败的非 JSON 片段也能安全处理）。

    副作用:
        无（只读入参；脱敏与截断均为纯函数式处理，不改外部状态）。
    """
    header = (
        "The previous assistant message contained invalid tool call output that "
        "could not be parsed; the tool calls were NOT executed. Retry this step. "
        "If you still need the tool(s), emit valid tool_calls only with strict JSON "
        "arguments matching the schema. Do not claim a tool or child agent started "
        "unless the call is valid and executed."
    )

    sections: list[str] = []
    total_chars = len(header)
    budget = INVALID_TOOL_CALL_TOTAL_BUDGET_CHARS
    truncated = False

    for repair_data in repair_datas[:INVALID_TOOL_CALL_SUMMARY_LIMIT]:
        tool_name = repair_data.get("tool_name", "")
        invalid_tc = repair_data.get("invalid_tool_call", {})
        if not isinstance(invalid_tc, dict):
            invalid_tc = {}

        # args 预览：先脱敏再截断，防止 secret 进上下文。
        raw_args = str(invalid_tc.get("args", ""))
        redacted_args = redact_terminal_output(raw_args)
        if len(redacted_args) > INVALID_TOOL_ARGS_PREVIEW_CHARS:
            redacted_args = (
                redacted_args[:INVALID_TOOL_ARGS_PREVIEW_CHARS] + "...[truncated]"
            )

        error = invalid_tc.get("error")
        error_line = f"error: {error}\n" if error else ""

        section = (
            f"## {tool_name}\n"
            f"name: {tool_name}\n"
            f"args: {redacted_args}\n"
            f"{error_line}"
        )

        # 整体预算约束：加上本段与段间换行后若超预算则停止并加末尾说明。
        if total_chars + len(section) + 1 > budget:
            truncated = True
            break
        sections.append(section)
        total_chars += len(section) + 1

    body = "\n".join(sections)
    if truncated:
        truncation_note = (
            "\n[truncated] Further invalid tool calls omitted due to length budget; "
            "fix the listed calls first and retry."
        )
        # 整条丢弃而非字符切片：保证每个已输出条目字段完整（计划 §6.2）。
        # 从后往前逐个丢弃 section，直至 header + 分隔符 + body + 说明 整体 ≤ 预算。
        while sections:
            candidate = "\n".join(sections) + truncation_note
            if len(header) + 2 + len(candidate) <= budget:
                break
            sections.pop()
        body = "\n".join(sections) + truncation_note

    return f"{header}\n\n{body}".rstrip()


def decide_invalid_tool_handling(
    *,
    invalid_tool_calls: list[dict[str, Any]],
    available_tool_names: set[str],
) -> dict[InvalidToolOutcome, Any]:
    """纯决策：对一批 ``invalid_tool_calls`` 给出 ``REPAIR`` / ``IGNORE`` 双列表结论。

    不写日志、不发事件、不构造消息，只做「该修还是该忽略」的判断，使调用方的
    执行分支扁平、可单测。修复不设次数预算（命中工具名即视为真实调用意图、
    总是修复）；无限修复由 graph 的 ``max_steps`` 安全网兜底。语义
    （见 :class:`InvalidToolOutcome`）：

    - 无非法调用 → ``REPAIR`` / ``IGNORE`` 双列表均为空；
    - 非法调用命中已注册工具名（经 ``invalid_tool_call_mention_tool_name``
      精确优先 + 词边界兜底判定，视为真实调用意图、仅字段非法）→ 归入
      ``REPAIR``（带 ``tool_name``）；
    - 非法调用未命中任何工具名 → 归入 ``IGNORE``（解析噪声，无法推断意图）。

    调用方直接以 ``result[InvalidToolOutcome.REPAIR]`` 是否非空判定是否存在
    可疑调用意图（情形 b 回流依据），无需额外参数。

    参数:
        invalid_tool_calls: LangChain 返回的 invalid_tool_calls 列表。
        available_tool_names: 当前模型可见的工具名集合。

    返回:
        形如 ``{InvalidToolOutcome.IGNORE: [...], InvalidToolOutcome.REPAIR: [...]}``
        的字典；``REPAIR`` 列表每项含 ``tool_name`` 与 ``invalid_tool_call``，
        ``IGNORE`` 列表直接放原始 invalid_tool_call。

    异常:
        无。

    副作用:
        无。
    """
    if not invalid_tool_calls:
        return {
            InvalidToolOutcome.IGNORE: [],
            InvalidToolOutcome.REPAIR: [],
        }
    result: dict[InvalidToolOutcome, list[dict[str, Any]]] = {
        InvalidToolOutcome.IGNORE: [],
        InvalidToolOutcome.REPAIR: [],
    }
    for invalid_tool_call in invalid_tool_calls:
        mentions_tool = invalid_tool_call_mention_tool_name(
            invalid_tool_call,
            available_tool_names,
        )
        if mentions_tool:
            result[InvalidToolOutcome.REPAIR].append({
                "tool_name": mentions_tool,
                "invalid_tool_call": invalid_tool_call,
            })
        else:
            result[InvalidToolOutcome.IGNORE].append(invalid_tool_call)
    return result
