# Context Entry Turn Ownership Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将运行时上下文从无归属的消息列表改造成可区分历史、当前 turn、恢复状态和非上下文轨迹的 task 级上下文容器。

**Architecture:** 持久化层继续以 `turn_messages.turn_id` 作为消息归属事实源；内存层新增 `ContextEntry`，显式记录 `turn_id`、`include_in_context` 和 `RuntimeMessage`。manager 维护 task 历史区与当前 turn 增量区，使用显式的 fresh/resume 生命周期，避免依赖 `_turn_context_baseline` 快照和隐含调用顺序。usage 基于最终有效模型上下文重新计算，而不是对不在上下文中的轨迹做增量累加。

**Tech Stack:** Python 3.11+, FastAPI service layer, SQLAlchemy/SQLite persistence, LangGraph checkpoint, pytest, Ruff。

**Spec:** `docs/plan/context-usage-main-agent-concurrency-fix-plan.md`

## Global Constraints

- 前后端作为同一个本地桌面应用交付；本计划只修改后端，不修改 `apps/desktop` 或 `apps/shared`。
- 并发粒度是 task；不同 task 可并发，同一 task 内 turn 必须串行。
- `RuntimeContextManager` 保持在 `core/context`，不得反向依赖 service/storage；通过 `RuntimeMessageStore` 协议获取持久化数据。
- Python 代码使用完整类型注解和中文四段式 docstring；新增代码必须通过 Ruff。
- 所有新行为先写失败测试，再写生产代码；不得提交 Git commit。
- 子 Agent 审查统一使用 `gpt-5.6-luna`，审查只读，不修改文件。

---

## 设计决策

### 1. 不直接扩展 `RuntimeMessage`

`RuntimeMessage` 是模型无关的持久化消息值对象，同时被工具执行、service 和 listener 使用。直接加入 `turn_id`、`include_in_context` 会把上下文生命周期字段泄漏到所有调用方。

新增内部值对象：

```python
@dataclass(frozen=True)
class ContextEntry:
    message: RuntimeMessage
    turn_id: int | None
    include_in_context: bool = True
```

约定：

- `turn_id=None`：system prompt 或 task 级上下文；
- `turn_id=<id>`：属于指定 turn 的消息；
- `include_in_context=False`：保留持久化轨迹，但不进入模型 prompt，也不计入 context usage。

### 2. 明确 manager 状态

`RuntimeContextManager` 维护以下状态：

```text
system_entries       task 级 system prompt
history_entries      已完成 turn 的有效历史
active_turn_id       当前绑定 turn
active_entries       当前 turn 的运行期消息
history_loaded       历史是否已加载/替换
execution_mode       fresh 或 resume
```

`load_message()` 只拼装：

```text
system_entries + history_entries + active_entries(include_in_context=True)
```

不再通过一份 `messages` 列表推断消息属于哪个 turn。

### 3. 显式区分 fresh 与 resume

当前 turn 有两种完全不同的入口：

- `fresh`：新执行或用户要求重跑。清理当前 turn 持久化轨迹，丢弃该 turn 内存增量，再追加新的 user 输入；
- `resume`：checkpoint/启动恢复。保留并加载当前 turn 已持久化轨迹，不重复追加已经存在的 user/assistant/tool 消息。

不能再使用“首次创建时排除当前 turn”作为所有场景的统一规则。

### 4. 图片不写入 turn_messages，但必须可恢复

图片二进制或 data URL 不进入 `turn_messages`。恢复当前 turn 时，通过 `TurnRecord.image_paths` 重新构建 `content_blocks`，并把当前 user entry 的运行期多模态内容补回内存。历史 turn 仍保持纯文本回放。

### 5. usage 以有效模型上下文为准

每次上下文变化后，由 manager 提供最终有效 `RuntimeMessage` 快照给 usage listener；listener 重新计算：

```text
usage = sum(entry.message.estimate_tokens()
            for entry in effective_entries
            if entry.include_in_context)
```

只持久化、不进入模型的轨迹不发普通 context usage 变化，也不计入圆环。

---

## 文件结构

**Create**

- `apps/backend/app/core/context/context_entry.py`: 定义 `ContextEntry`，不依赖 service/storage。
- `apps/backend/tests/test_context_entry.py`: 值对象字段和不可变性测试。

**Modify**

- `apps/backend/app/core/context/runtime_message_store.py`: 增加带 turn 归属的历史读取结果和 fresh/resume 所需能力。
- `apps/backend/app/service/turn_runtime_message_store.py`: 将 service 的 turn/message 记录映射为 context entry。
- `apps/backend/app/service/task/turn_service.py`: 明确 `fresh` 清理、`resume` 读取和当前 turn 轨迹查询的 service 端口。
- `apps/backend/app/core/context/runtime_context_manager.py`: 拆分 history/active entries，新增显式生命周期 API，移除 `_turn_context_baseline`。
- `apps/backend/app/core/context/context_listener/listener_event.py`: 明确 listener 收到的是有效上下文快照和变化类型。
- `apps/backend/app/core/context/context_listener/context_usage_compute_listener.py`: 基于有效上下文快照重新计算 usage。
- `apps/backend/app/core/workflows/react/workflow.py`: 传入 fresh/resume 模式，调用 `begin_turn`，追加当前 user entry。
- `apps/backend/app/core/runtime/runner.py`: 明确新执行与 checkpoint 恢复的运行模式来源。
- `apps/backend/app/service/task/turn_stream_service.py`: 若该层负责恢复/重驱动，传递 execution mode，不自行推断。
- `apps/backend/tests/test_context_usage_listener.py`: 更新 usage 语义测试。
- `apps/backend/tests/test_runtime_context_manager.py`: 新增生命周期、重跑、恢复和幂等测试。
- `apps/backend/tests/test_turn_runtime_message_store.py`: 新增历史 turn 排序、归属和过滤测试。
- `apps/backend/tests/test_react_workflow_context.py`: 验证 workflow 传递当前 user 文本和多模态内容。

**Do not modify**

- `apps/desktop/**`
- `apps/shared/**`
- `apps/backend/app/core/context/context_compressor/**` 的压缩算法实现；只适配其上下文输入输出契约。

---

## Task 1: 定义 ContextEntry 和持久化历史契约

**Files:**

- Create: `apps/backend/app/core/context/context_entry.py`
- Modify: `apps/backend/app/core/context/runtime_message_store.py`
- Modify: `apps/backend/app/service/turn_runtime_message_store.py`
- Test: `apps/backend/tests/test_context_entry.py`
- Test: `apps/backend/tests/test_turn_runtime_message_store.py`

**Interfaces:**

- `ContextEntry(message: RuntimeMessage, turn_id: int | None, include_in_context: bool = True)`。
- `RuntimeMessageStore.build_for_task(task_id: int, excluded_turn_ids: Collection[int] | None = None) -> list[ContextEntry]`。
- store 返回值必须保留每条记录的 `turn_id` 和 `in_context`；不得让 manager 从无归属的 `RuntimeMessage` 重新猜测。

- [ ] **Step 1: Write the failing tests**

测试以下行为：

```python
def test_context_entry_preserves_turn_ownership_and_context_flag():
    entry = ContextEntry(RuntimeMessage(role="user", content_text="x"), 11, False)
    assert entry.turn_id == 11
    assert entry.include_in_context is False

def test_store_history_returns_turn_owned_entries_and_excludes_requested_turn():
    entries = store.build_for_task(7, excluded_turn_ids={11})
    assert [entry.turn_id for entry in entries] == [10]
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/test_context_entry.py tests/test_turn_runtime_message_store.py -v`

Expected: FAIL because `ContextEntry` does not exist and the store still returns bare `RuntimeMessage` values.

- [ ] **Step 3: Implement the minimal value object and adapter mapping**

`TurnRuntimeMessageStore` must map every loaded row to `ContextEntry(message=..., turn_id=turn.id, include_in_context=True)`. If the lower layer exposes `in_context`, preserve it instead of forcing `True`.

- [ ] **Step 4: Run tests and Ruff**

Run: `uv run pytest tests/test_context_entry.py tests/test_turn_runtime_message_store.py -v` and `uv run ruff check app/core/context/context_entry.py app/core/context/runtime_message_store.py app/service/turn_runtime_message_store.py tests/test_context_entry.py tests/test_turn_runtime_message_store.py`

Expected: PASS and `All checks passed!`.

---

## Task 2: Split history and active turn state in RuntimeContextManager

**Files:**

- Modify: `apps/backend/app/core/context/runtime_context_manager.py`
- Test: `apps/backend/tests/test_runtime_context_manager.py`

**Interfaces:**

- `begin_turn(turn: TurnRecord, mode: Literal["fresh", "resume"]) -> None`。
- `load_history(*, excluded_turn_ids: Collection[int] | None = None, replace: bool = True) -> None`。
- `add_message(..., include_in_context: bool = True, persist: bool = True) -> None`；迁移期保留 `write_memory` 别名，但生产调用方不得继续使用该名称。
- `load_message() -> list[BaseMessage]` 只转换有效上下文 entries。

- [ ] **Step 1: Write failing lifecycle tests**

覆盖四个核心场景：

```python
def test_fresh_turn_has_previous_history_and_one_current_user_message(): ...
def test_rebinding_same_fresh_turn_discards_old_active_entries(): ...
def test_resume_turn_reloads_persisted_current_entries_without_duplicate_user(): ...
def test_load_history_replace_is_idempotent(): ...
```

断言必须落在 `manager.load_message()` 的真实结果上，同时检查 store 的 `clear`/`append` 调用次数。

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/test_runtime_context_manager.py -k "fresh or resume or history" -v`

Expected: FAIL because current manager has one flat message list and only `_turn_context_baseline` semantics.

- [ ] **Step 3: Implement explicit state transitions**

实现约束：

- `fresh` 清理当前 turn 持久化轨迹，并将 `active_entries` 置空；
- `resume` 加载当前 turn 的有效持久化 entries 到 `active_entries`；
- `load_history(replace=True)` 替换 `history_entries`，重复调用不会 append 重复；
- `add_message(include_in_context=False)` 可持久化，但不进入 `active_entries`，也不触发 context usage；
- 当前 user 输入必须以 `turn_id=current_turn.id` 进入 `active_entries`；
- 删除 `_turn_context_baseline`，不再依赖 list 快照恢复 turn 边界；
- 所有状态转换在 manager 的 `RLock` 内完成。

- [ ] **Step 4: Run lifecycle tests and full context tests**

Run: `uv run pytest tests/test_runtime_context_manager.py tests/test_context_usage_listener.py -v`

Expected: PASS。

---

## Task 3: Make usage derive from effective context

**Files:**

- Modify: `apps/backend/app/core/context/context_listener/listener_event.py`
- Modify: `apps/backend/app/core/context/context_listener/context_usage_compute_listener.py`
- Modify: `apps/backend/app/core/context/runtime_context_manager.py`
- Test: `apps/backend/tests/test_context_usage_listener.py`

**Interfaces:**

- `ListenerEvent.messages` 表示变化后的有效模型上下文快照，不表示任意持久化轨迹。
- `ContextUsageComputeListener.listen()` 对 `ADD_MESSAGE`、`LOAD_HISTORY`、`CONTEXT_COMPRESSED` 都基于快照重算，不再根据“事件是否落库”推断 token。

- [ ] **Step 1: Write failing usage tests**

覆盖：

```python
def test_non_context_persisted_entry_does_not_change_usage(): ...
def test_rebinding_fresh_turn_does_not_accumulate_usage_twice(): ...
def test_resume_usage_matches_reconstructed_context_snapshot(): ...
def test_compression_usage_equals_compressed_context_snapshot(): ...
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/test_context_usage_listener.py -k "usage or context" -v`

Expected: FAIL where usage is currently incremented from an entry that is not in `load_message()`.

- [ ] **Step 3: Implement snapshot-based usage**

manager 在每次有效上下文变化后传递 `effective_context_messages()` 的深拷贝；非上下文轨迹只落库，不触发 usage listener。保留事件写入失败的既有容错语义，但 task usage 和 manager usage 必须在异常路径保持一致。

- [ ] **Step 4: Run tests and Ruff**

Run: `uv run pytest tests/test_context_usage_listener.py -v` and `uv run ruff check app/core/context/context_listener app/core/context/runtime_context_manager.py tests/test_context_usage_listener.py`

Expected: PASS and `All checks passed!`。

---

## Task 4: Add explicit fresh/resume workflow wiring

**Files:**

- Modify: `apps/backend/app/core/workflows/react/workflow.py`
- Modify: `apps/backend/app/core/runtime/runner.py`
- Modify: `apps/backend/app/service/task/turn_stream_service.py`
- Test: `apps/backend/tests/test_react_workflow_context.py`

**Interfaces:**

- runner/stream service 必须提供明确的 `execution_mode: Literal["fresh", "resume"]`。
- `ReactLikeWorkflow.run(..., execution_mode=...)` 在构造/获取 manager 后调用 `begin_turn`。
- 新 turn fresh 路径追加当前 user；resume 路径只有在当前 turn 持久化轨迹中不存在 user entry 时才追加。

- [ ] **Step 1: Write failing workflow tests**

覆盖：

```python
async def test_fresh_workflow_sends_current_text_and_image_blocks_to_model(): ...
async def test_resume_workflow_does_not_duplicate_persisted_current_user(): ...
async def test_fresh_rerun_clears_old_current_turn_messages_before_model_call(): ...
```

测试使用假的 model/operations/store，断言传入 `model.astream()` 的真实消息列表，而不是只断言 manager 方法被调用。

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/test_react_workflow_context.py -v`

Expected: FAIL because workflow currently always uses the same implicit sequence and always appends the current user.

- [ ] **Step 3: Wire explicit mode and deduplication**

新执行由 runner 传 `fresh`；checkpoint/startup 恢复由恢复入口传 `resume`。不得根据“manager 是否存在”推断模式。resume 时以当前 turn 的持久化 entries 为准；若缺少 user entry，重新从 `TurnRecord.input_text` 构建并补回图片 block。

- [ ] **Step 4: Run workflow and backend regression tests**

Run: `uv run pytest tests/test_react_workflow_context.py tests/test_runtime_context_manager.py tests/test_context_usage_listener.py -v`

Expected: PASS。

---

## Task 5: Handle compression, recovery, and concurrency boundaries

**Files:**

- Modify: `apps/backend/app/core/context/context_compressor/context_compressor.py` only if the input/output contract needs `ContextEntry` adaptation; do not change compression algorithm.
- Modify: `apps/backend/app/core/context/runtime_context_manager.py`
- Modify: `apps/backend/app/core/runtime/runner.py`
- Test: `apps/backend/tests/test_runtime_context_manager.py`
- Test: `apps/backend/tests/test_context_usage_listener.py`

- [ ] **Step 1: Write failing edge-case tests**

覆盖：

```python
def test_history_reload_does_not_duplicate_entries(): ...
def test_resume_rebuilds_current_image_blocks_from_turn_record(): ...
def test_same_task_concurrent_turn_binding_is_rejected_or_serialized(): ...
def test_different_tasks_keep_history_and_usage_isolated(): ...
def test_checkpoint_resume_keeps_persisted_assistant_and_tool_entries(): ...
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/test_runtime_context_manager.py tests/test_context_usage_listener.py -k "reload or image or concurrent or checkpoint" -v`

- [ ] **Step 3: Implement defensive boundaries**

要求：

- 同 task 同时绑定两个不同 running turn 时必须在 manager 层显式拒绝或等待，不能静默互相覆盖；
- 不同 task 只通过 task_id key 共享 registry，不共享 mutable entries；
- checkpoint resume 必须加载当前 turn 已落库的 assistant/tool 轨迹；
- fresh rerun 必须清理当前 turn 轨迹，resume 不得清理；
- 压缩后更新有效 context 快照，不能恢复到压缩前的隐式基线；
- `load_history` 的 replace 语义必须在 writer 不可用、usage listener 异常和数据库读取异常路径下保持状态可解释。

- [ ] **Step 4: Run edge-case tests and Ruff**

Run: `uv run pytest tests/test_runtime_context_manager.py tests/test_context_usage_listener.py -v` and `uv run ruff check app/core/context app/core/runtime tests/test_runtime_context_manager.py tests/test_context_usage_listener.py`

Expected: PASS and `All checks passed!`。

---

## Task 6: Remove transitional baseline semantics and document the contract

**Files:**

- Modify: `apps/backend/app/core/context/runtime_context_manager.py`
- Modify: `apps/backend/app/core/context/runtime_message_store.py`
- Modify: `apps/backend/app/service/turn_runtime_message_store.py`
- Modify: `docs/plan/context-usage-main-agent-concurrency-fix-plan.md`
- Test: `apps/backend/tests/test_context_usage_listener.py`

- [ ] **Step 1: Search for old API usage**

Run: `rg -n "write_memory|_turn_context_baseline|load_history\(" apps/backend/app apps/backend/tests`

Expected: only the explicitly documented compatibility shim, if retained, remains.

- [ ] **Step 2: Write/adjust compatibility tests**

验证：旧测试调用 `write_memory=False` 时行为明确；生产调用方全部使用 `include_in_context` 或专用当前 turn API；自定义 `RuntimeMessageStore` 的签名错误会在测试中明确暴露，而不是静默丢消息。

- [ ] **Step 3: Remove the baseline field and stale comments**

删除 `_turn_context_baseline`、旧的“先落库再 load_history 防重复”注释，并在 manager/service 协议 docstring 中写明 fresh/resume 和 replace 语义。

- [ ] **Step 4: Run the full backend context regression**

Run: `uv run pytest tests/test_context_entry.py tests/test_turn_runtime_message_store.py tests/test_runtime_context_manager.py tests/test_context_usage_listener.py tests/test_react_workflow_context.py tests/test_turn_usage_stats.py tests/test_build_run_failed_payload.py tests/test_emit_run_cancelled.py tests/test_finalize_max_steps.py -v`

Expected: 0 failures。

---

## Review Gates

- Task 1 完成后：检查 `ContextEntry` 是否仍保持 core → service 单向依赖。
- Task 2 完成后：重点审查 fresh/resume 是否能区分新执行、同 turn 重跑和 checkpoint 恢复。
- Task 3 完成后：重点审查 usage 是否只统计 `load_message()` 实际会发送的消息。
- Task 4 完成后：重点审查 workflow 是否在 resume 时重复 user，是否丢失 assistant/tool 轨迹。
- Task 5 完成后：重点审查同 task 并发、压缩、writer 异常和恢复路径。
- Task 6 完成后：运行只读子Agent审查后端变更；子Agent使用 `gpt-5.6-luna`，不得修改文件。

## Known Issues To Track Separately

- `vision_content_blocks._resolve_image_limits(None)` 当前与其默认限制文档/测试不一致；属于独立视觉能力问题，不与本计划混合修复。
- `load_history()` 即使改为 replace，也应禁止在同一运行流程中无意义反复读取数据库；需要调用方明确 `reload` 意图。
- SQLite 多线程 session、跨 task 文件锁和 `file_snapshots.seq` 已有独立并发边界，本计划只验证 context manager 不引入新的共享状态。

## No-Commit Constraint

本计划执行期间不创建 Git commit、不 push、不修改前端；每个任务只保留工作区改动，最终由用户决定是否提交。
