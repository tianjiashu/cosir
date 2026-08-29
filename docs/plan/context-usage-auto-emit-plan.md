# 上下文占用事件自动触发改造方案（messages 变更即 emit）

> 状态：**草案待审**（2026-08-20）。本文是改造方案，尚未落地。
> 目标：让 `CONTEXT_USAGE` 事件从「模型步开始手动快照」迁移为「上下文 messages 一旦变更（添加 / 压缩 / 裁剪）即自动触发」，同时保持高频写入场景的事件防抖与分层红线。
> 依据第零铁律：以「方便项目长期稳定迭代」为尺，允许结构性改动、消除逐点打补丁式的 emit 调用；但复用既有 `ContextUsageMeter` 的防抖机制，不重复造轮子。

---

## 1. 背景与目标

当前 `CONTEXT_USAGE`（上下文占用圆环）事件的触发时机是**模型步开始的手动快照**：

- `model_node.py` 在每步开头调用一次 `_emit_context_usage(step_id, task_id)`。
- `_emit_context_usage` 依赖 `meter.read(force=True)` 强制重算，随后 `write_event(CONTEXT_USAGE)` + `get_task_service().update_context_usage(...)` 回写 task。

问题：

1. **时机不覆盖「变更」**：上下文在 `add_message`（模型落库 / 工具结果 / repair 注入）、`maybe_compact`（压缩 / 裁剪）、`load_history`（历史加载）等入口变化时**并不立即发事件**，只有等到下一次模型步开始才快照。若模型已结束（终态），最后一次变更（如压缩后的裁剪）不会产生事件，圆环显示的占用可能过时。
2. **手动调用会漏**：每次新增一个「上下文变化入口」，都必须记得在 `model_node` 补一次手动 emit，否则语义断裂——这正是「逐点打补丁」的维护陷阱。
3. **依赖方向**：`model_node._emit_context_usage` 依赖 `_runtime_context()`（LangGraph 全局上下文）+ `write_event` + `get_task_service()`，把「事件发布」职责绑死在 workflows 层的节点里，`core/context` 层无法独立表达「上下文变了」。

本方案的目标：

1. 把「上下文变化」抽象成 `core/context` 层的**订阅机制**：`RuntimeContextManager` 在消息变化的统一收口（`mark_context_changed`）通知订阅者。
2. 事件发布（`write_event` + task 回写）作为**订阅者**由装配层注入，`core/context` 只依赖协议，不依赖事件流 / service / LangGraph 上下文——保持分层红线（`core → service` 反向、`core → 事件流` 污染均不出现）。
3. **复用既有防抖 + 值去重**：不新增间隔防抖实现，直接复用 `ContextUsageMeter.read(force=False)` 的「脏标记 + 最小间隔」语义抑制重算；emitter 用「值变化检测」过滤重复事件与重复 task 写。
4. 删除 `model_node` 的手动 `_emit_context_usage` 调用，事件自动随消息变更产生。

---

## 2. 现状代码事实（已逐条核对真实代码）

> 行号以当前工作区为准。

### 2.1 上下文变化入口（`mark_context_changed` 的调用点）

`core/context/runtime_context_manager.py` 的 `mark_context_changed()` 是「上下文变化」的**统一收口**（docstring 已声明），当前唯一消费者是挂载的 `usage_meter.mark_context_changed()`（置脏，O(1)）。调用点共 5 处：

| 入口 | 位置 | 变化来源 | 备注 |
|------|------|----------|------|
| `attach_usage_meter` | `runtime_context_manager.py:176` | 挂载计量器（历史已计入） | 装配期调用 |
| `load_history` | `runtime_context_manager.py:270` | DB 历史读回内存 | 装配期调用 |
| `create_for_child` | `runtime_context_manager.py:308` | subAgent 派生上下文 | child 装配期调用 |
| `maybe_compact` | `runtime_context_manager.py:348` | 压缩 / 裁剪替换 `messages` | 运行期调用 |
| `add_message` | `runtime_context_manager.py:421` | 模型落库 / 工具结果 / repair 注入 | **高频**，运行期调用 |

> 其中 `attach_usage_meter` / `load_history` / `create_for_child` 发生在 `graph.astream` **之前**（`workflow.py:198-230` 装配时序），彼时无 LangGraph 运行上下文，`get_stream_writer()` 会抛 `RuntimeError`。这是订阅者必须「无 writer 时容错」的关键依据（见 §4.3）。

### 2.2 计量器与防抖（复用对象）

`core/context/context_usage_meter.py` 的 `ContextUsageMeter`：

- `mark_context_changed()`：置脏标记，O(1)。
- `read(*, force=False)`：仅在「脏且超过最小间隔」时重算；`force=True` 忽略脏标记与间隔强制重算。
- 最小间隔由 `Settings.CONTEXT_USAGE_MIN_INTERVAL_S` 控制。
- `_compute()` 用 `message_provider`（即 `ctx.load_message`）实时快照估算 used_tokens，`_total_tokens()` 经 `resolve_context_window` 算 total。

> 该对象天然具备「防抖」能力：`mark_context_changed` 置脏后，`read(force=False)` 在最小间隔内返回缓存、超间隔才重扫消息。本方案**直接复用**，不新增防抖实现。

### 2.3 事件发布与回写（订阅者能力来源）

`model_node._emit_context_usage`（`core/workflows/nodes/model_node.py:105-155`）现状逻辑：

1. 取 `_runtime_context().usage_meter`；`meter is None`（子 agent / 未挂载）静默跳过。
2. `usage = meter.read(force=True)`；异常记日志跳过。
3. `usage is None` 记 warning 跳过。
4. `write_event(EventType.CONTEXT_USAGE, ContextUsagePayload(used_tokens, total_tokens))`。
5. `get_task_service().update_context_usage(task_id, used_tokens)`；异常记日志不中断。

依赖：
- `write_event`（`helper/common.py:33`）→ `get_stream_writer()`（**必须在 LangGraph 协程上下文内**）。
- `get_task_service()`（`app/service/depends`）。
- `_runtime_context()`（`helper/common.py:90`）→ `get_config()["configurable"]["runtime_context"]`。

### 2.4 装配点（注入位置）

`core/workflows/react/workflow.py:198-230`：

```python
runtime_context_manager = RuntimeContextManager(
    agent_profile=agent_profile, workspace_root=..., task_id=...,
    store=operations.message_store, current_turn_id=turn_id,
)
runtime_context_manager._reset_message_sequence()
runtime_context_manager.add_message(RuntimeMessage(role="user", ...), write_memory=False)
runtime_context_manager.load_history()
runtime_context_manager.attach_usage_meter(ContextUsageMeter(
    message_provider=runtime_context_manager.load_message,
    model_name_provider=lambda: turn.model_name or agent_profile.model_name or "",
))
```

- 装配期（graph.astream 前）会触发 `attach_usage_meter` / `load_history` / `add_message` 三次变化通知。
- 子 agent（委派 child，`parent_turn_id` 非空）**不挂载** usage_meter（`meter` 保持 None），现 `_emit_context_usage` 会静默跳过，本方案需保持该语义。

### 2.5 相关测试（现状盘点——影响面远大于初稿预估）

`model_node._emit_context_usage` 是**多套测试直接调用的被测对象**，删除/迁移它影响面很大，须完整盘点。涉及 4 个测试文件 + 1 处注释引用：

| 文件 | 对 `_emit_context_usage` 的直接调用 | 锁定的 fail-safe 契约 |
|------|--------------------------------------|------------------------|
| `tests/test_context_usage.py` | `:388`、`:409`（2 处，mock `mn._runtime_context`） | meter 未挂载静默跳过；read 抛异常记日志跳过 |
| `tests/test_task_context_usage.py` | `:141`、`:159`（2 处，mock `model_node.get_task_service`/`_runtime_context`/`write_event`） | 合法 usage 回写 task；回写失败只记日志、事件仍发出 |
| `tests/test_task_context_usage_regression.py` | `:188`、`:214`、`:237`、`:261`、`:262`、`:289`、`:290`（7 处） | ① `meter.read(force=True)` 被调用；② `used_tokens=0` 是合法读数须发事件回写 0、不得判空；③ `meter.read` 返回 None 记 `context_usage_meter_empty` WARNING 跳过；④ None 后恢复无状态残留；⑤ `ContextUsage` 负值被值对象拦截时捕获记 `context_usage_meter_failed` ERROR |
| `tests/test_task_context_usage_adversarial.py` | `:269`、`:283`、`:297`、`:316`、`:336`、`:356`、`:375`、`:394`（8 处） | ⑥ meter 未挂载 `meter_none_is_safe_noop`；⑦ read 返回 None 只记 WARNING；⑧ read 抛异常只记 ERROR；⑨ 合法 usage 事件 payload 字段精确 + 真实落库；⑩ 回写失败 `context_usage_task_persist_failed` ERROR 事件仍发；⑪ 空/None task_id 不崩溃事件仍发 |
| `tests/test_react_workflow_graph.py` | `:98-100`（**注释引用**，非调用） | `usage_meter` 字段置 None 的假上下文，注释说明 `_emit_context_usage` 会静默跳过 |

> 合计 **19 处直接调用 + 1 处注释引用**。其中 `test_task_context_usage_regression.py:189` 明确断言 `meter.read.assert_called_once_with(force=True)`——这决定了 emitter 的核心行为变更必须显式化（见 §4.4 与 §7）。

相关但**不受影响**的测试：

- `test_context_usage_event_registered` / `test_context_usage_payload_is_exported` / `test_context_usage_in_ts_contract`：payload 未变，保持通过。
- `test_runtime_context_attach_meter_marks_dirty`：`mark_context_changed` 签名变更（加 `reason`），需同步更新调用（见 §7）。

---

## 3. 问题清单

1. **事件时机不覆盖「变更」**：上下文在 `add_message` / `maybe_compact` 等入口变化时不发事件，只等下一次模型步开始；终态前最后一次变更（如压缩裁剪）可能无事件。
2. **手动调用会漏**：每新增一个上下文变化入口，都要在 `model_node` 补手动 emit，是逐点打补丁的维护陷阱，违反「改动聚焦」与长期迭代目标。
3. **职责归属错误**：事件发布（`write_event` + task 回写）是「行为」，却绑死在 workflows 层节点；`core/context` 无法独立表达「上下文变了」，分层边界被破坏。
4. **重复造轮子风险**：若在订阅者里另写一套防抖，将与 `ContextUsageMeter` 的间隔防抖重复（Rule of Three 的反例）。

---

## 4. 目标设计

### 4.1 新增协议 `ContextChangeListener`

新文件 `core/context/context_change_listener.py`：

```python
from typing import Protocol


class ContextChangeListener(Protocol):
    """订阅运行时上下文变化，在 ``messages`` 变更后收到通知。

    实现方（如 ``ContextUsageEventEmitter``）负责决定是否真正执行代价高的动作
    （估算 / 发事件 / 回写），可内部去重。manager 不感知实现细节，仅保证
    在 ``mark_context_changed`` 时通知所有已订阅 listener。
    """

    def on_context_changed(self, reason: str) -> None:
        """上下文已变化。

        参数:
            reason: 变化来源标识（如 ``"add_message"`` / ``"maybe_compact"`` /
                ``"load_history"`` / ``"create_for_child"`` / ``"attach_usage_meter"``），
                供实现方与排查日志使用。
        """
        ...
```

### 4.2 `RuntimeContextManager` 增加订阅与 reason 透传

- 新增字段 `_listeners: list[ContextChangeListener] = field(default_factory=list, init=False)`（dataclass 非构造参数）。
- 新增方法 `add_change_listener(listener)`：追加订阅者；若 `listener` 是 `ContextUsageEventEmitter`，同时记录只读属性 `context_usage_emitter`（供 `model_node` 终态 `force_flush` 取用，见 §4.5 方式 A；`None` 表示未挂载）。
- `mark_context_changed` 增加必填参数 `reason: str`，在置脏 `usage_meter` 的同时遍历 `_listeners` 通知 `on_context_changed(reason)`。
- 5 个变化入口分别传入 reason：`attach_usage_meter`→`"attach_usage_meter"`、`load_history`→`"load_history"`、`create_for_child`→`"create_for_child"`、`maybe_compact`→`"maybe_compact"`、`add_message`→`"add_message"`。

> 注：现有 `mark_context_changed()` 无参、`attach_usage_meter` / `load_history` / `create_for_child` / `maybe_compact` / `add_message` 五处调用。因签名变更（加必填 `reason`），需同步更新 5 处内部调用 + 测试。为保持最小侵入，也可给 `reason` 默认 `""`，但第零铁律倾向显式语义，故设计为必填（见 §6 测试调整）。

### 4.3 新增订阅者 `ContextUsageEventEmitter`

新文件 `core/context/context_usage_event_emitter.py`：

```python
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.config.logging.logger import log
from app.core.context.context_listener.context_listener import ContextListener
from app.core.context.context_usage_meter import ContextUsageMeter
from app.models.enums.event_type import EventType
from app.models.payload import ContextUsagePayload
from app.models.payload.runtime_event_payload import RuntimeEventPayload


class ContextUsageEventEmitter:
   """上下文占用事件订阅者：消息变更后发出 ``CONTEXT_USAGE`` 并回写 task。

   实现 ``ContextChangeListener`` 协议。关注点拆分（不重复造轮子）：
   - **重算防抖**：委托 ``ContextUsageMeter.read(force=False)``（其「脏标记 + 最小间隔」
     抑制全量重算，见 ``context_usage_meter.py``）；
   - **事件/回写去重**：本 emitter 用「值变化检测」——仅在 ``used_tokens`` 较上次
     实际变化时 emit + persist，过滤高频变更产生的「相同值重复事件/重复 task 写」。

   依赖注入（不直接 import service / LangGraph 全局上下文）：
   - ``meter``: 已挂载到 manager 的计量器（复用其防抖与估算）。
   - ``task_id``: 回写目标任务。
   - ``emit``: 事件写入回调。装配期（graph 外）无 LangGraph 上下文时，
     ``get_stream_writer()`` 会抛 ``RuntimeError``，故采用**惰性捕获**：
     首次在 graph 上下文内调用时才解析 writer；捕获失败（无上下文）仅记日志跳过，
     不中断主流程（与现状 ``_emit_context_usage`` 的容错语义一致）。
   """

   def __init__(
           self,
           *,
           meter: ContextUsageMeter,
           task_id: str,
           emit: Callable[[EventType, RuntimeEventPayload], bool],
           persist_usage: Callable[[str, int], None],
   ) -> None:
      ...
      self._meter = meter
      self._task_id = task_id
      self._emit = emit
      self._persist_usage = persist_usage
      self._last_used_tokens: int | None = None  # 值去重：上次已 emit 的 used_tokens

   def _emit_and_persist(self, usage: ContextUsage) -> bool:
      """发布事件并回写 task；返回事件是否真正发出（供值去重状态更新判据）。

      返回 ``True`` 表示事件已真正写入事件流（writer 捕获成功）；``False`` 表示
      emit 被容错丢弃（如装配期无 LangGraph 上下文），此时调用方**不得**更新值去重
      基线，避免运行期首次真实变更被误去重。
      """
      emitted = self._emit(EventType.CONTEXT_USAGE, ContextUsagePayload(
         used_tokens=usage.used_tokens, total_tokens=usage.total_tokens,
      ))
      try:
         self._persist_usage(self._task_id, usage.used_tokens)
      except Exception:
         log.exception("context_usage_task_persist_failed", extra={"msg": ...})
      return emitted

   def on_context_changed(self, reason: str) -> None:
      """消息变更后调用：重算防抖 + 值去重后发布事件（容错跳过，不中断主流程）。"""
      try:
         usage = self._meter.read(force=False)
      except Exception:
         log.exception("context_usage_meter_failed", extra={"msg": ...})
         return
      if usage is None:
         log.warning("context_usage_meter_empty", extra={"msg": ...})
         return
      # 值去重：高频变更在防抖间隔内返回缓存（used_tokens 相同），此时不重复 emit/persist，
      # 避免事件风暴与 task 表高频写；仅当实际占用变化时才发布。
      if usage.used_tokens == self._last_used_tokens:
         return
      # 仅在事件**真正发出**后更新去重基线：若 emit 被 writer 容错丢弃（如装配期
      # 无 LangGraph 上下文），基线保持不变，运行期首次真实变更不会被误去重。
      if self._emit_and_persist(usage):
         self._last_used_tokens = usage.used_tokens

   def force_flush(self) -> None:
      """终态前强制重算并发布（忽略防抖与值去重，确保最终占用落定）。"""
      try:
         usage = self._meter.read(force=True)
      except Exception:
         log.exception("context_usage_meter_failed", extra={"msg": ...})
         return
      if usage is None:
         log.warning("context_usage_meter_empty", extra={"msg": ...})
         return
      # 终态调用必在 graph 上下文内（writer 可用），emit 必成功，可直接更新基线。
      if self._emit_and_persist(usage):
         self._last_used_tokens = usage.used_tokens
```

> **`emit` 回调返回契约**：`emit` 必须返回 `bool`——`True` 表示事件已真正写入事件流（writer 捕获成功），`False` 表示被容错丢弃（如装配期无 LangGraph 上下文）。emitter 依赖该返回值决定是否更新值去重基线（见 §4.3 `_emit_and_persist`）。
>
> **无 LangGraph 上下文时的 writer 捕获**：`emit` 回调的惰性解析应放在「装配期不调用、首个 graph 上下文内调用」的路径。设计两种注入方式：
> - **方式 A（推荐）**：`emit` 由装配层传入一个「惰性回调」，内部首次调用时用 `_make_write_event()` 捕获 writer，失败（无上下文）返回 `False` 跳过；但 `write_event` 无法跨协程持久持有 LangGraph writer，故**装配期注入的回调必须能容忍「无上下文」**——用 try/except RuntimeError 包住 `_make_write_event()`，捕获失败记日志并返回 `False`。
> - **方式 B**：`emit` 直接传 `helper/common._make_write_event` 的调用封装，由订阅者在 graph 上下文内首调时捕获。
>
> 两方式本质相同：**订阅者必须对「无 LangGraph 上下文」容错**。推荐方式 A 在装配层显式构造，把「writer 惰性捕获」的细节收口在装配处，订阅者只依赖注入的 `emit` 回调。
>
> **已知取舍**：装配期（`load_history` / `attach_usage_meter`）触发 `on_context_changed` 时无 writer，`meter.read(force=False)` 会重算一次但 emit 返回 `False` 被跳过——产生一次「计算但未发出」的估算开销。该开销是 O(消息条数) 的字符扫描、装配期仅一次，可接受。**关键防护**：因 emit 返回 `False`，`_last_used_tokens` **不被更新**，运行期首次真实变更不会被误去重（见 §4.3 代码注释与 §7.1 值去重测试第 3 条的装配期污染用例）。若需完全避免装配期重算，可在装配层「先挂 meter、后加 listener」，但会牺牲 `attach_usage_meter` 初始脏标记的语义，权衡后**不采用**。

### 4.4 关键行为变更：`read(force=True)` → `read(force=False)` + 值去重

原 `model_node._emit_context_usage` 用 `meter.read(force=True)`（**每次强制重算，无防抖**）——它只在「每步开始」被低频调用，无需防抖。现改为「每次变更即 emit」，若沿用 `force=True` 会在高频 `add_message` 下产生全量重算风暴。

故 emitter 的 `on_context_changed` 用 `meter.read(force=False)`（**复用 meter 既有「脏标记 + 最小间隔」防抖**，见 `context_usage_meter.py:77-94`）抑制重算，并用**值去重**（`_last_used_tokens` 相同则跳过）抑制重复 emit/persist。这是一次**有意的行为变更**：

| 维度 | 旧（步开始手动快照） | 新（变更自动触发） |
|------|----------------------|-------------------|
| 触发时机 | 每模型步开始一次 | 每次 `add_message` / `maybe_compact` 等变化 |
| read 模式 | `force=True` 每次重算 | `force=False` 脏 + 超最小间隔才重算（重算防抖） |
| 事件/回写 | 每步一次 | 每次变更触发，但**值未变化的重复变更被去重** |
| 防抖职责 | 无 | 重算防抖归 meter（`read(force=False)`）；事件/回写去重归 emitter（值变化检测） |

> **关注点拆分（不重复造轮子）**：meter 的 `read(force=False)` 防抖**重算**（昂贵的全量扫消息）；emitter 的值去重防**重复事件/重复 task 写**。两者职责不同、各司其职，不是两套重复的「间隔防抖」。
>
> **值去重的边界**：若两次变更间 `used_tokens` 恰好相同（如新增一条估算 token 数为 0 的消息），会漏发一次事件。这是可接受取舍——前端圆环只关心占用值变化，值未变时重复发相同值的事件无意义。
>
> 该变更必须同步到测试断言：`test_task_context_usage_regression.py:189` 的 `meter.read.assert_called_once_with(force=True)` 需改为 `force=False`（见 §7）。这是改造的**核心语义变化**，非「保持原行为」，须在实现与测试中显式化。

### 4.5 终态 force 快照与 emitter 访问路径

场景：`maybe_compact` 在 run 末尾裁剪后，若无后续模型步，最后一次变更虽已触发 `on_context_changed`（防抖 read 会更新缓存并 emit），但因为 `read(force=False)` 在间隔内可能返回旧缓存，终态时占用可能滞后一格。

为满足「变更即 emit」且保证最终准确，可让 `model_node` 终态（`RUN_FINISHED` / `RUN_FAILED` / `RUN_CANCELLED`）前调用 emitter 的 `force_flush()`（`meter.read(force=True)` + emit + 回写，**忽略防抖与值去重**），确保最后一次占用落定。

> **与值去重的关系**：`force_flush` 强制 emit，可能与运行期防抖事件产生「最后一次重复写」。这是可接受的——终态回写的是最终准确占用，重复一次不影响 task 表最终一致；且 `force_flush` 会更新 `_last_used_tokens`，后续（若有）`on_context_changed` 不再重复同值。

**访问路径**：`model_node` 经 `_runtime_context()`（`helper/common.py:90`）只能拿到 `RuntimeContextManager`，无法直接访问 emitter 实例。故需在 manager 暴露访问器。二选一：

- **方式 A（推荐）**：manager 新增只读属性 `context_usage_emitter`，在 `add_change_listener` 时若 listener 是 `ContextUsageEventEmitter` 则记录引用。`model_node` 经 `_runtime_context().context_usage_emitter` 取用；`None`（子 agent / 未挂载）时跳过。
- **方式 B**：装配层把 emitter 独立注入 `model_node`（经 `RuntimeConfig`），节点从 `rc` 取。破坏「emitter 归属 manager 订阅」的单一事实，不推荐。

> `force_flush()` 为**可选增强**，仅影响终态占用精度，不影响「变更即 emit」的核心目标，可在阶段 1 之后按需补充（见 §6 阶段 2）。若不做，终态占用最多滞后一个防抖间隔（`CONTEXT_USAGE_MIN_INTERVAL_S`），可接受。

---

## 5. 分层与职责

### 5.1 依赖方向（单向 DAG 保持）

```
core/context/RuntimeContextManager
   ├── 依赖协议 ContextChangeListener（leaf，同目录）
   ├── 依赖 ContextUsageMeter（同目录，已存在）
core/context/ContextUsageEventEmitter
   ├── 依赖 ContextChangeListener / ContextUsageMeter / ContextUsagePayload（leaf）
   └── 依赖注入的 emit / persist_usage 回调（不 import service / LangGraph 上下文）
装配层 workflow.py
   └── 构造 emitter，注入 emit（内部惰性捕获 writer）+ persist_usage（经 get_task_service）
```

- `core/context` **不反向依赖** service / 事件流 / LangGraph 全局上下文。
- `ContextUsageEventEmitter` 的所有外部能力（writer、task 回写）都经**注入回调**进入，符合项目既有「协议 + 注入」模式（同 `ToolTraceRecorder` / `TurnRunner`）。
- `model_node` 不再持有事件发布逻辑，只保留终态前的 `force_flush` 钩子（若启用 4.5）。

### 5.2 单一职责

- `RuntimeContextManager`：管消息读写 + 变化通知（新增），**不负责**事件发布。
- `ContextUsageEventEmitter`：把「上下文变化」翻译成占用事件 + 回写，含**值去重**（过滤重复事件/重复写），**不碰**消息读写。
- `ContextUsageMeter`：只做估算与**重算防抖**，**不碰**事件、不做去重。

---

## 6. 分阶段实施

### 阶段 1：订阅机制 + 事件自动触发（核心）

1. 新增 `core/context/context_change_listener.py`（协议）。
2. `RuntimeContextManager`：加 `_listeners` + `add_change_listener`；`mark_context_changed(reason)` 通知 listener；5 个入口传 reason。
3. 新增 `core/context/context_usage_event_emitter.py`（订阅者，复用 meter 防抖 + 惰性 writer 容错）。
4. 装配点 `workflow.py`：构造 emitter（注入 emit + persist_usage），`runtime_context_manager.add_change_listener(emitter)`；**仅在 `meter` 非 None（主 agent）时挂载**，子 agent 不挂载（保持现语义）。
5. `model_node.py`：删除手动 `_emit_context_usage` 调用与模块级函数（逻辑已迁移到 emitter）。

### 阶段 2：终态 force 快照（增强，可按需）

6. emitter 增加 `force_flush()`（`meter.read(force=True)` + emit + 回写）。
7. `model_node` 终态分支（`RUN_FINISHED` / `RUN_FAILED` / `RUN_CANCELLED`）前经 `_runtime_context().context_usage_emitter`（见 §4.5 方式 A）调用一次 `force_flush()`；emitter 为 `None`（子 agent / 未挂载）时跳过。

---

## 7. 测试计划

### 7.1 新增测试（建议新建 `tests/test_context_change_listener.py` 承载协议/通知/防抖，`tests/test_context_usage_event_emitter.py` 承载 emitter 契约）

1. **协议与通知**：`mark_context_changed(reason)` 触发已订阅 listener 的 `on_context_changed(reason)`（reason 透传正确）；未订阅时无副作用。
2. **事件自动触发**：构造 manager + meter + 假 emitter（记录 `on_context_changed` 调用次数），`add_message` / `maybe_compact` 各触发一次；manager 暴露 `context_usage_emitter` 属性（挂载后非 None、未挂载为 None）。
3. **值去重**：同一 `used_tokens` 的重复变更只 emit/persist 一次；`used_tokens` 变化后再次 emit。模拟高频 `add_message`（间隔内返回缓存同值），emitter 只发一次事件、只写一次 task（验证「值去重」抑制事件风暴与 task 高频写）。
3a. **装配期污染防护（关键边界）**：模拟「装配期触发（`emit` 返回 `False`，无 writer）+ 运行期首次同值变更」——断言装配期的 emit 失败**不更新** `_last_used_tokens`，运行期首次真实变更不被误去重、事件正常发出。这是对 §4.3 已知取舍的回归保护。
4. **emitter 契约（完整迁移 `_emit_context_usage` 的 fail-safe 语义）**：
   - meter `read(force=False)` 被调用（注意：**force 由 True 改为 False**，见 §4.4）。
   - `read` 返回 `None`：记 `context_usage_meter_empty` WARNING、不发事件、不回写；且 `used_tokens=0` 是合法读数、**不得判空**。
   - `read` 抛异常 / `ContextUsage` 负值被值对象拦截：记 `context_usage_meter_failed` ERROR、跳过、后续正常读数无状态残留（`recovers_after_none_read` 语义）。
   - 合法 usage：发 `CONTEXT_USAGE`（payload.used_tokens / total_tokens 精确）+ `persist_usage(task_id, used)` 被调。
   - `persist_usage` 抛异常：记 `context_usage_task_persist_failed` ERROR、**事件仍发出**、不中断。
   - 空 / `None` task_id：回写抛异常时事件仍发出、不崩溃。
   - emit（writer 捕获）失败（无 LangGraph 上下文）：容错跳过、不中断。
5. **终态 force**（阶段 2）：`force_flush()` 强制重算（`read(force=True)`）并 emit + 回写（忽略值去重）；`model_node` 终态分支经 `_runtime_context().context_usage_emitter` 调用一次，emitter 为 None 时跳过。
6. **子 agent 语义**：`meter` 为 None 时（child），装配层不挂 emitter，manager `context_usage_emitter` 为 None，不产生变化通知副作用。
7. **TS 契约 / payload 注册**：既有 `test_context_usage_event_registered` / `_payload_is_exported` / `_in_ts_contract` 保持通过（payload 未变）。

### 7.2 需同步调整的既有测试（完整迁移清单）

以下 4 个文件共 **19 处**直接调用 `model_node._emit_context_usage`，改造后该函数删除，测试对象改为 `ContextUsageEventEmitter`（patch 目标从 `model_node._runtime_context` / `model_node.write_event` / `model_node.get_task_service` 改为 emitter 的注入依赖 `emit` / `persist_usage` / `meter`）：

| 文件 | 用例（行号） | 迁移要点 |
|------|--------------|----------|
| `tests/test_context_usage.py` | `test_emit_context_usage_skips_when_meter_absent`（`:388`）、`test_emit_context_usage_swallows_meter_error`（`:409`） | 改测 emitter：meter 未挂载不构造 emitter / `read` 抛异常记日志跳过 |
| `tests/test_task_context_usage.py` | `test_emit_context_usage_writes_back_task`（`:141`）、`test_emit_context_usage_service_failure_only_logs`（`:159`） | 合法 usage 回写 task；回写失败事件仍发 |
| `tests/test_task_context_usage_regression.py` | 7 处（`:188/:214/:237/:261/:262/:289/:290`） | **`:189` 的 `meter.read.assert_called_once_with(force=True)` 改 `force=False`**；`used_tokens=0` 合法不判空；None 记 WARNING 跳过；None 后恢复无残留；负值被值对象拦截 |
| `tests/test_task_context_usage_adversarial.py` | 8 处（`:269/:283/:297/:316/:336/:356/:375/:394`） | meter 未挂载 noop；read 返回 None 仅 WARNING；read 抛异常仅 ERROR；合法 payload 精确 + 真实落库；回写失败事件仍发；空/None task_id 不崩 |
| `tests/test_react_workflow_graph.py` | `:98-100`（注释引用，无需改） | 假上下文 `usage_meter=None`，注释提及 `_emit_context_usage` 需同步改指 emitter 语义（可选） |

**关联签名变更**：

- `test_runtime_context_attach_meter_marks_dirty`（`tests/test_context_usage.py:256`）：`mark_context_changed` 加必填 `reason`，该测试若间接调用需同步；更多是 `add_message` 触发通知，需补 `add_change_listener` 断言。
- `mark_context_changed` 的 5 个内部调用点（`runtime_context_manager.py`）传 `reason`，如有直接调用 `mark_context_changed()` 的测试需补参。

**值去重对既有测试迁移的影响（新增注意）**：迁移到 emitter 后，凡是用 `MagicMock` 的 `meter.read.return_value` 返回**固定值**的测试，第二次调用 `on_context_changed` 会因值去重（`used_tokens` 相同）被跳过——如 `test_task_context_usage_regression.py` 的 `test_emit_context_usage_recovers_after_none_read`（`:250`，两次调用期望发 1 次事件）。此类测试迁移时需改用 `meter.read.side_effect`（依次返回 None → 新值），使第二次 `used_tokens` 变化以验证「None 后恢复」语义；凡期望「每次变更都 emit」的既有断言，需显式改为「值变化才 emit」并让 mock 返回递增的 `used_tokens`。

### 7.3 明确「保持通过」的测试（不受影响）

- `test_context_usage_event_registered` / `test_context_usage_payload_is_exported` / `test_context_usage_in_ts_contract`（payload 未变）。
- `ContextUsageMeter` 自身的 `read(force=False)` 防抖测试（`test_context_usage.py:147/:178`）——emitter 只是消费它，不改变 meter 行为。

---

## 8. 验收标准

1. `CONTEXT_USAGE` 事件在 `add_message` / `maybe_compact` / `load_history` 等消息变更时自动产生（不再是「仅模型步开始」）。
2. 高频 `add_message` 场景：重算被 `ContextUsageMeter` 防抖（复用 `CONTEXT_USAGE_MIN_INTERVAL_S`）；占用值未变化的重复变更被 emitter 值去重，不重复 emit / 不高频写 task。
3. `core/context` 不依赖 service / 事件流 / LangGraph 全局上下文（依赖方向经 `search_content` 核对无反向）。
4. 子 agent / 未挂载 meter 场景静默跳过，行为与现状一致；`context_usage_emitter` 为 None 不产生副作用。
5. **既有 19 处 `_emit_context_usage` 调用测试全部迁移**到 `ContextUsageEventEmitter`（§7.2 清单逐条核对），fail-safe 契约无遗漏；`force=True → force=False` 行为变更在测试断言中显式化。
6. 既有测试全绿；新增测试覆盖协议 / 通知 / 值去重 / 重算防抖 / 容错 / 终态 force。
7. 无新增第三方依赖；所有新函数具备完整 docstring。
8. 通过独立审查 Agent + 独立测试 Agent 闭环（见 §9）。

---

## 9. 独立审查与测试闭环（遵循开发-审查-测试规范）

- 开发完成后启动**独立审查 Agent**（对照《Agent代码开发规范》逐条检查：单一职责 / 依赖方向 / 防抖复用 / docstring / 日志 / 禁止行为）。
- 同步启动**独立测试 Agent**（执行业务逻辑层单测，覆盖边界：空消息 / 高频变更 / 无 writer / 子 agent / 异常路径）。
- 审查或测试不通过 → 修复 → 重新审查 + 测试，直到两者均通过。
- 开发 Agent 不自宣「代码完成」，必须有审查与测试的通过结论。
