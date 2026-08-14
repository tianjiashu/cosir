"""ReAct-like 工作流的模型节点（``_model_node``）。

本模块只承载「模型节点」单一职责：流式消费模型输出并决定下一步动作。节点从运行上下文
取出 ``operations`` / ``task`` / ``turn`` / ``model``，经 ``get_stream_writer()`` 把业务
生命周期事件与流式 token 增量写入自定义事件流；用 ``model.astream()`` 累积 ``AIMessage``，
回复 token 与思考 token 在节点内就地翻译为 ``MODEL_OUTPUT_DELTA`` / ``MODEL_THINKING_DELTA``
事件。根据模型最终输出决定进入工具分支、最终回答分支，还是因无效输出 / 超过最大步数终止。
状态写入 **turn**。

关于「文本 + 工具调用并存」：ReAct 中模型「边说明边调工具」是合法输出（例如先说
"我先用 grep 查一下文件结构" 再给出一个 ``search_files`` 调用）。此时文本**不计入最终
回复**（最终回复只来自纯文本分支的 ``FINAL_RESPONSE``），但模型这段说明并非丢弃——
它会经 ``MODEL_OUTPUT_DELTA`` 流式推给前端、经 ``_ai_to_runtime_message`` 落库进历史上下文，
并在进入工具分支时作为 ``instruction`` 键随 ``pending_tool_calls`` 下传给 ``tools`` /
``observe`` 节点，使下游执行与错误排查能看到模型当时的意图。

与工具节点共享的运行时原语见 ``common``；不负责 graph 构建、运行编排或事件翻译。
"""

import json
from datetime import UTC, datetime
from time import perf_counter
from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk, SystemMessage

from app.config.logging.logger import log
from app.config.settings import Settings
from app.core.llm.langchain_bridge import tool_calls_from_langchain
from app.models import RuntimeMessage
from app.models.enums.event_type import EventType
from app.models.payload import (
    FinalResponsePayload,
    ModelOutputDeltaPayload,
    ModelRequestedPayload,
    ModelThinkingDeltaPayload,
    RunCancelledPayload,
    RunFinishedPayload,
    StepStartedPayload,
)
from app.tools.schemas import ToolCall

from ..react.state import ReactGraphState
from .common import (
    _runtime_config,
    _runtime_context,
    build_run_failed_payload,
    terminal_state,
    write_event,
)
from .model_tool_helper import InvalidToolOutcome, ModelToolHelper


def _extract_text(content) -> str:
    """从 LangChain 消息 content 中提取纯文本分片。

    参数:
        content: LangChain 消息的 ``content`` 字段（字符串或分块列表）。

    返回:
        拼接后的纯文本；无法识别时返回空字符串。
    """

    if isinstance(content, str):
        return content  # 普通字符串直接返回
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)  # 列表里直接是字符串
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))  # 多模态文本块 {"type":"text","text":...}
        return "".join(parts)  # 拼接所有文本片段
    return ""  # 其它类型（如图片）返回空


def _has_content(message: AIMessage) -> bool:
    """判断 LangChain 消息是否含有可落库的有效内容。

    参数:
        message: LangChain ``BaseMessage``（通常为 ``AIMessage``）。

    返回:
        消息含非空文本或至少一个工具调用时返回 True，否则返回 False。

    异常:
        无。

    副作用:
        无。
    """

    if _extract_text(message.content).strip():
        return True  # 有文本即视为有效
    tool_calls = getattr(message, "tool_calls", None)
    return bool(tool_calls)  # 有工具调用也视为有效


def _ai_to_runtime_message(ai_message: "AIMessage") -> RuntimeMessage:
    """把合并后的 ``AIMessage`` 转为内部 ``RuntimeMessage`` 以供增量落库。

    参数:
        ai_message: 模型节点合并产出、待写入 graph state 的 ``AIMessage``。

    返回:
        与模型无关的 ``RuntimeMessage``：``metadata`` 中以 **JSON 字符串** 承载
        ``tool_calls``（键 ``tool_calls``），与
        :meth:`app.core.context.runtime_context.RuntimeContext._tool_calls_from_metadata`
        的反序列化契约严格对齐（读取端 ``json.loads``，故此处必须存字符串而非 list）。
        无工具调用时不写入该键。

    异常:
        无。

    副作用:
        无。
    """

    tool_calls = [
        {"name": call.get("name"), "args": call.get("args", {}), "id": call.get("id")}
        for call in (ai_message.tool_calls or [])
    ]
    metadata: dict[str, Any] = (
        {"tool_calls": json.dumps(tool_calls, ensure_ascii=False)} if tool_calls else {}
    )
    return RuntimeMessage(
        role="assistant",
        content_text=_extract_text(ai_message.content),
        metadata=metadata,
    )


def _invalid_tool_calls_from_chunks(chunks: list[AIMessageChunk]) -> list[dict[str, Any]]:
    """从原始流式 chunk 中收集非法工具调用。

    参数:
        chunks: 模型流式产出的原始 chunk 列表。

    返回:
        去重后的 invalid_tool_calls 列表。

    异常:
        无。

    副作用:
        无。
    """

    collected: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for chunk in chunks:
        for call in getattr(chunk, "invalid_tool_calls", None) or []:
            call_id = str(call.get("id") or "")
            name = str(call.get("name") or "")
            key = (call_id, name)
            if key in seen:
                continue
            seen.add(key)
            collected.append(dict(call))
    return collected


def _extract_reasoning_content(chunk) -> str:
    """从 LangChain 消息 chunk 的 additional_kwargs 提取 DeepSeek 思考过程分片。

    ``DeepSeekChatOpenAI`` 已把流式分块中的 ``reasoning_content`` 写入
    ``additional_kwargs["reasoning_content"]``；本函数在不支持 thinking 的模型（该字段缺失）
    时安全返回空串。逐 token 流式场景下，每个 chunk 携带的只是思考片段，由调用方累加到客户端。

    参数:
        chunk: 模型 ``astream`` 产出的 LangChain 消息 chunk。

    返回:
        思考过程文本分片；无则空串。
    """

    additional = getattr(chunk, "additional_kwargs", None)  # 防止无该属性时报错
    if not isinstance(additional, dict):
        return ""  # 非 dict 直接返回空
    value = additional.get("reasoning_content")  # 取思考字段
    return value if isinstance(value, str) else ""  # 非字符串也返回空


def _dump_merged_chunk_debug(merged: AIMessageChunk) -> None:
    """把合并后的完整 chunk 结构追加写入调试文件，供本地排查完整字段。

    常规结构化日志通道（JSONL 文件 + SQLite 日志库）对所有 ``data`` 字符串施加
    ``MAX_LOG_TEXT_LENGTH`` 截断，无法承载完整的消息 JSON；本函数绕过该预算，
    把 ``merged.model_dump()`` 以单行 JSON 追加到 ``logs/debug_merged_chunks.jsonl``，
    使开发者能在不被截断的前提下查看 chunk 累计后的完整结构。

    参数:
        merged: 合并完成后的 ``AIMessageChunk``。

    返回:
        无。

    异常:
        无（写入失败仅记录 warning，不影响主流程）。

    副作用:
        向 ``Settings.LOG_DIR / debug_merged_chunks.jsonl`` 追加一行 JSON；当
        ``Settings.DEBUG_DUMP_CHUNKS`` 为 ``False`` 时直接返回，不写盘。
    """
    if not Settings.DEBUG_DUMP_CHUNKS:
        return
    try:
        debug_path = Settings.LOG_DIR / "debug_merged_chunks.jsonl"
        record = {
            "ts": _utc_now_iso(),
            "merged": merged.model_dump(),
        }
        with open(debug_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError as exc:
        log.warning(
            "_dump_merged_chunk_debug_failed",
            extra={
                "msg": "写入合并 chunk 调试文件失败",
                "data": {"error": str(exc)},
            },
        )


def _dump_raw_chunk_debug(chunk: AIMessageChunk, index: int) -> None:
    """把单次流式产出的原始 chunk 结构追加写入调试文件，供本地逐 chunk 排查。

    与 ``_dump_merged_chunk_debug``（合并后落盘）互补：本函数在 ``model.astream``
    循环内逐条调用，记录每个原始分块的完整结构，使开发者能看到流式过程中 chunk
    的形态演变（如 ``content`` 从空到累积、``tool_call_chunks`` 逐片到达、
    ``usage_metadata`` 仅末 chunk 携带等）。同样绕过常规日志预算截断。

    参数:
        chunk: 模型 ``astream`` 产出的单个原始消息分块。
        index: 该 chunk 在流式序列中的序号（从 0 开始），便于定位先后。

    返回:
        无。

    异常:
        无（写入失败仅记录 warning，不影响主流程）。

    副作用:
        向 ``Settings.LOG_DIR / debug_raw_chunks.jsonl`` 追加一行 JSON；当
        ``Settings.DEBUG_DUMP_CHUNKS`` 为 ``False`` 时直接返回，不写盘。
    """
    if not Settings.DEBUG_DUMP_CHUNKS:
        return
    try:
        debug_path = Settings.LOG_DIR / "debug_raw_chunks.jsonl"
        record = {
            "ts": _utc_now_iso(),
            "index": index,
            "chunk": chunk.model_dump(),
        }
        with open(debug_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError as exc:
        log.warning(
            "_dump_raw_chunk_debug_failed",
            extra={
                "msg": "写入原始 chunk 调试文件失败",
                "data": {"error": str(exc)},
            },
        )


def _utc_now_iso() -> str:
    """返回毫秒精度、``Z`` 后缀的 UTC 时间文本。"""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _collect_chunk_to_ai_message(chunks: list[AIMessageChunk]) -> AIMessage:
    """把累积的 ``AIMessageChunk`` 列表合并为标准的 ``AIMessage``。

    合并时会保留 ``additional_kwargs``（如 DeepSeek 的 ``reasoning_content`` 思考过程），
    否则思考内容会在落库 / 进入 graph state 时被丢弃，导致下游无法将其作为 thinking 事件推送。

    合并后的 ``merged``（``AIMessageChunk``）完整结构经实测（见 ``logs/debug_merged_chunks.jsonl``）
    形如以下字段，下游消费与调试时应按此契约解读::

        {
          "content": str,                 # 正文；stop 分支非空，tool_calls 分支可能为空串
          "additional_kwargs": {          # provider 私有扩展字段
            "reasoning_content": str      # DeepSeek 思考链；合并后保留，落库前剥离
          },
          "response_metadata": {          # 本轮元信息；累加失真细节见下方「注意」段
            "model_provider": "openai",
            "finish_reason": "stop" | "tool_calls",
            "model_name": "deepseek-v4-flash",
            "system_fingerprint": str
          },
          "type": "AIMessageChunk",
          "name": None,
          "id": "lc_run--<uuid>",         # 消息运行 ID
          "tool_calls": [                 # 结构化工具调用；非空表示要调用工具
            {"name": str, "args": dict, "id": "call_<n>_<hash>", "type": "tool_call"}
          ],
          "invalid_tool_calls": [],       # 解析失败/非法的工具调用（通常为空）
          "usage_metadata": {             # token 用量
            "input_tokens": int,
            "output_tokens": int,
            "total_tokens": int,
            "input_token_details": {"cache_read": int},   # 命中缓存的 input token 数
            "output_token_details": {"reasoning": int}    # 推理 token 数
          },
          "tool_call_chunks": [           # 流式累积的工具调用分片
            {"name": str, "args": str(json), "id": str, "index": int, "type": "tool_call_chunk"}
          ],
          "chunk_position": "last"        # 标记这是合并后的最终块
        }

    两种典型分支（由 ``finish_reason`` 区分）：
    - ``stop`` 分支：``content`` 为完整正文，``tool_calls`` / ``tool_call_chunks`` 均为空列表。
    - ``tool_calls`` 分支：``content`` 可能为空串，``tool_calls`` 含一个或多个待执行工具调用，
      ``tool_call_chunks`` 为对应的流式分片（``args`` 为 JSON 字符串、``index`` 为并行调用序号）。

    注意（实测与源码一致）：
    ``AIMessageChunk`` 累加时 ``response_metadata`` 走 ``merge_dicts``；其中字符串类型的 key
    （不在白名单内）会被 LangChain 字符串拼接。白名单为：
    ``index`` / ``id`` / ``output_version`` / ``model_provider``。
    如 ``finish_reason`` 多 chunk 累加可能拼成 ``"stopstop"``。但流式下 ``finish_reason`` 通常只在
    最后一个 chunk 出现（前序为空 dict），``merge_dicts`` 取「首个非空值优先」，故合并结果即末 chunk
    值、并不失真；稳妥可取 ``chunks[-1].response_metadata``。本函数不依赖它做分支判断，无影响。

    参数:
        chunks: 模型流式产出的分块列表（可能为空）。

    返回:
        可安全存入 graph state 并交给下一步模型调用的 ``AIMessage``：
        - ``content`` 经 ``_extract_text`` 抽为纯文本（防御含 ``tool_call`` block 的 list 形态）；
        - ``tool_calls`` / ``usage_metadata`` 透传（usage 由 ``add_usage`` 正确累加后的完整统计）；
        - ``additional_kwargs`` 已剥离 ``reasoning_content``（思考内容已在流式阶段单独推送）；
        - ``id`` 透传 merged 的消息运行 ID（``lc_run--<uuid>``），供日志与 trace
          关联；缺失时为 None。

    异常:
        无（chunk 合并与调试落盘均不向外抛出；落盘失败已在
        ``_dump_merged_chunk_debug`` 内降级为 warning）。

    副作用:
        经 ``_dump_merged_chunk_debug`` 向 ``Settings.LOG_DIR / debug_merged_chunks.jsonl``
        追加一行完整 chunk JSON（调试通道，不受常规日志预算截断）。
    """

    merged: AIMessageChunk | None = None
    for chunk in chunks:
        merged = chunk if merged is None else merged + chunk  # LangChain chunk 支持 + 累加
    if merged is None:
        return AIMessage(content="")  # 空输入返回空消息

    # 完整结构落调试文件（不受日志预算截断），先于常规摘要日志执行。
    _dump_merged_chunk_debug(merged)

    # 剥离思考字段：思考内容已在流式阶段作为 MODEL_THINKING_DELTA 推送给前端，
    # 不应随消息回灌给模型（推理模型回灌 reasoning_content 易引发重复思考或协议错误）。
    additional = dict(merged.additional_kwargs) if merged.additional_kwargs else {}
    additional.pop("reasoning_content", None)
    # content 统一抽纯文本：防御 DeepSeek 偶发把工具调用 block 带进 content list 的形态，
    # 与 _ai_to_runtime_message 落库口径保持一致，避免回灌模型时重复携带工具结构。
    return AIMessage(
        content=_extract_text(merged.content),  # 合并后的纯文本（已防御 list 形态）
        tool_calls=merged.tool_calls or [],  # 工具调用（可能为空）
        invalid_tool_calls=merged.invalid_tool_calls or _invalid_tool_calls_from_chunks(chunks),
        additional_kwargs=additional,  # 仅保留非思考的额外字段
        usage_metadata=merged.usage_metadata,  # 透传完整 token 统计
        id=getattr(merged, "id", None),  # 消息 id 透传
    )


def _extract_usage_from_chunk(chunk: AIMessageChunk) -> dict[str, int | float] | None:
    """从模型流式分块中提取可用 token usage 元数据。

    不同 provider/SDK 把 usage 放在不同位置：LangChain 标准 ``usage_metadata``、
    OpenAI 适配器的 ``response_metadata.token_usage`` / ``response_metadata.usage`` 等。
    本函数按优先级尝试，返回第一个非空字典；都没有则返回 None。

    参数:
        chunk: 模型 ``astream`` 产出的单个消息分块。

    返回:
        可用的 usage 字典；无则 None。
    """

    usage = getattr(chunk, "usage_metadata", None)
    if isinstance(usage, dict) and usage:
        return usage
    response_metadata = getattr(chunk, "response_metadata", None)
    if not isinstance(response_metadata, dict):
        return None
    for key in ("token_usage", "usage"):
        candidate = response_metadata.get(key)
        if isinstance(candidate, dict) and candidate:
            return candidate
    return None


async def _model_node(state: ReactGraphState) -> dict:
    """ReAct 模型节点：流式消费模型输出并决定下一步动作。

    节点从运行上下文取出 ``operations`` / ``task`` / ``turn`` / ``model``，通过
    ``get_stream_writer()`` 把业务生命周期事件与流式 token 增量写入自定义事件流；
    用 ``model.astream()`` 累积 ``AIMessage``，回复 token 与思考 token 在节点内就地翻译为
    ``MODEL_OUTPUT_DELTA`` /
    ``MODEL_THINKING_DELTA`` 事件，由编排层统一透传。根据模型最终输出决定进入工具分支、
    最终回答分支，还是因无效输出 / 超过最大步数而终止。状态写入 **turn**。

    参数:
        state: 当前 graph state。

    返回:
        需要合并回 graph state 的增量（步数、标志位、待执行工具调用等）。

    副作用:
        - 经 ``operations.append_runtime_message`` 把本轮 ``AIMessage`` 逐条增量落库；
        - 同步 ``_runtime_context().add_message`` 写回运行时上下文，使下一轮模型节点
          经 ``load_message()`` 能累积看到本轮输出（否则上下文不增长会陷入死循环）；
        - 流式 token / 事件经 ``get_stream_writer`` 透传；状态写入 ``turn``；
        - 失败终态（``max_steps_reached`` / ``invalid_model_output``）的 ``RUN_FAILED`` 事件携带
          ``usage`` token 摘要（可排查本轮已消耗 token）；取消分支不发终态事件，改以 warning 日志
          记录 usage 摘要；
        - 模型产出的 ``invalid_tool_calls`` 不会静默丢弃，按双轨消费：
          * **IGNORE 列表**（未命中任何已注册工具名、无法推断真实意图）：仅打 warning 日志，
            不修复、不阻塞（保留既有逻辑，跨情形 a/b 恒打）；
          * **REPAIR 列表**（命中已注册工具名、视为真实调用意图、仅字段非法）按 ``requested_tool``
            （即 ``bool(tool_calls)``）二维分流——
            - 情形 a（``requested_tool=True``，有合法工具）：修复提示经 ``deferred_repair_message``
              延后写 RuntimeContext，落工具分支（不 return）；
            - 情形 b（``requested_tool=False``，无合法工具）：构造 ``SystemMessage(repair_message)``
              经 ``_runtime_context().add_message()`` 写 RuntimeContext，并 **return 非终态 patch**
              （``repair_requested="true"``、``terminal=False``、``pending_tool_calls=[]``）触发
              edges 回流 model 重试。该 return 位于 ``if output_text`` / ``invalid_output`` 终态
              **之前**，绝不滑落 invalid_output 被误判失败；
          - 情形 b 为**非终态回流**，不设次数预算，靠 graph 的 ``max_steps`` 安全网兜底：
            耗尽后由 ``max_steps_node`` 以 ``max_steps_reached`` 分类终态（非 ``parse_invalid``）。
    """

    rc = _runtime_config()  # 取运行时配置
    operations = rc.operations  # 领域操作（写 turn、跑工具、查状态）
    turn = rc.turn  # 当前 turn 记录
    model = rc.model  # 已构建好的 chat model

    step_count = state.step_count + 1  # 步数 +1（本轮模型步）
    step_id = f"step-{step_count}"  # 步唯一 id
    if operations.is_current_turn_cancelled():
        log.info(
            "model_node_cancelled_before_request",
            extra={
                "msg": f"模型请求前检测到 turn 已取消，跳过模型调用，step_id={step_id}",
                "data": {"step_id": step_id, "turn_id": turn.turn_id},
            },
        )
        return {
            "step_count": step_count,
            "repair_requested": "false",
            "requested_tool": False,
            "final_response": False,
            "terminal": True,
            "pending_tool_calls": [],
        }
    # 消息通道由 RuntimeContext 独占管理（不进 graph state）；提前取历史上下文供日志与事件计数。
    messages = _runtime_context().load_message()
    log.info(
        "model_node_started",
        extra={
            "msg": f"模型节点开始执行，step_id={step_id}",
            "data": {
                "step_id": step_id,
                "step_count": step_count,
                "message_count": len(messages),
            },
        },
    )
    # 步开始
    write_event(
        EventType.STEP_STARTED,
        StepStartedPayload(step_id=step_id, kind="model", index=step_count),
    )
    if operations.is_current_turn_cancelled():
        log.info(
            "model_node_cancelled_before_model_requested",
            extra={
                "msg": f"模型请求事件前检测到 turn 已取消，跳过模型调用，step_id={step_id}",
                "data": {"step_id": step_id, "turn_id": turn.turn_id},
            },
        )
        return {
            "step_count": step_count,
            "repair_requested": "false",
            "requested_tool": False,
            "final_response": False,
            "terminal": True,
            "pending_tool_calls": [],
        }
    write_event(
        # 请求模型，带上历史消息数（消息通道由 RuntimeContext 独占，不经 state 传递）。
        EventType.MODEL_REQUESTED,
        ModelRequestedPayload(step_id=step_id, message_count=len(messages)),
    )

    # collected_text 与 chunks 职责互补、不可合并：
    # - collected_text 攒"给人看的文本流"：既驱动流式增量事件 MODEL_OUTPUT_DELTA（边收边发），
    #   又循环结束后拼成 output_text 供落库判断与日志。仅含 chunk.content 抽出的纯文本片段。
    # - chunks 攒"给机器解析的结构化对象"：循环结束后合并成完整 AIMessage，供解析
    #   tool_calls / usage_metadata / finish_reason / invalid_tool_calls 等字段（决定走工具
    #   分支还是 FINAL_RESPONSE）。collected_text 不是 chunks 的文本副本，二者生命周期与
    #   粒度都不同，合并会丢失结构信息或重复抽取文本。
    collected_text: list[str] = []  # 累积输出文本（流式增量 + 最终文本双重来源）
    chunks: list[AIMessageChunk] = []  # 累积流式分块（合并出结构化 AIMessage）
    chunk_index = 0  # 流式 chunk 序号（从 0 开始）
    terminal = False  # 是否因取消而提前终止

    # 真正流式调用模型，messages 为历史+系统上下文（来自 RuntimeContext，不进 state）。
    async for chunk in model.astream(messages):
        # 逐 chunk 调试落盘：在检查取消前先记录，确保取消场景下也能看到已产出的 chunk。
        _dump_raw_chunk_debug(chunk, chunk_index)
        chunk_index += 1

        # 抽本 chunk 的 token usage。应该先统计token再检查取消
        chunk_usage = _extract_usage_from_chunk(chunk)
        rc.usage_stats.add_message_usage(chunk_usage)

        # 每收到 chunk 都检查 turn 是否被取消
        if operations.is_current_turn_cancelled():
            terminal = True  # 标记提前终止
            log.info(
                "model_node_cancelled",
                extra={
                    "msg": f"模型流式输出期间检测到 turn 已取消，提前终止，step_id={step_id}",
                    "data": {"step_id": step_id},
                },
            )
            break  # 跳出流式循环

        # 所有 chunk 都留着，后面合并成完整消息
        chunks.append(chunk)
        text = _extract_text(chunk.content)  # 抽本 chunk 文本
        reasoning = _extract_reasoning_content(chunk)  # 抽思考片段
        if text:
            collected_text.append(text)  # 有文本才累积
            write_event(
                # 回复 token 在节点内就地翻译为增量事件，避免依赖 messages 流
                # 导致完整回复被重复推送
                EventType.MODEL_OUTPUT_DELTA,
                ModelOutputDeltaPayload(step_id=step_id, text=text),
            )
        if reasoning and reasoning.strip():
            write_event(
                # 有思考内容就发思考增量事件，前端可实时渲染"思考中"
                EventType.MODEL_THINKING_DELTA,
                ModelThinkingDeltaPayload(step_id=step_id, text=reasoning),
            )

    if terminal:  # 因取消而终止
        # 取消路径不发 RUN_FAILED 终态事件（避免与取消流的其它信号重复），
        # 但本轮已累计的 token 消耗必须回传前端（StatusBadge 渲染）并可排查：
        # 同时发 RUN_CANCELLED 终态事件（携带扁平 token 字段）与 warning 日志摘要。
        usage_summary = rc.usage_stats.to_dict()
        log.warning(
            "model_node_cancelled_usage_summary",
            extra={
                "msg": f"模型流式因取消提前终止，本轮已消耗 token 摘要，step_id={step_id}",
                "data": {"step_id": step_id, "usage": usage_summary},
            },
        )
        write_event(
            EventType.RUN_CANCELLED,
            RunCancelledPayload(
                status="cancelled",
                step_id=step_id,
                error="turn_cancelled",
                langfuse_trace_id=rc.langfuse_trace_id,
                input_tokens=usage_summary["input_tokens"],
                output_tokens=usage_summary["output_tokens"],
                total_tokens=usage_summary["total_tokens"],
                cache_hit_tokens=usage_summary["cache_hit_tokens"],
                cache_miss_tokens=usage_summary["cache_miss_tokens"],
                reasoning_tokens=usage_summary["reasoning_tokens"],
            ),
        )
        return terminal_state(step_count)

    # 分块合并成完整 AIMessage。
    ai_message = _collect_chunk_to_ai_message(chunks)
    # 把 LangChain 的 tool_calls 转成内部 ToolCall 值对象
    tool_calls: list[ToolCall] = tool_calls_from_langchain(ai_message.tool_calls or [])
    # 解析失败/非法的工具调用不得静默丢弃：先记 warning 供排查，再按决策结论处置。
    # 决策（纯函数）与执行（下方分支）分离：命中已注册工具名视为真实调用意图且
    # 仅字段非法，需修复；但修复不阻塞其他合法工具执行，提示延后到下一轮追加。
    invalid_tool_calls = getattr(ai_message, "invalid_tool_calls", None) or []
    # requested_tool 前移到 REPAIR 块之前：REPAIR 块内情形 b 分流、下方工具分支均依赖此值，
    # 故在消费 invalid_tool_calls 前即确定，避免块内不可用。
    requested_tool = bool(tool_calls)

    repair_message: str | None = None
    repair_data: list[dict[str, Any]] | None = None

    if invalid_tool_calls:
        # 解析失败/非法的工具调用不得静默丢弃：记 warning 供排查。args 是模型原始未校验
        # 内容（可能含用户 prompt 片段、粘贴进对话的凭据），脱敏 + 截断 + 限条数，并带上
        # LangChain InvalidToolCall 自带的解析错误原因（排查「为什么失败」的关键字段）。

        available_tool_names = {tool.name for tool in operations.model_tools}
        result = ModelToolHelper.decide_invalid_tool_handling(
            invalid_tool_calls=invalid_tool_calls,
            available_tool_names=available_tool_names
        )

        if result[InvalidToolOutcome.IGNORE]:
            # 解析噪声（未命中任何已注册工具名、无法推断真实意图）：仅记录，不修复、不阻塞。
            # 日志 data 复用 invalid_tool_call_summaries 做脱敏 + 截断，避免把模型原始
            # 未校验 args（可能含用户 prompt 片段、粘贴进对话的凭据）原文写进日志。
            # IGNORE 列表只打 warning，不回流、不阻塞（跨情形 a/b 恒打，与 REPAIR 双轨独立）。
            log.warning(
                "model_node_invalid_tool_calls_ignored",
                extra={
                    "msg": (
                        "非法工具调用未命中已注册工具名，视为解析噪声忽略，"
                        f"保留合法工具继续执行，step_id={step_id}"
                    ),
                    "data": {
                        "step_id": step_id,
                        "invalid_tool_calls": result[InvalidToolOutcome.IGNORE],
                    },
                },
            )

        if result[InvalidToolOutcome.REPAIR]:
            # REPAIR 列表按 requested_tool 二维分流（核心，杜绝滑落 invalid_output）：
            # - 情形 a（requested_tool=True，有合法工具）：修复提示随 pending_tool_calls 延后
            #   到 tools 节点经 deferred_repair_message 写进 RuntimeContext，不 return。
            # - 情形 b（requested_tool=False，无合法工具但命中工具名）：构造 SystemMessage
            #   直接写 RuntimeContext，并 return 非终态 patch 触发 edges 回流 model 重试。
            #   该 return 必须在下方 if output_text / invalid_output 终态之前发生，
            #   确保情形 b 不被误判为非法输出失败（回流可达，靠 max_steps 兜底）。

            repair_data = result[InvalidToolOutcome.REPAIR]
            # 本块由 if result[InvalidToolOutcome.REPAIR] 守卫，repair_data 恒为非空 list；
            # assert 收窄 mypy 类型，下方调用与工具分支注入均依赖其非 None。
            assert repair_data is not None

            repair_message = ModelToolHelper.build_invalid_tool_call_repair_message(
                repair_datas=repair_data
            )

            if requested_tool:
                # 情形 a：合法工具先执行，修复提示延后追加（下方工具分支经
                # deferred_repair_message 注入），不阻断本轮工具分支、不 return。
                log.warning(
                    "model_node_invalid_tool_calls_deferred_repair_requested",
                    extra={
                        "msg": (
                            "非法工具调用命中已注册工具名，合法工具先执行，"
                            f"修复提示延后追加，step_id={step_id}"
                        ),
                        "data": {"step_id": step_id, "repair_data": repair_data},
                    },
                )
            else:
                # 情形 b：无合法工具但命中工具名 → 直接写 SystemMessage 进 RuntimeContext，
                # 并 return 非终态 patch（repair_requested="true" 触发 edges 回 model）。
                # 非终态、不重复发终态事件；重试耗尽由 max_steps_node 兜底。
                log.warning(
                    "model_node_invalid_tool_calls_no_tool_deferred",
                    extra={
                        "msg": (
                            "非法工具调用命中已注册工具名但本轮无合法工具，"
                            f"修复提示直接注入并回流 model 重试，step_id={step_id}"
                        ),
                        "data": {
                            "step_id": step_id,
                            "repair_message_length": len(repair_message),
                        },
                    },
                )
                _runtime_context().add_message(SystemMessage(content=repair_message))
                return {
                    "step_count": step_count,
                    "repair_requested": "true",
                    "requested_tool": False,
                    "final_response": False,
                    "terminal": False,
                    "pending_tool_calls": [],
                    "continuation_error_data": None,
                }

    # 逐条持久化本轮产生的 assistant 消息（替代 turn 结束后的批落库）。
    # 仅当消息有文本或工具调用时才落库，避免空壳消息污染跨轮历史。
    if _has_content(ai_message):
        operations.append_runtime_message(_ai_to_runtime_message(ai_message))
        # 同步写回运行时上下文，使下一模型步经 _runtime_context().load_message()
        # 能读到本轮累积的 assistant 消息，否则模型每步都看到不变的首轮快照，
        # 会陷入「相同上下文→相同输出」的死循环。
        _runtime_context().add_message(ai_message)
    output_text = "".join(collected_text).strip()  # 拼接文本并去首尾空白
    # requested_tool 已在上方 invalid_tool_calls 消费前计算（供 REPAIR 块分流），
    # 此处直接复用，不再重算。
    log.info(
        "model_node_completed",
        extra={
            "msg": f"模型产出完成，step_id={step_id}",
            "data": {
                "step_id": step_id,
                "has_tool_calls": requested_tool,
                "tool_count": len(tool_calls),
                "output_text_length": len(output_text),
            },
        },
    )

    if requested_tool:  # 模型要求调用工具
        log.info(
            "model_node_tool_branch",
            extra={
                "msg": f"模型请求调用 {len(tool_calls)} 个工具，进入工具节点，step_id={step_id}",
                "data": {
                    "step_id": step_id,
                    "tool_count": len(tool_calls),
                    "has_instruction": bool(output_text),
                    "instruction_length": len(output_text),
                },
            },
        )
        # 模型同时产出文本时，把说明文本作为 instruction 随每个工具调用下传，
        # 供 tools / observe 节点在执行与错误排查时看到模型意图（ReAct 中「边说明边调工具」合法）。
        # 无文本时 instruction 缺省为空串，向后兼容。
        instruction = output_text if output_text else ""
        # build_invalid_tool_call_repair_message 返回值是 str（不是带 .content 的对象），
        # 故直接 str(repair_message)；repair_data 非空才表示确有 REPAIR 项需延后注入。
        deferred_repair_content = (
            str(repair_message) if repair_message is not None and repair_data else ""
        )
        pending_tool_calls = [
            {
                "tool_name": call.tool_name,
                "arguments": call.arguments,
                "call_id": call.call_id,
                "instruction": instruction,
            }
            for call in tool_calls
        ]
        if deferred_repair_content:
            for item in pending_tool_calls:
                item["deferred_repair_message"] = deferred_repair_content
        return {
            "step_count": step_count,
            "repair_requested": "false",
            "requested_tool": True,
            "continuation_error_data": repair_data,
            "final_response": False,
            "terminal": False,  # 非终态，graph 会继续到 tools 节点
            # 待执行工具调用交给 tools 节点
            "pending_tool_calls": pending_tool_calls,
        }

    if output_text:  # 没有工具调用但有文本 → 最终回答
        completed_turn = operations.complete_turn_if_running(turn.turn_id, output_text)
        if completed_turn is None:
            log.info(
                "model_node_final_response_terminal_race_lost",
                extra={
                    "msg": f"最终回复落定时 turn 已非 running，跳过完成事件，step_id={step_id}",
                    "data": {"step_id": step_id, "turn_id": turn.turn_id},
                },
            )
            return terminal_state(step_count)
        log.info(
            "model_node_final_response",
            extra={
                "msg": f"模型给出最终回复，已落库，step_id={step_id}",
                "data": {"step_id": step_id, "output_text_length": len(output_text)},
            },
        )
        write_event(
            EventType.FINAL_RESPONSE,
            FinalResponsePayload(text=output_text, step_id=step_id, status="completed"),
        )
        # 整个 run 结束：计算耗时并汇总 token
        duration_ms = int((perf_counter() - rc.start_time) * 1000)
        usage = rc.usage_stats.to_dict()
        write_event(
            EventType.RUN_FINISHED,
            RunFinishedPayload(
                status="completed",
                step_id=step_id,
                duration_ms=duration_ms,
                input_tokens=usage["input_tokens"],
                output_tokens=usage["output_tokens"],
                total_tokens=usage["total_tokens"],
                cache_hit_tokens=usage["cache_hit_tokens"],
                cache_miss_tokens=usage["cache_miss_tokens"],
                reasoning_tokens=usage["reasoning_tokens"],
                langfuse_trace_id=rc.langfuse_trace_id,
            ),
        )
        return {
            **terminal_state(step_count, final_response=True),
            "final_text": output_text,  # 供上层取最终回复
        }

    log.warning(
        "model_node_invalid_output",
        extra={
            "msg": f"模型既未返回工具调用也无有效文本，判定为非法输出，step_id={step_id}",
            "data": {"step_id": step_id, "output_text_length": len(output_text)},
        },
    )
    failed_turn = operations.fail_turn_if_running(turn.turn_id, end_reason="invalid_model_output")
    if failed_turn is None:
        log.info(
            "model_node_invalid_output_terminal_race_lost",
            extra={
                "msg": f"非法模型输出失败落定时 turn 已非 running，跳过失败事件，step_id={step_id}",
                "data": {"step_id": step_id, "turn_id": turn.turn_id},
            },
        )
        return terminal_state(step_count)
    write_event(  # 既没工具调用也没文本 → 模型输出非法
        EventType.RUN_FAILED,
        build_run_failed_payload(
            step_id,
            "invalid_model_output",
            usage=rc.usage_stats,
            langfuse_trace_id=rc.langfuse_trace_id,
        ),
    )
    return terminal_state(step_count)
