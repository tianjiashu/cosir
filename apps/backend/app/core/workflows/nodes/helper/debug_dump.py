"""模型节点「chunk debug 落盘」辅助集。

本模块只承载「调试落盘」单一职责：把模型流式产出的原始 chunk 与合并后完整 chunk
结构追加写入 ``logs/debug_merged_chunks.jsonl`` / ``logs/debug_raw_chunks.jsonl``，
供本地逐 chunk 排查完整字段。常规结构化日志通道（JSONL 文件 + SQLite 日志库）
对所有 ``data`` 字符串施加 ``MAX_LOG_TEXT_LENGTH`` 截断，无法承载完整消息 JSON；
本模块绕过该预算，以单行 JSON 落盘，使开发者能在不被截断的前提下查看 chunk 结构。

与思考提取、chunk 组装（统归 ``model_chunk`` 的 ``ModelChunkProcessor``）职责分离：
本模块只关心「写盘」，不关心「抽出什么 / 如何合并」。无循环导入：本模块不
import ``model_node`` / ``common``。
"""

import json
from datetime import UTC, datetime

from langchain_core.messages import AIMessageChunk

from app.config.logging.logger import log
from app.config.settings import Settings


def _utc_now_iso() -> str:
    """返回毫秒精度、``Z`` 后缀的 UTC 时间文本。"""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


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


__all__ = [
    "_dump_merged_chunk_debug",
    "_dump_raw_chunk_debug",
    "_utc_now_iso",
]
