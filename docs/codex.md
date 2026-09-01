按第零铁律，应该把“发送链路”重新收敛成稳定的本地对话运行架构，而不是继续给 `onError`、重试按钮等局部打补丁。

核心方案是：

> 前端只负责提交用户意图和展示状态；后端负责创建 ConversationRun、持久化事实、执行 Agent、恢复运行，并通过 Assistant Transport 投影状态。

## 一、先修正职责边界

运行位置：

- React/Next：UI 进程，只负责 Composer、消息展示、错误展示、重试操作。
- FastAPI：Agent 主进程，负责 Conversation、Run、模型调用、工具、审批、checkpoint 和状态恢复。
- SQLite：唯一事实源，保存用户消息、assistant 消息、运行状态、命令幂等记录和 revision。
- 工具子进程：只承载需要 OS 故障隔离的工具。

Assistant Transport 只是本地 UI 进程与后端进程之间的 IPC 风格边界，不承担业务事实。

前端不应该保存或回传完整 `state`，也不应该根据本地 UI 状态判断消息是否已经成功落库。

## 二、重新设计一次发送的生命周期

发送操作应明确分成以下阶段：

```text
用户提交
  ↓
前端生成稳定 commandId
  ↓
后端校验并幂等占用 command
  ↓
后端事务创建 ConversationRun
  ↓
后端事务写入 user message + assistant placeholder
  ↓
立即返回首个 pending state
  ↓
后台执行 Agent
  ↓
持续写入 canonical facts
  ↓
Transport 推送 revision 快照
  ↓
completed / failed / cancelled
```

关键点是：用户消息在后端成功创建 Run 的事务中就已经成为事实，不应该等模型开始输出后才显示。

这样即使模型调用需要几十秒，UI 也能明确显示：

- 消息已接收
- Agent 正在运行
- 当前正在调用模型/工具
- 运行失败或需要用户操作

而不是让用户猜测消息有没有发出去。

## 三、前端方案

### 1. 不再通过 `prepareSendCommandsRequest` 临时补业务字段

当前前端依赖 assistant-ui command 对象，再通过：

```ts
prepareSendCommandsRequest
```

动态补 `commandId`、`taskId`、`threadId`。

这会让业务协议依赖第三方内部 command 生命周期，长期容易出现“类型上没有、后端却要求有”的问题。

建议建立一个明确的本地 Transport Adapter：

```text
assistant-ui Composer
        ↓
CosirConversationTransport
        ↓
CosirCommandRequest
        ↓
/assistant
```

Adapter 负责：

- 生成 commandId
- 注入 taskId
- 注入 threadId
- 注入当前模型选择
- 处理错误
- 维护发送操作的临时 UI 状态
- 将后端 state 映射为 assistant-ui 消息

assistant-ui 不应直接承担 Cosir 业务协议拼装。

### 2. commandId 由 Cosir Adapter 生成，而不是依赖运行时对象弱引用

当前使用 `WeakMap<object, string>` 绑定 command 对象和 commandId，不够稳定：

- runtime 重建后对象身份变化
- React Strict Mode、重试和恢复可能产生不同对象
- commandId 的生命周期与对象生命周期耦合

建议：

- 用户每次显式发送时，在 Cosir Adapter 层创建 `OutgoingCommand`
- commandId 和文本、taskId、发送状态绑定
- 初始消息、普通消息、重试都复用同一个 command record
- commandId 直到后端确认成功或明确判定不可重试后才结束生命周期

这不是为了增加复杂度，而是把幂等身份放到正确的业务边界。

### 3. 所有发送失败必须进入可见的 UI 状态

当前错误只是 assistant-ui 内部处理，用户看不到足够信息。

建议定义统一的发送状态：

```text
draft
submitting
accepted
running
completed
failed_retryable
failed_non_retryable
cancelled
requires_action
```

前端至少需要展示：

- “消息已发送，正在运行”
- “模型配置无效：reasoningEffort=ultra”
- “后端不可用，点击重试”
- “运行失败，但消息已保存”
- “消息已取消”

普通消息和初始消息必须使用同一套状态机制，不能为 initial message 单独做一套特殊补偿逻辑。

### 4. 不依赖前端 localStorage 作为模型配置事实

模型选择可以存在 localStorage，但发送前必须经过统一的前端 schema 归一化：

```text
localStorage
  ↓
ModelSelectionResolver
  ↓
合法的 Provider / Model / Reasoning 配置
  ↓
发送请求
```

其中：

- `ultra` 不能静默传给后端
- 非法旧值应自动清理或回退默认值
- UI 应显示当前有效配置
- 后端仍然必须再次校验，因为后端是最终边界

前端和后端应共享“协议类型定义”或通过生成方式同步，而不是两边各自维护允许值。

## 四、后端方案

### 1. 把 POST `/assistant` 拆成清晰的“命令接收”和“状态订阅”职责

当前 POST 端点同时负责：

- 解析 Assistant Transport
- 幂等命令处理
- 创建 Turn
- 创建流
- 启动 executor
- 订阅状态
- 处理恢复

这在功能少时可运行，但长期会继续膨胀。

建议内部拆成三个明确组件：

```text
AssistantTransportApi
  ├── ConversationCommandAcceptor
  ├── ConversationRunCoordinator
  └── ConversationStateStreamer
```

职责分别是：

- `CommandAcceptor`：校验命令、计算 payload hash、幂等占用
- `RunCoordinator`：创建/恢复/取消 ConversationRun
- `StateStreamer`：按 revision 投影 canonical facts

端点只做协议转换和生命周期管理，不直接承载业务流程。

### 2. 统一错误模型，彻底消除裸 422

当前有两类错误：

- 业务定义的结构化错误
- Pydantic 默认验证错误

这导致用户可能只看到“422”，看不到具体原因。

建议所有 Transport 请求错误都统一为：

```json
{
  "error": {
    "code": "REASONING_EFFORT_INVALID",
    "message": "当前模型不支持该推理级别",
    "retryable": false,
    "commandId": "...",
    "traceId": "..."
  }
}
```

包括：

- 缺少字段
- 字段类型错误
- 非法枚举值
- task/thread 不一致
- commandId 冲突
- active run 冲突
- 模型配置错误
- 后端不可用

HTTP 状态码只是传输层信息，前端真正依赖稳定的 `error.code` 和 `retryable`。

### 3. 运行状态必须以数据库事实为准

当前执行器有进程内 `_executions`，SQLite 也保存 Turn 状态。

这两个状态可以并存，但必须明确：

- SQLite 是权威状态
- 内存 executor 只是当前进程的执行句柄
- 进程重启后通过 SQLite 恢复
- HTTP 断开不等于业务运行取消
- Transport 断开后，后台运行继续
- 重新进入页面时根据 task/run/revision 重新订阅

建议把状态机明确化：

```text
pending → running → completed
pending → cancelled
pending → failed
running → completed
running → cancelled
running → failed
```

每次状态迁移必须经过一个统一的 `ConversationRunStateMachine`，禁止 API、executor、runtime 各自直接改 status。

### 4. Agent 输出只经一个 Mutation Writer 写入事实

这部分当前方向是对的，应继续强化：

- 模型输出
- reasoning
- tool call
- tool result
- assistant final message
- error
- cancellation

全部经 `ConversationMutationWriter` 写入 canonical conversation state。

不要让 Transport 层自己拼接文本，也不要让前端通过 stream 结果反向补数据库。

Transport 只根据 revision 重新读取事实并生成 UI 投影。

## 五、流式链路应该调整的地方

当前成功请求需要 6–49 秒完成，虽然理论上是流式，但用户体验仍像“没有返回”。

需要验证并固化三个保证：

1. HTTP 连接建立后立即发送 pending snapshot。
2. assistant-ui 在收到第一帧前，先展示本地 `submitting` 状态。
3. 后端每次 canonical revision 变化都能及时唤醒订阅者。

建议增加明确的协议事件语义：

```text
accepted
run_started
message_part_updated
tool_started
tool_finished
run_completed
run_failed
```

这些不一定要恢复旧的 RuntimeEvent 领域模型，可以继续使用 canonical state revision + 结构化日志；但 Transport 层要能从 state 明确表达当前阶段。

另外，当前订阅服务的 50ms 轮询可以作为兜底，但应优先接入进程内 notifier，避免正常运行依赖高频 SQLite 轮询。

## 六、启动、停止、崩溃和恢复

本项目是本地个人桌面 Agent，因此生命周期应明确：

### 启动

1. Tauri 启动本地 FastAPI 子进程。
2. FastAPI 初始化 SQLite、工具系统、Agent Runtime。
3. 扫描未终结 ConversationRun。
4. 恢复可恢复运行。
5. 写入 backend ready 状态。
6. 前端再挂载 Assistant Runtime。

### 停止

1. 前端发送取消只取消指定 run。
2. 应用退出时 executor 取消内存任务。
3. SQLite 中保留运行事实。
4. 下次启动根据状态决定恢复或标记失败。

### 后端崩溃

1. 前端 Transport 连接断开。
2. UI 显示“后端连接中断”，不删除用户消息。
3. Tauri 负责重启 FastAPI。
4. FastAPI 启动时扫描 pending/running run。
5. 根据 lease/fencing version 恢复或安全收束。
6. 前端重新拉取 task state，并恢复订阅。

### 工具子进程崩溃

- 由 executor 捕获
- 写入 tool failed
- assistant message 进入 failed 或 requires-action
- 不让整个 ConversationRun 静默卡死

## 七、实施顺序

建议分四个阶段，不要一次把所有层同时重写。

### 阶段 1：先建立发送状态和错误闭环

目标：

- 前端能看到 submitting/accepted/running/failed
- 所有请求错误都有稳定 code/message
- 消除用户“消息没发送”的错觉
- 不改变核心 Agent 执行逻辑

### 阶段 2：抽出 Cosir Transport Adapter

目标：

- assistant-ui 不再直接拼 Cosir 请求
- commandId 生命周期归 Adapter 管理
- initial message 和普通消息统一
- 重试、断线恢复复用同一个命令身份

### 阶段 3：后端拆分 Command / Run / Stream

目标：

- POST 端点瘦身
- 状态机集中管理
- executor、数据库状态和 Transport 订阅职责分离
- 失败、取消、恢复路径统一

### 阶段 4：补齐真实生命周期测试

必须覆盖：

- 新建任务首次发送
- 普通后续发送
- 双击发送
- 页面刷新后恢复
- Transport 断开后重连
- 后端重启后恢复
- 模型配置非法
- 模型调用失败
- 工具失败
- 用户取消
- command 重试和重复提交
- stream 在首帧前断开

## 最终判断

不是继续给当前代码加“重试按钮”就能解决。

应采用的长期结构是：

```text
Composer
  ↓
Cosir Transport Adapter
  ↓
Command Acceptor
  ↓
ConversationRun State Machine
  ↓
Agent Runtime
  ↓
ConversationMutationWriter
  ↓
SQLite canonical facts
  ↓
Revision-based State Stream
  ↓
Assistant UI converter
```

这样既符合本地单用户架构，也能让前端、后端、Agent Runtime 和 SQLite 各自拥有清晰的长期演进边界。