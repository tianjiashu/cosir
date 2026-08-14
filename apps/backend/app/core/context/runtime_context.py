from __future__ import annotations

import json
import threading
import types
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from platform import system
from typing import Any, Self

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from app.config.logging.logger import log
from app.core.agents.agent_profile import AgentProfile
from app.core.context import SystemPromptBuilder
from app.core.context.context_compressor import ContextCompressor
from app.core.llm.langchain_bridge import sanitize_assistant_messages
from app.models import RuntimeMessage, TurnRecord
from app.service.task.turn_service import TurnService


def _default_coding_rule_dir() -> str:
    """返回默认编码规则的绝对路径。

    返回:
        规则文件 ``default-coding-rules.md`` 的绝对路径字符串。
    """
    return str(Path(__file__).resolve().parent / "rules" / "default-coding-rules.md")


def _default_os_name() -> str:
    """惰性求值当前操作系统名称，避免在类定义时一次性固定为陈旧值。

    返回:
        平台标识字符串（如 ``Windows`` / ``Darwin`` / ``Linux``）。
    """
    return system()


def _default_today() -> str:
    """惰性求值当前日期，避免在类定义时一次性固定为陈旧值。

    返回:
        当天日期的 ISO 格式字符串（``YYYY-MM-DD``）。
    """
    return date.today().isoformat()


@dataclass
class RuntimeContext:
    """单个 task 下的运行时上下文管理器，提供 task 级隔离与上下文管理。

    职责边界：
    - **task 隔离**：每个实例绑定唯一 ``task_id``，``messages`` 与 ``lock`` 独立，
      不跨 task 共享可变状态；子 task（subAgent）经 :meth:`create_child` 派生的
      上下文继承父系统提示与只读历史快照，但拥有独立可写消息列表。
    - **上下文管理**：线程安全的追加/读取、按 task 显式加载历史、``with`` 生命周期、
      一致性快照。
    - **压缩预留**：通过可选 ``compressor`` 引用 ``ContextCompressor`` 协议与
      :meth:`maybe_compact` 暴露扩展点，暂不实现具体压缩算法。

    不负责：消息**写库**（由 ``runtime_operations.append_runtime_message`` 负责）；
    模型调用、工具执行、压缩算法实现。
    读路径（跨轮历史加载）经 ``TurnService`` 门面从持久层拉取，属本类职责边界内的
    「只读加载」，不在此列。
    """

    # 必填：task 身份与角色画像，构造即确定，是 task 隔离的锚点。
    task_id: str
    agent_profile: AgentProfile
    # 运行时消息列表（系统提示 + 历史 + 本轮增量）。
    messages: list[BaseMessage] = field(default_factory=list)
    coding_rule_dir: str = field(default_factory=_default_coding_rule_dir)
    language: str = "zh"
    os_name: str = field(default_factory=_default_os_name)
    workspace_root: str = ""
    today: str = field(default_factory=_default_today)
    # 可重入锁：模型节点内可能嵌套调用，RLock 避免自死锁。
    lock: threading.RLock = field(default_factory=threading.RLock)
    # subAgent 铺垫：父 task 上下文引用；顶层 task 为 None。
    parent_context: RuntimeContext | None = None
    # 压缩预留：可选压缩器，未配置时 maybe_compact 原样返回。
    compressor: ContextCompressor | None = None

    def __post_init__(self) -> None:
        """构造后初始化非字段状态（系统提示消息）。

        采用 ``__post_init__`` 而非自定义 ``__init__``，确保所有 dataclass 字段
        由框架自动初始化，避免漏字段导致的 ``AttributeError``。

        副作用:
            向 ``messages`` 追加一条系统提示消息（若尚未存在）。
        """
        # 避免 create_child 已预置系统提示时重复追加。
        if not self.messages or not isinstance(self.messages[0], SystemMessage):
            self.messages.insert(0, self._build_system_message())

    def __enter__(self) -> Self:
        """进入 ``with`` 上下文，返回自身供块内使用。

        返回:
            当前 ``RuntimeContext`` 实例。
        """
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: types.TracebackType | None,
    ) -> None:
        """退出 ``with`` 上下文，记录上下文规模并释放资源引用。

        说明:
            消息落库由 graph checkpoint / runtime_operations 负责，本方法不重复写库，
            仅做可排查日志与防御性清理，避免与既有持久化链路重复或覆盖。

        参数:
            exc_type: 异常类型，正常退出为 ``None``。
            exc_val: 异常实例，正常退出为 ``None``。
            exc_tb: 异常回溯，正常退出为 ``None``。

        返回:
            无。

        副作用:
            写入一条 info 级日志，记录 task 上下文的消息条数与是否正常退出。
        """
        status = "abnormal" if exc_type is not None else "normal"
        log.info(
            "runtime_context exited: task_id=%s parent=%s status=%s message_count=%d",
            self.task_id,
            self.parent_context.task_id if self.parent_context else None,
            status,
            len(self.messages),
        )

    @classmethod
    def load_for_task(
        cls,
        agent_profile: AgentProfile,
        workspace_root: str,
        task_id: str,
        excluded_turn_ids: tuple[str, ...] = (),
    ) -> RuntimeContext:
        """按 task 加载历史消息并构造运行时上下文（显式 I/O 入口）。

        与构造分离，使构造保持纯内存、可测试，历史加载成为可独立调用的
        有副作用操作。读路径收口到 ``TurnService`` 门面（core → service → storage），
        与写路径（``RuntimeOperations`` 经 ``TurnService`` 落库）保持同一分层口径。

        参数:
            agent_profile: 当前 agent 的角色画像。
            workspace_root: 工作区根目录。
            task_id: 目标 task 标识。

        返回:
            已加载该 task 全部历史消息的 ``RuntimeContext`` 实例。

        异常:
            sqlalchemy.exc.SQLAlchemyError: ``TurnService.list_turns_for_task`` /
            ``load_turn_messages`` 的数据库读取失败会直接透传（此处不吞异常也不额外捕获），
            由上层调用方决定降级或失败策略。

        副作用:
            经 ``TurnService`` 查询该 task 下各 turn 的消息并转换，向 ``messages``
            追加历史转换结果；加载完成后写一条 info 日志供排查（含 task_id、turn 数、
            消息总数）。
        """
        ctx = cls(task_id=task_id, agent_profile=agent_profile, workspace_root=workspace_root)
        # 读路径同样收口到 TurnService 门面（core → service，service → storage），
        # 与写路径（RuntimeOperations 经 TurnService 落库）保持一致，避免 core 直连 storage 层。
        turn_service = TurnService()
        excluded_turn_id_set = set(excluded_turn_ids)
        turn_list: list[TurnRecord] = turn_service.list_turns_for_task(task_id)
        message_count = 0
        for turn in turn_list:
            if turn.turn_id in excluded_turn_id_set:
                continue
            messages: list[RuntimeMessage] = turn_service.load_turn_messages(turn.turn_id)
            ctx.messages.extend(ctx._build_history_messages(messages))
            message_count += len(messages)
        log.info(
            "runtime_context_loaded",
            extra={
                "msg": (
                    f"已加载 task 历史上下文，task_id={task_id} "
                    f"turn 数={len(turn_list)} 消息数={message_count}"
                ),
                "data": {
                    "task_id": task_id,
                    "turn_count": len(turn_list),
                    "message_count": message_count,
                    "excluded_turn_count": len(excluded_turn_id_set),
                },
            },
        )
        return ctx

    def create_child(
        self,
        task_id: str,
        agent_profile: AgentProfile | None = None,
        workspace_root: str | None = None,
    ) -> RuntimeContext:
        """为 subAgent 派生一个子 task 上下文（subAgent 铺垫扩展点）。

        子上下文继承父的系统提示与历史只读快照，但拥有独立可写消息列表与锁，
        保证 task 间隔离、不共享可变状态。父上下文经 ``parent_context`` 反向可追溯。

        参数:
            task_id: 子 task 的唯一标识。
            agent_profile: 子 agent 角色画像，缺省沿用父画像。
            workspace_root: 子工作区根目录，缺省沿用父根目录。

        返回:
            新构造的、已预置系统提示与历史只读快照的子 ``RuntimeContext``。

        异常:
            无。

        副作用:
            构造新实例并复制父历史快照（拷贝，非引用）。
        """
        profile = agent_profile or self.agent_profile
        root = workspace_root or self.workspace_root
        child = RuntimeContext(
            task_id=task_id,
            agent_profile=profile,
            workspace_root=root,
            parent_context=self,
        )
        # 只读历史快照：拷贝父消息（不含系统提示，子已自带），供 subAgent 参考上下文。
        with self.lock:
            history_snapshot = list(self.messages[1:])
        child.messages.extend(history_snapshot)
        return child

    def maybe_compact(self) -> bool:
        """在压缩器已配置时压缩上下文，否则原样返回（压缩预留扩展点）。

        说明:
            当前不实现具体压缩算法，仅暴露调用入口。压缩器未配置时直接返回
            ``False`` 表示未发生压缩，调用方无需感知压缩细节。

        返回:
            是否实际执行了压缩（``True`` 已压缩 / ``False`` 无压缩器或无需压缩）。

        副作用:
            压缩器配置时，原地替换 ``messages`` 为压缩结果（线程安全）。
        """
        if self.compressor is None:
            return False
        with self.lock:
            self.messages = self.compressor.compact(self.messages)
        return True

    def _build_system_message(self) -> SystemMessage:
        """构建系统提示消息。

        返回:
            含 agent 系统提示的 ``SystemMessage``。
        """
        system_prompt = SystemPromptBuilder.build(self.agent_profile, self.workspace_root)
        return SystemMessage(content=system_prompt)

    def _build_history_messages(self, message_list: list[RuntimeMessage]) -> list[BaseMessage]:
        """将运行时消息列表转换为 langchain 消息列表。

        说明:
            如果 ``ToolCall`` 没有对应的 ``Message`` 模型将异常（沿用既有约束）。

        参数:
            message_list: 从 ``turn_message_crud`` 读出的运行时消息列表。

        返回:
            转换后的 langchain 消息列表。
        """
        converted: list[BaseMessage] = []

        for message in message_list:
            if message.role == "user":
                converted.append(HumanMessage(content=message.content_text))
            elif message.role == "assistant":
                tool_calls_meta = self._tool_calls_from_metadata(message.metadata.get("tool_calls"))
                langchain_tool_calls = [
                    {
                        "name": call["name"],
                        "args": call.get("args") if isinstance(call.get("args"), dict) else {},
                        "id": call.get("id") or "",
                    }
                    for call in tool_calls_meta
                ]
                converted.append(
                    AIMessage(
                        content=message.content_text,
                        tool_calls=langchain_tool_calls,
                    )
                )
            elif message.role == "tool":
                converted.append(
                    ToolMessage(
                        content=message.content_text,
                        tool_call_id=message.metadata.get("tool_call_id", ""),
                    )
                )

        return converted

    def load_message(self) -> list[BaseMessage]:
        """线程安全地读取当前全部消息的拷贝。

        说明:
            返回列表拷贝而非内部引用，避免调用方在锁外修改内部状态。消息在 :meth:`add_message`
            入口已被归一化（content 占位 + 非法 tool_calls 过滤 + 丢弃 invalid_tool_calls），
            此处仅做纯运输，不再重复清洗。

        返回:
            当前消息列表的独立拷贝。
        """
        with self.lock:
            return list(self.messages)

    def add_message(self, message: BaseMessage) -> None:
        """线程安全地向上下文追加一条消息，并在入口做归一化守卫。

        说明:
            使用可重入锁（``RLock``）阻塞获取，模型节点内嵌套调用不会自死锁；
            不采用超时静默丢弃，避免历史上下文在锁竞争时悄然丢失而难以排查。

            守卫收口在入口：消息「写入上下文」这一刻即被 :func:`sanitize_assistant_messages`
            归一化（assistant 消息 content 空串 → 非空占位、残缺 tool_calls 过滤、丢弃
            ``invalid_tool_calls`` 这一当轮解析噪声），下游 :meth:`load_message` / :meth:`snapshot`
            拿到的天然就是干净消息，无需在出口重复清洗。``invalid_tool_calls`` 只服务于
            ``model_node`` 当轮 REPAIR/IGNORE 决策——从合并出的 ``ai_message`` 直接读取、
            不经本方法，绝不进入上下文、绝不回灌下一轮对话。

        参数:
            message: 待追加的 langchain 消息（非 assistant 类型原样写入）。

        返回:
            无。

        异常:
            无。

        副作用:
            向 ``messages`` 追加（已归一化的）消息。
        """
        with self.lock:
            self.messages.append(sanitize_assistant_messages([message])[0])

    def snapshot(self) -> list[BaseMessage]:
        """返回消息列表的一致性快照（独立拷贝）。

        与 :meth:`load_message` 语义一致，命名强调「一致性视图」用途，
        供模型调用在多线程下读取稳定视图。消息在 :meth:`add_message` 入口已归一化，
        此处仅做纯运输。

        返回:
            消息列表的独立拷贝。
        """
        with self.lock:
            return list(self.messages)

    def _tool_calls_from_metadata(self, raw: str | None) -> list[dict[str, Any]]:
        """从 ``RuntimeMessage.metadata`` 的 JSON 字符串还原 assistant 的 tool_calls。

        ``workflows/react/nodes._ai_to_runtime_message`` 把 langchain ``tool_calls`` 序列化为
        JSON 字符串存入 ``metadata``，此处反序列化回 ``list[dict]`` 供 ``AIMessage`` 重建使用。

        参数:
            raw: ``metadata.get("tool_calls")`` 的 JSON 字符串，可能为空或非法。

        返回:
            tool_calls 字典列表；空串、非法 JSON 或非列表时返回空列表。

        异常:
            无。

        副作用:
            无。
        """
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return []
        return parsed if isinstance(parsed, list) else []
