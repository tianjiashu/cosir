# ConversationTaskSnapshot 统一事实源改造方案

> 状态：已实施，验收结论以独立验收 Agent 的最终报告为准。
>
> 本方案基于当前工作区代码事实，目标是删除四张旧表，并把职责拆成两个明确的事实源：
> `conversation_task_snapshots.state_json` 负责页面/Transport state，新的
> `conversation_task_contexts.context_json` 负责 Agent/LLM context。它会取代
> `conversation_messages`、`conversation_message_parts`、`conversation_tool_calls` 三张
> 对话事实表，以及当前未完成的 `human_approval_requests` 表。
>
> “唯一事实源”仅针对 Conversation state：消息、part、tool-call 展示/生命周期和审批
> 状态。`conversation_commands` 仍是命令幂等事实，`conversation_runs` 仍是执行控制和
> 崩溃恢复事实，二者不是 Conversation state 的重复投影，不能为了消灭重复而删除。

本项目是绿地开发，不提供任何旧实现兼容：本方案不保留旧 API、旧请求体、旧表名、旧
类名、旧 import、旧 re-export、双协议端点、feature flag、旧表双读/双写或旧数据库迁移。
文档中出现的旧代码均是待删除或直接替换的当前实现，不是需要包裹兼容层的公共契约；
结构变化通过删除并重建开发期 SQLite 和同步更新代码/fixture 完成。

当前目录决策：Assistant Transport 协议相关实现（包括 Transport snapshot 的中性协议
类型、snapshot owner、Transport mutation writer 和传输事务适配）统一归属
`apps/backend/app/assistant_transport/`。文中早期建议将这些协议边界类型/owner 移至
`app.models` 或 `app.service.task` 的目录示例不再作为强制目录要求；仍须保持它们不依赖
assistant-ui/React 运行时类型，Agent context 继续由 `app.service.task` 独立持有。

## 1. 目标与不可妥协的边界

### 1.1 目标

改造完成后，两个事实源的数据流是：

```text
用户命令 / Agent runtime mutation
             ↓
ConversationTaskSnapshotService
             ↓
ConversationTaskSnapshotModel.state_json
             ↓ commit 成功
       进程内 notifier
             ↓
Assistant Transport set / append-text
             ↓
assistant-ui converter

Agent context mutation
             ↓
ConversationTaskContextService
             ↓
ConversationTaskContextModel.context_json
             ↓
RuntimeContextManager / LLM
```

必须满足：

- 一个 Task 只有一份完整 JSON snapshot；
- 后端业务代码不再从消息、part、tool-call 多张表拼装 Conversation state；
- Transport、断线重连和页面首屏都读取 snapshot；Agent context 只读取 context snapshot；
- 所有可见状态更新都经过同一个 snapshot owner；
- 完整消息、tool result、Run 终态等当前语义 mutation 与相关 snapshot/context/Run 在同一
  SQLite 事务内提交；文本 chunk 是明确的 snapshot-only mutation，不写 context；审批只
  预留 schema，不属于本阶段 mutation；
- 事务提交后才向当前 HTTP 连接发布局部 mutation；
- 文本增量使用官方 `append-text`，结构和状态更新使用官方 `set`；
- 不保留旧表、旧 CRUD、旧 Record、兼容 re-export、双读或双写。

### 1.2 本地桌面进程边界

本项目是单用户本地桌面 Agent：

- Tauri Rust host 启动、停止并监控本机 Python/FastAPI backend；
- backend 进程拥有 Agent、工具、Conversation state 和 SQLite；审批属于未来预留能力，
  本阶段不启用；
- React 进程只持有 assistant-ui 的渲染副本，通过 localhost Assistant Transport 通信；
- 工具 subprocess 只执行 backend 分派的工具，不拥有 Conversation state；
- 不引入认证、多租户、Redis、Postgres、云端队列或分布式锁。

本次改造跨越 UI/backend 的本机 HTTP 边界，但 authoritative state 只在 backend 的
SQLite 主库中。`controller.state` 和 React state 都不是事实源。

### 1.3 “唯一事实源”的范围

必须明确区分三类事实：

| 类型 | 唯一来源 | 是否放入 Task snapshot |
| --- | --- | --- |
| Transport state：messages、parts、tool-call 展示、预留 approval、UI error | `conversation_task_snapshots.state_json` | 是 |
| Agent context：system/user/assistant/tool context、上下文顺序、压缩结果 | `conversation_task_contexts.context_json` | 否 |
| Command 幂等：`command_id`、payload hash、command → run 绑定 | `conversation_commands` | 否，保留独立表 |
| Run 执行控制：pending/running/终态、模型路由、重启恢复 | `conversation_runs` | snapshot 只保存 UI 可见运行状态副本 |

因此不能把 `ConversationTaskSnapshotModel` 理解成整个数据库的万能表。它是 Transport
state 的唯一事实源，`ConversationTaskContextModel` 是 Agent context 的唯一事实源；
Command/Run 是执行协议所需的独立事实。

## 2. 当前代码事实审计

### 2.1 Snapshot 已经存在，但权威范围尚未完成切换

当前实现包括：

- [conversation_task_snapshot_model.py](H:/coding-agent/apps/backend/app/storage/model/conversation_task_snapshot_model.py)：
  一 Task 一行、`state_json`；当前模型中的版本号字段随本次改造移除；
- [conversation_snapshot_service.py](H:/coding-agent/apps/backend/app/assistant_transport/service/conversation_snapshot_service.py)：
  进程内缓存、事务内 stage、commit 后 publish、`set`/`append-text`；
- [assistant_api.py](H:/coding-agent/apps/backend/app/assistant_transport/assistant_api.py)：
  首帧完整 state，后续消费局部 mutation；
- [converter.ts](H:/coding-agent/apps/desktop/lib/assistant/converter.ts)：
  将中性 snapshot 映射为 assistant-ui 消息和 tool 展示状态。

但这些代码仍存在过渡结构：

1. `ConversationMutationWriter` 仍直接依赖三张旧 conversation 表；
2. `ConversationRunMessageStore` 仍从 message/part/tool-call 表重建 LLM runtime message；
3. `ConversationStateService.build_messages()` 仍从三张旧表投影；
4. `ConversationTaskSnapshotService` 和 state 类型还位于 `app.assistant_transport`，造成
   service/core 对 Transport 目录的反向依赖风险；
5. `human_approval_requests` 没有加入
   [init_schema.py](H:/coding-agent/apps/backend/app/storage/init_schema.py) 的 `APP_MODELS`，
   没有 CRUD、读取接口或完整恢复路径，但 Writer 仍有写入/解析方法；
6. `tasks.message_sequence` 只服务旧消息表的 task 级序号，应随旧表一起删除；
7. 旧表的级联删除、测试替身、Record 和 CRUD 仍存在。

所以当前不是“删除四张表”的局部清理，而是一次 Conversation canonical state 的完整切换。

## 3. 官方 Assistant Transport 依据

官方文档将 Assistant Transport 定义为任意 JSON agent state 的状态流协议：后端提供
完整 state，前端 converter 把 state 映射为 assistant-ui 消息；用户命令通过
`add-message`、`add-tool-result` 或自定义命令返回 backend。

官方 `assistant-stream` 只规定两种 state mutation：

- `set`：设置字段、数组元素或结构；
- `append-text`：向指定字符串路径追加本次新增文本。

官方示例中 `controller.state["message"] = "Hello"` 产生 `set`，随后追加文本产生
`append-text`。这意味着协议只负责复制 JSON，不定义本项目的消息、tool 或审批领域语义；
这些语义必须由 backend 的 snapshot schema 固定下来。

官方参考：

- [Assistant Transport 官方文档](https://www.assistant-ui.com/docs/runtimes/custom/assistant-transport)
- 文档中的 [streaming protocol](https://www.assistant-ui.com/docs/runtimes/custom/assistant-transport#streaming-protocol)

本项目继续使用官方 `useAssistantTransportRuntime` 和 `protocol: "assistant-transport"`，
但不接受前端回传的 state 作为权威输入。前端当前已经在
[assistant-runtime.tsx](H:/coding-agent/apps/desktop/components/assistant/assistant-runtime.tsx)
中移除 request body 的 `state`，backend 按 Task 从 SQLite snapshot 读取。

## 4. 目标 snapshot 数据结构

### 4.1 顶层结构

`state_json` 保存中性、面向页面和 Assistant Transport 的完整 JSON。Agent context 不直接
读取这个 JSON，而是由独立的 Agent context owner 维护另一份明确的 context state：

```json
{
  "messages": [],
  "run": {
    "runId": 123,
    "status": "running",
    "endReason": null
  },
  "approvals": {},
  "error": null
}
```

约束：

- `messages` 按 Conversation 顺序保存全部历史和当前运行消息；
- `run` 只描述当前 Task 的 active/latest Run 展示状态，执行控制仍以
  `conversation_runs` 为准；
- `approvals` 只作为未来审批能力的预留字段，本阶段固定为空对象，不实现请求、决策或恢复；
- `error` 只保存页面需要展示的稳定错误结构，不保存堆栈、密钥或大响应；
- snapshot 不存 LLM provider secret，不存完整工具 stdout，不存二进制；大输出按已有工具
  输出预算与文件快照规则处理；
- 不引入 JSON schema 版本号、并发 revision、outbox 或 fencing version；这是本地单用户
  Agent，当前只校验固定的 JSON 结构，结构变化直接随代码和干净数据库一起演进。

### 4.2 Agent context 独立事实源

新增一张一 Task 一行的表：

```text
conversation_task_contexts
```

建议 ORM 字段：

```text
task_id         INTEGER UNIQUE REFERENCES tasks(id)
context_json    TEXT NOT NULL
updated_at      TEXT NOT NULL
```

`context_json` 不是 assistant-ui state，而是供 Agent/LLM 使用的 LangChain message 文档。
消息的业务类型必须直接由 LangChain 原生序列化格式表达；`ContextEntry` 仍然保留，
但只负责承载消息和其运行归属，不再引入第二套 `RuntimeMessage` 消息模型。

```json
{
  "entries": [
    {
      "runId": null,
      "message": {
        "type": "system",
        "data": {
          "content": "你是一个 coding agent...",
          "additional_kwargs": {},
          "response_metadata": {},
          "type": "system",
          "name": null,
          "id": "system-1"
        }
      }
    },
    {
      "runId": 123,
      "message": {
        "type": "human",
        "data": {
          "content": "读取 README.md",
          "additional_kwargs": {},
          "response_metadata": {},
          "type": "human",
          "name": null,
          "id": "message-1"
        }
      }
    },
    {
      "runId": 123,
      "message": {
        "type": "ai",
        "data": {
          "content": "我先读取文件。",
          "additional_kwargs": {},
          "response_metadata": {},
          "type": "ai",
          "name": null,
          "id": "message-2",
          "tool_calls": [
            {
              "name": "read_file",
              "args": {"path": "README.md"},
              "id": "call-1",
              "type": "tool_call"
            }
          ],
          "invalid_tool_calls": [],
          "usage_metadata": null
        }
      }
    },
    {
      "runId": 123,
      "message": {
        "type": "tool",
        "data": {
          "content": "文件内容...",
          "additional_kwargs": {},
          "response_metadata": {},
          "type": "tool",
          "name": null,
          "id": "message-3",
          "tool_call_id": "call-1",
          "artifact": null,
          "status": "success"
        }
      }
    }
  ]
}
```

实际写入/读取必须使用 LangChain 提供的 `message_to_dict()` / `messages_from_dict()`，
不得手写 `role`、`toolCalls` 到 LangChain 消息之间的映射。数据库仍然只能保存 JSON，
因此不可避免地存在一次 I/O 序列化；本次改造消除的是 `RuntimeMessage` 以及旧
message/part/tool-call 表造成的语义往返转换。

运行时值对象保持如下职责边界：

```python
@dataclass(frozen=True)
class ContextEntry:
    message: BaseMessage
    run_id: int | None
```

`ContextEntry` 不参与 JSON 消息字段映射；context service 只在 hydrate 时把 JSON entry
的 `runId` 与 `messages_from_dict()` 返回的同序 LangChain message 组合起来。这样压缩
 算法仍可按 `run_id` 识别系统消息、历史 run、当前 run 和失败 run，但模型节点拿到的
 始终是原生 LangChain 消息列表。

约束：

- context 是 Agent context 的唯一持久化事实源；`RuntimeContextManager` 只持有它的进程内
  working copy；
- `ContextEntry` 必须保留 `run_id: int | None`；首个 Task 级 system prompt 使用 `None`，
  其余消息以及运行期间追加的 system message 使用产生它的 `ConversationRun.id`，供未来
  压缩、裁剪、失败运行处理和上下文边界判断；
- `ContextEntry.message` 直接使用 `langchain_core.messages.BaseMessage`，允许的持久化
  消息类型为 `SystemMessage`、`HumanMessage`、`AIMessage`、`ToolMessage`；不持久化
  `AIMessageChunk`；
- tool-call 参数、tool result 和 `tool_call_id` 必须由 LangChain 原生消息字段表达，不能
  另建自定义 `toolCalls` 或 JSON 字符串 metadata；
- 每个 `AIMessage.tool_calls[*].id` 必须非空；provider 未提供 id 时，只能在规范化阶段
  生成一次稳定 id，并同时写入内存中的 `AIMessage`、snapshot tool-call part 和后续
  `ToolMessage.tool_call_id`，禁止在不同层分别生成 fallback id；
- 消息身份使用 LangChain message 的 `id`；写入前为缺少 id 的消息补齐稳定 id，不能依赖
  已删除的数据库 message 自增主键；
- system prompt 作为首个 `SystemMessage` 持久化；prompt 规则发生变化时由 context owner
  原子替换该条消息，禁止每次 hydrate 重复追加；
- `entries` 的数组顺序就是模型输入顺序；每个 entry 必须恰好包含一个可恢复的 LangChain
  message，`runId` 只能为 `null` 或属于当前 Task 的已存在 `ConversationRun.id`。未知
  run、重复/缺失 entry、未知 message type、消息数量与 entry 数量不一致时必须显式失败，
  不得静默丢弃或回退旧表；
- 首个 Task 级 system prompt 的 `runId` 为 `null`；运行过程中为修复提示或其他模型可见
  原因追加的 `SystemMessage` 使用当前 run id，不能把所有 `SystemMessage` 都标为 `null`；
- context 的压缩结果、保留边界和上下文顺序只由 context owner 决定；
- context 与 Transport snapshot 是两份不同投影，不能互相回填或互相覆盖；
- 不引入 context JSON 版本号、并发 revision 或兼容迁移协议；当前只校验固定结构，结构
  变化直接随代码和干净数据库一起演进；
- context 不保存完整工具 stdout、secret 或二进制，沿用工具输出预算和 artifact 引用规则。

### 4.3 Message 与 part

本节结构属于 Transport snapshot，不是 Agent context。为匹配当前
`apps/desktop/lib/assistant/contract.ts` 和 `converter.ts`，snapshot 顶层消息只允许
`user`、`assistant`；`SystemMessage` 只存在于 Agent context，`ToolMessage` 只存在于
Agent context，Transport 中的工具调用/结果/错误通过 assistant message 的 tool-call
part 表达。否则会把 LangChain 消息类型错误地暴露给前端 converter。

```json
{
  "id": "message-uuid",
  "runId": 123,
  "role": "assistant",
  "status": "running",
  "endReason": null,
  "createdAt": "2026-09-03T10:00:00Z",
  "updatedAt": "2026-09-03T10:00:01Z",
  "parts": [
    {"type": "text", "text": "", "status": "running"},
    {"type": "reasoning", "text": "", "status": "running"},
    {
      "type": "tool-call",
      "toolCallId": "call-1",
      "toolName": "read_file",
      "args": {"path": "README.md"},
      "status": "pending",
      "result": null,
      "error": null,
      "isError": false,
      "approvalRequestId": null
    }
  ]
}
```

要求：

- message/part ID 在 snapshot 生命周期内稳定，不再依赖数据库自增 ID；
- snapshot 顶层 `role` 只支持 `user`、`assistant`；`system`、`tool` 属于 Agent context
  的 LangChain message，不直接进入 Transport snapshot 顶层消息；
- text/reasoning 是可增量追加的字符串 part；
- tool-call 是结构化领域 part，不能靠前端字段存在与否猜状态；
- tool result 即使是 JSON `null` 也必须明确写入；
- tool error 必须保留可诊断的安全信息；
- 不把 assistant-ui 类型、React 类型或 `assistant_stream` 类型放入 models/storage。

### 4.4 Tool-call 生命周期

后端状态机固定为：

```text
pending → running → completed
pending/running → failed
pending/running → cancelled
```

`completed`、`failed`、`cancelled` 都是终态，不允许再次迁移；重复写入相同终态只允许
幂等成功，其他终态冲突必须失败。`requires-action` 是未来审批能力的保留状态，本阶段
不得由当前工具执行链路产生，也不进入当前状态机的可执行迁移。

Tool-call 的唯一索引是当前 Task 内的 `toolCallId`。Snapshot owner 负责：

- 新建 tool-call 时拒绝同 Task 重复 ID，或对相同 ID 做幂等重放校验；
- 状态迁移校验；
- result/error/cancelled 的字段一致性校验；
- run_id 与 tool-call 所属 message 的一致性校验；
- backend 重启时把遗留 pending/running tool-call 和遗留 run 一起收束为 failed。

前端 converter 继续按已安装的 assistant-ui 类型映射：

- `pending/running`：父 assistant message 为 running；
- `completed`：写入 `result`，包括 `null`；
- `failed`：写入 error envelope 和 `isError: true`；
- `cancelled`：写入 cancelled result envelope。

`requires-action` 和 approval 摘要仅为未来 schema 位置预留，本阶段 converter 不产生、
不处理该状态。

这些是本项目领域映射，不声称是 Assistant Transport 官方 tool 状态规范。

### 4.5 Approval（预留，不在本阶段实现）

审批请求未来可能覆盖一批 tool-call，不能只把审批数据隐式塞进某个 UI 字段。本阶段只
保留 `approvals` 这个明确的空对象位置和 tool part 的 `approvalRequestId: null` 占位，
不创建审批 ORM 表、CRUD、Transport command 或 Agent context entry：

```json
{
  "approvals": {}
}
```

本阶段 `approvals` 永远为空对象，`approvalRequestId` 永远为 `null`（也可以在实现时直接
省略该字段）；本阶段不产生、不读取、不恢复任何审批状态，不定义审批状态枚举，不接入
LangGraph `interrupt()`/checkpoint 恢复，也不实现审批相关的 context mutation。未来开放
审批时，再另行定义 request、tool-call 关联、决策、参数预算以及恢复协议；不得把本阶段
的空对象解释为已经实现了审批。

## 5. Snapshot owner 与分层

### 5.1 唯一 owner

将当前 snapshot service 结构整理为 Assistant Transport 层的唯一 owner，例如：

```text
apps/backend/app/assistant_transport/state/conversation_state_snapshot.py
apps/backend/app/assistant_transport/state/conversation_state_part/...
apps/backend/app/assistant_transport/service/conversation_task_snapshot_service.py
apps/backend/app/service/task/conversation_task_context_service.py
apps/backend/app/assistant_transport/service/conversation_mutation_writer.py
apps/backend/app/storage/model/conversation_task_snapshot_model.py
apps/backend/app/storage/model/conversation_task_context_model.py
apps/backend/app/storage/crud/conversation_task_snapshot_crud.py
apps/backend/app/storage/crud/conversation_task_context_crud.py
```

职责严格分开：

- `ConversationTaskSnapshotModel`：只描述 SQLite 表；
- `ConversationTaskSnapshotCrud`：只负责事务内读取/upsert JSON；
- `ConversationTaskSnapshotService`：维护 schema 校验、内存副本、事务 staging、mutation
  应用、订阅通知；
- `ConversationTaskContextService`：作为 Agent context 唯一 owner，维护唯一可变的进程内
  working copy、事务 staging 和 context entry 顺序，不发布 Assistant Transport mutation；
- `ConversationMutationWriter`：把当前 message/text/reasoning/tool/run 状态意图提交给
  snapshot service，不直接操作 ORM 表；approval writer/resolve 仅作为未来扩展，不在
  本阶段创建；
- Assistant Transport API：只负责请求校验、stream 建立、controller adapter；
- Runtime/Agent：不 import `assistant_stream` 或 assistant-ui；模型增量通过 workflow custom
  stream 传给 workflow，再由注入的 RuntimeOperations/Transport writer port 更新 snapshot。

`ConversationTaskSnapshotService` 可以保留进程内缓存，但缓存不是独立事实源：

```text
SQLite state_json → hydrate 进程内 snapshot → commit 后 notifier → controller.state
```

任何内存副本丢失都必须能从 SQLite 重新 hydrate。

Agent context 同样只有一个可变 working copy，由 `ConversationTaskContextService` 持有。
`RuntimeContextManager` 不再持有第二份可变 canonical context，也不得直接修改 context
owner 的数组；它只获取当前 context 的只读/不可变运行视图，并通过 context mutation port
提交消息变更。这样 snapshot owner、context owner 各自只有一份可变 working copy，不会出现
两个内存副本互相覆盖。

### 5.2 依赖方向

目标依赖：

```text
assistant_transport API → service snapshot/mutation/subscription port
core → service mutation port
service → storage + models
storage → models
models → utils
```

禁止：

- `core`、`service`、`storage` import `assistant-ui` 或 `assistant_stream`；
- `service` import `app.assistant_transport.*`；
- snapshot model import service、API、Transport；
- frontend converter 成为后端状态机；
- controller.state 反向写入 snapshot。

中性 snapshot 类型必须从 `app.assistant_transport.state` 移到 `app.models`，不保留旧路径
re-export。Assistant Transport 专属 request schema 和 controller adapter 仍留在
`app.assistant_transport`。

### 5.3 Task mutation transaction

`assistant_transport/service/conversation_transaction.py` 只提供轻量 SQLite transaction
上下文，不再维护独立的 Task 锁，也不再拥有 snapshot/context service：

- 一个需要持久化的 mutation 使用一个 SQLAlchemy `Session` 和一个 SQLite transaction；
- snapshot、context、Command/Run service 在调用方提供的 session 中读写，不在同一操作中
  隐式打开第二个 session；
- 对完整消息、工具结果和 Run 终态，数据库事实按需要在同一 transaction 内提交；
- 文本 chunk 只写 `state_json`，因为 chunk 尚未形成完整 LangChain message；
- commit 成功后由调用方分别选择 snapshot 或 context publisher；两者不要求共享发布边界，
  允许 UI snapshot 与 Agent context 短暂不一致；
- 写入失败时 SQLite transaction rollback，未提交的 owner 变化不发布；
- notifier 不属于 transaction，禁止把未提交的 mutation 放入队列。

## 6. 事务、并发与生命周期

### 6.1 一个 mutation 的原子流程

所有 mutation 必须先由 owner 分类，再走对应流程。

文本 chunk 的 snapshot-only mutation：

```text
读取当前 snapshot working copy
  ↓
校验并追加本次文本/推理增量
  ↓
同一 SQLite transaction 更新 state_json
  ↓ commit 成功
更新 snapshot working copy 并 publish SnapshotChange
  ↓
各 controller.state 应用 append-text
```

完整消息、工具结果和 Run 状态等当前语义 mutation 的原子流程（审批未来接入时复用该
流程，但不在本阶段实现）：

```text
读取需要修改的 owner working copy，并在事务内创建候选副本
  ↓
校验并应用 snapshot mutation + context entry mutation + Run mutation
  ↓
同一 SQLite transaction 更新 state_json、context_json 和必要的 Run/Command 行
  ↓ commit 成功
commit 成功后按调用方选择替换 snapshot/context working copy，并 publish 已提交变化
  ↓
各 controller.state 应用 set / append-text
```

commit 失败时：

- SQLite transaction 回滚；
- 未提交 working copy 和待发布 mutation 丢弃；
- 不向 Transport 发送成功状态；
- Runtime 得到异常并停止后续依赖执行；
- 下次连接从上一份已提交 snapshot hydrate。

### 6.2 并发

当前运行拓扑是单 backend 进程、单 `ConversationRunExecutor`、每个 run 一个
`asyncio.Task`。Executor 在 Agent Run 生命周期内持有 Task 级 `asyncio.Lock`，因此 Run
内部的 mutation 不再由 transaction helper 重复加锁：

- Runtime mutation、cancel、start command 仍必须通过各自的数据库 transaction 完成；
  approval command 本阶段不存在；
- 同一 Task 的 snapshot mutation 按提交顺序进入 notifier；
- 多个 stream 只读并消费自己的 controller 副本；
- SQLite `BEGIN IMMEDIATE` 负责持久化写事务；Agent Run 的 Executor lock 负责同一 Task
  内的运行串行化；
- 不增加 revision、fencing、lease、outbox 或分布式锁。

### 6.3 初始命令

`ConversationCommandModel` 继续负责 `(task_id, command_id)` 幂等占用。首次
`add-message` 必须在一笔 transaction 内完成：

1. 创建 Task（新对话时）；
2. 占用 command；
3. 创建 ConversationRun；
4. 将 user message 和 assistant running baseline 追加到 snapshot；
5. 将 `HumanMessage`（以及必要时的首个 `SystemMessage`）追加到 context；不把
   snapshot 的 assistant running baseline 写成尚未完成的 `AIMessage`；
6. 在同一 transaction 写入 snapshot、context、Command/Run；
7. commit；
8. 启动 backend executor 和 Transport stream。

重复命令不重新追加消息，不重新创建 run：

- `task_id + command_id` 不存在时，按首次命令流程创建 Command/Run 并追加一次消息；
- 已存在且 payload hash 相同（无论原请求响应是否丢失），返回既有 Command/Run handle，
  然后只订阅该 Task 的当前 snapshot，不重新执行、不重新写 context；
- 已存在但 payload hash 不同，返回结构化 `409 COMMAND_ID_CONFLICT`，不创建新
  snapshot/context/run；

payload hash 只用于同一 `command_id` 的幂等冲突判断，不是状态版本号，不参与并发控制。
前端传来的 `state` 不参与任何事实计算。

### 6.4 Runtime message/context

当前 `ConversationRunMessageStore` 直接查询 message/part/tool-call 表，必须删除；其调用方
统一改为依赖 `ConversationTaskContextService` 提供的 context reader/writer port，并将
Agent context 写入独立的 `conversation_task_contexts`：

- context JSON 由按顺序排列的 `ContextEntry` 组成，每个 entry 保留 `run_id` 和一个
  LangChain `BaseMessage`；
- `ContextEntry.message` 直接使用 `SystemMessage`、`HumanMessage`、`AIMessage` 或
  `ToolMessage`，不再经过 `RuntimeMessage`；
- context service 写入时对每条消息调用 `message_to_dict()`，读取时批量调用
  `messages_from_dict()`，只负责 JSON I/O，不负责 role/content/tool-call 的二次映射；
- `AIMessage.tool_calls` 直接保存工具名称、参数和 call id；`ToolMessage.tool_call_id`
  直接闭合对应的 AI tool call；
- system/user/assistant/tool 的顺序以 context entries 顺序为准，entry 的 `run_id` 不
  参与消息字段转换，只供未来压缩、裁剪和失败运行边界判断使用；
- `RuntimeContextManager` 只读取 context owner 提供的不可变运行视图；消息 mutation 交给
  context owner 在事务内生成候选 context，commit 成功后由 owner 一次性替换 working copy；
  RuntimeContextManager 不持有第二份可变 context，也不在 commit 前修改 owner 的 working
  copy；
- 每个语义消息 mutation 的 context flush、对应的 snapshot mutation 和 Run 状态变更必须
  在同一个业务 transaction 内提交，避免页面 state 和下一次 LLM context 看到不同的事实；
- 不再使用 `tasks.message_sequence`、`build_for_run()`、`excluded_run_ids` 或按 run 清理
  message 表；context 是 Task 级有序消息文档，`run_id` 只保留在 `ContextEntry` 中；
- 每个新 run 在 command transaction 中追加 snapshot baseline，并追加新的 `HumanMessage`
  到 context；snapshot 的 assistant running baseline 不是 context entry；
- `RuntimeContextManager` 不再调用旧 `clear_run()` 删除 message/part/tool-call 表；重试是
  新 run，context 由新 run 的消息 entry 继续追加；
- context compaction 仍可保留为后续独立能力，但其输出必须写回 context JSON，不得重建
  旧 conversation 表。

上下文不是每个流式 chunk 的落盘缓冲区：模型节点继续在内存中把 `AIMessageChunk` 组装
成一个 `AIMessage`，再按消息边界写入 context；Transport snapshot 才负责每个文本 chunk
的 `append-text`。因此模型执行中允许 snapshot 暂时保存部分 assistant 文本，而 context
尚未保存对应的完整 `AIMessage`；这是有意的 UI partial 状态，不是 context 不一致。进程
中断后该 partial 只作为失败运行的 UI 记录保留，下一次模型调用使用上一次已提交的完整
context，并先按工具闭合规则修复未闭合调用。

若崩溃点是“snapshot 已有 pending tool-call，但 context 尚未提交对应 `AIMessage`”，
恢复只需将 snapshot 中该 assistant message 和 tool-call 收束为 failed，不得凭 snapshot
反推或补写 `AIMessage`；若 context 已有对应 `AIMessage.tool_calls`，才为每个未闭合 call
补写中断 `ToolMessage`。随后“继续”才追加新的 `HumanMessage` 并创建新的 Run。

工具链必须一并迁移：`ToolExecutionService` 直接产出 `ToolMessage`，并把
`ToolObservation` 转为 `ToolMessage(content=..., tool_call_id=..., status=...)` 的逻辑
集中在工具结果边界；`tools_node.py` 不再从 `metadata["tool_call_id"]` 读取关联，
`runtime_operations.py`、`ToolRunResult`、`workflow.py`、context listeners 和 compressor
均不得继续接收或返回 `RuntimeMessage`。模型节点只在内存中组装 `AIMessageChunk`，落库
时写入最终 `AIMessage`。

Tool context 的闭合规则固定为：

- `AIMessage.tool_calls` 可以在工具执行期间暂时没有对应 `ToolMessage`；这时禁止进入下
  一次模型调用；
- 工具完成写 `ToolMessage(status="success")`，失败或取消写
  `ToolMessage(status="error")`，并在 content 中保留经过预算和脱敏的失败/取消说明；
- `requires-action`、approval request 和审批结果只保留为未来扩展的 schema 位置，本阶段不
  由工具执行链路产生，也不触发 context、snapshot 或 checkpoint 的审批恢复逻辑；
- 每次模型调用前、Run 进入终态前，当前 AI tool call 必须与恰好一个同
  `tool_call_id` 的 ToolMessage 配对；禁止 orphan、重复或未知 call id；
- backend 启动恢复时，发现遗留 pending/running Run 中未闭合的 AI tool
  call，必须在同一恢复 transaction 中追加 `ToolMessage(status="error")`，将其内容标为
  `execution_interrupted`，同时将 snapshot tool 状态和 Run 标为 `failed`；恢复操作必须
  幂等，第二次启动不能重复追加。

这里不能把 snapshot adapter 当作 context adapter。snapshot 面向 UI state，context 面向
LLM 输入；两者由同一个领域 mutation 同步更新，但分别持久化、分别校验、分别读取。

### 6.5 Tool 与 approval mutation

`ConversationMutationWriter` 应改为 snapshot domain writer，至少提供以下意图明确的方法：

- `create_message` / `append_text` / `append_reasoning`；
- `create_tool_call`；
- `transition_tool_call`；
- `complete_tool_call`；
- `cancel_tool_call` / `fail_open_tool_calls`；
- `settle_run`。

`record_approval_request`、`resolve_approval` 和 `resolve-approval` custom command 只作为
未来审批能力的扩展点，本阶段不创建、不调用。

这些方法不执行 SQL 表级 CRUD，而是在一个 owner transaction 中对 snapshot JSON 做受约束
的路径更新，同时返回对应的 `ConversationStateMutation`：

```text
文本正文          → append-text [messages, i, parts, j, text]
新增消息/part      → set [messages, i] / [messages, i, parts, j]
消息状态          → set [messages, i, status]
tool 状态          → set [messages, i, parts, j, status]
tool result/error  → set 对应字段
run 状态           → set [run, status]
```

不做每个 chunk 的完整 state diff，也不正常替换整个 `messages` 数组。只有明确的删除、
分支编辑或数组重排才允许使用较大路径的 `set`，并由领域操作显式声明。

### 6.6 Stream 与重连

`ConversationRunSubscriptionService` 只订阅 owner 的已提交 `SnapshotChange`：

1. 建立 stream 时从 SQLite snapshot hydrate；
2. 注册 notifier；
3. 注册后重新读取 snapshot，补齐注册窗口内变化；
4. 发送一次 root `set` 完整 snapshot；
5. 后续只发送 commit 后的 `set` / `append-text`；
6. run 进入 completed/failed/cancelled 后结束 stream。

现有 `50ms` 轮询只能作为没有 notifier 时的短期兜底，不能重新查询旧表做全量重建或 diff。
最终可由进程内 notifier 完全替代。

### 6.7 重启、停止、崩溃

- Tauri supervisor 负责启动/停止 backend 并报告 readiness；
- backend 正常停止时取消 executor tasks，已提交 snapshot 保留；
- backend 崩溃时内存 snapshot、notifier、controller 连接和 asyncio tasks 全部丢失；
- 新 backend 启动必须按“schema 初始化 → hydrate snapshot/context → 启动恢复
  UnitOfWork → backend ready”的顺序执行；不得先从旧表 bootstrap 或在 ready 后异步补偿；
- 启动恢复以 `conversation_runs` 的 pending/running 为执行恢复依据，在同一恢复事务中将
  遗留 Run 标为 failed，将 snapshot 中属于该 Run 的 pending/running tool-call 标为
  failed，并为 context 中尚未配对的 `AIMessage.tool_calls` 追加原生
  `ToolMessage(status="error", tool_call_id=...)`，content 标记为
  `execution_interrupted`；恢复必须幂等，第二次启动不能重复追加 ToolMessage；未来审批
  状态不得在本阶段伪装成 LangChain message 状态，也不参与恢复；
- 恢复完成后 backend 才报告 ready；现有 Tauri supervisor 的用户触发重试/重启动作可以
  再次启动 backend，本方案不增加自动重启策略、不自动 replay 旧 asyncio task；
- 前端重新连接只从 `conversation_task_snapshots` hydrate；
- 用户发送“继续”创建新的 command/run 和新的 snapshot 增量，不恢复旧 asyncio task；
- 不续租、不做 executor lease、不做自动 run replay。

## 7. 删除与重构范围

### 7.1 删除的 Conversation state 表和代码

删除：

- `app/storage/model/conversation_message_model.py`；
- `app/storage/model/conversation_message_part_model.py`；
- `app/storage/model/conversation_tool_call_model.py`；
- `app/storage/model/human_approval_request_model.py`；
- `app/storage/crud/conversation_message_crud.py`；
- `app/storage/crud/conversation_message_part_crud.py`；
- `app/storage/crud/conversation_tool_call_crud.py`；
- `app/models/conversation_message_record.py`；
- `app/models/conversation_message_part_record.py`；
- `app/models/conversation_tool_call_record.py`。
- `app/service/conversation_run_message_store.py`；
- `app/core/context/runtime_message_store.py`；
- `app/service/task/conversation_state_service.py` 及其
  `get_conversation_state_service` 依赖入口；所有调用方直接改用
  `ConversationTaskSnapshotService`/对应的 snapshot dependency，或
  `ConversationTaskContextService`，不保留同名 facade、别名或兼容入口；旧表投影、
  bootstrap 和消息读取职责全部删除。

同步删除所有 import、测试替身、model registry、级联删除分支和旧表文档。

新增并保留独立的 Agent context 存储：

- `app/storage/model/conversation_task_context_model.py`；
- `app/storage/crud/conversation_task_context_crud.py`；
- `app/service/task/conversation_task_context_service.py`；
- `app/models/conversation_task_context.py` 及其 entry 类型。

它只保存 LLM context，不向 Assistant Transport 发布 mutation，也不被前端直接读取。

### 7.2 删除旧序号与清理接口

删除：

- `TaskModel.message_sequence` 及 `TaskRecord.message_sequence`；
- `_next_message_sequence()`；
- `ConversationRunMessageStore.next_run_sequence()` 的旧持久化语义；
- `ConversationMutationWriter.clear_run()` 中按 message/part/tool-call 表删除事实的逻辑；
- `RuntimeContextManager._reset_message_sequence()` 及其 task message sequence 副作用；
- `ConversationRunModel.response_text`、`ConversationRunRecord.response_text` 及其读写参数；
  通过删除并重建开发期 SQLite 移除现有列。最终 assistant 正文只由 snapshot 的
  assistant text part 和 Agent context 的 `AIMessage.content` 分别负责，Run 不再保存
  第三份正文；
- 所有“从旧表 bootstrap snapshot”的路径。

新 run 的 Transport 消息由 snapshot owner 直接追加，Agent context entry 由 context owner
在同一个 start transaction 中追加；不需要先创建数据库 message row 再反向投影。

### 7.3 审批表的处理

`HumanApprovalRequestModel` 当前不是完整可用功能：未注册 schema，只有 Writer 的写/解
析方法，且没有 API/CRUD/恢复读取路径。切换到 snapshot 后：

- 删除该 model；
- 删除直接 ORM 写入和 `resolve_approval()` 的旧表实现；
- snapshot 可以保留空的 `approvals` 对象和可选的 `approvalRequestId` 字段作为未来 schema
  位置，但本阶段不写入审批请求、决策或 requires-action 状态；
- 本阶段不增加 Assistant Transport approval custom command，不接入 LangGraph
  `interrupt()`/checkpoint 恢复，也不实现审批相关的 context mutation；
- 删除当前代码中仅服务审批的执行残留，包括 `RuntimeConfig.approval_resolver`、
  `request_tool_approval`、`APPROVAL_INTERRUPT_KEY`、审批 helper、
  `resolve_approved_calls`、审批专用 `interrupt()` 分支、
  `mark_tool_calls_requires_action`，以及桌面端 converter/runtime 中的
  `requires-action`/审批分支；不删除未来仍可能需要的通用 LangGraph checkpoint 基础设施，
  但本阶段不得有后端或前端路径产生、处理或恢复 `requires-action`；
- 未来开放用户审批时，再增加明确的 `resolve-approval` custom command，并单独定义
  Task/Run/requestId 校验、状态迁移、checkpoint 恢复和幂等规则。

### 7.4 Schema 与数据库策略

当前项目为绿地项目，不兼容旧数据库：

- `APP_MODELS` 只保留 Task snapshot、Command、Run 及其他仍有领域意义的模型；
- `APP_MODELS` 必须注册 `ConversationTaskSnapshotModel` 和
  `ConversationTaskContextModel`；
- 新鲜 SQLite schema 中不得创建四张旧表；
- 不写旧表删除迁移、不写兼容读取、不保留旧表名；
- 开发环境删除/重建现有 SQLite 主库和相关测试 fixture；
- schema 初始化只负责当前模型建表、索引和约束；删除当前面向历史库的列补齐、列重命名、
  `_COLUMN_DROP_MAP` 和旧列清理逻辑，不建立数据库迁移/版本管理层；开发期通过删除并
  重建 SQLite 完成结构变化；
- `conversation_task_snapshots.task_id` 保持唯一约束；Task 级联删除必须删除 snapshot；
- `conversation_task_contexts.task_id` 保持唯一约束；Task 级联删除必须删除 context，
  并在 `cascade_deletion.py` 和删除 service 中有明确覆盖；
- 从 `ConversationTaskSnapshotModel`、`ConversationTaskSnapshotCrud`、schema 初始化和
  测试 fixture 中删除 `schema_version`；同时删除 `fencing_version`、
  `executor_lease_owner`、`executor_lease_expires_at`、`workflow_version` 和
  `conversation_revision` 的模型、读写、迁移清理与测试引用；它们不是本地单用户 Agent
  所需的能力；
- snapshot/context 行不存在、JSON 非法或不符合当前固定结构时显式失败并写结构化错误日志，
  不静默从旧表恢复；不实现 schema version 协商、版本兼容或版本迁移。

## 8. 实施顺序

### Phase 1：固定中性契约和 owner 边界

1. 在 `app.assistant_transport` 固定 `ConversationStateSnapshot`、message、part、tool 等
   Transport 协议类型及 snapshot owner；approval 类型只保留文档级预留，不在本阶段创建
   运行时代码；
2. 将 Agent context service 保持在 `app.service.task`，Transport mutation writer 与事务
   适配保持在 `app.assistant_transport`；
3. 将 `assistant_stream` 依赖限制在 `app.assistant_transport` API/adapter；
4. 删除旧路径 re-export，更新依赖注入和所有 import；
5. 明确 `ConversationTaskSnapshotService` 是唯一可写 owner。

### Phase 2：把 Conversation state 完整收口进 snapshot

1. 扩展 snapshot schema：消息、text/reasoning、tool-call、run/error；保留空的
   `approvals` 和可选 `approvalRequestId` 位置，但不实现审批状态；
2. 实现消息、ID 唯一性和 tool 状态迁移校验；仅校验预留 `approvals`/`approvalRequestId`
   的结构位置，不实现审批状态迁移；
3. 将 command/run 初始创建与 snapshot 初始消息放入一笔事务；
4. 将所有 UI/Transport runtime mutation 改为 snapshot mutation；Agent context mutation
   走独立 context owner，不写入 snapshot；
5. 保证 commit 后 publish，事务失败不发 Transport mutation；
6. 明确当前 UI 和 Transport 不提供审批决策入口；审批 custom command 延后到独立阶段。

### Phase 3：迁移 LLM context

1. 新增 `ConversationTaskContextModel`、CRUD、service 和保留 `run_id` 的 `ContextEntry` 类型；
2. 删除 `ConversationRunMessageStore` 及 `RuntimeMessageStore`；将所有调用方直接改用
   `ConversationTaskContextService` 的 context reader/writer port，不再查询旧表，也不保留
   旧类名 adapter；
3. 将 context working copy 和 workflow 输入统一改为 LangChain 原生消息，持久化只使用
   `message_to_dict()` / `messages_from_dict()`；
4. 移除 `RuntimeMessage` 及其在 `runtime_context_manager.py`、`runtime_operations.py`、
   `workflow.py`、`tools_node.py`、`tool_execution_service.py`、context listener 和
   compressor 链路中的所有参数、返回值和转换函数；
5. 移除 `RuntimeContextManager` 的 task message sequence 和旧 message 表 clear side effect；
6. 覆盖历史消息、reasoning、tool-call、tool result、failed/cancelled 的 context 重建；
7. 确认完整消息、工具结果和 Run 状态等语义 mutation 中 Agent context 与 snapshot
   在同一业务 mutation 内原子提交；chunk 级 snapshot-only mutation 不写 context，但其
   partial 语义和恢复行为必须有测试；
8. 确认 Agent 每次调用的 context 与 Conversation 顺序一致。

### Phase 4：删除旧事实模型

1. 删除四张旧表模型、CRUD、Record、imports 和 cascade 分支；
2. 删除 `Task.message_sequence` 和旧 bootstrap；
3. 从 `APP_MODELS` 移除旧模型，注册 Agent context model，更新新鲜数据库 fixture；
4. 删除旧代码文档和测试名称；
5. 删除 snapshot/context 的所有版本号字段及读写逻辑；对干净 SQLite 执行 schema 检查，
   确认旧表不存在、两张 JSON 表存在且没有版本号列。

### Phase 5：Transport 与桌面验收

1. 首次 GET/POST 只从 snapshot 读取；
2. 文本 chunk 只产生 append-text；
3. 状态和 tool 只产生最小 set；预留 approval 字段本阶段不产生 mutation；
4. 断线重连只 hydrate snapshot，不依赖旧表；
5. backend 停止/崩溃后由 Tauri supervisor 的重试/重启动作再次启动时，先执行明确的
   startup recovery service，再报告 ready；遗留 run/tool 状态正确失败；
6. 用户“继续”创建新 command/run，不复用旧运行 task；
7. 后续再把 50ms fallback 替换为进程内 notifier。

## 9. 验收标准

### 数据与模型

- 干净 SQLite 中存在 `conversation_task_snapshots`，每个 Task 最多一行；
- 干净 SQLite 中存在 `conversation_task_contexts`，每个 Task 最多一行；
- 干净 SQLite 中不存在 `conversation_messages`、`conversation_message_parts`、
  `conversation_tool_calls`、`human_approval_requests`；
- Task 删除不会留下 snapshot；
- Task 删除不会留下 Agent context；
- snapshot/context JSON 非法、缺字段或不符合当前固定结构时显式失败；不做版本号协商、
  版本兼容或版本迁移；
- Command/Run 仍保持各自的唯一约束、状态约束和崩溃恢复职责。

### Conversation 与 context

- user/assistant 展示消息、tool-call part 和错误状态完全由 snapshot 保存；approval 仅
  保留空 schema 位置，不产生当前展示状态；
- system/tool 的模型上下文消息完全由 `conversation_task_contexts` 保存，二者不互相读取；
- text、reasoning、tool-call 的顺序和状态可完整重建；
- LLM context 只从 `conversation_task_contexts` 重建，不查询四张旧表或 snapshot；
- Agent context 的每次 mutation 都能持久化并从独立表重新 hydrate；
- `ContextEntry.run_id` round-trip 后保持 `None`/对应 Run id、消息 ID、消息顺序和消息类型；
- context JSON 的 entry 数量与可恢复消息数量必须一致；非法 JSON、未知 message type、未知
  run、重复/缺失 entry 必须显式失败；
- 新 run 不会重复追加旧 run 消息；失败 run 的历史消息可作为后续“继续”的上下文；
- `message_to_dict()` / `messages_from_dict()` round-trip 必须覆盖多并行 tool calls、复杂
  参数、消息 id、ToolMessage 的 `tool_call_id`、成功结果、null 结果和错误结果；
- 不再存在 task message sequence 或旧 clear 逻辑；
- 静态 AST/import 门禁必须扫描 `apps/backend/app/` 和 `apps/backend/tests/`（不扫描本方案
  文档中的历史示例），确认旧表 model/CRUD/Record/import/query/bootstrap、旧
  `RuntimeMessageStore`、旧 message writer/adapter、旧 `ConversationStateService` 消息
  投影、`RuntimeMessage`、`_langraph_message_to_runtime_message()`、
  `_runtime_message_to_langraph_message()`、`message_sequence` 和 `response_text` 重复
  事实均已删除；同时确认 `schema_version`、`conversation_revision`、`fencing_version`、
  `executor_lease_owner`、`executor_lease_expires_at`、`workflow_version` 以及对应的
  `_COLUMN_DROP_MAP`/历史迁移清理逻辑均不存在；同时确认 `RuntimeConfig.approval_resolver`、
  `request_tool_approval`、`APPROVAL_INTERRUPT_KEY`、审批 helper、
  `resolve_approved_calls`、审批专用 `interrupt()` 分支、
  `mark_tool_calls_requires_action` 以及桌面端 `requires-action`/审批分支均不再存在于
  生产执行路径；不能只检查数据库表名。
### Tool（本阶段）与 Approval（未来）

- tool-call 的当前五种状态 `pending`、`running`、`completed`、`failed`、`cancelled`
  均覆盖；`requires-action` 只作为未来审批状态预留，不在本阶段产生；
- completed 的 `result: null` 不会被误判为未完成；
- failed 包含稳定错误；cancelled 包含明确取消结果；
- 进程重启后遗留 pending/running run 和 tool-call 均被标记 failed。
- 每个 AI tool call 在下一次模型调用前和 Run 终态前恰好对应一个同
  `tool_call_id` 的 ToolMessage；禁止 orphan、重复和未知 call id；
- provider 缺失 tool-call id 时，只生成一次稳定 id，并验证该 id 在 AIMessage、snapshot
  tool part 和 ToolMessage 中一致；
- failed、cancelled 分别覆盖 snapshot 状态、ToolMessage `status`、error/result 内容和
  “继续”时的闭合行为；恢复重复执行不会重复追加中断 ToolMessage。

Approval 本阶段只验证 snapshot 中 `approvals` 固定为空、`approvalRequestId` 不被当前
链路产生；不验收审批 resolve、断线恢复或 LangGraph interrupt/checkpoint 绑定。这些内容
属于未来独立阶段。

### Transport

- 初次连接发送一次 root `set` 完整 snapshot；
- 后续文本只发送 `append-text`，value 只包含新增文本；
- 新 message/part、状态、tool result/error 使用最小路径 `set`；预留 approval 字段本阶段
  不产生 mutation；
- backend 不信任客户端回传 state；
- snapshot commit 失败时不发送对应 mutation；
- 多个订阅者收到同一已提交顺序，但各自维护独立 controller state；
- converter 不伪造 assistant-ui 类型不存在的 tool part 字段。

### 分层与工程

- `core`、`service`、`storage` 不依赖 `assistant_stream` 或 assistant-ui/React 类型；
- Assistant Transport 协议类型、snapshot owner、Transport writer 和事务适配统一位于
  `app.assistant_transport.*`；core/service 只通过运行时注入的 mutation port 使用它们，不能
  复制 Transport projection 或把 UI state 当作 Agent context；
- models 只描述中性 JSON，不依赖 service/API/Transport；
- 不保留兼容 re-export、旧表双读/双写、revision、outbox 或分布式设施；
- 新增/修改函数有与真实行为一致的完整 docstring；
- 关键失败路径写入结构化本地日志；
- snapshot/context/Run 任一写入失败时，事务整体回滚、两个 working copy 不变且 Transport
  不发布；commit 成功后才发布 mutation；
- Task/Workspace 删除测试确认 snapshot、context、Run 相关数据均无残留；
- 必须经过独立架构审查 Agent 和测试 Agent 验收，直到两者均 PASS。

## 10. 风险与处理

### 10.1 Snapshot 与 Agent context 双事实源的一致性

风险：snapshot 和 context 都包含消息相关信息，若更新路径不统一会出现页面显示与 LLM
输入不一致。

处理：把 mutation 分为两类。文本/推理 chunk 是 snapshot-only mutation，在提交后发布
`append-text`，不要求 context 同步，因为此时还没有完整 `AIMessage`；完整 AIMessage、
ToolMessage、Run 终态等当前语义 mutation 同时生成 snapshot mutation 和 context mutation，
在同一 SQLite transaction 中分别写入两个一对一 JSON 文档，并与必要的 Run 行一起提交。
approval 仅是空 schema 预留，不参与本阶段 transaction。两者拥有不同 schema 和 owner，
但不允许一个当前语义 mutation 一边成功一边失败。
snapshot 只服务 Transport，context 只服务 Agent；禁止第三份 Conversation message
canonical store。

### 10.2 单行 JSON 的写放大

风险：每个文本 chunk 都重写 Task 的 `state_json`。

第一阶段优先正确性、一致性和可恢复性；先使用 SQLite 单行 snapshot。只有 profiling 证明
写放大成为实际瓶颈时，才设计事务内 mutation 合并或 chunk flush。不能通过 Transport
先发、snapshot 延迟落盘或新增 outbox 破坏 durable-before-publish 原则。

### 10.3 大工具结果

风险：tool result 可能很大，导致 snapshot 行膨胀。

处理：沿用工具输出预算和文件快照策略；snapshot 只保存 UI 所需的裁剪结果、摘要或
受控引用，context 只保存 LLM 所需的裁剪文本，不把任意 stdout 无限写入 JSON。若未来
需要完整审计，另建明确命名的
Audit/Artifact 事实，不重新引入 conversation tool-call 表。

### 10.4 SQLite 损坏或写入失败

风险：snapshot 或 context 任一 JSON 损坏都会影响对应的 UI 或 Agent 能力；两者不应
静默互相回填。

处理：事务原子写入、结构化日志、启动时分别进行 JSON/schema 校验、错误可见化；按本地
应用策略提供数据库备份/重置入口。不要静默从已删除的旧表恢复。

## 11. 最终架构结论

最终关系应是：

```text
ConversationCommandModel
  └── command_id 幂等事实

ConversationRunModel
  └── Agent 执行控制 / 崩溃恢复事实

ConversationTaskSnapshotModel
  └── Task Transport/UI state 唯一事实源
      ├── messages
      ├── text/reasoning parts
      ├── tool-call lifecycle/result/error
      ├── approval requests/decisions（未来预留，当前为空）
      ├── current run presentation
      └── user-visible error

ConversationTaskContextModel
  └── Task Agent/LLM context 唯一事实源
      ├── ordered context entries
      ├── LangChain-native tool calls/results
      └── compaction boundary/metadata
```

这不是把四张旧表简单合并成一个 JSON 字段，而是删除旧 Conversation 事实模型，建立
两个职责清晰、由同一领域 mutation 原子更新的 JSON 事实源：snapshot owner 负责 UI/Transport，
context owner 负责 Agent/LLM。两者不互相替代，也不再保留第三份消息表。
它符合本地单进程 Agent 的实际需求，也避免为不存在的多租户/分布式场景引入额外基础设施。
