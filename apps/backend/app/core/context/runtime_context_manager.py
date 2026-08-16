from __future__ import annotations

import dataclasses
import json
import threading
import types
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from platform import system
from typing import TYPE_CHECKING, Any, Self

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from app.config.logging.logger import log
from app.config.settings import Settings
from app.core.agents.agent_profile import AgentProfile
from app.core.context import SystemPromptBuilder
from app.core.context.context_compressor import ContextCompressor
from app.core.context.context_usage_meter import ContextUsageMeter
from app.models import RuntimeMessage

if TYPE_CHECKING:
    from app.core.context.runtime_message_store import RuntimeMessageStore


def _tool_calls_from_metadata(raw: str | None) -> list[dict[str, Any]]:
    """从 ``RuntimeMessage.metadata`` 的 JSON 字符串还原 assistant 的 tool_calls。

    ``workflows/react/nodes._ai_to_runtime_message`` 把 langchain ``tool_calls`` 序列化为
        JSON 字符串存入 ``metadata``，此处反序列化回 ``list[dict]`` 供 ``AIMessage`` 重建使用。
        同时是 ``RuntimeMessage → BaseMessage`` 转换的唯一反序列化收口，供
        ``runtime_context_manager`` 复用（避免两套几乎一致的实现）。

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
class RuntimeContextManager:
    """单个 task 下的运行时上下文管理器，提供 task 级隔离与上下文管理。

    职责边界：
    - **task 隔离**：每个实例绑定唯一 ``task_id``，``messages`` 与 ``lock`` 独立，
      不跨 task 共享可变状态；子 task（subAgent）经 :meth:`create_child` 派生的
      上下文继承父系统提示与只读历史快照，但拥有独立可写消息列表。
    - **上下文管理**：线程安全的追加/读取、按 task 显式加载历史、``with`` 生命周期、
      一致性快照。
    - **读写唯一入口**：内存形态（``messages``）与持久化形态（``turn_messages``）的
      转换、落库、写内存、序号维护全部在本类内完成，经注入的 ``store`` 端口落库
      （依赖倒置，避免 ``core/context`` 反向依赖 service）。``add_message`` 是唯一写入
      API，联合类型收口 ``BaseMessage`` / ``RuntimeMessage`` / ``str``，经 ``persist`` /
      ``write_memory`` 双开关正交控制落库与写内存（``write_memory=False`` 承载 turn
      启动基线的「只落库不写内存」）。
    - **压缩预留**：通过可选 ``compressor`` 引用 ``ContextCompressor`` 协议与
      :meth:`maybe_compact` 暴露扩展点，暂不实现具体压缩算法。

    不负责：模型调用、工具执行、压缩算法实现；``turn_messages`` 表的**删除清理**
    （task/workspace 级联删除由 service 层 ``delete_by_turn_ids`` 承担，不在收敛范围）。
    """

    # 必填：task 身份与角色画像，构造即确定，是 task 隔离的锚点。
    task_id: str
    agent_profile: AgentProfile
    # 运行时消息列表（系统提示 + 历史 + 本轮增量）。
    messages: list[RuntimeMessage] = field(default_factory=list)
    coding_rule_dir: str = field(default_factory=_default_coding_rule_dir)
    language: str = Settings.DEFAULT_LANGUAGE
    os_name: str = field(default_factory=_default_os_name)
    workspace_root: str = ""
    today: str = field(default_factory=_default_today)
    # 可重入锁：模型节点内可能嵌套调用，RLock 避免自死锁。
    lock: threading.RLock = field(default_factory=threading.RLock)
    # subAgent 铺垫：父 task 上下文引用；顶层 task 为 None。
    parent_context: RuntimeContextManager | None = None
    # 压缩预留：可选压缩器，未配置时 maybe_compact 原样返回。
    compressor: ContextCompressor | None = None
    # 上下文占用计量器：可选，挂载后随消息变化估算当前上下文窗口 token 占用；
    # 不挂载时 mark_context_changed 为空操作（保持向前兼容与零开销）。
    usage_meter: ContextUsageMeter | None = None
    # 消息持久化端口（依赖倒置）：由 service 层实现并注入，使 manager 成为读写唯一入口。
    # 为 None 时表示纯内存上下文（无落库能力），persist 落库请求退化为仅写内存。
    store: RuntimeMessageStore | None = None
    # 当前绑定的 turn 标识：落库（add_message persist=True）需要它定位目标 turn；
    # 为 None 时表示尚未进入某 turn，落库请求退化为仅写内存。
    current_turn_id: str | None = None
    # 轮内逐条落库序号计数器：由 manager 内部维护（替代原 RuntimeOperations 内部计数），
    # reset_message_sequence 归零、add_message 落库时自增。
    _message_sequence: int = field(default=0, init=False)

    def attach_usage_meter(self, meter: ContextUsageMeter) -> None:
        """挂载上下文占用计量器。

        参数:
            meter: 已构造的 ``ContextUsageMeter`` 实例。
            仅对主 agent 有效，子 agent 不挂载。

        返回:
            无。

        异常:
            无。

        副作用:
            写入 ``usage_meter`` 字段；并立即标记一次脏（加载完成后的历史已计入）。
        """
        if not self.agent_profile.main_agent:
            return
        self.usage_meter = meter
        self.mark_context_changed()

    def mark_context_changed(self) -> None:
        """通知计量器上下文已变化（若已挂载）。

        说明:
            所有上下文变化（系统提示、历史加载、本轮增量、工具结果、压缩）最终都经过
            ``add_message`` / ``load_for_task`` / ``maybe_compact`` 等入口，统一在此通知
            计量器置脏标记；高频写入只置脏（O(1)），真正的估算在读取时按需、防抖执行。

        返回:
            无。

        异常:
            无。

        副作用:
            若 ``usage_meter`` 已挂载，调用其 ``mark_context_changed``。
        """
        if self.usage_meter is not None:
            self.usage_meter.mark_context_changed()

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

    def load_history(self) -> None:
        """从注入 store 读回 task 跨轮历史并写回内存（显式 I/O 入口）。

        经 ``store.build_for_task`` 读回 ``RuntimeMessage`` 列表，经
        :meth:`_build_history_messages` 转换为 ``BaseMessage`` 追加进 ``messages``。
        仅当 ``store`` 注入时有效；无 store（纯内存构造）时为空操作。

        参数:
            excluded_turn_ids: 需排除的 turn 标识元组。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: ``store.build_for_task`` 读取失败会透传。

        副作用:
            向 ``messages`` 追加历史转换结果；加载完成后写 info 日志并置脏计量器。
        """
        if self.store is None:
            return
        history = self.store.build_for_task(self.task_id)
        with self.lock:
            self.messages.extend(history)
        self.mark_context_changed()
        log.info(
            "runtime_context_loaded",
            extra={
                "msg": (
                    f"已加载 task 历史上下文，task_id={self.task_id} "
                ),
                "data": {
                    "task_id": self.task_id,
                    "message_count": len(history),
                },
            },
        )

    def create_for_child(
        self,
        task_id: str,
        agent_profile: AgentProfile | None = None,
        workspace_root: str | None = None,
    ) -> RuntimeContextManager:
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
        child = RuntimeContextManager(
            task_id=task_id,
            agent_profile=profile,
            workspace_root=root,
            parent_context=self,
        )
        # 只读历史快照：拷贝父消息（不含系统提示，子已自带），供 subAgent 参考上下文。
        with self.lock:
            history_snapshot = list(self.messages[1:])
        child.messages.extend(history_snapshot)
        child.mark_context_changed()
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
        self.mark_context_changed()
        return True

    def _build_system_message(self) -> RuntimeMessage:
        """构建系统提示消息。

        返回:
            含 agent 系统提示的 ``SystemMessage``。
        """
        system_prompt = SystemPromptBuilder.build(self.agent_profile, self.workspace_root)
        return self._langraph_message_to_runtime_message(SystemMessage(content=system_prompt))

    def _batch_convert_langraph_messages(self, message_list: list[RuntimeMessage]) -> list[BaseMessage]:
        """将运行时消息列表转换为 langchain 消息列表。

        说明:
            逐条委托 :meth:`_runtime_message_to_langraph_message` 完成单条转换，并跳过其
            返回 ``None`` 的消息（system / 未知 role，历史重放不包含系统提示）。如果
            ``ToolCall`` 没有对应的 ``Message`` 模型将异常（沿用既有约束）。

        参数:
            message_list: 从 ``turn_message_crud`` 读出的运行时消息列表。

        返回:
            转换后的 langchain 消息列表。

        异常:
            无。

        副作用:
            无（纯转换）。
        """
        converted: list[BaseMessage] = []
        for message in message_list:
            langchain_message = self._runtime_message_to_langraph_message(message)
            if langchain_message is not None:
                converted.append(langchain_message)
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
            return self._batch_convert_langraph_messages(self.messages)

    def add_message(
        self,
        message: BaseMessage | RuntimeMessage,
        *,
        persist: bool = True,
        write_memory: bool = True,
    ) -> None:
        """线程安全地向上下文追加一条消息（唯一写入入口），并按需落库/写内存。

        说明:
            三种输入形态统一收口，消除「``BaseMessage`` / ``RuntimeMessage`` 往返转换」
            与「启动基线专用方法」的重复：
            - ``BaseMessage``（模型节点产出、普通消息）：落库前经 :meth:`_to_runtime_message`
              转换（assistant tool_calls→JSON），写内存前经 :func:`sanitize_assistant_messages`
              归一化（content 空串占位、残缺 tool_calls 过滤、丢弃 ``invalid_tool_calls``）；
            - ``RuntimeMessage``（工具观察等已序列化消息）：直接落库原始形态，写内存经
              :meth:`_runtime_message_to_langraph_message` 转换，保留 ``tool_call_id`` 元数据链路；
            - ``str``（turn 启动用户基线文本）：构造 ``role="user"`` 的 ``RuntimeMessage``
              落库，写内存经 ``_runtime_message_to_langraph_message`` 转为 ``HumanMessage``。

            ``persist`` 控制落库、``write_memory`` 控制写内存，两者独立正交：
            - ``persist=True, write_memory=True``（默认）：完整双写；
            - ``persist=True, write_memory=False``：turn 启动基线专用——先把用户提问落库为
              ``role="user"`` 基线、不写内存，随后 :meth:`load_history` 从 DB 读回完整历史
              （含本基线），避免内存双写重复；
            - ``persist=False, write_memory=True``：仅写内存（运行时提示，如 repair 话术，
              无需重放）；
            - ``persist=False, write_memory=False``：空操作。

            ``persist=True`` 时采用「先落库、成功后写内存」的防撕裂语义：落库失败抛
            ``SQLAlchemyError`` 且内存不写（继承 tools_node 既有防撕裂语义）；落库成功后才
            写内存并自增序号。当 ``store`` 或 ``current_turn_id`` 未注入（纯内存构造 / 测试
            场景）时，``persist=True`` 退化为仅写内存——manager 无落库能力则无法持久化，
            内存形态仍是最终一致视图。

        参数:
            message: 待追加的消息。``BaseMessage``（langchain 形态）、``RuntimeMessage``
                （已序列化形态）或 ``str``（user 文本，构造 user 消息）。
            persist: 是否落库（默认 True）；False 时跳过落库。
            write_memory: 是否写内存（默认 True）；False 时仅落库（turn 启动基线场景）。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: ``persist=True`` 且已注入 store/current_turn_id
                时落库失败抛出，此时内存不写（防撕裂）。

        副作用:
            ``write_memory=True`` 时向 ``messages`` 追加（已归一化的）消息并置脏计量器；
            ``persist=True`` 且有落库能力时同步向 ``turn_messages`` 表写一行并自增序号。
        """
        runtime_message = message
        if not isinstance(runtime_message, RuntimeMessage):
            runtime_message:RuntimeMessage = self._langraph_message_to_runtime_message(message)

        if persist and self.store is not None and self.current_turn_id is not None:
            self.store.append(self.current_turn_id, runtime_message, self._message_sequence)
            self._message_sequence += 1

        if write_memory and runtime_message is not None:
            with self.lock:
                self.messages.append(runtime_message)
            self.mark_context_changed()

    def reset_message_sequence(self) -> None:
        """清空当前 turn 的消息轨迹并归零序号（turn 开始执行时调用，保证幂等）。

        经注入 ``store`` 清空当前 ``turn_id`` 在 ``turn_messages`` 表的全部残留并复位
        序号，之后每条消息经 :meth:`add_message` 自增落库；历史 turn 因按 ``turn_id``
        隔离不受影响。无 store / 无 current_turn_id
        时仅归零序号（纯内存）。

        返回:
            无。

        异常:
            sqlalchemy.exc.SQLAlchemyError: 清理失败时抛出（由底层 CRUD 透传）。

        副作用:
            删除当前 turn 在 ``turn_messages`` 表的全部行；``_message_sequence`` 归零。
        """
        if self.store is not None and self.current_turn_id is not None:
            self.store.clear(self.current_turn_id)
        self._message_sequence = 0

    def snapshot(self) -> list[BaseMessage]:
        """返回消息列表的一致性快照（独立拷贝）。

        与 :meth:`load_message` 语义一致，命名强调「一致性视图」用途，
        供模型调用在多线程下读取稳定视图。消息在 :meth:`add_message` 入口已归一化，
        此处仅做纯运输。

        返回:
            消息列表的独立拷贝。
        """
        with self.lock:
            return self._batch_convert_langraph_messages(self.messages)

    def _langraph_message_to_runtime_message(self, message: BaseMessage) -> RuntimeMessage:
        """将 langchain ``BaseMessage`` 转为内部 ``RuntimeMessage``（落库前转换收口）。

        ``AIMessage → RuntimeMessage`` 只此一处（收口自原 ``model_node._ai_to_runtime_message``）：
        assistant 的 ``tool_calls`` 以 **JSON 字符串** 存进 ``metadata["tool_calls"]``，与
        ``core.llm.langchain_bridge.tool_calls_from_metadata`` 的反序列化契约严格对齐（读取端
        ``json.loads``，故此处必须存字符串而非 list）。无工具调用时不写入该键。非 assistant
        消息（user / tool / system）按 role 与文本直接转换。

        参数:
            message: 待落库的 langchain ``BaseMessage``。

        返回:
            与模型无关的 ``RuntimeMessage``，供 ``store.append`` 落库。

        异常:
            无。

        副作用:
            无（纯转换）。
        """
        message = self._sanitize_assistant_messages(message)
        if isinstance(message, AIMessage):
            tool_calls = [
                {"name": call.get("name"), "args": call.get("args", {}), "id": call.get("id")}
                for call in (message.tool_calls or [])
            ]
            metadata: dict[str, Any] = (
                {"tool_calls": json.dumps(tool_calls, ensure_ascii=False)} if tool_calls else {}
            )
            return RuntimeMessage(
                role="assistant",
                content_text=message.content,
                metadata=metadata,
            )
        if isinstance(message, ToolMessage):
            return RuntimeMessage(
                role="tool",
                content_text=message.content,
                metadata={"tool_call_id": message.tool_call_id or ""},
            )
        if isinstance(message, SystemMessage):
            return RuntimeMessage(role="system", content_text=message.content)
        return RuntimeMessage(role="user", content_text=message.content)

    def _runtime_message_to_langraph_message(self, message: RuntimeMessage) -> BaseMessage | None:
        """将单条运行时消息转换为 langchain ``BaseMessage``（正向单条转换收口）。

        是 :meth:`_langraph_message_to_runtime_message` 的逆转换：user / assistant / tool
        三种 role 分别映射 ``HumanMessage`` / ``AIMessage`` / ``ToolMessage``；assistant 的
        ``metadata["tool_calls"]`` JSON 字符串经 :func:`_tool_calls_from_metadata` 反序列化
        回 langchain ``tool_calls``，与落库侧序列化契约严格对齐。system 与其他未知 role
        返回 ``None``（历史重放不包含系统提示，系统提示由构造期 :meth:`_build_system_message`
        构建，避免重复），由调用方（:meth:`_build_history_messages`）跳过。

        参数:
            message: 待转换的 ``RuntimeMessage``。

        返回:
            转换后的 ``BaseMessage``；system / 未知 role 返回 ``None``（跳过语义）。

        异常:
            无。

        副作用:
            无（纯转换）。
        """
        content_text = message.content_text if message.content_text is not None else ""
        if message.role == "user":
            return HumanMessage(content=content_text)
        if message.role == "assistant":
            tool_calls_meta = _tool_calls_from_metadata(message.metadata.get("tool_calls"))
            langchain_tool_calls = [
                {
                    "name": call["name"],
                    "args": call.get("args") if isinstance(call.get("args"), dict) else {},
                    "id": call.get("id") or "",
                }
                for call in tool_calls_meta
            ]
            return AIMessage(content=content_text, tool_calls=langchain_tool_calls)
        if message.role == "tool":
            return ToolMessage(
                content=content_text,
                tool_call_id=message.metadata.get("tool_call_id", ""),
            )
        if message.role == "system":
            return SystemMessage(content=content_text)
        return None

    def _sanitize_assistant_messages(self, message: BaseMessage) -> BaseMessage:
        """在消息进入上下文（入口守卫）前对 assistant 消息做最终清洗，避免脏字段回灌下一轮对话。

        重建后只保留安全的 ``content`` + 合法 ``tool_calls`` + ``id``。``ToolMessage`` 及其他
        角色不受影响（其 content=null 协议允许），保持原对象引用。

        参数:
            messages: 即将进入上下文的 LangChain 消息列表。

        返回:
            清洗后的新列表；非 assistant 类消息保持原对象引用不变。

        异常:
            无。

        副作用:
            无（不修改入参对象；仅在需要清洗的 assistant 消息时新建对象）。
        """

        if not isinstance(message, AIMessage):
            return message

        if message.content is None or message.content.strip() == "":
            message.content = "(ignore)"
            log.info("_sanitize_assistant_messages", extra={"msg": f"清洗消息", "data": {"message": message.model_dump()}})
        return message
