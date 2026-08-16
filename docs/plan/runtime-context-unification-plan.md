# RuntimeContextManager 上下文唯一事实源收敛方案

> 状态：待实施（已完成独立审查 Agent 对照真实代码逐条核验并修订；§8 扩查问题已全部回答并回写正文，待复审通过后进入实施）。
> 目标：让 `RuntimeContextManager` 成为「模型上下文」的唯一事实源——不仅管理内存上下文，也统一收口数据库上下文的读写，消除散落在节点与 runner 中的多类写路径。

---

## 1. 背景与目标

当前代码中，「模型上下文」实际存在**双重事实**：

- **内存形态**：`RuntimeContextManager` 持有 `messages: list[BaseMessage]`，节点经 `load_message()` 读取、`add_message()` 写入。
- **持久化形态**：SQLite `turn_messages` 表（`role=user/assistant/tool` 轨迹），经 `RuntimeOperations.append_runtime_message` / `reset_message_sequence` 落库。

`state.py` 注释与 `AGENTS.md` 早已声明设计意图是「消息持久化事实来源是 SQLite，checkpoint 只承载控制流状态，错误恢复时由 `RuntimeContext.build_for_task` 从 DB 重建上下文」——即 **DB 为真、内存为缓存视图**。但**写路径没有收口**：各节点手动维护「落库 + 写内存」配对，转换逻辑散落多处，导致同一语义存在多套实现。

本方案的目标：

1. `RuntimeContextManager` 成为上下文读写**唯一入口**：内存形态（`BaseMessage`）与持久化形态（`RuntimeMessage`）的转换、写库、写内存、序号维护全部在 manager 内完成，节点只调一个 API。
2. 保持分层红线：`core/context` 不反向依赖 `service`，通过「协议 + 注入」消化（同项目既有 `ToolTraceRecorder` / `TurnRunner` 模式）。
3. 语义升级：把「哪些消息该落库」从**注释约定**升级为**参数契约**。

---

## 2. 现状代码事实（已逐条核对真实代码）

> 以下行号以 `apps/backend/app/` 为根的当前工作区代码为准。

### 2.1 三类不一致的写路径

| # | 位置 | 行为 | 内存/DB 是否成对 |
|---|------|------|------------------|
| 1 | `core/workflows/nodes/model_node.py:767-772` | 正常输出：`operations.append_runtime_message(_ai_to_runtime_message(ai_message))` + `_runtime_context().add_message(ai_message)` | 成对（先落库后写内存） |
| 2 | `core/workflows/nodes/tools_node.py:68-72`（`_persist_tool_observations`） | 逐条：`runtime_to_langchain([obs_message])[0]` 转换 → `operations.append_runtime_message(obs_message)` → `_runtime_context().add_message(langchain_obs)` | 成对，且**落库失败则不写内存**（防撕裂语义） |
| 3 | `core/runtime/runner.py:349-354` | turn 启动：`operations.reset_message_sequence()` + `operations.append_runtime_message(RuntimeMessage(role="user", content_text=turn.input_text))` | **只落库不写内存**（时序上先落库、`build_for_task` 稍后读回历史） |
| 4 | `core/workflows/nodes/model_node.py:754` | REPAIR 情形 b：`_runtime_context().add_message(SystemMessage(content=repair_message))` | **只写内存不落库**（运行时提示，无需重放） |
| 5 | `core/workflows/nodes/tools_node.py:340-341` | REPAIR 情形 a 的延后注入：`_runtime_context().add_message(SystemMessage(content=deferred_repair_message))`（模型「文本+工具并存」时修复提示随工具分支延后写入） | **只写内存不落库**（与 #4 同类，运行时提示，无需重放） |

> 说明：路径 #3 的「只落库不写内存」是**刻意的时序设计**（先落基线、再经 `build_for_task` 读回完整历史），不是遗漏；路径 #4/#5 的「只写内存不落库」也是**刻意**的（repair 提示是运行时话术）。问题是这两种刻意语义目前靠注释约定、没有结构化表达。

### 2.2 转换逻辑散落四处

| 转换 | 位置 | 方向 |
|------|------|------|
| `_ai_to_runtime_message` | `core/workflows/nodes/model_node.py:173-185` | `AIMessage` → `RuntimeMessage`（`tool_calls` 以 **JSON 字符串** 存进 `metadata["tool_calls"]`，与 `RuntimeContext._tool_calls_from_metadata` 反序列化契约对齐） |
| `runtime_to_langchain` | `core/llm/langchain_bridge.py:75` | `RuntimeMessage` → `BaseMessage`（含 tool_calls 配对剥离的防御性清洗） |
| `_build_history_messages`（DB → 内存） | `core/context/runtime_context_manager.py:321` 内部 | DB `RuntimeMessage` → `BaseMessage` 历史重建 |
| `_tool_calls_from_metadata`（重复两套） | `core/llm/langchain_bridge.py:227` 与 `core/context/runtime_context_manager.py:421` | `metadata["tool_calls"]` JSON 字符串 → `list[dict]` 反序列化；两处逻辑几乎一致（`json.loads` + list 校验），应去重收口 |

### 2.3 序号归属

- `_message_sequence` 由 `RuntimeOperations`（`core/runtime/runtime_operations.py`）内部维护，`append_runtime_message` 自增、`reset_message_sequence` 归零。
- 落库实际经 `TurnService.append_turn_message(turn_id, message, sequence, in_context=True)`（`service/task/turn_service.py:223`）→ storage CRUD。
- `TurnService.load_turn_messages(turn_id)`（`turn_service.py:218`）是 `RuntimeContext.build_for_task` 重建历史的读源。

### 2.4 生命周期

- `RuntimeContextManager.build_for_task(agent_profile, workspace_root, task_id, excluded_turn_ids=())`（`runtime_context_manager.py:190`，`@classmethod` 在 189）在 workflow 装配时被调用（`core/workflows/react/workflow.py:169`），负责从 DB 重建历史并挂 usage_meter。
- `__exit__`（`runtime_context_manager.py:157`）记录上下文规模并释放引用，**不重复写库**。
- **child agent（delegation）同样调用 `build_for_task`**：`core/delegation/child_agent_runner.py` 经注入的 `run_agent` 回调（即 `AgentRuntime.run_agent`）→ `agent.workflow.run` → `ReactLikeWorkflow.run` → `build_for_task`（`workflow.py:169`）。child 与 parent 的差异**不是「是否使用 build_for_task」**，而是 `delegation_executor.py:173` 传入 `context_excluded_turn_ids=(parent_turn_id,)`（经 `ChildAgentProfileBuilder` → `agent_profile.context_excluded_turn_ids` → `workflow.py:173`），使 child 重建历史时排除父 turn，实现父子隔离。因此 **child 路径同样受本次收敛影响**，收敛时需一并验证，而非「无交集」（详见 §6）。

---

## 3. 问题清单

1. **写路径不唯一**：5 个写点分属 3 种配对模式（含 tools_node REPAIR 情形 a 的「只写内存不落库」），新增一类消息时容易漏配或配错（落库不写内存 / 写内存不落库 / 转换不一致）。
2. **转换逻辑重复**：`AIMessage → RuntimeMessage` 与 `RuntimeMessage → BaseMessage` 两向转换散落在节点与 bridge，manager 无法作为事实源自我描述；且 `_tool_calls_from_metadata` 反序列化在 `langchain_bridge.py:227` 与 `runtime_context_manager.py:421` 存在两套几乎一致的重复实现。
3. **序号与写入分离**：序号在 `RuntimeOperations`，转换在节点，落库在 manager 之外，同一「一条消息」的完整生命周期跨越 3 个对象。
4. **防撕裂语义靠约定**：tools_node 的「DB 失败则内存也不写」只在 docstring 与 try/except 里体现；未来新增写点无法保证继承该语义。

---

## 4. 目标设计

### 4.1 单一事实源边界

`RuntimeContextManager` 职责扩展为：

```
唯一事实源
├── 内存形态   : messages: list[BaseMessage]（现状不变）
└── 持久化形态 : turn_messages 表（经注入的 store 端口）
```

manager 对外暴露**统一写入 API**，节点与 runner 不再直接碰 `TurnService` / `RuntimeOperations.append_runtime_message`。

**边界说明（删除清理不在收敛范围）**：`turn_messages` 表的**删除清理**路径（`service/task/workspace_service.py:90` 与 `service/task/task_service.py:204` 经 `TurnMessageCrud.delete_by_turn_ids` 级联删除 task/workspace）**不在本次收敛范围**。本次收敛的是「模型上下文的读写」，即 node/runner 在 turn 执行期间的落库与内存写入；删除清理属任务/工作区生命周期管理，保留在 service 层。若未来要求「唯一事实源」覆盖删除，再另行收口。

### 4.2 依赖倒置：`RuntimeMessageStore` 协议

在 `core/context/` 新增窄端口协议，manager 构造时注入：

```python
class RuntimeMessageStore(Protocol):
    """运行时消息持久化端口（由 service 层实现，注入避免 core→service 反向依赖）。"""

    def append(self, turn_id: str, message: RuntimeMessage, sequence: int) -> None:
        """落库一条消息；失败抛 SQLAlchemyError（透传给 manager 决定防撕裂语义）。"""

    def clear(self, turn_id: str) -> None:
        """清空某 turn 的全部消息（turn 启动重置用）。"""

    def build_for_task(self, task_id: str, excluded_turn_ids: tuple[str, ...] = ()) -> list[RuntimeMessage]:
        """按 task 维度读回有序历史（含跨轮），供 build_for_task 重建内存上下文。"""
```

- 实现由 service 层提供（复用 `TurnService.load_turn_messages` / `append_turn_message` / `clear_turn_messages`，见 `turn_service.py:218/223/245`），由 runner 组装后注入 workflow 装配链。
- 分层约束：`core/context → 协议`（新文件，无 service 依赖）；service 实现方负责满足协议。`RuntimeOperations` 中与消息落库相关的方法（`append_runtime_message` / `reset_message_sequence`）随收敛**退役或瘦身为纯转调**，由注入的 store 承担。

### 4.3 统一写入 API

manager 新增/改造：

```python
def add_message(self, message: BaseMessage, *, persist: bool = True) -> None:
    """向上下文追加一条消息（唯一写入入口）。

    persist=True 时：先转 RuntimeMessage（含 tool_calls 序列化）→ store.append 落库，
    落库失败抛异常、内存不写（继承 tools_node 既有防撕裂语义）；成功后再写内存并归一化。
    persist=False 时：仅写内存（运行时提示，如 repair 话术）。
    """

def append_startup_user_message(self, text: str) -> None:
    """turn 启动基线落库专用：仅落库不写内存（供 build_for_task 稍后读回历史）。"""
```

- 节点改造为单调用：model_node 正常输出 `_runtime_context().add_message(ai_message)`（`persist=True` 默认）；REPAIR 情形 b（`model_node.py:754`）显式 `add_message(SystemMessage(...), persist=False)`；REPAIR 情形 a 的延后注入（`tools_node.py:340-341` 的 deferred_repair_message）同样显式 `add_message(SystemMessage(...), persist=False)`；tools_node 正常观察去掉 `runtime_to_langchain` 前置转换，直接 `add_message(tool_message)`。
- `persist` 语义从「注释约定」升级为「参数契约」。

### 4.4 转换收口

- `_ai_to_runtime_message`（`model_node.py:173`）收进 manager（或随 manager 的私有转换模块），`AIMessage → RuntimeMessage` 只此一处。
- `runtime_to_langchain`（`langchain_bridge.py:75`）保留在 bridge 作为 `RuntimeMessage ↔ BaseMessage` 的唯一转换点，由 manager 内部调用（避免 `core/context` 自行实现第二套）。
- `_build_history_messages` 保留在 manager（已在其内），改走注入 store 的 `build_for_task` 读源。
- **去重 `_tool_calls_from_metadata`**：`runtime_context_manager.py:421` 的私有实现删除，改复用 `langchain_bridge.py:227` 的 `_tool_calls_from_metadata`（或将其作为 bridge 公开符号导出），消除两套几乎一致的 JSON 反序列化。

### 4.5 序号与 reset 归属

- `_message_sequence` 从 `RuntimeOperations` 移入 store 实现（或 manager 内部），与落库同处维护。
- `reset_message_sequence` 的**语义**保留在 runner 的 turn 启动流程（它本质是「turn 启动初始化」，不是「上下文管理」），实现改为转调注入 store 的 `clear(turn_id)`。

### 4.6 启动期用户消息

- runner 的「先落 user 基线、再由 `build_for_task` 读回」时序依赖保留；写入改为 `_runtime_context().append_startup_user_message(turn.input_text)`，把「启动期例外」显式化。

---

## 5. 改动清单

| 文件 | 改动 |
|------|------|
| `core/context/runtime_message_store.py`（**新增**） | `RuntimeMessageStore` 协议 |
| `core/context/runtime_context_manager.py` | 构造注入 store；新增 `add_message(persist=)` / `append_startup_user_message`；收口 `_ai_to_runtime_message`；`_build_history_messages` 改走 store；删除私有 `_tool_calls_from_metadata` 改复用 bridge |
| `service/task/turn_service.py` | 提供 `RuntimeMessageStore` 适配实现（复用现有三个方法，或将方法组合为端口实现） |
| `core/runtime/runner.py` | turn 启动改用 `append_startup_user_message`；组装 store 注入 |
| `core/runtime/runtime_operations.py` | `append_runtime_message` / `reset_message_sequence` 退役或瘦身为转调（视调用面决定） |
| `core/workflows/nodes/model_node.py` | 正常输出改 `add_message`；REPAIR 情形 b 改 `add_message(..., persist=False)`；删除 `_ai_to_runtime_message` |
| `core/workflows/nodes/tools_node.py` | `_persist_tool_observations` 改 `add_message(tool_message)`，删除前置转换；REPAIR 情形 a 的 deferred_repair_message 改 `add_message(..., persist=False)` |
| `core/workflows/react/workflow.py` | 装配链传入 store（或经 runner 已组装对象透传）；`build_for_task` 改为接收注入的 store，避免内部 `TurnService()` 自建违反分层 |
| `core/llm/langchain_bridge.py` | 将 `_tool_calls_from_metadata` 作为公开符号导出（供 manager 复用，消除重复） |
| 测试 | `tests/` 新增/更新：manager 落库/防撕裂/序号、节点单调用收敛、runner 启动基线、REPAIR 情形 a/b persist=False、child 路径回归（child 同样走 build_for_task） |

---

## 6. 边界与风险

- **child agent 同样走 `build_for_task`，收敛须覆盖而非排除**：delegation 的 child 经 `ChildAgentRunner.run_child` → `run_agent` → `workflow.run` → `build_for_task`（`workflow.py:169`），父子隔离由 `delegation_executor.py:173` 传入 `context_excluded_turn_ids=(parent_turn_id,)` 实现，而非「child 不用 build_for_task」。故本次收口 manager 后，child 与 parent **共用同一套统一写入 API**，语义天然一致；需在测试中补 child 路径回归（child 带 excluded_turn_ids 重建历史仍正确、child 落库仍写入自身 turn）。
- **checkpoint 恢复**：`__exit__` 与压缩路径明确「不重复写库」；收口后压缩若发生内容替换，需另行定义压缩结果是否落库（当前 `maybe_compact` 未实现，暂不处理，留注释）。
- **时序**：manager 落库需要 `turn_id`，构造时已知（`build_for_task` 有 task_id，turn_id 由 runner 在启动期注入或经 store 按当前 turn 解析）。
- **调用面**：`RuntimeOperations.append_runtime_message` / `reset_message_sequence` 若仍有节点调用，需先收敛调用面再退役，避免半迁移。
- **改动面**：约 9-10 个文件，属结构性重构；按第零铁律「长期可维护性优先」推进，但不扩大至无关模块。

---

## 7. 验证计划

1. 单元测试：manager 新增 API（persist 落库成功/失败防撕裂、persist=False 不落库、序号自增、启动基线只落库不写内存）。
2. 回归：现有工具/模型节点测试全绿（尤其 tool_calls 配对闭合、REPAIR 回流）。
3. 集成：跑一轮真实 turn（模型→工具→观察→模型），核对 `turn_messages` 表轨迹与内存上下文一致。
4. 独立审查 + 独立测试 Agent 闭环：审查 Agent 对照本方案与真实代码逐条核验；测试 Agent 跑业务测试，双方通过后交付。

---

## 8. 审查 Agent 扩查结论（已回写正文）

以下问题已由独立审查 Agent 扩大代码事实范围后确认，结论均已回写进 §2.4 / §4.1 / §6：

1. **`append_runtime_message` / `reset_message_sequence` 调用点已穷举**：生产调用仅 `model_node.py:768`、`tools_node.py:71`、`runner.py:349/352` 三处；`compaction`（`maybe_compact` 仅 manager 内定义无节点调用）、`max_steps` 分支、delegation child（经同一 workflow 的 model/tools 节点调用）均无额外直接调用。
2. **`TurnService` 三方法端口化**：`load_turn_messages` 仅 `build_for_task`（`runtime_context_manager.py:231`）消费，`append_turn_message` / `clear_turn_messages` 仅 `RuntimeOperations` 消费，端口化不受影响；但删除清理走 CRUD 直调（见 §4.1 边界说明）。
3. **`excluded_turn_ids` 有实际传值方**：`workflow.py:173` 传 `agent_profile.context_excluded_turn_ids`，由 `delegation_executor.py:173`（`(parent_turn_id,)`）填充，**child 隔离正是通过它生效**（见 §2.4 / §6）。
4. **manager 之外读写 turn_messages**：存在**删除清理**路径（`workspace_service.py:90`、`task_service.py:204` 的 `delete_by_turn_ids`），已声明不在收敛范围（见 §4.1）；checkpoint 恢复不读 turn_messages（仍走 `build_for_task`）。
5. **`usage_meter` / `CONTEXT_USAGE` 不受影响**：`ContextUsageMeter` 经 `message_provider` 实时取 `ctx.load_message()`（内存 `messages`），收口后 manager 仍维护内存形态并保留 `load_message` 出口。
