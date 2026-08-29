# Remove Runtime Context Messages Projection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 删除 `RuntimeContextManager.messages` 这份冗余的完整列表投影，让 system/history/active entries 成为唯一内存事实源，同时保持模型调用、usage 统计、压缩和 turn 恢复行为不变。

**Architecture:** `RuntimeContextManager` 只维护 `_system_entry`、`_history_entries` 和 `_active_entries`。模型节点通过现有 `load_message()` 获取 LangChain 消息；需要运行时消息快照的内部逻辑直接从 entries 生成临时列表，不再写回或缓存 `messages`。上下文变化通知只负责传递快照和聚合 usage，不再通过 listener 反向修改 manager 状态。

**Tech Stack:** Python 3.11+, dataclass, LangChain messages, pytest, Ruff, mypy strict。

**Spec:** 本方案基于当前需求：绿地项目，不保留 `RuntimeContextManager.messages` 兼容字段；运行时上下文的归属仍由 `ContextEntry` 表达。

## Global Constraints

- 不保留 `messages` 字段、属性、别名或兼容 getter。
- `_system_entry`、`_history_entries`、`_active_entries` 是 manager 内部唯一的上下文状态源。
- `load_message() -> list[BaseMessage]` 是模型调用方的唯一公开读取出口。
- `RuntimeMessageStore` 和 `turn_messages` 持久化协议不因本次删除投影而改变。
- 不修改 `apps/desktop/**` 或 `apps/shared/**`。
- 新增或修改的 Python 代码必须带完整类型注解、中文四段式 docstring，并通过 Ruff。
- 执行阶段先更新测试，再修改生产代码；本方案本身不创建 Git commit。

---

### Task 1: 建立唯一上下文状态源的测试基线

**Files:**

- Modify: `apps/backend/tests/test_context_usage_listener.py`

**Interfaces:**

- 验证对象：`RuntimeContextManager.load_message()`、`begin_turn()`、`add_message()`、`maybe_compact()`。
- 生产实现仍由 `_system_entry`、`_history_entries`、`_active_entries` 组成有效上下文。

- [ ] **Step 1: 写失败测试，确认 `messages` 字段不再属于公共状态**

```python
def test_manager_has_no_messages_projection() -> None:
    manager = RuntimeContextManager(
        task_id=1,
        agent_profile=_profile(main_agent=True),
        total_tokens=100,
    )

    assert not hasattr(manager, "messages")
    assert [message.type for message in manager.load_message()] == ["system"]
```

- [ ] **Step 2: 写失败测试，确认 entries 变更仍完整反映到模型读取出口**

```python
def test_load_message_reads_system_history_and_active_entries() -> None:
    store = _TurnScopedMemoryMessageStore(
        {10: [RuntimeMessage(role="user", content_text="history")]}
    )
    manager = RuntimeContextManager(
        task_id=1,
        agent_profile=_profile(main_agent=True),
        total_tokens=100,
        store=store,
        current_turn_id=20,
    )
    manager.load_history()
    manager.add_message(RuntimeMessage(role="user", content_text="active"), persist=False)

    contents = [message.content for message in manager.load_message()]
    assert contents[1:] == ["history", "active"]
```

这两个测试直接追加到现有 `test_context_usage_listener.py`，复用该文件已经定义的 `_profile()` 和 `_TurnScopedMemoryMessageStore()` 测试夹具，不新建第二套 store 或 profile 工厂。

- [ ] **Step 3: 运行测试确认当前实现失败**

Run:

```powershell
uv run pytest tests/test_context_usage_listener.py -q
```

Expected: 新增的 `not hasattr(manager, "messages")` 断言失败，证明测试确实约束了删除行为。

- [ ] **Step 4: 将旧测试中的 `manager.messages` 读取改为真实公开出口**

把 `tests/test_context_usage_listener.py` 中的以下断言迁移到 `manager.load_message()` 或测试专用的 token 计算辅助函数：

```python
manager.messages[0].estimate_tokens()
len(manager.messages)
```

测试不再依赖 manager 的内部列表字段；对于 usage 预期值，使用同一条 `RuntimeMessage` 的 token 估算或事件 payload 断言。

- [ ] **Step 5: 重新运行测试，记录仅由生产代码导致的失败**

Run:

```powershell
uv run pytest tests/test_context_usage_listener.py -q
```

Expected: 测试文件不再因访问 `manager.messages` 失败；唯一剩余失败来自尚未移除的生产字段或相关实现。

---

### Task 2: 从 RuntimeContextManager 删除 `messages` 投影

**Files:**

- Modify: `apps/backend/app/core/context/runtime_context_manager.py:126, 407-418, 486-507, 517-545, 578-584, 650-663`
- Test: `apps/backend/tests/test_context_usage_listener.py`

**Interfaces:**

- 保留：`load_message() -> list[BaseMessage]`。
- 保留：私有 `_effective_entries() -> list[ContextEntry]` 和 `_effective_runtime_messages() -> list[RuntimeMessage]`，它们只生成临时快照，不保存到实例字段。
- 删除：`messages` dataclass 字段和 `_sync_messages()` 方法。

- [ ] **Step 1: 删除 dataclass 中的 `messages` 字段**

移除：

```python
messages: list[RuntimeMessage] = field(default_factory=list)
```

`__post_init__()` 不再从外部传入列表建立 entries，而是直接构造 system entry：

```python
def __post_init__(self) -> None:
    """构造 task 级 system entry 并初始化轮内序号。"""
    self._system_entry = ContextEntry(self._build_system_message(), None)
```

同时保持 `_history_entries` 和 `_active_entries` 的 dataclass 默认空列表语义。

- [ ] **Step 2: 删除 `_sync_messages()` 并替换所有写入调用点**

删除：

```python
def _sync_messages(self) -> None:
    self.messages = [entry.message for entry in self._effective_entries()]
```

移除 `begin_turn()`、`upsert_current_user_message()`、`load_history()`、`maybe_compact()`、`add_message()` 中的 `_sync_messages()` 调用。entries 的增删本身已经是唯一状态更新，不需要再同步第二份列表。

- [ ] **Step 3: 改造所有模型上下文读取点**

`load_message()` 直接从有效 runtime snapshot 转换：

```python
def load_message(self) -> list[BaseMessage]:
    """线程安全地读取当前有效上下文的 LangChain 消息拷贝。"""
    with self.lock:
        return self._batch_convert_langraph_messages(self._effective_runtime_messages())
```

`maybe_compact()` 的 compressor 输入改为：

```python
compressed_messages = self.compressor.compact(self._effective_runtime_messages())
```

这样压缩器不再依赖已删除的缓存字段。

- [ ] **Step 4: 运行上下文和 usage 回归测试**

Run:

```powershell
uv run pytest tests/test_context_usage_listener.py -q
```

Expected: entries 的历史加载、当前 turn 添加、fresh/resume、usage 统计和多模态 user 消息测试通过。

- [ ] **Step 5: 运行静态检查**

Run:

```powershell
uv run ruff check app/core/context/runtime_context_manager.py tests/test_context_usage_listener.py
uv run mypy --config-file mypy.strict.ini app/core/context/runtime_context_manager.py
```

Expected: Ruff 无错误；mypy 不出现由本次删除字段引入的新错误。

---

### Task 3: 让压缩和监听器不再反向写入已删除状态

**Files:**

- Modify: `apps/backend/app/core/context/runtime_context_manager.py:361-403, 517-545`
- Modify: `apps/backend/app/core/context/context_listener/listener_result.py`
- Modify: `apps/backend/app/core/context/context_listener/context_listener.py`
- Modify: `apps/backend/app/core/context/context_listener/context_compress_listener.py`
- Test: `apps/backend/tests/test_context_usage_listener.py`

**Interfaces:**

- `mark_context_changed()` 只通知 listeners，并回写 `used_tokens`。
- `ListenerResult` 只保留 usage 聚合结果，不再携带 `messages_after_compressor`。
- `maybe_compact()` 负责压缩结果写入 `_system_entry`、`_history_entries` 和 `_active_entries`，监听器不修改 manager 状态。

- [ ] **Step 1: 写失败测试，验证 listener 不能造成上下文状态分叉**

在测试文件中补充以下 import：

```python
from app.core.context.context_listener.context_listener import ContextListener
from app.core.context.context_listener.listener_result import ListenerResult
```

```python
class _MutatingListener(ContextListener):
    """测试 listener 不得通过事件快照修改 manager 的 entries。"""

    def listen(self, event: ListenerEvent, result: ListenerResult) -> None:
        """清空收到的快照，不修改 manager。"""
        event.messages.clear()


def test_context_listener_snapshot_does_not_replace_manager_context() -> None:
    manager = RuntimeContextManager(
        task_id=1,
        agent_profile=_profile(main_agent=True),
        total_tokens=100,
    )

    manager.add_change_listener(_MutatingListener())
    manager.add_message(RuntimeMessage(role="user", content_text="original"), persist=False)

    assert [message.content for message in manager.load_message()][-1] == "original"
```

测试 listener 可以参与 usage/result 聚合，但不能通过结果对象替换 manager 内部 entries。

- [ ] **Step 2: 删除 `messages_after_compressor` 结果字段**

将 `ListenerResult` 收敛为只保存 usage：

```python
class ListenerResult:
    """上下文监听器的 usage 聚合结果。"""

    def __init__(self, usage: int) -> None:
        self.usage = usage
```

同步删除 `ContextListener`、`ContextCompressListener` 中关于 `messages_after_compressor` 的说明和无效接口语义。

- [ ] **Step 3: 删除 `mark_context_changed()` 的 manager 状态回写分支**

删除以下逻辑：

```python
if event_type == ContextEventType.CONTEXT_COMPRESSED:
    self.messages = result.messages_after_compressor
```

`mark_context_changed()` 的 `finally` 只保留：

```python
self.used_tokens = result.usage
```

- [ ] **Step 4: 保持压缩结果只写入 entries**

`maybe_compact()` 继续将 compressor 结果拆分为 system 和非 system 消息，但最终只更新 entries：

```python
self._system_entry = ContextEntry(compressed_messages[system_index], None)
self._history_entries = [
    ContextEntry(message=message, turn_id=None)
    for message in remaining_messages
]
self._active_entries = []
```

随后以 `_effective_runtime_messages()` 的深拷贝通知 listener；通知过程不得再改变 entries。

- [ ] **Step 5: 添加压缩回归测试并运行**

```python
def test_compaction_updates_entries_and_load_message() -> None:
    class _FixedCompressor:
        """返回固定压缩结果的测试压缩器。"""

        def __init__(self, result: list[RuntimeMessage]) -> None:
            """保存固定压缩结果。"""
            self.result = result

        def compact(self, messages: list[RuntimeMessage]) -> list[RuntimeMessage]:
            """返回固定结果，不修改输入消息。"""
            return list(self.result)

    manager = RuntimeContextManager(
        task_id=1,
        agent_profile=_profile(main_agent=True),
        total_tokens=100,
        compressor=_FixedCompressor(
            [
                RuntimeMessage(role="system", content_text="compressed system"),
                RuntimeMessage(role="assistant", content_text="summary"),
            ]
        ),
    )
    manager.add_message(RuntimeMessage(role="user", content_text="old"), persist=False)

    assert manager.maybe_compact() is True
    assert [message.content for message in manager.load_message()] == [
        "compressed system",
        "summary",
    ]
```

Run:

```powershell
uv run pytest tests/test_context_usage_listener.py -q
```

Expected: 压缩后的上下文、usage 和 listener 异常路径全部通过。

---

### Task 4: 清理全仓引用、文档和最终验证

**Files:**

- Modify: `apps/backend/app/models/enums/event_type.py`
- Modify: `apps/backend/app/models/payload/context_usage_payload.py`
- Modify: `apps/backend/tests/test_context_usage_listener.py`
- Verify: `apps/backend/app/core/workflows/nodes/model_node.py`
- Verify: `apps/backend/app/core/workflows/nodes/tools_node.py`
- Verify: `apps/backend/app/core/workflows/nodes/observation_node.py`

**Interfaces:**

- 生产 workflow 节点继续通过 `add_message()` 写入、通过 `load_message()` 读取。
- usage payload 文档改称“有效运行时上下文快照”，不再引用 `RuntimeContext.messages`。

- [ ] **Step 1: 搜索并清零旧字段引用**

Run:

```powershell
rg -n "\.messages\b|messages_after_compressor|_sync_messages|RuntimeContext\.messages" apps/backend/app apps/backend/tests
```

Expected: 只允许 LangChain/业务局部变量中的普通 `messages` 名称；不允许出现 `manager.messages`、`RuntimeContext.messages`、`messages_after_compressor` 或 `_sync_messages`。

- [ ] **Step 2: 更新过时注释**

把 `event_type.py` 和 `context_usage_payload.py` 中“数据来自 `RuntimeContext.messages`”改为“数据来自 manager 当前有效 entries 生成的运行时上下文快照”。

- [ ] **Step 3: 验证 workflow 没有隐式依赖 `messages` 字段**

确认以下调用链保持不变：

```text
model_node -> RuntimeContextManager.load_message()
model_node/tools_node/observation_node -> RuntimeContextManager.add_message()
ContextUsageComputeListener -> ListenerEvent.messages 快照
```

如果生产代码发现直接读取 manager 字段，改为调用 `load_message()`；不得重新引入新的完整缓存字段。

- [ ] **Step 4: 运行完整上下文回归**

Run:

```powershell
uv run pytest tests/test_context_entry.py tests/test_turn_runtime_message_store.py tests/test_context_usage_listener.py tests/test_react_workflow_context.py tests/test_turn_usage_stats.py tests/test_build_run_failed_payload.py tests/test_emit_run_cancelled.py tests/test_finalize_max_steps.py -v
```

Expected: 0 failures。

- [ ] **Step 5: 运行格式、lint 和新增文件严格类型检查**

Run:

```powershell
uv run ruff format --check app/core/context tests/test_context_usage_listener.py
uv run ruff check app/core/context tests/test_context_usage_listener.py
uv run mypy --config-file mypy.strict.ini app/core/context/runtime_context_manager.py app/core/context/context_listener/listener_result.py
```

Expected: 所有检查通过；仓库中不存在 `messages` 的 manager 兼容入口。

## Self-Review

- 状态归属：system/history/active 仍由 `ContextEntry` 保留，删除的只是冗余完整列表。
- 模型调用：`load_message()` 仍在调用边界生成 LangChain 消息，不改变模型输入协议。
- usage：listener 仍收到完整有效上下文快照，但该快照只在通知时临时创建，不作为 manager 字段长期保存。
- 压缩：压缩器输入和输出均经过 entries，不再出现 `messages` 与 entries 分叉。
- 持久化：`RuntimeMessageStore` 和 SQLite `turn_messages` 不改，避免把本次内存投影删除误当成持久化协议变更。
- 兼容性：按绿地项目要求，不保留旧字段、旧属性或迁移别名。
