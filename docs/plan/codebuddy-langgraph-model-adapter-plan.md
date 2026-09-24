# CodeBuddy SDK 作为 LangGraph 模型的适配方案

状态：方案设计

## 1. 结论

CodeBuddy Python SDK 可以作为本项目的一个 LangChain/LangGraph Chat Model 使用，前提是只使用它的模型响应能力，不使用 CodeBuddy 的内置工具、MCP、子 Agent、Skills、Commands 或项目配置。

适配目标不是继承 `ChatOpenAI`，而是实现一个 `BaseChatModel` 适配器：

```text
CodeBuddy Python SDK
        │
        │  TextBlock / ThinkingBlock / ResultMessage
        ▼
CodeBuddyChatModel : BaseChatModel
        │
        │  AIMessageChunk / AIMessage
        ▼
LangGraph model node
```

LangGraph 的 StateGraph 节点只依赖模型的 LangChain 接口；模型的具体接入方式、消息转换和进程控制属于 `llm_provider`。这符合 LangGraph 对“节点调用模型、图负责状态和流程”的边界设计。[LangGraph Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api)

CodeBuddy SDK 的 Python API 是异步的，`query()` 返回异步消息迭代器，消息包括 `AssistantMessage`、`ThinkingBlock`、`ToolUseBlock` 和 `ResultMessage`。[CodeBuddy Python SDK 参考](https://www.codebuddy.cn/docs/cli/sdk-python)

## 2. 目标与非目标

### 2.1 目标

- CodeBuddy 作为一个可替换的 `BaseChatModel`。
- 支持 LangGraph 当前的 `model.astream(messages)` 调用方式。
- 支持文本流式输出。
- 支持思考内容流式输出，并映射到现有的 reasoning 管道。
- 支持 system、human、assistant 历史消息转换。
- 支持模型名、工作目录、认证环境变量和资源限制配置。
- 不执行 CodeBuddy 内置工具。
- 不加载 CodeBuddy 项目配置、MCP、Skills、Commands、Rules 或 Subagents。
- 不把 CodeBuddy 会话作为本项目的第二套对话事实源。
- 继续由本项目的 LangGraph checkpoint、RuntimeContextManager 和 Conversation Run 管理对话事实及恢复。

### 2.2 非目标

- 不实现 CodeBuddy 的 Agent 工具循环。
- 不把 CodeBuddy 的 `ToolUseBlock` 转成 LangChain `AIMessage.tool_calls`。
- 不支持 CodeBuddy MCP 工具、内置 Read/Bash/Write 工具或 AskUserQuestion。
- 不让 CodeBuddy 自己维护跨 Run 的会话历史。
- 不把 CodeBuddy HTTP API 或 CLI 当作公网服务部署。
- 第一阶段不承诺图像、视频和其他多模态输入。

## 3. 官方接口依据

### 3.1 CodeBuddy SDK

根据官方 Python SDK 文档：

- Python 要求为 3.10 及以上；本项目后端要求 Python 3.11，满足版本条件。
- SDK 使用 `asyncio`。
- `query()` 接收 prompt 并返回异步消息迭代器。
- SDK 支持 `CodeBuddyAgentOptions`，包括 `model`、`cwd`、`system_prompt`、`max_turns`、`permission_mode`、`allowed_tools`、`disallowed_tools`、`setting_sources`、`persist_session` 和 `can_use_tool` 等选项。
- 未设置 `CODEBUDDY_CODE_PATH` 时，SDK 会优先查找 SDK 包内置的 CodeBuddy CLI 二进制文件。
- SDK 默认不加载文件系统配置；如需加载，必须显式指定 `setting_sources`。
- SDK 返回的 `AssistantMessage` 内容由多个 content block 组成，文本和思考分别由 `TextBlock`、`ThinkingBlock` 表示。

因此，SDK 适合作为本地后端内的异步模型适配器，但它不是 OpenAI Chat Completions 协议的直接实现。

### 3.2 LangChain/LangGraph

LangChain 的自定义 Chat Model 应继承 `BaseChatModel`，至少实现 `_generate` 和 `_llm_type`，流式模型可以实现 `_astream`。本项目已经通过 `BaseChatModel` 类型承载不同供应商模型。

LangGraph 的 `astream()` 可以消费模型的消息流；官方文档说明，`messages` stream mode 会转发 LangChain 模型的消息 chunk。如果底层能力不能直接作为 LangChain 集成，也可以从节点通过 `custom` stream mode 转发自定义数据。[LangGraph Streaming](https://docs.langchain.com/oss/python/langgraph/streaming)

本方案选择前一种方式：CodeBuddy 适配器实现标准 LangChain `AIMessageChunk`，继续使用当前模型节点和已有 snapshot 投影链路，不新增 CodeBuddy 专用的 workflow stream 协议。

LangGraph 的持久化由 graph checkpointer 按 `thread_id` 管理短期线程状态；本项目另外由 RuntimeContextManager 管理 canonical context。因此适配器不应再使用 CodeBuddy `resume` 保存第二份对话历史。[LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)

## 4. 当前项目边界

当前模型工厂位于：

`apps/backend/app/core/llm_provider/model_factory.py`

当前 ReAct-like workflow：

1. 根据 provider/model 构建 `BaseChatModel`；
2. 尝试调用 `bind_tools()`；
3. 在 model node 中调用 `model.astream(messages)`；
4. 将 `AIMessageChunk` 交给 RuntimeContextManager、LangGraph custom stream 和 Transport snapshot。

CodeBuddy 适配器应接入第 1 步和第 2 步之间，不能把 CodeBuddy 类型泄漏到 workflow、RuntimeContextManager、Assistant Transport 或 storage。

## 5. “禁用工具”的确定性策略

仅仅把 `allowed_tools=[]` 作为禁用工具的依据不够稳妥，因为该字段是工具允许策略的一部分，不应被解释为一个稳定的“纯模型模式”契约。

适配器需要采用多重防线：

```python
CodeBuddyAgentOptions(
    model=model_name,
    cwd=workspace_path,
    system_prompt=merged_system_prompt,
    max_turns=1,
    allowed_tools=[],
    disallowed_tools=known_disallowed_tools,
    mcp_servers={},
    agents={},
    setting_sources=[],
    persist_session=False,
    include_partial_messages=True,
    can_use_tool=deny_all_tools,
)
```

具体规则：

1. `setting_sources=[]`，不读取用户、项目或本地 CodeBuddy 设置。
2. `mcp_servers={}`，不注入 MCP 服务器。
3. 不注册 SDK custom tools。
4. `can_use_tool` 对任何工具调用返回拒绝，不让工具真正执行。
5. `max_turns=1`，避免一次 LangGraph model step 变成 CodeBuddy 内部多轮 Agent 执行。
6. `persist_session=False`，避免写入 CodeBuddy transcript 和 session 持久化事实。
7. 如果 SDK 仍产生 `ToolUseBlock`，适配器不将其映射成 LangChain `tool_calls`。
8. 如果工具调用导致 SDK 最终没有可用文本，适配器返回明确的“模型尝试调用被禁用工具”错误，不静默返回空消息。

这里的“禁用工具”含义是“不允许工具执行”。CodeBuddy SDK 仍然是 Agent SDK，模型可能产生一次工具意图；适配器必须将该意图视为被拒绝的模型输出，而不是交给当前 LangGraph 的工具节点执行。

## 6. 适配器设计

### 6.1 目录结构

建议新增：

```text
apps/backend/app/core/llm_provider/
├─ adapters/
│  ├─ __init__.py
│  └─ codebuddy/
│     ├─ __init__.py
│     ├─ chat_model.py
│     ├─ message_codec.py
│     ├─ stream_mapper.py
│     ├─ options.py
│     ├─ errors.py
│     └─ runner.py
├─ model_factory.py
└─ capability/
   ├─ provider_capability.py
   └─ llm_provider.json
```

### 6.2 `CodeBuddyChatModel`

`CodeBuddyChatModel` 继承 `BaseChatModel`，不继承 `ChatOpenAI`。

主要职责：

- 保存已经解析的 CodeBuddy provider 配置；
- 将 LangChain messages 转换为 CodeBuddy prompt/options；
- 调用 CodeBuddy SDK；
- 将 SDK 消息流转换为 LangChain message chunks；
- 处理取消、错误和结果元数据；
- 明确不实现工具绑定。

建议接口：

```python
class CodeBuddyChatModel(BaseChatModel):
    model: str
    cwd: str | None = None
    codebuddy_code_path: str | None = None
    env: dict[str, str] = {}
    max_turns: int = 1
    persist_session: bool = False

    @property
    def _llm_type(self) -> str:
        return "codebuddy-agent-sdk"

    async def _astream(...):
        ...

    async def _agenerate(...):
        ...

    def _generate(...):
        ...
```

`_generate` 需要提供同步兼容入口，但后端 workflow 的主路径使用原生异步 `_astream`。同步入口不能直接在已有事件循环中调用 `asyncio.run()`；应使用项目统一的异步到同步桥接方式，或在独立线程事件循环中执行。

### 6.3 消息输入转换

CodeBuddy SDK 文档公开的稳定入口是字符串 prompt 和异步消息输入流。第一阶段不依赖未在文档中明确约定的结构化输入字典，而是在适配器中完成可审计的文本编码：

```text
[SYSTEM]
系统消息一
系统消息二

[CONVERSATION]
[USER]
用户消息

[ASSISTANT]
历史助手消息

[USER]
本轮用户消息
```

转换规则：

- `SystemMessage` 合并为 `CodeBuddyAgentOptions.system_prompt`；
- `HumanMessage`、历史 `AIMessage` 转换为带角色标记的 prompt 文本；
- `ToolMessage` 不应该出现在 CodeBuddy 纯模型路径；若出现，适配器记录受控 warning，并以普通上下文文本表示，不执行工具；
- 图片、文件和其他非字符串 content 第一阶段拒绝或使用已有文本降级规则，不能静默丢失；
- prompt 编码器必须是纯函数，单独测试，不写数据库、不访问工作区。

这样做的目的不是复制 CodeBuddy 会话，而是把 LangGraph 当前 canonical context 的本轮消息快照传给一次 CodeBuddy model request。

### 6.4 消息输出转换

`runner.py` 读取 CodeBuddy SDK 异步消息：

| CodeBuddy 消息 | LangChain 输出 | 处理方式 |
|---|---|---|
| `AssistantMessage` + `TextBlock` | `AIMessageChunk.content` | 原样按流输出文本 |
| `AssistantMessage` + `ThinkingBlock` | `AIMessageChunk.additional_kwargs["reasoning"]` | 复用当前 reasoning 提取管道 |
| `ToolUseBlock` | 不转换为 `tool_calls` | 记录被禁用工具意图并拒绝执行 |
| `ToolResultBlock` | 不进入模型输出 | 记录异常诊断，不执行任何外部工具 |
| `ResultMessage(success)` | `response_metadata.finish_reason = "stop"` | 标记正常完成 |
| `ResultMessage(is_error=True)` | 适配器异常 | 映射到项目模型错误分类 |

CodeBuddy 的 `ThinkingBlock` 没有直接对应当前项目的 `reasoning_content` 字段，因此 capability 中将其声明为通用 `reasoning` channel，由现有 `ModelChunkProcessor` 提取。

### 6.5 工具绑定行为

CodeBuddyChatModel 不实现真正的 LangChain tool calling。

`bind_tools()` 有两种可接受方案：

1. 保持 `BaseChatModel` 默认的 `NotImplementedError`，让当前 workflow 的已有降级逻辑继续使用不绑定工具的模型；
2. 在适配器中显式抛出带有 provider/model 信息的 `NotImplementedError`，便于日志排查。

不允许返回一个看似已绑定、实际丢弃工具 schema 的 Runnable，因为这会造成调用方误以为模型拥有工具能力。

当前 workflow 已经对 `bind_tools()` 不支持做防御性降级，因此第一阶段不需要写 CodeBuddy 专用的 workflow 分支。

## 7. 生命周期与进程边界

运行拓扑：

```text
Tauri Rust 主进程
└─ Python/FastAPI 后端
   └─ CodeBuddyChatModel
      └─ CodeBuddy Python SDK
         └─ SDK 包内置或 CODEBUDDY_CODE_PATH 指向的 CLI 子进程
```

本项目仍由 Tauri Rust 主进程拥有后端生命周期。CodeBuddy CLI 子进程由 Python SDK/适配器间接创建，不新增独立业务服务。

第一阶段优先使用一次性 `query()`：

- 每次 LangGraph model step 产生一次 CodeBuddy SDK 查询；
- 读取到 `ResultMessage` 后结束本次迭代；
- SDK 异步迭代器结束后释放 CLI 子进程；
- 不保存 CodeBuddy session id；
- 不使用 `resume` 或 `continue_conversation`。

取消路径：

1. LangGraph/Run cancellation 触发适配器异步任务取消；
2. 适配器调用 SDK query 的 interrupt/关闭机制（若当前 SDK 版本提供 query interrupt，则优先调用）；
3. 关闭异步迭代器；
4. 确认 CodeBuddy CLI 子进程及其子进程树不残留；
5. 原有 workflow 负责将 Run 收敛为 `cancelled`。

如果 SDK 的一次性 `query()` 无法提供可靠的中断句柄，必须在 `runner.py` 中保留 SDK 句柄并增加进程树清理兜底；不能只取消 Python task 而留下 CLI 子进程。

## 8. Provider 与模型工厂改造

### 8.1 能力元数据

在 `llm_provider.json` 增加 CodeBuddy provider 能力，例如：

```json
{
  "codebuddy": {
    "provider_type": "agent_sdk",
    "adapter_kind": "codebuddy",
    "models": [],
    "accepts_arbitrary_model": true,
    "thinking_channel": "reasoning",
    "supports_external_tools": false,
    "vision_input_format": ""
  }
}
```

`models: []` 不能继续被解释为“没有可用模型”。CodeBuddy 模型由 SDK/服务端配置决定，因此能力类需要增加显式的 `accepts_arbitrary_model` 或等价字段。不能用 `model_name` 前缀推断 CodeBuddy 模型能力。

### 8.2 工厂选择

`model_factory.py` 保持统一入口，但将构建分派到适配器注册表：

```text
resolve_chat_model()
    └─ build_chat_model()
       └─ adapter_registry.resolve(capability.adapter_kind)
          ├─ openai_chat -> ChatOpenAI
          └─ codebuddy   -> CodeBuddyChatModel
```

CodeBuddy 分支不得把 `api_key`、`base_url`、`temperature`、`reasoning_effort` 等 OpenAI 参数机械透传给 SDK。只有 CodeBuddy SDK 文档明确支持的字段才能进入 `CodeBuddyAgentOptions`。

认证优先级：

1. provider 显式配置的 CodeBuddy API Key/Token，传入 SDK `options.env`；
2. 当前用户已经登录的 CodeBuddy 凭据；
3. 两者都没有时，在构建或首个请求阶段返回受控认证错误。

密钥不能进入日志、`to_dict()`、Transport snapshot 或异常正文。

### 8.3 依赖与打包

后端 `pyproject.toml` 增加固定版本的 `codebuddy-agent-sdk`，并更新 `uv.lock`。

由于 SDK 可能依赖包内置 CLI 二进制，发布构建必须验证：

- Windows 开发环境可以从 SDK 包中发现二进制；
- PyInstaller 构建结果包含对应平台的 SDK 二进制和必要资源；
- `CODEBUDDY_CODE_PATH` 显式覆盖仍然有效；
- 后端启动时不因缺少 CodeBuddy 配置而阻塞其他 provider；
- CodeBuddy provider 首次调用失败时，错误能落到 backend 日志和 Run failure，而不是只出现在 console。

## 9. 配置和事实所有权

### 9.1 数据库事实

Provider/model 表继续保存：

- provider 名称和类型；
- CodeBuddy 模型名；
- 是否启用；
- 非敏感显示配置；
- 必要的认证引用或密钥字段，沿用现有 provider secret 处理约定。

### 9.2 运行时状态

以下内容只存在于当前后端进程的适配器运行对象中：

- CLI 子进程句柄；
- 当前 query 异步迭代器；
- 当前请求取消事件；
- 本轮临时 stream buffer；
- CodeBuddy SDK 的临时结果元数据。

以下内容不由 CodeBuddy 适配器持久化：

- LangGraph checkpoint；
- Conversation Run 状态；
- canonical context；
- Transport snapshot；
- CodeBuddy session transcript。

LangGraph 官方文档将 checkpointer 定义为线程范围的短期状态持久化机制；本项目已经有 SQLite checkpoint 和 RuntimeContextManager，因此不应再引入 CodeBuddy session 作为恢复事实源。[LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)

## 10. 错误、日志和取消

适配器至少需要区分：

- CLI 找不到或内置二进制不可用；
- SDK/CLI 启动失败；
- 认证失败；
- 网络或模型服务失败；
- CodeBuddy 工具调用被拒绝；
- SDK 返回错误结果；
- 用户取消；
- CLI 子进程异常退出；
- 输出为空或没有正常 `ResultMessage`。

日志使用后端结构化日志：

```text
codebuddy_model_request_started
codebuddy_model_stream_failed
codebuddy_tool_call_blocked
codebuddy_model_request_completed
codebuddy_process_cleanup_failed
```

日志字段只记录 provider、model、task_id、run_id、trace_id、错误分类和输出长度，不记录 token、密码、完整 prompt、完整模型输出或工作区敏感内容。

## 11. 测试方案

### 11.1 纯单元测试

- `message_codec`：system/human/assistant 消息顺序和编码；
- 多条 system message 合并；
- 非字符串 content 的拒绝或降级；
- `TextBlock` → `AIMessageChunk`；
- `ThinkingBlock` → `additional_kwargs["reasoning"]`；
- `ResultMessage` → `finish_reason=stop`；
- `ToolUseBlock` 不产生 `tool_calls`；
- 所有 `can_use_tool` 回调都返回 deny；
- `bind_tools()` 明确抛出 `NotImplementedError`；
- SDK 异常到项目错误分类的映射；
- prompt、错误和日志不泄漏密钥。

### 11.2 Fake SDK runner 测试

不启动真实 CLI，用 fake async iterator 覆盖：

- 多个文本 block 连续流出；
- 文本与思考交错；
- SDK 返回错误 ResultMessage；
- 异步取消时调用 interrupt/close；
- CLI 异常退出；
- 工具调用被拒绝后无文本结果。

### 11.3 真实本机冒烟测试

单独增加需要 CodeBuddy CLI/SDK 凭据的测试标记，不进入普通 pytest：

- `ainvoke` 返回 `AIMessage`；
- `astream` 能产生文本 chunk；
- SDK 包内置 CLI 被正确发现；
- `CODEBUDDY_CODE_PATH` 覆盖路径生效；
- 不加载项目 MCP、Skills 和 settings；
- 不产生 CodeBuddy 工具执行；
- 取消后不存在遗留 CLI 进程。

### 11.4 LangGraph 集成测试

使用现有 ReAct workflow 验证：

- CodeBuddy provider 能构建为 `BaseChatModel`；
- workflow 调用 `bind_tools()` 时安全降级为无工具模型；
- `model.astream()` 的文本和 reasoning 能进入现有 snapshot；
- 正常完成时 Run 进入 completed；
- SDK 失败时 Run 进入 failed；
- 用户取消时 Run 进入 cancelled；
- LangGraph checkpoint 仍由现有 SQLite checkpointer 管理。

## 12. 分阶段实施

### Phase 1：纯文本适配器

- 添加 SDK 依赖和锁文件；
- 新增 CodeBuddy adapter；
- 实现文本、思考、结果和错误转换；
- 禁用工具和 session 持久化；
- 接入 model factory；
- 增加 fake runner 单测；
- 不支持图片和工具。

### Phase 2：生命周期与桌面打包

- 验证 SDK 内置 CLI 在 Windows 开发环境可用；
- 验证 PyInstaller 资源收集；
- 增加取消和 CLI 进程树清理；
- 增加 backend boot/runtime 日志；
- 增加真实本机冒烟测试。

### Phase 3：模型配置完善

- provider/model API 展示 CodeBuddy 模型；
- 支持已登录凭据、API Key 和企业认证环境；
- 完善模型能力和上下文窗口配置；
- 增加 provider connection test，但不把 connection test 当作 Run 成功。

## 13. 验收标准

方案完成后必须满足：

- CodeBuddy 可以在当前 LangGraph workflow 中作为 `BaseChatModel` 调用；
- CodeBuddy 任何内置工具、MCP 工具、Skills 和项目 settings 都不会被执行或加载；
- LangGraph/RuntimeContextManager 仍是对话和恢复事实源；
- `model.astream()` 的文本能正常进入现有 UI 流；
- thinking 能进入现有 reasoning 通道；
- `bind_tools()` 不会造成工具执行或伪造工具调用；
- SDK/CLI 错误、取消和子进程退出都能被收敛；
- 不改变 OpenAI-compatible provider 的现有行为；
- 不向公网暴露 CodeBuddy CLI 或后端端口；
- 后端关闭后不遗留 CodeBuddy CLI 子进程。

## 14. 最终判断

在“不使用 CodeBuddy 工具，只当模型 SDK”的前提下，接入难度明显低于完整 Agent 适配，且可以把绝大多数改动限制在 `apps/backend/app/core/llm_provider/`。

真正需要重点验证的不是 LangGraph 的模型调用接口，而是两个运行事实：

1. CodeBuddy SDK 在当前 Windows/PyInstaller 分发形态下能否稳定发现和清理内置 CLI；
2. SDK 在 `can_use_tool` 全拒绝、`setting_sources=[]`、`max_turns=1` 下能否稳定产出纯文本模型响应。

只要这两个事实成立，当前 workflow 可以继续把 CodeBuddy 当作普通 `BaseChatModel` 使用，而不需要引入 CodeBuddy 专用工具节点或第二套 Agent 状态机。
