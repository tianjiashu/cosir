"""Task 标题支线：Run 首次并入用户输入后，与主线并行地请模型给对话起一个简短标题；每个 Run 至多一次。

为什么挂在驱动上：支线复用本次 Run 的 ``rc.model``（同一 client、同一工具定义，命中 Task 的前缀缓存），
而 client 在 ``ReactLikeWorkflow.run`` 收尾时关闭，所以支线由驱动持有（``rc.title_branch``），
``settle`` 在关闭 client 前收尾它。成败都不影响 Run：失败、超时、取消只记日志，标题保持原样
（创建 Task 时的用户输入兜底，或上一次生成的标题）。

用量不计入 Run：``ConversationRunUsageStats`` 是「最新一次调用覆盖」语义并驱动上下文窗口展示，并行的
标题请求写进去会覆盖主线用量，只记进日志。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING

from openai.types.chat import ChatCompletionMessageParam, ChatCompletionUserMessageParam
from openai.types.shared_params import ResponseFormatJSONObject

from app.config.constant import Constant
from app.config.logging.logger import log
from app.core.events import TaskTitleUpdatedEvent
from app.core.llm_provider.openai_model import usage_from_response
from app.core.workflow.node_helper.common import emit
from app.service import depends as service_depends
from app.utils.datetime_utils import TASK_TITLE_LIMIT, preview
from app.utils.message_content import content_to_text

if TYPE_CHECKING:
    from app.core.workflow.execution_state.react_execution_state import ReactExecutionState
    from app.core.workflow.execution_state.runtime_context import RuntimeContext

# 侧栏可见的对话 Task；子 Agent 与 Team 节点的标题是 Agent / 节点名，不能被改写。
_TITLED_TASK_TYPES = frozenset({"user", "fork"})
# 收尾等待期间复查取消信号的间隔：信号没有可等待的通知，停止与编辑重跑不该被支线拖住。
_SETTLE_POLL_SECONDS = 0.2


def should_launch(rc: RuntimeContext, state: ReactExecutionState) -> bool:
    """本 Run 尚未起过支线，且 Task 是侧栏对话；调用方据此决定是否准备上下文副本。

    「起过」记在持久执行状态 ``title_requested`` 上而不是驱动期的 ``rc.title_branch``：挂起后唤醒、
    重启后恢复都是新驱动，只看驱动期字段会让同一个 Run 重复起标题。
    """
    return not state.title_requested and rc.task.task_type in _TITLED_TASK_TYPES


def launch(rc: RuntimeContext, messages: list[ChatCompletionMessageParam]) -> None:
    """以 ``messages``（调用方在主线写入用户输入后取的请求副本）启动支线；调用方随后置位
    ``title_requested``，由 workflow 随节点结果落库。

    副本由调用方同步取好再交进来，支线不读 working copy：主线流式输出会持续改写它。
    """
    rc.title_branch = asyncio.get_running_loop().create_task(_generate(rc, messages))


async def settle(rc: RuntimeContext) -> None:
    """关闭 client 前收尾支线：有取消信号就立即取消，否则等它自行结束（自身受总时限约束）。

    外层被取消（进程关闭）时同样取消支线。
    """
    branch = rc.title_branch
    if branch is None:
        return
    try:
        while not branch.done() and not rc.is_cancelled():
            await asyncio.wait({branch}, timeout=_SETTLE_POLL_SECONDS)
    finally:
        if not branch.done():
            branch.cancel()
            log.info(
                "task_title_branch_cancelled",
                extra={
                    "msg": "Run 已停止，取消标题支线，保留原标题",
                    "data": {"task_id": rc.task.id, "run_id": rc.run.id},
                },
            )
        await asyncio.gather(branch, return_exceptions=True)


async def _generate(rc: RuntimeContext, messages: list[ChatCompletionMessageParam]) -> None:
    """请求、解析并写入标题；任何异常都在这里收敛为日志。"""
    started = time.monotonic()
    current = rc.task.title
    try:
        messages.append(
            ChatCompletionUserMessageParam(role="user", content=_instruction(current))
        )
        response = await asyncio.wait_for(
            rc.model.complete(  # type: ignore[union-attr]  # 驱动期已补齐
                messages, response_format=ResponseFormatJSONObject(type="json_object")
            ),
            timeout=Constant.Workflow.TASK_TITLE_TIMEOUT_SECONDS,
        )
        title = _parse_title(content_to_text(response.choices[0].message.content))
        usage = usage_from_response(response.usage)
        if title is None or title == current:
            log.info(
                "task_title_unchanged",
                extra={
                    "msg": "标题支线未给出新标题，保留原标题",
                    "data": {
                        "task_id": rc.task.id,
                        "run_id": rc.run.id,
                        "parsed": title is not None,
                        "usage": usage,
                    },
                },
            )
            return
        await asyncio.to_thread(service_depends.get_task_service().update_title, rc.task.id, title)
    except asyncio.CancelledError:
        raise
    except KeyError:
        log.info(
            "task_title_task_missing",
            extra={
                "msg": "Task 已删除，丢弃生成的标题",
                "data": {"task_id": rc.task.id, "run_id": rc.run.id},
            },
        )
        return
    except Exception as exc:
        log.warning(
            "task_title_generation_failed",
            extra={
                "msg": "标题支线失败，保留原标题",
                "data": {
                    "task_id": rc.task.id,
                    "run_id": rc.run.id,
                    "error_type": type(exc).__name__,
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                },
            },
            exc_info=exc,
        )
        return
    emit(TaskTitleUpdatedEvent(task_id=rc.task.id, run_id=rc.run.id, title=title))
    log.info(
        "task_title_generated",
        extra={
            "msg": "标题支线已更新 Task 标题",
            "data": {
                "task_id": rc.task.id,
                "run_id": rc.run.id,
                "title": title,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "usage": usage,
            },
        },
    )


def _instruction(current_title: str) -> str:
    """请求末尾的临时指令，不写回 canonical context；带上当前标题，主题未变时让模型原样返回。"""
    return "\n\n".join(
        [
            "Do not answer or continue the conversation above. Your only job now is to name it.",
            (
                "Write a short title that summarizes what the user wants in this conversation, "
                "in the same language the user writes in, "
                f"at most {TASK_TITLE_LIMIT} characters, without quotes or trailing punctuation."
            ),
            f"Current title: {json.dumps(current_title, ensure_ascii=False)}",
            "If the topic has not clearly changed, return the current title unchanged.",
            'Return only JSON: {"title": "..."}. Do not call tools.',
        ]
    )


def _parse_title(text: str) -> str | None:
    """取出 ``title`` 并归一为单行、按统一上限截断；不合格返回 ``None``。"""
    try:
        value = json.loads(text.strip())
    except ValueError:
        return None
    title = value.get("title") if isinstance(value, dict) else None
    if not isinstance(title, str):
        return None
    return preview(title.strip().strip("\"'“”「」"), limit=TASK_TITLE_LIMIT) or None
