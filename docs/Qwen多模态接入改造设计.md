# Qwen（通义千问）多模态接入改造设计

> 状态：v1（改造设计方案）｜ 依据：《Qwen多模态接入方案.md》（v1.1）+ 真实代码调研 + 《AGENTS.md》+ 《Agent代码开发规范.md》
> 撰写日期：2026-08-05

---

## 1. 目标与范围

在现有 Python + FastAPI + LangGraph 后端中，以 **OpenAI 兼容模式**接入阿里云百炼（DashScope）的 Qwen 模型，使系统支持 **文字 / 图片 / 视频**三类输入。改造必须：

1. 复用既有 `LLMProvider` 抽象与 `build_chat_model` 工厂，不引入平行 Backend 体系（避免重复造轮子）。
2. 在模型无关层引入统一的多模态内容契约 `ContentBlock`，使 LangGraph 节点不被厂商格式绑架（对齐 AGENTS.md「契约单一事实来源」）。
3. 贯通「请求 → 运行时上下文 → 模型调用 → 持久化」全链路，且对纯文本 Agent 完全向后兼容。
4. 遵循《Agent代码开发规范.md》第零铁律与五条铁律：单一职责、不重复造轮子、改动聚焦、目录结构清晰、可排查日志。

**不在本次范围**：桌面客户端（Tauri/React）UI 改造、富前端预览、`langgraph-responses-gateway` 联动、原生 DashScope SDK、模型热切换策略。

---

## 2. 与《AGENTS.md》/ 开发规范的对齐 & 方案原文偏差修正

方案原文（《Qwen多模态接入方案.md》）假设了 `ModelBackend` / `OpenAIBackend` / `DeepSeekBackend` / `QwenBackend` 这一套接口，并建议新增 `QwenBackend` 直接持有 `openai.OpenAI` client。经代码调研，真实架构与原文存在关键偏差，落地时必须修正：

| 方案原文假设 | 真实代码（事实） | 修正后的落点 |
|---|---|---|
| `ModelBackend` 抽象 + `QwenBackend` 直接调用 `openai` SDK | `LLMProvider(ABC)` + `DeepSeekProvider` + `build_chat_model` 工厂；模型用 `langchain_openai.ChatOpenAI` 封装 | **新增 `QwenProvider(LLMProvider)`**，返回 `langchain_openai.ChatOpenAI`（base_url 指向 DashScope）。复用 `build_chat_model` 路由，不新建 Backend 体系 |
| `QwenBackend.to_openai_messages()` 做 ContentBlock 转换 | 转换集中在 `core/llm/langchain_bridge.runtime_to_langchain` | **`ContentBlock` 作为模型无关契约**放在 `app/models/`；`runtime_to_langchain` 负责 `ContentBlock → OpenAI part dict` 的忠实转换 |
| 多模态消息直接进 `messages[].content`（part 数组） | 全链路纯文本：`RuntimeMessage.content_text` / `turn_messages.content_text` / `CreateTurnRequest.input_text` / `TurnRecord.input_text` | 给 `RuntimeMessage` 增加 `content_blocks` 可选字段，并同步扩展请求、记录、持久化与上下文构建 |
| 通过 `MODEL_PROVIDER=qwen` 切换 | 无此环境变量；工厂按 `model_name` → `DeepSeekProvider` 硬编码 | 在 `ModelSettings` 增加 `provider` 可选字段（向后兼容），`build_chat_model` 按 `provider` 路由 |
| 配置项放 `.env`（`DASHSCOPE_*` / `QWEN_*`） | 模型配置不在 `config/settings.py`，而在 `core/llm/model_settings.py` 的 `ModelSettings` 值对象 | **复用 `ModelSettings`（base_url + api_key_env）** 承载 Qwen 端点，无需新配置层；密钥/端点走环境变量 |

核心结论：**不要新建一套 Qwen 专属调用栈**。把 Qwen 当作「又一个 OpenAI 协议兼容的 Provider」接入现有 `LLMProvider` 体系，把多模态当作「消息内容契约的扩展」，是改动最小、最贴合既有架构的路径。

---

## 3. 总体架构落点

```
apps/desktop (React / Tauri)              apps/backend (FastAPI)
        │  POST /tasks/{id}/turns              │
        │  { input_text, attachments:[...] }   │
        ▼                                      ▼
CreateTurnRequest ──▶ TurnService.create_turn ──▶ turns 表(attachments_json)
                                                          │
                                                  RuntimeContextBuilder.build_messages
                                                          │  current_turn.attachments
                                                          ▼
                                              RuntimeMessage(content_blocks=[ContentBlock...])
                                                          │
                                                  langchain_bridge.runtime_to_langchain
                                                          │  ContentBlock → OpenAI part dict
                                                          ▼
                                              HumanMessage(content=[text/image_url/video...])
                                                          │
                                              ReactLikeWorkflow._model_node (astream)
                                                          │  build_chat_model(provider="qwen")
                                                          ▼
                                              QwenProvider.build → QwenChatOpenAI(DashScope)
                                                          │
                                                  turn_messages 持久化(content_json)
```

设计要点：
- **契约单一事实来源**：`ContentBlock` 类比 `ToolDefinition`，是跨 provider 的多模态内容契约。
- **转换唯一出口**：`langchain_bridge` 仍是 `RuntimeMessage → LangChain` 的唯一转换点；多模态 part 在此生成。
- **门控在上下文层**：仅当 Agent 模型支持多模态（profile 标记）且 turn 带附件时，才注入 `content_blocks`；纯文本 Agent 完全走旧路径。

---

## 4. 分层改造清单

### 4.1 LLM Provider 路由（必须做）

**新增文件** `apps/backend/app/core/llm/llm_provider/qwen_provider.py`：

```python
"""Qwen 模型提供商——基于 LangChain OpenAI 兼容客户端构建 chat model。

单一职责：把 DashScope 兼容端点的 base_url / api_key / thinking 差异封装为
``langchain_openai.ChatOpenAI`` 实例，与 DeepSeekProvider 平级。所有流式解析、
工具调用累积、消息格式转换交给 LangChain + langchain_bridge + LangGraph。
"""

from os import environ
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessageChunk
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from app.config.logging.logger import log
from app.core.llm.llm_provider.base import LLMProvider
from app.core.llm.model_settings import ModelSettings


class QwenChatOpenAI(ChatOpenAI):
    """Qwen 兼容的 ChatOpenAI：收拢 DashScope 在 delta 中返回的 reasoning_content。"""

    def _convert_chunk_to_generation_chunk(self, chunk, default_chunk_class, base_generation_info):
        generation_chunk = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info
        )
        if generation_chunk is None:
            return generation_chunk
        message = generation_chunk.message
        if not isinstance(message, AIMessageChunk):
            return generation_chunk
        choices = chunk.get("choices") or chunk.get("chunk", {}).get("choices", [])
        if not choices:
            return generation_chunk
        reasoning = (choices[0].get("delta") or {}).get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            message.additional_kwargs["reasoning_content"] = reasoning
        return generation_chunk


class QwenProvider(LLMProvider):
    """Qwen 模型适配器，基于 LangChain OpenAI 兼容客户端（DashScope compatible-mode）。"""

    def build(self, model_name: str, model_settings: ModelSettings | None = None) -> BaseChatModel:
        api_key_env = model_settings.api_key_env if model_settings is not None else None
        api_key = environ.get(api_key_env) if api_key_env else None
        base_url = model_settings.base_url if model_settings is not None else None
        extra: dict[str, Any] = {}
        if model_settings is not None:
            if model_settings.thinking:
                extra["thinking"] = {"type": "enabled"}
            if model_settings.temperature is not None:
                extra["temperature"] = model_settings.temperature
            if model_settings.top_p is not None:
                extra["top_p"] = model_settings.top_p
            if model_settings.max_tokens is not None:
                extra["max_tokens"] = model_settings.max_tokens
        log.info("llm_qwen_build", extra={"msg": f"构建 Qwen chat model，model={model_name}"})
        return QwenChatOpenAI(
            model=model_name,
            base_url=base_url,
            api_key=SecretStr(api_key) if api_key else None,
            streaming=True,
            **extra,
        )
```

**修改** `core/llm/model_settings.py`：增加 `provider` 字段（向后兼容）。

```python
_FIELDS = ("provider", "temperature", "top_p", "max_tokens", "thinking", "base_url", "api_key_env")

@dataclass(frozen=True)
class ModelSettings:
    provider: str | None = None          # 新增：显式路由标记，"qwen" / "deepseek" / None
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    thinking: bool | None = None
    base_url: str | None = None
    api_key_env: str | None = None
```

**修改** `core/llm/factory.py`：按 `provider` 路由（在 API Key 校验之后）。

```python
from app.core.llm.llm_provider.qwen_provider import QwenProvider
...
provider = model_settings.provider if model_settings is not None else None
if provider == "qwen":
    return QwenProvider().build(model_name, model_settings)
return DeepSeekProvider().build(model_name, model_settings)
```

**修改** `core/agents/agent_profile.py`：新增 Qwen 多模态 profile（端点复用 `ModelSettings`，密钥走 env）。

```python
def multimodal_qwen_agent() -> AgentProfile:
    return AgentProfile(
        agent_id="qwen_vl",
        role="multimodal-understanding",
        goal="基于文字/图片/视频理解用户意图并完成软件工程任务",
        allowed_tools=list(DEFAULT_DEVELOPER_TOOLS),
        context_policy="text_only_v1",
        model_name="qwen3.7-plus",           # 默认通用视频模型，见第 6 节确认项
        model_settings=ModelSettings(
            provider="qwen",
            base_url=os.getenv("QWEN_BASE_URL",
                               "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            api_key_env="DASHSCOPE_API_KEY",
        ),
    )
```

### 4.2 多模态消息载体 `ContentBlock`（必须做）

**新增文件** `apps/backend/app/models/content_block.py`：模型无关的多模态内容契约。

```python
"""多模态消息内容块——模型无关的内部契约（类比 ToolDefinition 之于工具）。"""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ContentBlock:
    """表示一个多模态内容块。

    type:
        "text"         -> text（纯文本）
        "image_url"    -> url（公开 URL 或 base64 data URI）
        "video"        -> frames（帧图片 URL 列表，按 fps 抽帧）
        "video_url"    -> url（视频文件 URL 或 base64，<7MB）
    """

    type: Literal["text", "image_url", "video", "video_url"]
    text: str | None = None
    url: str | None = None
    frames: list[str] | None = None
    fps: int = 2

    def to_dict(self) -> dict:
        data = {"type": self.type, "fps": self.fps}
        if self.type == "text":
            data["text"] = self.text
        elif self.type == "image_url":
            data["url"] = self.url
        elif self.type == "video":
            data["frames"] = self.frames
        elif self.type == "video_url":
            data["url"] = self.url
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "ContentBlock":
        return cls(
            type=data["type"],
            text=data.get("text"),
            url=data.get("url"),
            frames=data.get("frames"),
            fps=data.get("fps", 2),
        )
```

**修改** `app/models/runtime_message.py`：增加可选 `content_blocks`。

```python
from app.models.content_block import ContentBlock

@dataclass(frozen=True)
class RuntimeMessage:
    role: str
    content_text: str
    metadata: dict[str, str] = field(default_factory=dict)
    content_blocks: list[ContentBlock] | None = None  # 多模态载体；None 时退化为纯文本
```

### 4.3 消息转换器适配 `langchain_bridge`（必须做）

修改 `runtime_to_langchain`：user 消息若带 `content_blocks`，转为 part 数组；否则维持 `content_text` 旧路径（向后兼容）。

```python
from app.models.content_block import ContentBlock

def _blocks_to_parts(blocks: list[ContentBlock]) -> list[dict]:
    parts = []
    for b in blocks:
        if b.type == "text":
            parts.append({"type": "text", "text": b.text})
        elif b.type == "image_url":
            parts.append({"type": "image_url", "image_url": {"url": b.url}})
        elif b.type == "video":
            parts.append({"type": "video", "video": b.frames, "fps": b.fps})
        elif b.type == "video_url":
            parts.append({"type": "video_url", "video_url": {"url": b.url}, "fps": b.fps})
    return parts
```

在 `runtime_to_langchain` 的 `user` 分支：

```python
elif message.role == "user":
    if message.content_blocks:
        converted.append(HumanMessage(content=_blocks_to_parts(message.content_blocks)))
    else:
        converted.append(HumanMessage(content=message.content_text))
```

> 说明：`type:"video"` / `type:"video_url"` 是 DashScope 对 OpenAI 协议的扩展；`ChatOpenAI` 会原样把 content list 透传给端点，非标准 part 不影响调用。门控（是否注入 video 块）在 4.5 上下文层完成，bridge 只做忠实转换。

### 4.4 Turn 请求与持久化扩展（必须做）

**修改** `api/schemas/request/CreateTurnRequest.py`：新增 `attachments`。

```python
from typing import Literal
from pydantic import BaseModel, field_validator


class AttachmentInput(BaseModel):
    type: Literal["image", "video", "video_url"]
    url: str | None = None          # image / video_url 用
    frames: list[str] | None = None  # video（抽帧列表）用
    fps: int = 2


class CreateTurnRequest(BaseModel):
    input_text: str
    agent_id: str | None = None
    attachments: list[AttachmentInput] | None = None  # 新增

    @field_validator("input_text")
    @classmethod
    def input_text_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("input_text must not be blank")
        return value
```

**修改** `models/turn_record.py`：增加 `attachments`。

```python
from app.models.attachment import Attachment  # 见下

@dataclass
class TurnRecord:
    turn_id: str
    task_id: str
    input_text: str
    status: str
    created_at: datetime
    updated_at: datetime
    end_reason: str | None = None
    response_text: str | None = None
    agent_id: str | None = None
    attachments: list[Attachment] | None = None  # 新增；持久化字段映射
```

> `Attachment` 为内部值对象（与 `AttachmentInput` 对应但属 `models/` 层），用于 `turns` 表的 `attachments_json` 反序列化。

**修改** `storage/model/turn_model.py`（turns 表）：增加 `attachments_json` 列。
**修改** `storage/crud/turn_crud.py`：create / load 时读写 `attachments_json`（JSON 序列化）。
**修改** `service/task/turn_service.py`：`create_turn(task_id, input_text, agent_id=..., attachments=...)` 透传。
**修改** `api/turns_api.py`：`create_turn` 调用 `turn_service.create_turn(..., attachments=payload.attachments)`。

**修改** `storage/model/turn_message_model.py`：增加 `content_json` 列。
**修改** `storage/crud/turn_message_crud.py`：

```python
# save_messages：
content_json = json.dumps(
    [b.to_dict() for b in message.content_blocks], ensure_ascii=False
) if message.content_blocks else None
# load_messages：
content_blocks = (
    [ContentBlock.from_dict(b) for b in json.loads(row.content_json)]
    if row.content_json else None
)
RuntimeMessage(role=row.role, content_text=row.content_text,
               metadata=..., content_blocks=content_blocks)
```

> 两种持久化均为 **可空新增列**，不破坏既有纯文本数据，满足「向后兼容 / 迁移最小」。

### 4.5 上下文构建注入（必须做）

**修改** `core/context/runtime_context_builder.py`：把当前 turn 的 `attachments` 转为 `ContentBlock` 注入 user 消息；门控：仅当 Agent 模型支持多模态时注入，否则降级为纯文本（并在日志中告警丢弃）。

```python
def _turn_attachments_to_blocks(self, turn, agent_profile) -> list[ContentBlock] | None:
    if not getattr(turn, "attachments", None):
        return None
    if not self._supports_multimodal(agent_profile):
        log.warning("multimodal_skipped_non_mm_agent", ...)
        return None
    blocks = [ContentBlock(type="text", text=turn.input_text)]
    for att in turn.attachments:
        if att.type == "image":
            blocks.append(ContentBlock(type="image_url", url=att.url))
        elif att.type == "video":
            blocks.append(ContentBlock(type="video", frames=att.frames, fps=att.fps))
        elif att.type == "video_url":
            blocks.append(ContentBlock(type="video_url", url=att.url, fps=att.fps))
    return blocks
```

`build_messages` 中：

```python
blocks = self._turn_attachments_to_blocks(current_turn, profile)
if current_turn is not None:
    if blocks:
        messages.append(RuntimeMessage(role="user", content_text=current_turn.input_text,
                                        content_blocks=blocks))
    else:
        messages.append(RuntimeMessage(role="user", content_text=current_turn.input_text))
```

`_supports_multimodal`：读取 `agent_profile.model_settings.provider == "qwen"`（或模型名白名单）。兜底策略：非多模态 Agent 收到附件时，**记录告警并忽略附件**（不阻断任务），保证纯文本链路健壮。

### 4.6 配置项（必须做 / 建议做）

复用既有「密钥走环境变量」约定，不新增配置层：

- `DASHSCOPE_API_KEY`（必须）：百炼 API Key，**禁止硬编码**。
- `QWEN_BASE_URL`（建议）：完整 compatible-mode URL；缺省回落 `https://dashscope.aliyuncs.com/compatible-mode/v1`。带 Workspace 前缀的 URL 用于配额/计费隔离时由该变量提供。
- 默认模型与 fps 由 `AgentProfile`（`multimodal_qwen_agent`）固化，不进全局 `config/settings.py`。

> 不把 WorkspaceId / Region 硬编码进代码；由 `QWEN_BASE_URL` 环境变量整体携带，符合「密钥与端点走环境变量」。

### 4.7 本地媒体上传（建议做）

Qwen 需要**可公开访问的 URL** 或 **base64 data URI**。短/小图可用 base64 内联；大图/视频需先获得公开 URL。

- **建议做**：新增 `POST /media/upload`（multipart / base64）→ 落 OSS 或本地静态目录 → 返回公开 URL。桌面端先上传再随 `attachments` 传 URL，解耦客户端与存储细节。
- 抽帧逻辑（`video → frames`）：**建议做**，放在服务端 media 模块或桌面端。OpenAI 兼容模式无 `max_frames`，需自行控制帧数（qwen3.7-plus 系列 URL 图上限 2048 张，注意总 token）。
- **以后做**：桌面端本地文件 → 临时公开 URL（Tauri sidecar / 本地静态服务）。

> 若当前暂无 OSS，第一版可先要求附件以「公开 URL 或 base64」直接经 `CreateTurnRequest.attachments` 传入（必须做已覆盖）；上传模块作为建议做独立推进。

### 4.8 前端 shared 协议同步（建议做，前端轨道）

`apps/shared/ts/turn.ts` 由 `scripts/generate_api_ts.py` 从后端 Pydantic 生成（标注「勿手改」）。后端 `CreateTurnRequest` 增加 `attachments` 后，**必须重新生成** `turn.ts` 及桌面端 `CreateTurnRequest` 类型，否则前后端协议漂移。

- 后端 schema 变更（必须做）落地后，运行生成脚本同步 TS（建议做，前端协作）。
- 桌面端实际选择文件 → 调 `/media/upload`（建议做）→ 组装 `attachments` 发请求（前端改造，独立 tracking）。

### 4.9 `backend_health` 修正（必须做）

`core/runtime/runner.py` 的 `backend_health` 硬编码 `"model_provider": "deepseek"`。需改为按当前/默认 Agent profile 的 `model_settings.provider` 动态返回（缺省回落 `deepseek`），避免 Qwen Agent 健康上报失真。

### 4.10 日志与可观测（必须做）

遵循「可排查日志」铁律，在以下位置补结构化日志（复用统一 `log` 单例，事件名语义化）：

- `runtime_to_langchain`：转换 user 消息时记 `multimodal_blocks_converted`，含 block 类型计数。
- `RuntimeContextBuilder`：附件降级时记 `multimodal_skipped_non_mm_agent`。
- `QwenProvider.build`：记 `llm_qwen_build`（已含）。
- 视频块注入时记帧数与 fps，便于排查 token 爆量。

---

## 5. 改造优先级分级

| 级别 | 项 | 说明 |
|---|---|---|
| **必须做** | 4.1 `QwenProvider` + `build_chat_model` 路由 + `ModelSettings.provider` | 模型接入主干，复用既有抽象 |
| **必须做** | 4.2 `ContentBlock` 契约 + `RuntimeMessage.content_blocks` | 多模态承载点 |
| **必须做** | 4.3 `langchain_bridge` 转换适配 | 唯一转换出口 |
| **必须做** | 4.4 Turn 请求/记录/持久化扩展 | 全链路贯通（可空新增列，向后兼容） |
| **必须做** | 4.5 `RuntimeContextBuilder` 注入 + 门控 | 把附件变成模型可见内容 |
| **必须做** | 4.6 配置项（环境变量） | 密钥/端点不硬编码 |
| **必须做** | 4.9 `backend_health` 动态化 | 避免健康上报失真 |
| **必须做** | 4.10 结构化日志 | 可排查 |
| **建议做** | 4.7 `/media/upload` 上传模块 + 抽帧 | 解耦客户端与存储 |
| **建议做** | 4.8 重新生成 shared TS + 桌面端改造 | 前后端协议同步 |
| **以后做** | 桌面端本地文件→临时公开 URL | 依赖 Tauri 能力 |
| **以后做** | 原生 Responses API / 模型热切换 | 本方案不纳入 |

---

## 6. 对方案原文第 9 节「待确认事项」的回应

1. **OpenAI 兼容模式路线**：接受。与 AGENTS.md「优先 OpenAI 协议」一致；以 `ContentBlock` 内部 schema 隔离 DashScope 的 `video`/`video_url` 扩展，未来切 provider 仅改 bridge 的 part 映射。
2. **默认模型选型**：建议默认 `qwen3.7-plus`（通用、支持视频、性价比高）；高分辨率/强 VL 场景用 `qwen-vl-max` / `qwen2.5-vl` 作为独立 profile。**需用户确认默认模型**。
3. **视频默认传入方式**：推荐默认「抽帧列表」（`type:"video"`，frames + fps），可控 token、对时序更友好；短/小视频直接用 `video_url`。**需用户确认默认策略**。
4. **OSS 可用性**：需用户确认是否已有 OSS/对象存储。若无，建议先做「建议做」的 `/media/upload` 落本地静态目录（或最小 OSS 封装），第一版后端已支持直接传 URL/base64。

---

## 7. 实施风险与缓解

| 风险 | 缓解 |
|---|---|
| 视频不支持音轨 | 文档/系统提示明确：仅画面理解；语音依赖不保证 |
| `type:"video"`/`video_url` 非标准 | 收敛在 `ContentBlock` + bridge，provider 切换仅改映射 |
| 帧数 / token 爆量 | `/media/upload` 侧控制 `max_frames`；日志记帧数；长视频建议先关键帧抽取 |
| 本地 `file://` 不支持 | 强制走 URL/base64；不存在本地路径直通 Qwen 的路径 |
| 非多模态 Agent 收到附件 | 上下文层门控降级为纯文本并告警，不阻断任务 |
| 前后端协议漂移 | 后端 schema 变更后必须重跑 `generate_api_ts.py` |

---

## 8. 验收与测试要点（遵循 dev→review→test 闭环）

- **单元测试**：
  - `ContentBlock.to_dict/from_dict` 往返。
  - `runtime_to_langchain` 对 `content_blocks` 生成正确 part 数组；纯文本路径不变。
  - `QwenProvider.build` 返回的 `ChatOpenAI` 携带正确 `base_url`/`api_key`（用 fake key 验证构造不报错）。
  - `build_chat_model` 按 `provider` 路由到 `QwenProvider` / `DeepSeekProvider`。
  - `TurnMessageCrud` 对带 `content_blocks` 的消息「存→取」往返一致。
  - `RuntimeContextBuilder` 对多模态 turn 注入 `content_blocks`；非多模态 Agent 正确降级。
- **集成/契约测试**：用 fake key 启动，验证 `POST /tasks/{id}/turns` 接受 `attachments`；`backend_health` 对 Qwen profile 返回 `model_provider:"qwen"`。
- **日志核查**：上述结构化日志在改后默认开启，便于排查。
- **零编译/运行门槛**：保持「无 API Key 回退 fake 模型」机制，本地与单测可跑（QwenProvider 在无 key 时同样走 factory 的 fake 回退，无需真实调用）。

---

## 9. 落地顺序建议

1. 4.1 + 4.2（provider 路由 + ContentBlock 契约）—— 主干，先打通「能构建 Qwen 模型」。
2. 4.3 + 4.4 + 4.5（转换 + 持久化 + 上下文注入）—— 打通「多模态消息能进模型」。
3. 4.6 + 4.9 + 4.10（配置 + 健康 + 日志）—— 收口可运维性。
4. 4.7 + 4.8（上传 + 前端同步）—— 体验闭环，独立 tracking。
