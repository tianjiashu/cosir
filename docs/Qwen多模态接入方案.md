# Qwen（通义千问）多模态接入方案

> 目标：在现有 Python + FastAPI + LangGraph 后端中接入阿里云百炼（DashScope）的 Qwen 模型，**支持文字、图片、视频三类输入**。
> 撰写日期：2026-08-05 ｜ 状态：v1.1（已按官方视频文档校准：模型范围、视频双传入方式、fps/音轨/大小限制）

---

## 1. 结论与推荐路径

**推荐采用 OpenAI 兼容模式**（而非原生 DashScope SDK），理由：

1. 与项目《AGENTS.md》「模型接入：优先 OpenAI 协议，优先适配 DeepSeek，后续陆续接入其他大模型」的决议一致——Qwen 作为又一个「OpenAI 协议兼容」后端接入，不引入新的 SDK 范式。
2. 复用现有 OpenAI client 封装与 LangGraph 模型调用层，Qwen 与 OpenAI/DeepSeek 仅通过 `base_url` + `api_key` + `model` 配置切换。
3. 多模态消息格式（`content` 为 part 数组）与 OpenAI 标准一致，仅视频部分需要扩展 `type: "video"`（帧列表）或 `type: "video_url"`（视频文件）part，改动面最小。

> 说明：你提供的控制台 deep-link（`url=2845871` 为视频专项文档、`url=3016807` 为 API 文档）均为登录态 SPA，无法在外部读取。本方案视频部分事实依据来自与之等价的阿里云官方公开文档《视觉理解（视频）》（help.aliyun.com / platform.qianwenai.com），与控制台文档指向同一套 API。

---

## 2. 官方接入方式对比

| 方式 | 端点 / base_url | 鉴权 | 消息格式 | 多模态支持 |
|---|---|---|---|---|
| **OpenAI 兼容模式**（推荐） | `https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1` | `api_key = DASHSCOPE_API_KEY` | OpenAI Chat Completions（`client.chat.completions.create`） | text / image_url / **video（帧列表）/ video_url（视频文件，均为扩展 type）** |
| 原生 DashScope 多模态 API | `POST .../api/v1/services/aigc/multimodal-generation/generation` | `Authorization: Bearer $DASHSCOPE_API_KEY` | `{"model","input":{"messages":[...]}}`（`MultiModalConversation.call`） | text / image / video（原生字段） |

要点：
- 北京 region 的 URL **必须包含 `{WorkspaceId}`**（你的百炼工作空间 ID），不同 region 的 URL 与 API Key 互相隔离。
- 也可用无 Workspace 前缀的 `https://dashscope.aliyuncs.com/compatible-mode/v1`，但生产建议带 Workspace 以隔离配额与计费。
- API Key 获取：阿里云百炼控制台「API Key 管理」，存入环境变量 `DASHSCOPE_API_KEY`，**切勿硬编码**。

---

## 3. 模型选择（关键决策点）

| 输入类型 | 推荐模型 | 说明 |
|---|---|---|
| 纯文字 / 推理 | `qwen-plus`、`qwen-max`、`qwen3.x` 系列 | 文本与深度推理，成本低 |
| 图片 + 文字 | `qwen-vl-plus`、`qwen-vl-max`、`qwen2.5-vl` | VL 视觉语言模型 |
| 视频 + 文字（或图+视频混合） | `qwen3.7-plus` / `qwen3.8-max`（Qwen3.6 系列）**或** `qwen-vl-max` / `qwen2.5-vl` / `qwen3-vl`（VL 系列） | 视频理解已下放至 Qwen3.x 通用模型，不限于 VL 系列；视频由模型按帧理解，**不支持音轨** |

> **模型选型提示**：视频理解不限于 VL 系列——`qwen3.7-plus` / `qwen3.8-max` 同样支持；仅纯文本小模型（qwen-plus/max）不支持。若业务是「同一会话里文字→图片→视频混合」，统一用支持视频的模型即可覆盖全部三种模态，无需切换模型。

---

## 4. 多模态消息结构（OpenAI 兼容模式）

`content` 为 part 数组，三类 part 如下：

```jsonc
// 文字
{ "type": "text", "text": "描述这段视频讲了什么" }

// 图片（公开 URL 或 base64 data URI）
{ "type": "image_url", "image_url": { "url": "https://.../ui.png" } }

// 视频 · 方式二：直接传视频文件 URL / Base64（扩展字段 type:"video_url"）
{
  "type": "video_url",
  "video_url": { "url": "https://.../1.mp4" },   // 或 "data:video/mp4;base64,...."
  "fps": 2
}
```

完整请求示例（Python，OpenAI SDK）：

```python
from openai import OpenAI
import os

client = OpenAI(
    api_key=os.getenv("DASHSCOPE_API_KEY"),
    base_url="https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
)

resp = client.chat.completions.create(
    model="qwen3.7-plus",  # 支持视频的模型即可（Qwen3.x 或 VL 系列）
    messages=[{
        "role": "user",
        "content": [
            {"type": "video", "video": [f"https://.../f{i}.jpg" for i in range(1, 5)], "fps": 2},
            {"type": "image_url", "image_url": {"url": "https://.../poster.png"}},
            {"type": "text", "text": "结合视频与这张海报，总结核心信息"},
        ],
    }],
)
print(resp.choices[0].message.content)
```

> 备注：在 OpenAI 兼容模式下，视频有两种扩展表达——帧列表用 `type:"video"`（值为图片 URL 数组），文件直传用 `type:"video_url"`（值为视频 URL 或 base64）。二者均为 DashScope 对 OpenAI 协议的扩展（标准 OpenAI 无 video/video_url 类型）。`fps` 含义：每 `1/fps` 秒提取一帧，取值范围 `[0.1, 10]`，默认 2.0。两种都支持，由 `ContentBlock` 的 `kind` 区分。

---

## 5. 在现有架构中的落点

```
apps/desktop (React)
        │  HTTP
        ▼
apps/backend (FastAPI)
        │
        ├── LLM 接入层（新增 QwenChatModel，复用 OpenAI client）
        │      ├── OpenAIBackend（既有）
        │      ├── DeepSeekBackend（既有）
        │      └── QwenBackend  ← 本次新增，base_url 指向 DashScope
        │
        └── LangGraph Runtime
               └── llm_node → 调用当前 ModelBackend（按配置选择）
```

设计要点：
1. **模型后端抽象**：在既有 `ModelBackend` 接口新增 `QwenBackend`，与 OpenAI/DeepSeek 并列；通过配置 `MODEL_PROVIDER=qwen` 切换。
2. **多模态消息归一化**：定义内部 `ContentBlock`（`text` / `image` / `video`）schema，由 `QwenBackend.to_openai_messages()` 转换为上面的 part 数组。这样 LangGraph 节点只处理统一的内部 schema，不被厂商格式绑架（契合 AGENTS.md「工具系统解耦、契约单一事实来源」原则）。
3. **本地文件处理**：Qwen 需要**可公开访问的 URL** 或 **base64 data URI**。
   - 图片：小图可用 `data:image/jpeg;base64,...`；大图/视频建议先上传到对象存储（OSS）换公开 URL，避免巨量 base64 拖慢请求。
   - 视频：强烈建议**先抽帧**再按帧列表传入（见第 7 节），而非传整段视频 URL（帧列表 + `fps` 对理解时序与动态变化更友好，且可控 token 消耗）。
4. **配置项**（建议加入 `.env` / 配置中心）：
   - `DASHSCOPE_API_KEY`
   - `DASHSCOPE_WORKSPACE_ID`（替换 `{WorkspaceId}`）
   - `DASHSCOPE_REGION`（默认 `cn-beijing`）
   - `QWEN_MODEL`（默认 `qwen-vl-max`，支持按模态切换）
   - `QWEN_BASE_URL`（默认 `https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`）

---

## 6. 参考实现（Python，可直接落地）

### 6.1 客户端封装

```python
# apps/backend/app/llm/qwen_backend.py
from __future__ import annotations
import os
from openai import OpenAI
from dataclasses import dataclass, field
from typing import Literal


@dataclass
class ContentBlock:
    kind: Literal["text", "image", "video", "video_url"]
    # text -> text; image -> url; video -> list[str] 帧URL; video_url -> 视频URL/Base64
    data: str | list[str]
    fps: int = 2


class QwenBackend:
    def __init__(self, *, model: str | None = None, workspace_id: str | None = None):
        ws = workspace_id or os.getenv("DASHSCOPE_WORKSPACE_ID", "")
        region = os.getenv("DASHSCOPE_REGION", "cn-beijing")
        base = f"https://{ws}.{region}.maas.aliyuncs.com/compatible-mode/v1"
        self.client = OpenAI(api_key=os.getenv("DASHSCOPE_API_KEY"), base_url=base)
        self.model = model or os.getenv("QWEN_MODEL", "qwen-vl-max")

    def _to_parts(self, blocks: list[ContentBlock]) -> list[dict]:
        parts = []
        for b in blocks:
            if b.kind == "text":
                parts.append({"type": "text", "text": b.data})
            elif b.kind == "image":
                parts.append({"type": "image_url", "image_url": {"url": b.data}})
            elif b.kind == "video":
                parts.append({"type": "video", "video": b.data, "fps": b.fps})
            elif b.kind == "video_url":
                parts.append({"type": "video_url", "video_url": {"url": b.data}, "fps": b.fps})
        return parts

    def chat(self, blocks: list[ContentBlock], *, system: str | None = None) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": self._to_parts(blocks)})
        resp = self.client.chat.completions.create(model=self.model, messages=messages)
        return resp.choices[0].message.content
```

### 6.2 LangGraph 节点调用

```python
# 在既有 llm_node 中按 provider 路由
from app.llm.qwen_backend import QwenBackend, ContentBlock

def llm_node(state):
    backend = QwenBackend()  # 实际应从配置/工厂获取
    blocks = [
        ContentBlock(kind="video", data=state["frame_urls"], fps=2),
        ContentBlock(kind="text", data=state["user_prompt"]),
    ]
    answer = backend.chat(blocks, system="你是专业的视频分析助手。")
    return {"messages": [("ai", answer)]}
```

---

## 7. 风险与约束（实施前必读）

1. **视频模型范围**：视频理解不限于 VL 系列——`qwen3.7-plus` / `qwen3.8-max`（Qwen3.6 系列）同样支持视频；但纯文本小模型（qwen-plus/max）不支持。混合模态统一用支持视频的模型即可。
2. **视频不支持音轨**：视觉理解模型只处理画面帧，**忽略视频中的音频**，不要依赖语音/对白理解。
3. **帧抽策略与 fps**：视频转为帧列表后每帧消耗 token。`fps` 取值范围 `[0.1, 10]`，默认 2.0（即每 0.5s 一帧）；运动快场景调高、长/静态视频调低。长视频建议先做关键帧抽取；超长视频考虑先转写摘要再送模型。`max_frames` 参数**仅 DashScope SDK 支持**，OpenAI 兼容模式需自行控制帧数。
4. **视频大小与本地文件限制（OpenAI 兼容模式）**：
   - 直接传视频文件仅支持 **URL 或 Base64（原始 < 7MB）**；**不支持 `file://` 本地路径**（本地路径仅 DashScope SDK 可用）。
   - 较大视频（7–100MB、>100MB）必须用**公开 URL**（建议放 OSS）；非流式调用超时 180s。
   - 帧列表法受图像数量限制：qwen3.7-plus 系列 URL 图最多 2048 张、其他模型 256 张、Base64 最多 250 张；且总 token 不超过模型上限。
5. **区域与隔离**：北京 region 的 URL 必须带 `{WorkspaceId}`；API Key 按 region 隔离，跨区不通用。
6. **本地媒体**：图片可用 `data:image/jpeg;base64,...` 或公开 URL；大图/视频优先 OSS 临时 URL。
7. **鉴权安全**：`DASHSCOPE_API_KEY` 走环境变量/密钥管理，禁止提交到仓库。
8. **`type:"video"` / `type:"video_url"` 非标准**：二者均为 DashScope 对 OpenAI 兼容模式的扩展。若未来切换到原生 Responses API 或别的 provider，需在 `to_parts` 层做适配，保持内部 `ContentBlock` schema 不变即可隔离影响。

---

## 8. 后续可选（暂不实施）

- **原生 Responses API**：DashScope 也提供 OpenAI-compatible Responses 模式（官方多轮对话文档提及）。若后续要与你之前关注的 `langgraph-responses-gateway` 联动（对外暴露 `/v1/responses`），可统一走 Responses 协议；但多模态 video 在 Responses 模式下的字段需另行核实。
- **模型热切换**：在 `ModelBackend` 工厂里按「是否含视频」自动选 VL 模型，对上层透明。

---

## 9. 待你确认的事项

1. 是否接受「OpenAI 兼容模式 + 支持视频的模型（Qwen3.x 或 VL 系列）统一覆盖三类模态」的路线？
2. 默认模型选 `qwen3.7-plus` / `qwen3.8-max`（通用、性价比高）还是 `qwen-vl-max` / `qwen2.5-vl`（VL 专用，分辨率更强）？
3. 视频输入默认走「抽帧列表」还是「直接传视频文件 URL」？（本方案两种都封装，默认推荐抽帧列表，更可控；短/小视频可直接 `video_url`）
4. 本地媒体是否已有 OSS/对象存储可用？还是需要我补充「本地文件→临时公开 URL」的上传模块？
