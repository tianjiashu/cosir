# 用户输入附件事件与对话内渲染改造方案

> 状态：已实施，待子 Agent 按本方案独立验收。
>
> 实现范围已落地：后端初始化事件已收缩为纯骨架，用户输入事件改为有序 parts，前端用户
> 消息已在同一气泡内按 parts 区分渲染图片和普通文件；未新增数据库、迁移、附件存储或进程。
>
> 本方案只调整现有事件、Transport snapshot、前端 converter 和消息渲染逻辑，不新增数据库、
> 数据表、迁移脚本、附件存储文件或新的运行时进程。现有 `conversation_runs.image_paths`、
> `ConversationRunExtra.attachments`、workspace `.cosir/Attachment` 和 Assistant Transport
> snapshot 继续复用。

## 1. 设计结论

`RunInitializedEvent` 只表达 Run 生命周期和消息骨架建立；用户本轮实际输入（文本、图片、普通
文件）统一由 `UserInputAppendedEvent` 表达。

```text
RunInitializedEvent
  -> 创建 Run snapshot
  -> 创建空的 user / assistant 消息骨架
  -> 设置 current_run_id 和 usage 基线

UserInputAppendedEvent
  -> 追加本轮用户输入的有序 parts
  -> 同时表达 text / image / file
  -> 让 Transport snapshot 与 canonical user message 在同一个输入事实边界上收敛
```

你提出的 `file_attachments` 归属调整是正确的。最终契约不采用
`text + image_paths + file_attachments` 成为三套平行字段：如果用户输入中出现
“文字、图片、文字、普通文件”的顺序，三个字段无法表达原始顺序。本方案让
`UserInputAppendedEvent` 携带有序 `parts`，其中普通附件仍然是 `file` part，图片仍然是
`image` part。

本方案已确定采用有序 `parts`，不保留上述三套平行字段作为正式契约。

## 2. 当前问题

当前 [RunInitializedEvent](../../apps/backend/app/assistant_transport/event/run_event.py) 携带
`image_paths` 和 `file_attachments`，并在事件投影时提前创建用户消息附件 part；随后
`RuntimeContextManager` 再写入 canonical `HumanMessage`，仅通过
`UserInputAppendedEvent` 追加文本。

这造成三个问题：

1. Run 生命周期事件携带了用户消息内容，职责边界不清晰。
2. 图片、普通文件和文本不是同一个用户输入事实的一部分，无法稳定保留顺序。
3. 前端 converter 会把 image/file part 转成 assistant-ui 的 `attachments` 属性，
   [thread.aui.tsx](../../apps/desktop/components/assistant-ui/elements/thread.aui.tsx) 又把
   `UserMessageAttachments` 放在正文气泡外，因此发送后附件显示在气泡上方。

附件本身并没有丢失。当前前端只是选择了“附件区域”和“正文区域”分开渲染。

## 3. 附件类型边界

图片和普通附件不能共用同一种视觉渲染，也不能因为都叫 attachment 就共用同一种数据语义。

| 类型 | Transport part | 内容所有权 | 发送后展示 | 是否读取文件内容 |
| --- | --- | --- | --- | --- |
| 图片 | `image`，携带 `cosir-attachment://<sha256>` | workspace `.cosir/Attachment` | 缩略图卡片、预览弹窗、右上角操作 | 由附件内容接口按 locator 读取 |
| 普通附件 | `file`，携带 `cosir-local-file:<id>`、名称、MIME | 本机原始路径和 `RunExtra.attachments` | 文件图标 + 文件名的内联 token/chip | 不通过 Transport 发送二进制 |

### 3.1 图片附件

- 仍使用现有图片上传、规范化和 workspace 附件目录。
- 仍使用 SHA-256 作为稳定身份。
- Transport 只暴露受控 image locator，不暴露本机路径。
- 前端使用现有 `ImageAttachmentCard` / 图片预览逻辑。
- 图片不能降级成普通文件名 chip，否则会丢失图片预览能力。

### 3.2 普通附件

- 仍使用现有本机文件 ID、名称和 MIME 元数据。
- Transport 不发送原始本机路径和文件二进制。
- 前端使用文件图标、文件名和可删除/恢复所需的本地 attachment registry。
- 普通附件不能套用图片缩略图组件，也不能通过图片内容接口读取。
- Composer 中可以使用 contenteditable inline token；已发送消息中应使用只读的同款视觉
  token，不应继续把 Composer 的删除按钮带到历史消息。

## 4. 后端改造

### 4.1 `RunInitializedEvent` 收缩为骨架事件

修改范围：

- `apps/backend/app/assistant_transport/event/run_event.py`
- `apps/backend/app/service/task/conversation_run_service.py`
- `apps/backend/app/assistant_transport/service/conversation_run_command_service.py`

调整内容：

- 移除 `RunInitializedEvent.file_attachments`。
- 同时移除 `RunInitializedEvent.image_paths`，避免图片仍然绕过用户输入事件。
- 保留建立 Run snapshot、user/assistant 消息骨架、`current_run_id` 和 usage 基线的职责。
- user 消息骨架初始为空 parts；不在初始化事件中预填图片或普通文件。
- 不删除数据库中的 `image_paths` 或 `RunExtra.attachments`，它们仍是模型输入、历史重建和
  本机附件恢复所需的持久化事实。

如果为了兼容当前 projector 的空 text part 而暂时保留占位 text part，也只能把它视为渲染
占位，不能再在初始化事件中写入任何真实附件内容。

### 4.2 扩展 `UserInputAppendedEvent`

修改范围：

- `apps/backend/app/assistant_transport/event/run_event.py`
- `apps/backend/app/assistant_transport/state/conversation_state_part.py` 或现有 part 类型
- `apps/backend/app/assistant_transport/state/conversation_state_mutation.py`

确定契约：

```python
UserInputAppendedEvent(
    task_id=task_id,
    run_id=run_id,
    parts=[
        {"type": "text", "text": "请检查这个页面"},
        {"type": "image", "image": "cosir-attachment://<sha256>"},
        {
            "type": "file",
            "file": "cosir-local-file:<id>",
            "name": "index.html",
            "contentType": "text/html",
        },
    ],
)
```

契约要求：

- `parts` 至少包含一个有效 part，支持只有图片或只有普通附件的输入。
- `image` 只能是受控的 `cosir-attachment://` locator。
- `file` 只能是受控的 `cosir-local-file:<id>` locator。
- 普通附件只带 `id`、`name`、`contentType` 和 locator，不带原始路径。
- 事件投影不读取磁盘、不查询附件内容、不生成新的附件文件。
- 事件要保持用户输入的原始 part 顺序。

### 4.3 调整用户输入事实生产点

修改范围：

- `apps/backend/app/core/context/runtime_context_manager.py`
- `apps/backend/app/assistant_transport/service/conversation_run_command_service.py`
- `apps/backend/app/service/task/conversation_run_service.py`

流程调整为：

```text
1. 创建并持久化 Run
2. RunInitializedEvent 创建空消息骨架
3. RuntimeContextManager 写入本轮 HumanMessage
4. HumanMessage 持久化成功后，发布一次 UserInputAppendedEvent
5. projector 将完整有序 parts 写入 user message
```

`UserInputAppendedEvent` 必须在 canonical context 写入成功后发布，避免 Transport 显示了尚未
成功保存的用户输入。

为在不新增数据库字段的前提下保留 Composer 中图片与文字/普通文件的交错顺序，发送适配层
在现有 `ConversationRunExtra.display_text` 中写入内部 `[[cosir-image:<sha256>]]` marker；
`ConversationRunService` 将该 marker 从模型输入中移除，`ConversationTaskStateRebuilder` 与
事件生产共用的纯函数再按 marker 还原 image part。旧记录没有 marker 时，仍按现有
`image_paths` 顺序追加图片。

对于“只有附件没有文本”的输入，也必须发布事件，不能继续用当前 `text: min_length=1`
的契约把这类输入静默跳过。

### 4.4 projector 的投影规则

`UserInputAppendedEvent.plan()` 负责把有序 parts 写入已存在的 user message：

- 首次输入：将完整 parts 写入 user message。
- 重复事件：沿用现有 event/projector 去重和消息定位规则，不重复追加。
- 缺少 user message：视为事件顺序或协议错误，记录结构化日志并按现有错误策略处理。
- 不把普通文件的本机路径写入 Transport snapshot。
- 不把图片文件内容编码进 snapshot，只保存 locator。

现有 `ConversationTaskStateRebuilder` 继续使用持久化的
`conversation_runs.image_paths` 和 `ConversationRunExtra.attachments` 作为冷启动重建兜底，
不新增表、不新增数据库文件，也不要求事件历史持久化。

## 5. 前端改造

### 5.1 converter 保留类型，不混淆图片和普通文件

修改范围：

- `apps/desktop/lib/assistant/converter.ts`
- `apps/desktop/lib/assistant/contract.ts`

当前 converter 在 user message 中把 image/file 从 `content` 映射掉，再填充 assistant-ui
的 `attachments`。本次确定调整为：

- Transport 的 `image` 映射为 assistant-ui 的 image message part。
- Transport 的 `file` 映射为 assistant-ui 的 file message part，保留名称和 MIME。
- 如编辑/恢复流程仍需要 `ThreadUserMessage.attachments`，可以继续填充该字段，但发送后
  的展示不能再依赖它单独渲染。
- 不要从文件名或 MIME 推断它是图片；以后端 part type 为准。

assistant-ui 的 `MessagePrimitive.Parts` 支持按 `text`、`image`、`file` 分支渲染，参考
[官方 Message Primitive 文档](https://www.assistant-ui.com/docs/primitives/message)。

### 5.2 用户消息气泡布局

修改范围：

- `apps/desktop/components/assistant-ui/elements/thread.aui.tsx`
- `apps/desktop/components/assistant-ui/elements/attachment.aui.tsx`
- 必要时复用现有 `components/image.tsx`、`components/file.tsx`

目标结构：

```text
用户消息 Root
└─ 用户消息气泡
   ├─ 文本 part -> Markdown 文本
   ├─ 图片 part -> 图片缩略图/预览卡片
   └─ 普通 file part -> 只读文件 token/chip
```

至少需要完成：

- 不再把 `UserMessageAttachments` 放在正文气泡外。
- 图片使用图片卡片和预览逻辑。
- 普通附件使用文件 token/chip，不使用图片卡片。
- Composer 的附件删除按钮只在编辑态出现，历史消息使用只读展示。
- 消息 action bar 仍然位于消息内容下方，不被附件布局挤出气泡。

### 5.3 已确定的对话内渲染方式

本次采用“同一用户消息气泡 + 按有序 parts 渲染”的方式，不采用附件区域置于气泡外的布局。

必须使用有序 `message.parts` 渲染：

```text
text("请看")
file("index.html")
text("中的入口")
image("...")
```

不能继续把所有附件集中到单独的 `attachments` 数组，否则转换阶段已经丢失交错顺序。
普通文件此时渲染成只读 inline token；图片仍渲染成图片卡片，但在文本流中应作为独立块，
避免把大图强行塞进文字行高。

具体规则如下：

- 普通文件以只读 inline token/chip 渲染，可出现在文本之间。
- 图片以缩略图卡片渲染，并作为独立视觉块，不能强行压缩成文字 token。
- 文本、普通文件和图片按照 `parts` 顺序出现。
- 不再在用户消息 Root 下单独渲染一个位于气泡外的 `UserMessageAttachments` 区域。
- Composer 的附件预览和历史消息的附件展示继续使用不同的交互状态：Composer 可删除，
  历史消息只读。

## 6. 编辑、重发和历史重建

- 编辑入口继续使用现有 `ThreadUserMessage.attachments` 或本地 registry 恢复 Composer。
- 发送后的历史渲染以 Transport `message.parts` 为准，不从 Composer 临时状态反推。
- 普通文件恢复时仍使用 `cosir-local-file:<id>` 和本地 registry，不把路径写入 UI snapshot。
- 图片恢复时仍使用 workspace attachment locator，并通过现有附件内容接口生成预览。
- `ConversationTaskStateRebuilder` 只从现有持久化 Run/context facts 重建，不引入新的事实表。
- 旧 snapshot 不作为业务事实；进程内 snapshot 重建后应直接得到新的 parts 结构。

## 7. 不做的事情

- 不新增 SQLite 数据库、数据表、字段或迁移文件。
- 不新增附件存储服务、对象存储或网络上传服务。
- 不让前端直接读取 workspace 文件路径绕过后端边界。
- 不把普通文件二进制塞入 Assistant Transport。
- 不把普通文件当图片渲染。
- 不通过修改 `RunInitializedEvent` 继续预填用户附件来维持旧 UI。
- 不新增第二套前端消息事实或附件状态机。

## 8. 测试与验收

### 后端

- `RunInitializedEvent` 不再创建真实图片或普通文件 part。
- `UserInputAppendedEvent` 可以投影文本、图片、普通文件和附件-only 输入。
- 输入 parts 顺序在 snapshot 中保持不变。
- 普通附件 locator、名称、MIME 正确，原始路径不会进入 Transport snapshot。
- 图片 locator 正确，图片内容仍由现有附件接口提供。
- 重复事件不会重复追加 user parts。
- 冷启动重建仍能从现有 `image_paths` 和 `RunExtra.attachments` 恢复。

### 前端

- 发送后普通附件显示为文件 token/chip，不显示为图片卡片。
- 发送后图片显示为缩略图卡片，可打开预览。
- 两类附件都位于同一个用户消息气泡内部。
- 普通附件的只读历史展示不带 Composer 删除按钮。
- 按 parts 顺序渲染时，文字、文件、图片不会重排或重复。
- 编辑/重发能恢复两类附件。
- pending command、canonical snapshot 和冷启动重建的视觉结果一致。

## 9. 建议实施顺序

1. 先调整事件契约和 projector 测试，确认 Run 初始化不再携带用户附件。
2. 再调整 `RuntimeContextManager` 和两个 Run command service 的事件生产顺序。
3. 更新前端 Transport contract/converter，确保 image/file 类型不被混为一类。
4. 将用户附件渲染移动到消息气泡内部。
5. 最后实现按有序 parts 的精确 inline 渲染，并补齐编辑、重发和冷启动验收。

整个改造仍然运行在现有 Tauri → FastAPI → SQLite / workspace 文件系统边界内，不增加新的
数据库或进程边界。
