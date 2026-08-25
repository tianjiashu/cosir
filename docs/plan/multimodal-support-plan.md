# 多模态接入改造计划（图片优先，音视频预留）

> 状态：方案待评审
> 决策基准：第零铁律（以长期稳定迭代为尺）+ 用户确认的三项决策
> 适用代码基线：`apps/backend` + `apps/desktop` + `apps/shared/ts`（以 2026-08-22 真实代码为准）

---

## 〇、用户已确认决策（约束本方案）

1. **不做兼容迁移**：绿地项目，旧库不兼容，直接完整改造，不留「`content_text` + `content_blocks` 双轨并存」的技术债。消息协议一步到位为多模态 content blocks。
2. **音视频先预留协议，不打通通路**：`ContentBlock.type` 预留 `video` / `audio` / `file_ref`，但第一版只实现图片（`image`）的端到端闭环；视频/音频仅协议预留，不接模型推理。
3. **媒体存储位置：先落本地**：桌面应用前后端同源本地运行，附件落 workspace 内本地目录（如 `workspace/.attachments/`），模型调用时按厂商规范转 `data_uri` / 本地 `file://` 传给 LLM，不上传远端。
4. **常用还是图片**：第一版范围收敛到图片多模态（最高频、最成熟），优先打通图片上传 → 协议承载 → 模型推理 → 渲染回放全链路。

---

## 一、现状诊断（基于 CodeGraph 实证）

| 层 | 文件 | 现状 | 多模态瓶颈 |
|----|------|------|-----------|
| 消息协议（leaf） | `app/models/runtime_message.py` | `RuntimeMessage` 仅有 `content_text: str` | 无 content blocks 概念，无法承载媒体 |
| 存储模型 | `app/storage/model/turn_message_model.py` | `content_text` 列 `nullable=False`，`metadata_json` 列 | 媒体无结构化落库位置 |
| 上下文构建（core） | `app/core/context/runtime_context_manager.py:389-390` | `user` 消息 `HumanMessage(content=content_text)` 纯文本 | 转换单一收口点是纯文本，未展开 blocks |
| LLM 工厂 | `app/core/llm/factory.py` | `ChatLiteLLM` 透传纯文本 | **非瓶颈**——LangChain `ChatLiteLLM` + litellm **原生支持**多模态 `content=[{type:"image_url",...}]` |
| 厂商能力 | `app/core/llm/provider_capability.py` | 无多模态相关能力元数据 | 无法做「模型是否支持多模态」门禁 |
| API 接入 | `app/api/turns_api.py` + 请求 schema | `TurnCreateRequest` 仅纯文本 message | 无附件入口 |
| 前端输入 | `apps/desktop/src/components/chat/UserMessage.tsx` + `InputBar` | `UserMessage` 仅 `content: string`，无附件上传 | 无附件选择/上传 UI |
| 前端协议 | `apps/shared/ts/` | 无 `Attachment` 类型 | TS 层无协议对齐 |

**核心结论**：瓶颈全在自建协议与消息构建链路把多模态「阉割」成纯文本，**LLM 底座无需替换**（符合第零铁律「不重复造轮子」）。改造是「在自建协议正确承载 content blocks，并在转 LangChain 消息时如实展开」。

---

## 二、目标架构

```
用户输入(图片附件)
   ↓ [前端 InputBar 选择/拖拽]
本地落盘 workspace/.attachments/<uuid>.<ext>   ← 媒体存储位置决策③
   ↓ [构造 AttachmentMeta: type/mime/local_path/size]
TurnCreateRequest.attachments[]                 ← API 层扩展
   ↓ [后端落库]
RuntimeMessage.content_blocks: list[ContentBlock]  ← 协议层完整改造（决策①，不双轨）
   ↓ [runtime_context_manager 展开]
HumanMessage(content=[{type:"text"},{type:"image_url",...}])  ← 单点展开
   ↓ [ChatLiteLLM 原生多模态]
模型推理（图片理解）
   ↓ [事件流 + 历史回放]
前端 AttachmentView 渲染（图片缩略图）
```

---

## 三、改造分层与职责（严格遵循既有分层，不跨层）

### 阶段 1：协议与存储层（leaf，无编排依赖）

**3.1 新增 `app/models/content_block.py`（一文件一类，单一职责）**
- `ContentBlockType` 枚举：`TEXT` / `IMAGE` / `VIDEO` / `AUDIO` / `FILE_REF`（音视频/文件为决策②预留）。
- `ContentBlock` dataclass（frozen 值对象）：
  - `block_type: ContentBlockType`
  - `text: str | None`（TEXT 块）
  - `mime: str | None`
  - `local_path: str | None`（本地落盘路径，决策③）
  - `data_uri: str | None`（按需由 local_path 转换，不入库存 data_uri 避免膨胀）
  - `detail: str | None`（图片 `low`/`high`，openai 规范）
  - `size_bytes: int | None`
  - `duration_seconds: float | None`（AUDIO/VIDEO 预留）
  - 工厂方法 `from_text` / `from_image` / `from_file`（值对象自带工厂，不留 Mapper 类）。
- `to_langchain_part()`：把单个 block 转为 LangChain/OpenAI 多模态 part 字典（`{"type":"image_url","image_url":{"url":...,"detail":...}}`）；按厂商差异在此收口（openai 系 vs gemini 系字段名），与现有 thinking 分派模式一致。

**3.2 改造 `app/models/runtime_message.py`（完整改造，不留双轨）**
- **移除** `content_text: str` 单字段；改为：
  - `role: str`（保留）
  - `blocks: list[ContentBlock]`（**唯一**内容载体，TEXT 块承载原 content_text）
  - `metadata: dict`（保留 tool_calls / tool_call_id 等）
- 向后兼容代码全部改写（绿地，无旧库负担，决策①）：所有 `RuntimeMessage(content_text=...)` 调用点改为 `RuntimeMessage(blocks=[ContentBlock.from_text(...)])`。
- `estimate_tokens()` 重写：遍历 blocks——TEXT 走字符启发式；IMAGE 按 `size_bytes` + `detail` 估算（high≈像素/750 经验比率，low 固定 85 token）；AUDIO 按 `duration_seconds` 预留；不进入模型的 metadata 不计入。

**3.3 改造 `app/storage/model/turn_message_model.py`**
- 移除 `content_text` 列（`nullable=False` 的硬约束随之消除）。
- 新增 `blocks_json: Mapped[str]` 列（JSON 序列化 `list[ContentBlock]`），承载全部内容。
- `in_context` 列保留。
- 落库/读库在 `turn_message_crud.py` 做 `ContentBlock` ↔ JSON 双向序列化。

**3.4 更新调用点（完整改造，不残留旧签名）**
- 全局搜索 `RuntimeMessage(` 构造与 `.content_text` 访问，全部迁移到 `blocks` 范式。涉及：`runtime_operations.py`、`turn_runtime_message_store.py`、`run_result.py`、`observation_node.py` 等（以 CodeGraph `RuntimeMessage` 的 19 个 caller 为准）。
- 涉及 `runtime_context_manager` 的 `_langraph_message_to_runtime_message`（LangChain 消息 → RuntimeMessage 反向）同步改写，支持从 assistant/content 反序列化 blocks。

### 阶段 2：上下文构建层（core，单点展开）

**3.5 改造 `runtime_context_manager._to_model_message`（唯一转换收口，现 389-390 行）**
- `user` 消息：当 `blocks` 含非纯文本块时，构建 `HumanMessage(content=[block.to_langchain_part() for block in message.blocks])`；纯文本 blocks 退化为 `HumanMessage(content=text)`（LangChain 接受 str 或 list）。
- 抽象出 `multimodal_part_builder`（可独立为 `core/context/multimodal_part_builder.py`，单一职责），负责：按 `ProviderCapability.multimodal_format` 选择厂商格式、把 `local_path` 读为 `data_uri`（带大小/类型校验与错误归一）、拼装 part 字典。

**3.6 新增 `ProviderCapability` 多模态能力元数据（`app/core/llm/provider_capability.py`）**
- `supports_multimodal: bool`
- `multimodal_format: Literal["openai","gemini",...]`（决定 part 字段名差异）
- `supported_media_types: set[ContentBlockType]`（如 `{IMAGE}`，音视频后续追加）
- `provider_capability` 注册表（5 类厂商）同步补充上述字段。**不改动 factory 本身**——能力校验放在 model_node 构建前（不支持时返回清晰 `error` + `retryable=False`，而非让 litellm 抛 400 崩溃），符合既有 thinking 分派模式。

### 阶段 3：API 与前端层（端到端闭环）

**3.7 后端 API（`app/api/turns_api.py` + 请求 schema）**
- `TurnCreateRequest` 新增 `attachments: list[AttachmentMeta]`（可选）。
- 新增 `AttachmentMeta` 请求模型（`app/api/schemas/request/`）：`type` / `mime` / `filename` / `data`(base64 或 data_uri) / `size_bytes`。
- 处理流程：接收附件 → 落盘 `workspace/.attachments/<uuid>.<ext>`（路径经 `ProjectPathResolver.resolve` 边界校验，复用既有 workspace 边界守卫）→ 构造 `ContentBlock.from_image(local_path=...)` → 组装 `RuntimeMessage.blocks`。
- **日志规范**：写 `info` 级 `turn_attachment_received`（类型/大小/数量），**绝不记 data_uri/base64 原文**（遵循项目日志规范，避免敏感/体积污染）。

**3.8 前端（`apps/desktop`）**
- `InputBar`（`components/layout/InputBar.tsx` 或相关）：新增附件选择/拖拽上传，本地读文件 → 调后端附件上传端点或随 turn 创建一并提交 `attachments`。
- `UserMessage.tsx`：属性由 `content: string` 改为 `blocks: ContentBlockView[]`（或保留 text 兼容层仅用于旧历史？否——决策①完整改造，前端同步改为 blocks 渲染）。
- 新增 `AttachmentView` 组件（`components/chat/AttachmentView.tsx`，单一职责）：图片缩略图（点击放大）、音频/视频占位（决策②仅渲染占位，待后续接通）、文件卡片。
- 历史回放（TurnTimeline）复用同一 `AttachmentView`，保证「发送即所见、回放即所见」一致。

**3.9 共享协议（`apps/shared/ts/`）**
- 新增 `Attachment` / `ContentBlock` TS 类型，经现有 TS 生成链路对齐（与 `events.ts` 生成机制一致，不手改核心协议）。
- 前端 `useBackend` / `turn` 提交逻辑同步扩展 `attachments` 字段。

---

## 四、测试与闭环（开发-审查-测试闭环，强制）

按项目规范，改造完成后必须启动**独立**审查 Agent + 测试 Agent，不得自行宣布完成。

**单元测试（业务逻辑层，pytest）**
- `ContentBlock`：工厂方法、枚举、JSON 序列化往返、`to_langchain_part` 各类型输出。
- `RuntimeMessage`：`blocks` 范式构造、`estimate_tokens` 各媒体类型估算（图片 high/low、文本）。
- `runtime_context_manager._to_model_message`：纯文本退化、图片 blocks 展开为 `HumanMessage(content=[...])`、反向 `_langraph_message_to_runtime_message` 往返一致。
- `turn_message_crud`：blocks JSON 落库/读库往返。
- `multimodal_part_builder`：openai/gemini 格式差异、local_path → data_uri 转换、超大小/类型错误归一。
- `provider_capability`：多模态元数据注册表取值。

**集成/边界测试**
- 给不支持多模态的模型发图片 → 期望清晰 error（非 400 崩溃）。
- 音视频块（决策②预留）→ 协议层可承载、构建层不强行推理、前端渲染占位。
- 附件落盘路径越界（workspace 外）→ 复用 `ProjectPathResolver` 拦截。

**审查 Agent 重点**
- 是否残留 `content_text` 双轨（决策①红线）；
- 是否跨层（api 直接拼 LangChain / tools 调 service）；
- 日志是否漏记上下文或误记 data_uri；
- 每个新函数 docstring 是否同步。

---

## 五、依赖与版本（不重复造轮子）

- **不引入新依赖**：多模态展开、data_uri 转换、JSON 序列化均用标准库 + 既有 `TokenEstimator` + LangChain 原生能力即可。
- **音视频转码/抽帧**（决策②后续阶段）：届时优先引入成熟库（如 `ffmpeg-python`），不手写编解码。本方案阶段内不涉及。
- 路径边界复用既有 `ProjectPathResolver`，不新造轮子。

---

## 六、落地顺序（敢改但聚焦，按依赖拓扑）

1. `content_block.py` → 2. `runtime_message.py` + 调用点迁移 → 3. `turn_message_model` + crud → 4. `runtime_context_manager` 展开 + `multimodal_part_builder` → 5. `provider_capability` 多模态元数据 → 6. API 附件端点 + 落盘 → 7. 前端 InputBar/UserMessage/AttachmentView + shared 协议 → 8. 测试 + 独立审查/测试闭环。

阶段 1-3 为图片端到端闭环；音视频仅协议预留（类型/字段/前端占位），不接推理。

---

## 七、风险与判据（第零铁律）

- **收益**：协议一步到位多模态，后续加视频/音频只是加 `ContentBlock.type` + `multimodal_part_builder` 分支，长期迭代成本低，无双轨债。
- **风险点**：`runtime_context_manager` 是消息转换唯一收口，改动需配套双向转换测试；厂商多模态格式差异（openai vs gemini）需在 `multimodal_part_builder` 单一收口，避免散落。
- **不做的**：不替换 LLM 底座、不引入多模态专用库、不做旧库兼容迁移（决策①）。
