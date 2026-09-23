# Run 级模型配置与 Task Runtime Session 改造方案

> 状态：设计方案
>
> 基准日期：2026-09-10
>
> 目标：Task 负责任务容器和运行空间；每个 Run 保存自己实际使用的模型、参数和工具；同一个 Task 的 Run 串行；在模型配置不变时复用 TaskRuntimeSpace 中的模型 session，配置变化时为下一个 Run 替换 session。

## 1. 结论先行

本方案不再把模型配置作为 Task 的持久化配置。模型配置属于一次 Run，Task 只持有进程内缓存。

```text
TaskModel
  └── 任务、上下文、父子关系等持久化事实

ConversationRunModel.extra["model_runtime"]
  └── 本次 Run 实际使用的完整模型配置

TaskRuntimeSpace
  └── 当前 backend 进程内可复用的 TaskModelSession
      ├── base_model
      ├── tool-bound model
      ├── ToolDefinition
      └── tool schemas

RunModelBinding
  └── 当前 Run 的不可变内存绑定，不是数据库记录，不是版本号
```

核心语义：

1. 创建 Task 时只创建 Task 记录和 TaskRuntimeSpace，不创建模型 client。
2. 第一次创建 Run 时沿用当前模型解析流程，解析 profile、请求参数和工具策略，物化模型 session。
3. Run 创建时把完整有效配置写入 `ConversationRunModel.extra["model_runtime"]`。
4. 后续创建 Run 时：没有新的模型配置就继承最近一次 Run 的完整配置；有新配置就使用新配置。
5. 新 Run 的配置与 TaskRuntimeSpace 当前 session 相同时复用 session；不同时创建候选 session 并替换。
6. resume 使用被 resume 的 Run 自己保存的配置，不读取 Task 的“当前配置”。
7. 一次 Run 创建后，模型、参数、工具、system prompt 输入和 tool schemas 全程固定。
8. 不新增 SQLite 表，不增加 revision、generation、schema_version、ModelLease 或独立 token counter。

这个方案取消 Task 级 pending model config，因此不需要 Task 配置 PATCH、Task 配置版本、Task 创建时的模型物化，也不需要为主 Task 和 child Task 维护两套配置生命周期。

## 2. 运行边界和当前代码事实

### 2.1 进程和数据边界

功能运行在 Python/FastAPI 后端进程：

```text
Tauri Rust 主进程
  └── 管理 Python/FastAPI 后端生命周期

Python/FastAPI 后端
  ├── Task / Run / Context / Provider：SQLite
  ├── TaskRuntimeSpace：进程内 Task 协调和缓存
  ├── LangChain model client：进程内对象
  └── ToolRegistry / ToolExecutor：进程内工具系统
```

SQLite 保存声明式 Run 配置；`BaseChatModel`、Runnable、HTTP client、ToolDefinition handler 和 RuntimeContextManager 都是进程内对象。后端重启后不恢复 Python 对象，只能从 Run.extra 重新物化。

### 2.2 TaskRuntimeSpace 和 Task 锁

当前 `TaskRuntimeSpace` 已提供同一个 Task 的统一 operation lock：

- `ConversationRunCommandService` 在创建、编辑和 resume Run 时使用同步 operation lock；
- `ConversationRunExecutor._execute()` 在整个 runner 生命周期内持有异步 operation lock；
- 同一个 Task 的 Run、配置相关操作和删除操作不能交叉破坏 Context、Run 状态和 model session；
- 不同 Task 仍然可以并行。

模型 session 的替换必须发生在同一个 Task operation lock 内。当前 Run 持锁期间不替换 session，也不更新当前 Run 的模型或工具。

### 2.3 当前模型创建流程

当前 `ReactLikeWorkflow` 在 Run 执行时：

1. 通过 `resolve_chat_model(run, agent_profile)` 构建 `BaseChatModel`；
2. 从 `WorkflowOperations.model_tools` 生成 schemas；
3. 调用 `bind_tools()`；
4. 创建 `RuntimeConfig` 并运行 graph。

改造后，这些动作从 workflow 内移到 Run 创建/准备边界。workflow 只接收固定的 `RunModelBinding`，不再在每个 workflow 内重新解析 Task 或 Run 配置。

### 2.4 当前子 Agent 路径

`DelegationExecutor` 当前顺序是：

```text
解析 child AgentProfile
  -> 计算 decision.effective_tools
  -> TaskService.get_or_create_task(task_type="delegate_task")
  -> ConversationRunService.create_run(... child profile 的 provider/model)
  -> derive_for_run(... allowed_tools=decision.effective_tools)
  -> ChildAgentRunner.run_child()
```

改造后，child Task 仍通过同一个 `TaskService.get_or_create_task()` 创建，但该方法不负责模型物化。child Run 创建时把 child profile 和 `decision.effective_tools` 组装成完整 Run 配置，然后走与主 Run 相同的 session ensure 和 Run.extra 持久化流程。

这样 child Agent 不需要单独的 Task 模型配置，也不需要直接把 child profile 保存到 Task.extra。

### 2.5 当前 Context 和 usage 流程

`RuntimeContextManager` 保存 Task 级 canonical context working copy，`begin_run()` 接收当前 Run 的 tool schemas。`ContextUsageComputeListener` 负责按事件重新计算 context usage。

改造后，Run 开始时必须从 `RunModelBinding` 设置：

- 最终有效工具的 tool schemas；
- 当前 Run 的 system prompt 工具输入；
- 当前模型的 context window；
- listener 计算所需的完整输入。

不增加 Task session token counter，也不把 system prompt 重复写入 canonical history。

## 3. 持久化模型

### 3.1 Run.extra 是 Run 配置事实

每一个新 Run 都保存完整、规范化后的有效配置：

```json
{
  "model_runtime": {
    "provider_id": 1,
    "model_name": "gpt-5.6",
    "settings": {
      "temperature": null,
      "top_p": null,
      "max_tokens": 8192,
      "reasoning_effort": "high",
      "stream": true,
      "response_format": null
    },
    "tool_names": [
      "read_file",
      "search_files"
    ]
  }
}
```

字段语义：

- `provider_id`、`model_name`：本次 Run 使用的模型路由；
- `settings`：本次 Run 的完整有效模型参数，不保存 profile 覆盖差异；
- `tool_names`：经过工具策略收敛后的最终工具名称集合；
- 不保存 API key、Runnable、HTTP client、handler、Python callable、Pydantic class 或 prompt；
- 不保存 tool schema 快照，Run 恢复时按工具名称从当前 ToolRegistry 重新解析；
- `extra` 中其他命名空间由其他领域功能使用，模型配置只修改 `model_runtime`。

不增加 `schema_version`。这是 0-1 绿地项目，配置结构变化时直接同步代码和数据库使用方式，不提供旧数据兼容或迁移路径。

### 3.2 既有 Run 字段的关系

`ConversationRunModel.provider_id`、`model_name`、`reasoning_effort` 继续写入，因为现有查询、响应和日志可能使用这些字段。

它们是 `extra["model_runtime"]` 的结构化投影，不是另一套配置来源。创建或编辑 Run 时必须同时更新；执行恢复时以 `extra["model_runtime"]` 的完整配置为准。

### 3.3 TaskModel.extra 的范围

本方案不在 `TaskModel.extra` 中保存模型配置。Task.extra 继续承载既有 Task 级扩展字段，例如 fork 来源或其他业务元数据，但不再承担：

- Task 当前模型；
- Task 当前工具集合；
- Task 下一次 Run 的 pending 配置；
- 模型配置版本或 revision。

## 4. 配置继承和 Run 生命周期

### 4.1 第一个 Run

第一次创建 Run 时，配置来源沿用当前系统的解析顺序：

```text
主 Agent：请求中的 Run 配置 + AgentProfile 默认 settings
child Agent：child AgentProfile + decision.effective_tools
```

服务端将其规范化为完整 `model_runtime` 后校验、物化 session、创建 Run，并写入 Run.extra。

如果 provider、model 或必要配置无法解析，Run 创建失败，不创建一个没有可执行模型的成功 Run。

### 4.2 后续新 Run

新 Run 支持两种情况：

```text
没有 modelRuntime
  -> 读取该 Task 最近一次 Run.extra["model_runtime"]
  -> 原样作为新 Run 的配置基础

提供 modelRuntime
  -> 将其视为完整 desired config
  -> 不做隐式深层 merge
  -> 规范化、校验后作为新 Run 配置
```

推荐新 Run 的 `modelRuntime` 要么完全省略，要么提交完整配置。不要让客户端只提交一个 `reasoning_effort` 后由后端猜测其他参数来自哪一次 Run。

如果 UI 需要“先编辑配置，之后再发送消息”，可以在前端保存编辑草稿；后端只有创建下一个 Run 时才把该配置写入数据库。

### 4.3 配置不变时复用

TaskRuntimeSpace 保存最近一次成功物化的 session 及其规范化配置。新 Run 创建时：

- provider、model、完整 settings、tool names 都相同：复用 `base_model` 和 tool-bound Runnable；
- 任意一项不同：创建候选 model session；
- 候选成功后，替换 TaskRuntimeSpace 中的 idle session；
- 当前或已结束 Run 的 `RunModelBinding` 不被替换。

这里的“相同”是值对象相等比较，不是持久化 revision。没有任何配置版本字段。

### 4.4 resume

resume 不创建新的 Run，也不读取 Task 最近一次 Run 的配置。它只读取被 resume 的 Run 自己的 `extra["model_runtime"]`：

```text
resume cancelled Run
  -> 读取该 Run 的 model_runtime
  -> Task lock 内 ensure 对应 session
  -> 创建该 Run 的 RunModelBinding
  -> 继续原 checkpoint
```

因此 Task 之后创建了其他配置的 Run，也不会改变旧 Run 的 resume 语义。

### 4.5 edit/restart

当前 `edit_or_restart()` 复用同一个 `run_id`，删除旧 Context 并重新执行。保留这一持久化行为，但将它定义为新的执行基线：

- 如果 edit 请求没有 `modelRuntime`，保留该 Run 当前的 `model_runtime`；
- 如果 edit 请求提供了 `modelRuntime`，在同一个 Task lock 内替换该 Run 的 `extra["model_runtime"]` 和既有路由字段；
- 随后按新配置重新物化 session 和 binding；
- 不生成 revision，也不保留旧配置副本。

普通 resume 和 edit/restart 的区别仅在于：resume 保留该 Run 原配置，edit 可以显式替换配置。

## 5. TaskModelSession 和 RunModelBinding

### 5.1 TaskModelSession 的职责

建议新增 `TaskModelSession`，由 `TaskRuntimeSpace` 持有。它是一个可替换的进程内缓存，不是持久化事实：

- 保存最近一次物化的规范化配置；
- 通过现有 `build_chat_model()` 创建 `base_model`；
- 按最终工具名称解析 `ToolDefinition`；
- 生成 tool schemas 并调用 `bind_tools(strict=True)`；
- 保存 tool-bound Runnable；
- 提供关闭 HTTP client 的生命周期方法；
- 在配置相同时供多个串行 Run 复用。

它不负责：

- 决定新 Run 的配置来源；
- 修改 Run 或 Task 数据库事实；
- 执行 tool handler；
- 计算 token usage；
- 决定 Run 状态；
- 处理 Assistant Transport schema。

### 5.2 RunModelBinding 的职责

```python
@dataclass(frozen=True)
class RunModelBinding:
    config: RunModelConfig
    base_model: BaseChatModel
    model: Runnable
    model_tools: tuple[ToolDefinition, ...]
    tool_schemas: tuple[Mapping[str, Any], ...]
    thinking_channel: str
    vision_input_format: str
    context_window: int
```

`RunModelBinding` 只存在于当前 backend 进程。它把一次 Run 的模型、工具和能力解析结果固定在一起，避免 model node、tools node、RuntimeContextManager 和 listener 各自读取可变对象。

它不是：

- SQLite 行；
- 配置版本；
- 并发锁；
- ModelLease；
- Run.extra 的替代品。

Run 重启或 resume 时，可以从 Run.extra 重新物化一个新的 binding。

### 5.3 物化算法

```python
def materialize(config: RunModelConfig) -> RunModelBinding:
    base_model = build_chat_model(
        provider_id=config.provider_id,
        model_name=config.model_name,
        model_settings=config.settings,
    )

    tools = tuple(resolve_tool_definitions(config.tool_names))
    schemas = tuple(tool.to_model_tool_definition() for tool in tools)

    model = (
        base_model.bind_tools(schemas, strict=True)
        if schemas
        else base_model
    )

    return RunModelBinding(
        config=config,
        base_model=base_model,
        model=model,
        model_tools=tools,
        tool_schemas=schemas,
        thinking_channel=resolve_thinking_channel(config),
        vision_input_format=resolve_vision_format(config),
        context_window=resolve_context_window(config),
    )
```

`temperature`、`top_p`、`max_tokens`、`reasoning_effort`、`stream` 等 `ModelSettings` 由现有 model factory 在构造 `BaseChatModel` 时应用，不能全部盲目传给 `Runnable.bind()`。只有明确属于 provider 支持的调用级 kwargs 才允许额外 bind。

## 6. Run 创建和锁边界

### 6.1 新 Run 推荐流程

```text
Run 创建命令
  -> 取得 Task operation lock
  -> 检查同一 Task 没有 active Run
  -> 读取显式 modelRuntime，或读取最近 Run.extra
  -> 解析并严格校验完整 RunModelConfig
  -> 复用或构建候选 TaskModelSession
  -> 在同一数据库事务中创建 Run
  -> 写入 provider/model/reasoning 字段和 extra["model_runtime"]
  -> 安装成功 session
  -> 创建 RunModelBinding
  -> 启动 executor
```

候选 session 必须在 Run 成功创建前通过静态校验和模型/tool binding。候选失败时：

- 不创建成功 Run；
- 不替换现有 session；
- 不修改最近 Run 配置；
- 记录结构化错误日志。

如果数据库事务失败，候选 client 必须关闭，旧 session 保持不变。

### 6.2 pending Run 间隔

当前 command service 会先写入 pending Run，随后由 `ConversationRunExecutor.start()` 创建后台任务。这个间隔不能让 executor 读取 TaskRuntimeSpace 的“最新 session”作为事实，因为 session 可能在其他生命周期操作中被重新物化。

executor 应以 `ConversationRunRecord.extra["model_runtime"]` 为配置来源，在取得 Task lock 后 ensure 对应 session，再创建 binding。TaskRuntimeSpace 的 session 是缓存，Run.extra 才是本次 Run 的配置事实。

这样不需要把 Python binding 放入 `ConversationRunStartResult`，也不需要 pending binding registry。

### 6.3 配置变更的生效时间

配置不再通过独立 Task PATCH 持久化。配置只随新 Run 创建生效：

| 场景 | 行为 |
| --- | --- |
| 新 Run 未提供 modelRuntime | 继承最近 Run 配置 |
| 新 Run 提供 modelRuntime | 使用新配置 |
| 当前 Run 正在执行 | 新 Run 创建被现有 active-run 检查拒绝；当前 Run 不变 |
| resume | 使用被 resume Run 的配置 |
| edit/restart | 无配置则保留原配置，有配置则替换原 Run 配置 |

不新增 active marker、pending config 或专用并发检查。现有 Task lock 和 active Run 检查已经提供所需串行边界。

## 7. 工具集合规则

### 7.1 最终工具集合

Run.extra 保存最终有效工具集合，而不是客户端未经校验的请求集合：

```text
main Run:
  requested tools
  ∩ AgentProfile.allowed_tools
  ∩ ToolRegistry 当前可用工具

child Run:
  decision.effective_tools
  ∩ ToolRegistry 当前可用工具
```

推荐锁定以下行为：

- 客户端提交未知工具或未授权工具时，整体拒绝 Run 配置；
- 不静默取交集后继续执行；
- child Run 必须使用 delegation 已计算的 `decision.effective_tools`；
- `tool_names` 为空时不调用 `bind_tools([])`，直接使用未绑定的 base model；
- 工具 handler 和任意 schema 不允许由客户端提交。

### 7.2 ToolRegistry 变化

已固定的 Run 使用自己的 `ToolDefinition` 和 schemas，不受 ToolRegistry 后续变化影响。

新 Run 需要重新确认工具定义。可以使用当前 `ToolRegistry.generation` 做进程内 session 缓存失效判断，也可以在 ToolRegistry 变更时显式让相关 session 失效；这不是 Run 配置版本，也不写入 SQLite。

如果工具注册表在运行期允许动态变更，则相同 `tool_names` 也不能永久保证 schema 未变。下一次 Run 必须重新检查工具定义，工具不存在时创建失败。

## 8. Context、system prompt 和 token usage

### 8.1 Run 开始输入

workflow 使用 binding 初始化 RuntimeContextManager：

```python
runtime_context.begin_run(
    run,
    execution_mode,
    tool_schemas=binding.tool_schemas,
    context_window=binding.context_window,
    effective_tools=binding.model_tools,
)
```

实际参数名按最终代码接口调整，但语义必须保持：system prompt、tool schemas、model context window 和 model binding 都来自同一个 Run。

### 8.2 system prompt

当前 `RuntimeContextManager` 初始化时会根据首次传入的 `AgentProfile.allowed_tools` 构建 system entry。改造后不能继续只依赖 profile，因为后续 Run 的工具集合可能变化。

每次 Run 开始都应使用最终有效工具集合重建本次 Run 的非持久化 system entry：

- system prompt 展示实际可用工具；
- 不把 system prompt 追加为历史消息；
- 不修改 canonical context history；
- 旧 Run 不读取新 Run 的工具集合。

### 8.3 listener 负责全部 usage 计算

`ContextUsageComputeListener` 继续是 token usage 的唯一计算实现：

```text
used_tokens = tokenize(
    current system prompt
    + canonical context messages
    + current Run tool schemas
)

total_tokens = current Run model context window
```

Run 开始设置新的 tool schemas 和 context window 后，listener 重新计算。不要新增独立 counter，不要在 TaskModelSession 中累加 token。

当前 listener 的 `main_agent_only=True` 仍然是一个产品统计口径：如果 usage 只展示主 Task，保持现状；如果要求 child Run 也统计 system prompt/tool schemas，需要移除该限制或为 child 注册对应 listener。

## 9. 主 Task、child Task 和 fork

### 9.1 主 Task

`WorkspaceService.create_task()` 和 `TaskService.get_or_create_task()` 只创建 Task 容器及 runtime space，不接收 `model_config`，不构建模型。

主 Run 创建时负责：

1. 从请求和 AgentProfile 组装 `RunModelConfig`；
2. ensure TaskRuntimeSpace session；
3. 创建并持久化 Run；
4. 启动固定 binding。

### 9.2 child Task

child Task 创建流程改为：

```text
DelegationExecutor
  -> 解析 child AgentProfile
  -> 计算 decision.effective_tools
  -> TaskService.get_or_create_task(... task_type="delegate_task")
  -> 组装 child RunModelConfig
  -> 创建 child Run，并写入 Run.extra
  -> ensure child TaskRuntimeSpace session
  -> ChildAgentRunner.run_child()
```

child Task 不需要 Task 级模型配置，也不开放通用 Task 配置 API。child 的模型和工具事实属于 child Run；同一个 delegation 的 child Run 重试/resume 使用它自己的 Run.extra。

### 9.3 fork

当前 `ConversationRunCrud.clone_for_task()` 已复制源 Run 的 `extra`。因此 fork 不需要复制 Task 模型配置：

- 历史 cloned Run 保留源 Run 的 `model_runtime`；
- fork Task 的 TaskRuntimeSpace 初始为空；
- fork 后创建新 Run 且未提供新配置时，继承 fork Task 最近 cloned Run 的配置；
- 如果用户需要换模型或工具，在创建 fork 后的第一个新 Run 时显式提交 `modelRuntime`。

这使 fork 的配置自然跟随被复制的 Context/Run 边界，不需要额外的 fork 配置字段。

## 10. API 和领域契约

### 10.1 Task 创建

Task 创建请求不再要求模型配置：

```json
{
  "text": "分析这个项目"
}
```

Task API 不创建模型 client，也不承诺 Task 已经具备可调用模型。

### 10.2 Run 创建

Run 创建命令增加可选完整 `modelRuntime`：

```json
{
  "input": "继续分析",
  "modelRuntime": {
    "providerId": 1,
    "modelName": "gpt-5.6",
    "settings": {
      "temperature": null,
      "topP": null,
      "maxTokens": 8192,
      "reasoningEffort": "high",
      "stream": true
    },
    "toolNames": ["read_file", "search_files"]
  }
}
```

`modelRuntime` 的语义：

- 第一个 Run：可以由当前请求和 AgentProfile 共同解析；
- 后续 Run：省略时继承最近 Run；显式传入时视为完整 desired config；
- 不做隐式深层 merge；
- 不支持 Run 运行中修改。

### 10.3 Assistant Transport

Assistant add-message 不再维护一套独立的 provider/model/reasoning 覆盖事实。Run 创建命令统一负责解析完整 model runtime。

重复 command 只 attach 原有 Run，不重新计算模型配置；原 Run 的 Run.extra 仍然是唯一配置来源。

### 10.4 Run response

Run response 可以返回脱敏后的实际配置摘要：

- provider id；
- model name；
- normalized settings；
- effective tool names。

不返回 API key、Runnable、HTTP client、handler 或完整内部 prompt。

## 11. Provider 和资源生命周期

### 11.1 Provider 配置变化

Run.extra 只保存 provider id，Provider 的 base URL、API key 和 enabled 状态仍由 Provider 表维护。

TaskRuntimeSpace 中缓存的 HTTP client 不会自动读取 Provider 更新。因此 ProviderService 更新或删除 Provider 时，应让使用该 provider 的进程内 Task sessions 失效；下一次 Run 从 Run.extra 重新构建 client。

这不需要新增 SQLite 字段或配置版本。可以由 `TaskRuntimeSpaceRegistry` 提供按 provider 失效入口，也可以在 session ensure 时检查现有 Provider 记录变化。当前 Run 不替换 client，Provider 变化只影响后续 Run。

### 11.2 session 替换

配置变化时使用候选替换：

```text
Task lock
  -> 解析并校验 RunModelConfig
  -> 构建候选 base_model / tool-bound model
  -> 候选成功
  -> 创建或提交 Run
  -> 安装候选 session
  -> 关闭旧 idle session
```

候选失败时旧 session 和旧 Run 都保持不变。

### 11.3 Task 删除和 backend close

删除 Task 或关闭 backend 时：

1. 复用现有 workspace/task 操作边界；
2. 等待或取消 active Run 收束；
3. 调用 TaskModelSession.close() 关闭 `httpx.Client` 和 `AsyncClient`；
4. 再 unload TaskRuntimeSpace；
5. backend close 时逐个关闭所有 session，不能只清空 registry 引用。

后端重启后，TaskRuntimeSpace 为空；下一次新 Run、resume 或 edit 时，从对应 Run.extra 重新物化。

## 12. 代码改造边界

### 12.1 新增运行时值对象

建议新增：

```text
apps/backend/app/task_runtime/model_config.py
    RunModelConfig
    ModelSettings 复用现有实现或窄适配
    Run.extra 解析/序列化/严格校验

apps/backend/app/task_runtime/task_model_session.py
    TaskModelSession
    RunModelBinding
    base_model / tool-bound model 生命周期
```

不新增 storage model、CRUD 表或 schema migration。

### 12.2 TaskRuntimeSpace

增加 session 槽位：

```python
_model_session: TaskModelSession | None

def ensure_model_session(config: RunModelConfig) -> TaskModelSession: ...
def create_run_binding(config: RunModelConfig) -> RunModelBinding: ...
def close_model_session() -> None: ...
```

`ensure_model_session()` 只在 Run 创建、resume、edit 或 executor 恢复 Run 时调用，不在 Task 列表/详情查询时调用。

### 12.3 TaskService

`TaskService.get_or_create_task()` 保持统一 Task 创建入口，但不再接收 `model_config`：

- 新建 Task：创建 Task 记录；
- 获取已有 Task：登记或取得 TaskRuntimeSpace；
- 不创建 base_model；
- 不写入 Task.extra.model_runtime；
- child Task 和主 Task 继续共用这条入口。

模型配置由 Run 创建服务组装并交给 TaskRuntimeSpace。

### 12.4 ConversationRunService / CommandService

Run 创建服务负责：

- 解析显式 modelRuntime 或继承最近 Run；
- 组装完整 RunModelConfig；
- 校验 provider、model、settings 和 tools；
- 在 Task lock 内 ensure session；
- 同一数据库事务写入 Run 路由字段和 Run.extra；
- 为 executor 提供可恢复的 Run 记录。

当前 `start_or_attach()`、`edit_or_restart()`、`resume_latest_run()` 都必须进入同一配置流程，但 attach 已有 command 时不能重新生成配置。

### 12.5 Workflow 和 RuntimeConfig

workflow 不再：

- 调用 `resolve_chat_model()` 创建模型；
- 从 AgentProfile 临时推导最终工具集合；
- 在每个 model step 读取 Task 或 Run 的可变配置；
- 对 `bind_tools()` 失败静默降级。

workflow 接收固定 `RunModelBinding`，将其中的 Runnable、model_tools、tool schemas 和能力字段注入 `RuntimeConfig` 和 RuntimeContextManager。

### 12.6 Model factory

继续使用现有 `build_chat_model()` 作为唯一 BaseChatModel 创建入口。TaskModelSession 不复制 Provider、reasoning effort、disabled params、extra_body 或 HTTP client 构造逻辑。

`ModelSettings` 在构造 `BaseChatModel` 时应用；`bind_tools()` 只负责工具绑定，不承担通用模型参数动态修改。

## 13. 错误、恢复和日志

### 13.1 配置错误

以下错误阻止 Run 创建：

- provider 不存在、禁用或模型不属于 provider 能力；
- settings 字段未知或组合不支持；
- 工具不存在或不在 profile/delegation 允许集合；
- 模型不支持本次非空工具集合的 `bind_tools()`；
- tool schema 被 provider 拒绝。

错误不会创建一个看似成功但不可执行的 Run，也不会替换当前 session。

### 13.2 取消和重启

取消当前 Run 后，Run 仍保留其原有 model_runtime。下一个新 Run 可以继承它，也可以显式提交新配置。

backend 重启时不自动重放旧模型调用或工具调用。遗留 active Run 按现有恢复规则收敛为 cancelled；用户 resume 时从该 Run.extra 重新物化。

### 13.3 日志

记录以下结构化事件：

- Run model runtime resolved；
- Task model session reused；
- Task model session replaced；
- model/tool materialization failed；
- provider/session invalidated；
- model session closed。

日志只记录 task_id、run_id、provider_id、model_name、工具名称和错误类型，不记录 API key、完整 prompt、完整 tool args 或模型正文。

## 14. 实现顺序

### Phase 0：Run 配置契约

1. 定义 `RunModelConfig` 和 `RunModelBinding`。
2. 固定 `Run.extra["model_runtime"]` JSON 结构。
3. 删除 TaskModel.extra.model_runtime、revision 和 schema_version 设计。
4. 增加 Run 配置严格解析、序列化和脱敏测试。

### Phase 1：TaskModelSession

1. 在 TaskRuntimeSpace 中加入 model session 槽位。
2. 复用 `build_chat_model()` 构建 base model。
3. 解析工具、生成 schemas、调用 bind_tools。
4. 实现配置相同复用、配置变化替换。
5. 实现 close、Task unload 和 backend close 的 client 清理。

### Phase 2：Run 创建和恢复

1. 第一次 Run 沿用当前 profile/request 解析流程。
2. 后续 Run 无配置时继承最近 Run.extra。
3. Run 创建时写入完整 model_runtime。
4. resume/edit/executor 都从 Run.extra 恢复 binding。
5. 删除 workflow 内重复创建模型和工具绑定的逻辑。

### Phase 3：Context 和 usage

1. Run 开始使用 binding 设置 system prompt 工具输入。
2. 设置 tool schemas 和 context window。
3. 由 ContextUsageComputeListener 计算 system prompt、messages 和 tool schemas。
4. 根据产品决定 child Run 是否纳入 listener 统计。

### Phase 4：主 Task、child、fork

1. 修改 `DelegationExecutor`，child Run 使用 child profile 和 decision.effective_tools。
2. 修改 fork 复制和新 Run 继承测试。
3. 更新 Assistant Transport 的 Run 配置请求。
4. 删除旧的 Task 配置入口和 workflow 级模型覆盖入口。

## 15. 验收测试

### 15.1 Run 配置

- Task 创建不创建 model client。
- 第一个 Run 保存完整 `extra["model_runtime"]`。
- 后续 Run 无配置时完整继承最近 Run，而不是只继承 provider/model。
- 后续 Run 显式配置时使用新配置，不做隐式深层 merge。
- 配置不变时复用 TaskRuntimeSpace session。
- provider/model/settings/tool names 任一变化时创建新的候选 session。
- 物化失败时不创建成功 Run、不替换旧 session。
- Run.extra 与既有 provider/model/reasoning 字段保持一致。

### 15.2 Run 生命周期

- 同一 Task 不能同时有两个 active Run。
- 当前 Run 的模型、工具和参数不会被后续 Run 改变。
- resume 使用被 resume Run 的 Run.extra。
- edit/restart 无新配置时保留原配置，有新配置时替换原配置。
- executor 在 pending 间隔后仍按 Run.extra 恢复正确配置。

### 15.3 child 和 fork

- child Task 不创建 Task 级模型配置。
- child Run 使用 `decision.effective_tools`，不使用未收窄的 child profile 原始工具集合。
- child Run 的 model_runtime 与主 Run 使用同一 JSON 结构。
- fork cloned Run 复制源 Run.extra。
- fork 后新 Run 无配置时继承最近 cloned Run 的配置。

### 15.4 Context 和 usage

- system prompt 展示本次 Run 最终工具集合。
- tool schemas 计入 listener 的 used tokens。
- system prompt 计入 listener 的 used tokens。
- 新 Run 的 context window 影响 total tokens 和 ratio。
- system prompt 不重复写入 canonical history。
- 已固定 Run 不受 ToolRegistry 后续变化影响。

### 15.5 生命周期清理

- 删除 Task 会关闭 model session client。
- backend close 会关闭所有 Task session client。
- Provider 更新/删除后，后续 Run 不继续使用失效 session。
- backend 重启后可以从 Run.extra 恢复新 Run 或 resume 所需 session。
- 日志和 API 响应不泄露密钥、完整 prompt 或工具参数。

## 16. 仍需产品确认的边界

采用本方案后，需要确认的内容明显减少：

1. 后续 Run 未提供 `modelRuntime` 时，是否始终继承最近一次 Run。推荐是。
2. `modelRuntime` 是否要求完整配置而不是 PATCH。推荐是。
3. child Run 是否纳入 ContextUsageComputeListener 统计。主 Task usage 口径保持现状则不纳入；需要全量 usage 则移除 `main_agent_only` 限制。
4. Provider 更新后是否让后续 Run 重建 session。推荐是，当前 Run 不受影响。
5. ToolRegistry 动态变化后是否让后续 Run 重新解析工具。推荐是。

以下内容已经锁定，不再作为产品选项：

- 不新增 SQLite 表；
- 不新增 Task 模型配置；
- 不新增 revision、generation、schema_version 或配置版本字段；
- 不支持 Run 执行中切换模型、工具或参数；
- Run.extra 保存完整有效配置；
- TaskRuntimeSpace 只保存进程内缓存；
- RunModelBinding 只保存进程内不可变执行引用；
- 不提供旧 Task 或旧配置兼容路径；
- 不新增独立 token counter。

## 17. 调研来源

### LangChain / LangGraph 官方来源

- [BaseChatModel 源码](https://github.com/langchain-ai/langchain/blob/master/libs/core/langchain_core/language_models/chat_models.py)
- [Runnable 基础实现](https://github.com/langchain-ai/langchain/blob/master/libs/core/langchain_core/runnables/base.py)
- [Runnable configurable 实现](https://github.com/langchain-ai/langchain/blob/master/libs/core/langchain_core/runnables/configurable.py)
- [ChatOpenAI 源码与 bind_tools 实现](https://github.com/langchain-ai/langchain/blob/master/libs/partners/openai/langchain_openai/chat_models/base.py)
- [LangGraph 动态模型与工具绑定说明](https://github.com/langchain-ai/langgraph/blob/main/libs/prebuilt/langgraph/prebuilt/chat_agent_executor.py)

### 本项目主要调研位置

- `apps/backend/app/storage/model/task_model.py`
- `apps/backend/app/storage/model/conversation_run_model.py`
- `apps/backend/app/storage/crud/conversation_run_crud.py`
- `apps/backend/app/core/llm_provider/model_factory.py`
- `apps/backend/app/core/runtime/runner.py`
- `apps/backend/app/core/workflows/react/workflow.py`
- `apps/backend/app/core/workflows/react/runtime_config.py`
- `apps/backend/app/core/workflows/nodes/model_node.py`
- `apps/backend/app/core/workflows/nodes/tools_node.py`
- `apps/backend/app/task_runtime/task_runtime_space.py`
- `apps/backend/app/task_runtime/task_runtime_space_registry.py`
- `apps/backend/app/task_runtime/service/task_service.py`
- `apps/backend/app/core/context/runtime_context_manager.py`
- `apps/backend/app/core/context/context_listener/context_usage_compute_listener.py`
- `apps/backend/app/core/context/system_prompt_builder.py`
- `apps/backend/app/core/tools/tool_registry.py`
- `apps/backend/app/core/delegation/delegation_executor.py`
- `apps/backend/app/core/delegation/child_agent_runner.py`
- `apps/backend/app/assistant_transport/service/conversation_run_command_service.py`
- `apps/backend/app/assistant_transport/service/conversation_run_executor.py`
