# LLM 多厂商适配改造方案（长期迭代导向）

> 状态：**设计文档 + 代码骨架**（未开工实施）。基于第零铁律：绿地项目不兼容旧结构，以「长期稳定迭代」为唯一尺度，接受大规模结构性改造；但绝不重复造轮子——厂商差异全部委托 litellm 消化，本项目只做「配置/能力收口」这一薄层。
>
> **本期交付边界**（2026-08-18 用户决议）：先只做**设计文档 + 代码骨架**——本文件即设计文档；代码骨架清单见 §九。实施在用户审查本文档并明确放行后再启动，按 §七 阶段顺序独立提交、独立审查/测试闭环。
>
> **2026-08-18 修订**（依据官方文档 + litellm 1.97.0 本地源码 + 用户视角审查 + 独立审查/测试闭环）：① 修正厂商前缀表——`zhipu`→`zai`，补 `volcengine`/`tencent`/`minimax`/`xfyun`，文心 `qianfan` 前缀废弃改走 `openai/` 兼容组，初始覆盖 11→15 类；② 阶段 3 重构为 thinking **生命周期**（开启/抽取/回传/展示开关），新增 P0 级「多轮回传 signature 丢失→工具调用 400」风险与 `test_thinking_roundtrip.py` 验收，并定位真实剥离点 `_collect_chunk_to_ai_message`（423-426）须按 capability 区分；③ 阶段 2 并入用户视角三件套（连通性测试端点/http_proxy/凭据存储），凭据存储显式确认为「明文 SQLite，本地单机无加密」；④ 阶段 4 错误码表修正——litellm 1.97.0 **无 `InsufficientQuotaError` 类**，余额不足经 HTTP 429+消息特征识别，超时类名为 `Timeout`，补内容安全拦截与上下文窗口细分；⑤ 阶段 5 并入成本统计；⑥ 新增「八、未来扩展」章节记录用户视角 10 项缺口（本期不实施）；⑦ 新增 §三.X「用户流程与模型选择策略」明确「配置厂商 → 自动发现 → 平铺选择」三步流与「无默认模型、未配置报错」策略。

## 一、目标与背景

让 `apps/backend` 能正确、可维护地接入并调用各大厂商大模型：DeepSeek、智谱（GLM）、字节豆包/Seedance（火山方舟）、MiniMax、通义千问（DashScope）、OpenAI、Anthropic、Gemini 等 15 类厂商。

改造后必须满足：
1. 新增一家厂商 = 改一张静态注册表，**不改任何业务代码**。
2. 流式 / 非流式 / thinking / 工具调用在全部厂商上行为一致。
3. 失败可按稳定错误码分类，前端能给出修复引导。
4. 参数差异（`drop_params`、`thinking` 通道、`api_version`）不散落 if-else。
5. **用户流程为「配置厂商 → 自动发现模型 → 对话时平铺选择」**：用户在设置面板仅配置厂商 + API Key（不手填模型名），后端调用厂商 `GET /models`（或对应端点）发现可用模型并落库；对话窗口的模型选择器把全部已发现且启用的模型按厂商分组**平铺**展示，用户在创建任务时选定模型才允许发送消息。
6. **5 个内置 Agent profile 不内置默认模型**：未配置模型时前端禁止发送消息（前端优先校验）、后端在 runner 入口兜底返回 `MODEL_NOT_CONFIGURED` 错误码（后端兜底）。

## 二、现状事实与缺口（已核实）

### 版本栈（正确，无需动）
- `litellm==1.97.0`（2026-08-16 PyPI 最新稳定，`pyproject.toml` 精确锁定）
- `langchain-litellm==0.7.0`、`langchain-core==1.4.9`、`langgraph==1.2.10`

### litellm 原生前缀实测（本地 `.venv` 1.97.0 源码 + 各厂商官方文档核实，2026-08-18）

> 本节是「注册表怎么写」的唯一事实依据。前缀与默认端点均以 litellm 源码与厂商官方文档为准，任何一处与本节不符的注册表条目都是错的。

| 厂商 | litellm 前缀 | 默认端点（litellm/官方） | 方案要点 |
|---|---|---|---|
| DeepSeek | `deepseek/` | `api.deepseek.com` | 原生 |
| Anthropic | `anthropic/` | — | 原生 |
| Gemini | `gemini/` | — | 原生 |
| Azure OpenAI | `azure/` | — | 原生，须 `api_version` + deployment 形 base_url |
| 通义 Qwen | `dashscope/` | litellm 默认 **`dashscope-intl.aliyuncs.com/compatible-mode/v1`**（国际区，`litellm/constants.py` 实锤）；国内官方为 `dashscope.aliyuncs.com/compatible-mode/v1` | 原生，**默认走国际区，国内需 `regions` 切 base_url** |
| Kimi | `moonshot/` | `api.moonshot.ai/v1`（**国内 `api.moonshot.cn`**） | 原生，端点分区域 |
| **智谱 GLM** | **`zai/`（不是 `zhipu/`）** | `api.z.ai/api/paas/v4` | **原方案写 `zhipu` 是错的** |
| **火山方舟/豆包** | **`volcengine/`** | `ark.cn-beijing.volces.com/api/v3` | **原方案 11 类遗漏** |
| **腾讯混元** | **`tencent/`** | `tokenhub-intl.tencentcloudmaas.com/v1` | **原方案 11 类遗漏** |
| **MiniMax** | **`minimax/`** | `api.minimax.io`（国内 `api.minimaxi.com`） | **原方案 11 类遗漏** |
| 百度文心 | 无前缀（`qianfan` 已废弃） | `https://qianfan.baidubce.com/v2`（Bearer Key） | 走 `openai/` + base_url |
| 讯飞星火 | 无前缀 | `spark-api-open.xf-yun.com/v1` | 走 `openai/` + base_url（原方案遗漏） |
| Ollama | `ollama/` | — | 原生，无 key |

litellm 枚举实测（`types/utils.py` `LlmProviders`）：`deepseek/anthropic/gemini/azure/dashscope/moonshot/zai/volcengine/tencent/minimax/ollama` 均原生；`llms/` 目录确认 `zai/`（智谱）、`minimax/`、`tencent/`、`volcengine/` 实现存在；**无 `qianfan`、无 `xfyun` 前缀**（故文心/讯飞归入 `openai/` 兼容组）。

### thinking / reasoning 通道与回传约束实测（阶段 3 依据）

| 厂商 | 请求侧开启 | 响应侧字段 | **多轮回传（round-trip）硬约束** |
|---|---|---|---|
| DeepSeek | `thinking: {type, reasoning_effort}` | `reasoning_content` | 未要求回传 |
| Anthropic | `thinking: {type: enabled, budget_tokens}` | `thinking` 块 + **`signature`** | **signature 必须原样回传，否则 400** |
| Gemini | `thinkingConfig: {includeThoughts, thinkingLevel, thinkingBudget}` | `thought` parts + **`signature`** | **无状态模式必须原样回传 thought（含 tool call 后）** |
| OpenAI o 系列 | `reasoning_effort` | `reasoning`（`encrypted_content` + summary） | **`encrypted_content` 必须原样回传** |
| Kimi K2 | — | `reasoning_content` | 每轮 tool-call 消息须携带（litellm 已内置 `fill_reasoning_content` 兜底） |

**结论**：阶段 3 不能只做「抽取展示」。对 Anthropic/Gemini/OpenAI o 系列，若历史 assistant 消息里的 thinking 块（含签名）未原样透传回下一轮请求，**工具循环直接 400**——这是 agent 工具调用的生死线。好消息：`langchain_litellm 0.7.0` 源码确认 `_convert_dict_to_message` 把 `reasoning_content` 存入 `additional_kwargs` 并注入 thinking 块，`_convert_message_to_dict` 转发 `reasoning_content` 让 litellm 为 Anthropic 注入 thinking blocks——**回传链路大概率已内置，阶段 3 的重心是「验证不丢」而非「自研回传」**。

### 关键代码事实

| 位置 | 现状 |
|---|---|
| `app/core/llm/factory.py` `build_chat_model`（41-107） | model_name 前缀原样透传 litellm；api_key/api_base/temperature/top_p/max_tokens/model_kwargs 透传；`request_timeout=_DEFAULT_REQUEST_TIMEOUT_SECONDS`（120.0 硬编码，38 行）。**未传 max_retries / drop_params / api_version** |
| `factory.py` `resolve_chat_model`（110-162） | resolver.resolve → LLMRuntimeConfig → ModelSettings → build_chat_model |
| `app/models/llm_runtime_config.py` `from_model_entry`（110-146） | `thinking=True if model_entry.supports_thinking else None`（关键赋值 137-146），**无 provider 维度控制** |
| `app/api/schemas/request/ProviderCreateRequest.py:4` | `PROVIDER_TYPES = ("deepseek", "openai-compatible", "anthropic", "ollama", "custom")`；`_type_must_be_known`（61-79） |
| `app/service/provider/provider_discover_service.py:27-32` | `PROVIDER_TYPE_PREFIXES` 仅 4 类（deepseek/openai/anthropic/ollama） |
| `app/service/provider/provider_service.py:27` | `_KEYLESS_PROVIDER_TYPES = frozenset({"ollama"})`，硬编码 |
| `apps/shared/ts/model.ts`（26-31） | 前端 `ProviderType` 与后端 5 类型手写对齐（非脚本生成） |
| `app/storage/model/provider_model.py`（providers 表） | provider_id/name/type/base_url/api_key/enabled/sort_order/时间戳。**无 api_version 列** |
| `app/storage/model/model_entry_model.py`（models 表） | model_id/provider_id/model_name/display_name/max_context_window/supports_thinking/temperature/top_p/max_tokens/enabled/sort_order/时间戳 |
| `app/core/workflows/nodes/model_node.py` `_extract_reasoning_content`（225-249） | **只读 `additional_kwargs["reasoning_content"]`**（DeepSeek 专属），Anthropic `thinking_blocks` / Gemini `thought` 无通路 |
| `model_node.py` astream（597） | `model.astream(messages)`，流式参数归一由 ChatLiteLLM 自动 `stream_options={"include_usage": True}` |
| `app/core/workflows/react/workflow.py`（174-186） | `bind_tools(strict=True)` + `NotImplementedError` 降级为无工具运行；ChatLiteLLM 对 Claude+thinking 自动降 tool_choice=auto |
| `app/core/runtime/runner.py`（398-441） | `except Exception` 统一 `RunFailedPayload(status="failed", error=str(exc), end_reason=None)`，**无错误码归一** |
| `app/models/enums/error_kind.py` | 仅 6 类（PARSE_INVALID/SCHEMA_INVALID/UNKNOWN_TOOL/RUNTIME_FAILED/PERMISSION_DENIED/CANCELLED），**无模型错误细分** |
| `app/models/payload/run_failed_payload.py` | 含 `end_reason`（语义化枚举码，目前仅 client_disconnected），**无 error_code 字段** |
| `app/storage/init_schema.py` | **「加列不删列」机制**（`_ensure_model_columns` 自动 `ALTER TABLE ADD COLUMN` 补齐存量库 + `_ensure_model_indexes` 补索引）——新增列无需手工迁移 |

### 主要缺口（按影响排序）
1. **厂商枚举过窄且前缀有错**（P0）：后端 5 类、前缀表 4 类，Azure/Gemini/通义/月之暗面/智谱无原生入口，只能 `custom` 手填；**且原方案 `zhipu` 前缀不存在（litellm 为 `zai/`）**，照写会导致模型名不识别。
2. **厂商覆盖遗漏**（P0）：火山方舟（豆包）、腾讯混元、MiniMax、讯飞星火无注册表条目；百度文心 `qianfan` 前缀已废弃需走 `openai/`。
3. **参数裁剪缺失**（P0）：未传 `drop_params`，非严格厂商遇 `thinking` 等不支持参数直接 400。
4. **thinking 只做抽取、未做回传**（P1，agent 工具循环生死线）：仅读 `reasoning_content` 单一通道；且 **`model_node._collect_chunk_to_ai_message`（423-426）对所有模型无条件剥离 `reasoning_content`**，Anthropic/Gemini/OpenAI o 系列工具调用将 400——剥离逻辑必须按 capability 区分（见阶段 3.3）。
5. **无错误码归一**（P1）：`error=str(exc)` 自由文本，前端无法分类引导；且漏了国内厂商高频的余额不足（litellm 1.97.0 **无 `InsufficientQuotaError` 类**，余额不足经 HTTP 429 + 消息特征识别）与内容安全拦截。
6. **Azure 缺 api_version**（P1）：providers 表无列、factory 不透传。
7. **窗口表过窄**（P2）：`ModelCatalog` 仅 deepseek 两条，其余 128k 兜底；litellm 内置价格/窗口表未复用。
8. **Key 明文存储、无连通性测试、无代理配置入口**（P1，用户视角）：桌面端配完 key 无法验证有效性，国内访问外网厂商无代理通道。
9. **无成本统计**（P2）：litellm 内置 `model_cost` 价格表现成能力未用，每次调用成本不可见。

## 三、目标架构

### 用户流程与模型选择策略（用户视角主线）

> 本节是后续所有改造的「用户视角主线」——一切抽象与代码骨架都为这条主线服务。

**三步流**：

```
[Step 1 设置面板]        [Step 2 后端发现]              [Step 3 对话窗口]
配置厂商 + API Key  →  调用厂商 GET /models  →  模型选择器按厂商分组平铺
（不手填模型名）        落库 models 表                  选中模型才允许发送消息
```

**Step 1 配置厂商（Provider）**：
- 用户在设置面板「模型厂商」分区新建厂商，从下拉选择 `provider_type`（15 类，由注册表派生）。
- 仅填两类字段：**API Key**（必填，除 ollama 外）与 **base_url 覆盖**（可选，仅当用户想切区域或自部署时填，未填走 capability 默认端点）。
- Azure 额外填 `api_version`；其余厂商不出现该字段（表单按 capability 动态渲染字段）。
- **不手填模型名**——模型名全部由 Step 2 自动发现填充。
- 凭据存储：**明文 SQLite（providers 表 `api_key` 列），本地单机应用不做加密**（2026-08-18 用户决议，反转早期「加密存储」倾向）。约束升级为「Key 明文不进入日志/事件/API 响应/SSE payload」——序列化刻意不输出（`provider_record.py` 现状已具备，本期保留并加测试断言）。

**Step 2 自动发现模型（Model Discovery）**：
- 用户在厂商列表点「发现模型」按钮 → 后端调 `POST /providers/{id}/discover` → `provider_discover_service` 按 `capability.litellm_prefix` 拼模型名前缀，调厂商 `GET /models`（OpenAI 兼容组）/厂商专属发现端点（详见 §三.X 发现端点表）→ 返回模型清单。
- 发现结果**全量替换**该厂商下 `enabled=True` 的模型条目（按 `model_name` 幂等 upsert，已存在的 `enabled` 状态保留；新增模型默认 `enabled=True`；已存在但本次未返回的模型标 `enabled=False` 而非物理删除，避免误删用户的 max_tokens 调优）。
- 每个模型落库时携带 `provider_id`、`model_name`（含 litellm 前缀，如 `deepseek/deepseek-v4-flash`）、`display_name`、`max_context_window`（capability 静态表优先；缺则回退 litellm `get_model_info`；再缺 128k 兜底）、`supports_thinking`（capability 静态声明）。
- **失败可重试**：发现失败返回结构化错误（走 §阶段 4 错误码），不阻塞其它厂商。

**Step 3 对话时平铺选择**：
- 创建任务页（`NewTaskPage`）的模型选择器调 `GET /models?enabled=true` → 后端按 `provider.sort_order, model.sort_order` 排序返回扁平 list（**不嵌套厂商层级**，但每条携带 `provider_id`/`provider_name` 供前端 optgroup 分组展示）。
- 前端 `<select>` 用 `<optgroup label="{provider_name}">` 分组平铺，用户一眼看完全部可用模型。
- **无默认选中**：5 个内置 Agent profile（developer/reviewer/analyst/tester/coder）**不内置 `model_name`**——`AgentProfile.model_name` 改为 `str | None`，默认 `None`。
- **前端优先校验**：用户未选模型时，「发送消息」按钮 disabled，tooltip 提示「请先选择模型」。前端 store 持久化上次选择（localStorage），下次进入同 workspace 自动回填但**仍允许发送前修改**。
- **后端兜底**：runner 入口（`turn_prepare_service` 或 `runner` 首部分支）检查 `agent.model_name is None`，命中即 raise `ModelNotConfiguredError(error_code="MODEL_NOT_CONFIGURED")` → SSE 推 `RunFailedPayload(error_code="MODEL_NOT_CONFIGURED", guidance="请先在设置中为该 Agent 选择模型")`，turn 标 `failed`。

**数据契约**：
- `providers` 表：`provider_id`（PK，UUID）、`name`、`provider_type`、`base_url`、`api_key`（明文）、`api_version`（Azure 用）、`enabled`、`sort_order`、时间戳。
- `models` 表：`model_id`（PK，UUID）、`provider_id`（FK）、`model_name`（含 litellm 前缀，唯一索引 `(provider_id, model_name)`）、`display_name`、`max_context_window`、`supports_thinking`、`temperature`/`top_p`/`max_tokens`（用户可调覆盖）、`enabled`、`sort_order`、时间戳。
- `agent_profiles` 表（既有）：`model_name` 改 `str | None`，移除 NOT NULL 约束（init_schema 加列不删列机制自动补齐存量库）。

### 厂商模型发现端点实测（litellm 1.97.0 源码 + 厂商官方文档，2026-08-18）

> 本表是 `provider_discover_service` 实现「Step 2 自动发现」的唯一事实依据。

| provider_type | 发现端点 | 协议形态 | 备注 |
|---|---|---|---|
| `deepseek` | `GET https://api.deepseek.com/models` | OpenAI 兼容（`{object:"list", data:[{id, object, owned_by}]}`） | 官方文档核实，返回 `deepseek-v4-flash`/`deepseek-v4-pro` |
| `openai-compatible`（含 OpenAI 本身、文心 `qianfan`、讯飞 `xfyun`） | `GET {base_url}/models` | OpenAI 兼容 | base_url 由用户填或 capability 默认 |
| `anthropic` | `GET https://api.anthropic.com/v1/models`（Bearer Key） | Anthropic 原生（`{data:[{id, type, display_name}]}`） | 需 `anthropic-version: 2023-06-01` header |
| `gemini` | `GET https://generativelanguage.googleapis.com/v1beta/models?key={api_key}` | Gemini 原生（`{models:[{name, supportedGenerationMethods}]}`） | 过滤 `supportedGenerationMethods` 含 `generateContent` 的条目 |
| `azure` | **厂商未提供 list 端点**——只能由用户手填 deployment 名 | 不自动发现 | capability 标 `requires_manual_model_entry=True`，前端在该厂商下显示「请手填 deployment 名」提示并跳过 Step 2 |
| `dashscope` | `GET https://dashscope.aliyuncs.com/compatible-mode/v1/models`（Bearer Key） | OpenAI 兼容 | 默认国际区端点，国内区切 base_url |
| `moonshot` | `GET https://api.moonshot.cn/v1/models` | OpenAI 兼容 | 国内端点 |
| `zai` | `GET https://api.z.ai/api/pass/v4/models` | OpenAI 兼容 | 智谱 |
| `volcengine` | `GET https://ark.cn-beijing.volces.com/api/v3/models`（Bearer Key） | OpenAI 兼容 | 火山方舟/豆包/Seedance |
| `tencent` | `GET https://tokenhub-intl.tencentcloudmaas.com/v1/models` | OpenAI 兼容 | 腾讯混元 |
| `minimax` | `GET https://api.minimaxi.com/v1/models`（Bearer Key） | OpenAI 兼容 | 国内端点 |
| `ollama` | `GET {ollama_host}/api/tags` | Ollama 原生（`{models:[{name, model}]}`） | base_url 必填，无 key |

### 核心抽象：ProviderCapability 静态注册表（单一事实源）

新增 `app/models/provider_capability.py`（**放 models/ leaf 层**，api/core/service/tools 均可依赖，不违反分层 DAG；一文件一职责，文件名=类名）：

```python
@dataclass(frozen=True)
class ProviderCapability:
    provider_type: str                 # "deepseek" / "azure" / ...，与 litellm 官方术语对齐
    litellm_prefix: str | None         # "deepseek/"、"openai/"…；None=custom 不按前缀过滤
    default_base_url: str | None       # azure/gemini/dashscope 等官方端点模板
    requires_api_key: bool             # ollama=False
    requires_api_version: bool         # azure=True
    requires_manual_model_entry: bool  # azure=True（厂商未提供 list 端点，需手填 deployment 名）
    discover_endpoint: str | None      # 自动发现端点模板（None=不可自动发现，走 manual_model_entry）
    default_drop_params: bool          # 非严格兼容厂商 True（litellm 丢弃不支持参数）
    thinking_channels: tuple[str, ...] # ("reasoning_content",) / ("thinking_blocks",) / ("thought",) / ()
    supports_thinking: bool
    default_timeout_seconds: float
    default_max_retries: int
    regions: tuple[str, ...] = ()      # ("cn", "intl") 等；国内/国际双端点厂商（moonshot/minimax/zai/dashscope）标注，供 UI 提示与端点切换
    parameter_constraints: dict[str, Any] = field(default_factory=dict)
        # 参数形态元数据（键值对形态，实现时以 dataclass 字段 + 默认 dict 为准）：
        #   "temperature_range": (0.0, 1.0)   —— UI 参数范围提示
        #   "max_tokens_field": "max_tokens" 或 "max_completion_tokens"
        #   "thinking_request": "reasoning_effort" | "budget_tokens" | "thinkingConfig" 等
        # 供 UI 参数范围提示（见「未来扩展」）；未定义键读不到时 UI 不提示，不得抛错
```

- 注册表 `PROVIDER_CAPABILITIES: dict[str, ProviderCapability]` + `get_capability(provider_type) -> ProviderCapability`（未知类型回退 `custom` 语义）。
- 初始覆盖（15 类，前缀与端点以上文「litellm 原生前缀实测」表为准）：
  - 原生前缀：`deepseek`、`openai-compatible`、`anthropic`、`gemini`、`azure`、`dashscope`（通义）、`moonshot`（Kimi）、`zai`（智谱，**注意不是 zhipu**）、`volcengine`（火山方舟/豆包）、`tencent`（腾讯混元）、`minimax`、`ollama`。
  - `openai/` 兼容组（无原生前缀，base_url 指向官方 OpenAI 兼容端点）：`qianfan`（文心：`https://qianfan.baidubce.com/v2`）、`xfyun`（讯飞：`spark-api-open.xf-yun.com/v1`）。
  - `custom`（任意 base_url + 任意前缀）。

### 分层落位

- `api/ProviderCreateRequest.PROVIDER_TYPES` ← 由注册表键派生（校验器 `_type_must_be_known` 读注册表）。
- `service/provider_discover_service.PROVIDER_TYPE_PREFIXES` ← 删，改读 `get_capability().litellm_prefix`。
- `service/provider_service._KEYLESS_PROVIDER_TYPES` ← 删，改读 `get_capability().requires_api_key`。
- `core/llm/factory.build_chat_model` ← 读 capability 决定 max_retries/drop_params/api_version/thinking 注入。
- `core/workflows/nodes/model_node` ← 经 `LLMRuntimeConfig` 携带 `thinking_channels`，`_extract_reasoning_content` 按通道分派。
- `core/runtime/runner` ← 经 litellm 异常映射器（`core/llm/model_error_mapper.py`）归一为稳定错误码。

## 四、分阶段改造

### 阶段 1：ProviderCapability 注册表 + 枚举派生（P0，核心底座）

**新增文件**
- `app/models/provider_capability.py`：上述 dataclass + 静态注册表 + `get_capability()` + docstring（含「不负责」边界：不做厂商连通性验证，验证在 discover/连接时进行）。
- `app/models/enums/__init__` 或直接导出 `PROVIDER_TYPES`（由注册表键推导，保持单一来源）。

**修改文件**
- `app/api/schemas/request/ProviderCreateRequest.py`：`PROVIDER_TYPES` 改为从注册表键派生；`_type_must_be_known` 校验逻辑不变（读注册表而非字面元组）。
- `app/service/provider/provider_discover_service.py`：删 `PROVIDER_TYPE_PREFIXES`，`discover_models` 内改 `get_capability(provider_type).litellm_prefix` 过滤。
- `app/service/provider/provider_service.py`：删 `_KEYLESS_PROVIDER_TYPES`，`api_key_configured` 改 `get_capability(provider.provider_type).requires_api_key`。
- `apps/shared/ts/model.ts`：`ProviderType` 扩展为 15 类（与注册表键一致，手写同步 + vitest 契约测试断言）。
- `apps/desktop/src/components/settings/ProviderFormDialog.tsx`：厂商类型下拉扩展；azure 显示 api_version 输入框、`qianfan`/`xfyun` 显示 base_url 提示、`moonshot`/`minimax`/`zai` 显示国内/国际端点提示。

**验收**
- 后端单测：`test_provider_capability.py`——注册表完整性（每个类型都有 capability）、`get_capability` 未知类型回退、`PROVIDER_TYPES` 与注册表键一致。
- API 单测：新增类型（azure/gemini/…）可创建 provider 且校验通过。
- 前端 vitest：ProviderFormDialog 新类型渲染、`shared/ts/model.ts` 契约测试。

### 阶段 1.5：Agent profile 无默认模型策略（P0，用户视角主线）

> 用户 2026-08-18 决议：5 个内置 Agent profile **不内置默认模型**；未配置时前端优先校验、后端兜底报错。本阶段是「配置厂商 → 自动发现 → 平铺选择」主线在 Agent 侧的对应改造，必须与阶段 2 数据层改造同批落地。

**修改文件**
- `app/core/agents/agent_profile.py`：`AgentProfile.model_name` 类型 `str` → `str | None`，默认 `None`；docstring 标注「None 表示未配置，由前端优先校验、后端兜底报错」。
- `app/core/agents/define_agents.py`：5 个 profile（`developer` / `delegate_reviewer` / `delegate_analyst` / `delegate_tester` / `delegate_coder`）**移除 `model_name="deepseek/deepseek-v4-flash"` 硬编码**，改为不传（默认 `None`）；docstring 同步移除「默认模型 deepseek」表述。
- `app/models/enums/error_kind.py`：新增枚举 `MODEL_NOT_CONFIGURED`（与阶段 4 的模型错误细分同批引入，但本期仅此一项先落，其余细分见阶段 4）。
- `app/models/payload/run_failed_payload.py`：新增可选字段 `error_code: str | None = None`（与阶段 4 同字段，本期先引入空字段）。
- `app/core/llm/model_error_mapper.py`（**新增文件骨架**，本期仅含 `ModelNotConfiguredError` 异常类 + `map_model_not_configured()` 函数，完整 mapper 见阶段 4）：定义 `class ModelNotConfiguredError(RuntimeError)` 携带 `error_code="MODEL_NOT_CONFIGURED"` + 中文 guidance。
- `app/service/task/turn_prepare_service.py`（或 `core/runtime/runner.py` 入口前置分支，由实现时确认）：turn 启动前置检查——`if agent.model_name is None: raise ModelNotConfiguredError(...)`；异常经 runner 异常分支（398-441 区域）写入 `RunFailedPayload(error_code="MODEL_NOT_CONFIGURED", guidance=...)`，turn 标 `failed`，SSE 推送给前端。
- `app/api/schemas/response/AgentProfileResponse.py`（或对应响应模型）：`model_name` 字段类型改为 `str | None`，前端能区分「未配置」与「已配置」。
- `apps/shared/ts/agents.ts`：`AgentProfile.model_name: string | null`，前端类型同步。
- `apps/desktop/src/services/api.ts` + `apps/desktop/src/stores/`：API 类型重新生成后，store 中 `agent.model_name` 可空。
- `apps/desktop/src/pages/chat/NewTaskPage.tsx`（或当前任务创建页）：模型选择器**必填**——`agent.model_name is null` 时「发送消息」按钮 disabled + tooltip「请先选择模型」；选模型后写入 task 创建请求体。**localStorage 持久化**上次选择（key 含 workspace_id 维度，避免跨工作区污染），下次进入同 workspace 自动回填，但发送前允许修改。
- `apps/desktop/src/components/layout/InputBar.tsx`（或实际发送入口组件）：发送按钮的 disabled 条件加入 `!selectedModelId`。

**验收**
- 后端单测：`test_agent_profile_no_default_model.py`——5 个 profile `model_name is None`；`AgentProfile` 序列化/反序列化 round-trip `None` 不抛。
- 后端单测：`test_model_not_configured_error.py`——`agent.model_name=None` 时 `turn_prepare_service`（或 runner 入口）raise `ModelNotConfiguredError`；payload `error_code="MODEL_NOT_CONFIGURED"`；turn 标 `failed`。
- 前端 vitest：`NewTaskPage` 在 `agent.model_name=null` 时发送按钮 disabled；选模型后 enabled；localStorage 持久化/回填断言。
- 前端 vitest：`shared/ts/agents.ts` 类型契约——`model_name` 必须是 `string | null`。

### 阶段 2：数据层 + factory 参数透传（P0）

**修改文件**
- `app/storage/model/provider_model.py`：新增列 `api_version: Mapped[str | None]`（Text nullable）。init_schema「加列不删列」机制自动补齐存量库，无需手工迁移。
- `app/models/provider_record.py`：同步 `api_version` 字段 + docstring。
- `app/storage/crud/provider_crud.py` + `app/service/provider/provider_service.py create_provider`：透传/读取 `api_version`。
- `app/api/schemas/request/ProviderCreateRequest.py` + response：`api_version: str | None`。
- `app/core/llm/factory.py` `build_chat_model`：
  - `max_retries=capability.default_max_retries`（透传给 ChatLiteLLM，覆盖其默认 1）。
  - `drop_params=capability.default_drop_params`（非严格厂商 True）。
  - azure：`api_version` 从 config 取（透传 litellm `api_version`）。
  - thinking 注入仅在 `capability.supports_thinking` 且 config.thinking 时进行。
- `app/core/llm/model_settings.py`：`ModelSettings` 增加 `max_retries`/`drop_params`/`timeout_seconds` 可选字段（None 时回退 capability 默认），保持零配置可跑。
- `app/models/llm_runtime_config.py` `from_model_entry`：携带 `provider_type` + `thinking_channels`（供 model_node 分派）与 `api_version`。

**用户视角三件套（本阶段一并落地，属基础体验而非锦上添花）**
- **凭据存储（明文 SQLite，本地单机无加密）**：providers 表 `api_key` 明文存储。**2026-08-18 用户决议**——本地桌面应用、单机场景、无多用户共享、无远程服务暴露面，加密存储属过度工程，**不做加密**。**安全约束升级**（保留 `provider_record.py:7-8` 现有约定并扩展为）：「Key 明文仅允许在 providers 表 → `ProviderRecord` 值对象 → `LLMRuntimeConfig` → litellm 调用 之间流动，**禁止**进入日志 / 运行时事件 / SSE payload / API 响应 / Langfuse trace；序列化（pydantic `model_dump` / ORM `to_dict` / 事件 payload）刻意不输出 `api_key` 字段」。落地时**必须**：① 在 CHANGELOG 显式声明该决策（2026-08-18 用户决议，明确不做加密）；② `provider_record.py`、`provider_service.py`、`provider_crud.py`、`provider_model.py` 四处保留/扩展「明文存储、序列化不输出」docstring；③ 新增 `test_provider_record_no_leak.py` 断言——`ProviderRecord.model_dump()` 输出不含 `api_key` 键、序列化 JSON 字符串不含明文 key 字面量；④ SSE payload / API response schema 由 pre-commit `generate-runtime-event-ts.py` / `generate_api_ts.py` 校验自动不含 `api_key` 字段（pydantic 不声明字段即不输出）。**不做**：keychain/DPAPI 集成、加密迁移、桌面端 secret 引用、密钥轮换——均排除在范围外。
- **连通性测试端点**：新增 `POST /providers/{id}/test`——用已配置参数（base_url/api_key/api_version）发起一次最小 chat 请求（如 `{"messages":[{"role":"user","content":"ping"}]}`，`max_tokens=1`），返回成功/失败 + 可读错误（走阶段 4 错误码）。前端 ProviderFormDialog 加「测试连接」按钮。**复用 litellm proxy 既定模式**（官方 Quickstart 的「Test Connect」按钮即此能力）。
- **http_proxy 配置**：`Settings` 增加 `WEB_PROXY_*`（可选），透传 litellm `api_base` 请求的 http_client proxy；国内访问 OpenAI/Anthropic 必需。

**验收**
- 单测：`test_llm_factory_litellm.py` 扩展——mock ChatLiteLLM 断言各厂商构造参数（max_retries/drop_params/api_version/thinking 有无）。
- 单测：`from_model_entry` 按 provider_type 输出正确 `thinking_channels`。
- 单测：`test_provider_test_connection.py`——连通性端点：成功/认证失败/网络错误映射。
- 单测：`test_provider_record_no_leak.py`——`ProviderRecord.model_dump()` 不含 `api_key` 键；序列化 JSON 字符串不含明文 key 字面量；`provider_crud` 查询返回的对象经 SSE payload 序列化后不含 key。
- 单测：`test_provider_discover_service.py`——mock 厂商 `GET /models` 响应，断言 upsert 幂等、新增模型默认 enabled、未返回模型标 enabled=False、azure 跳过自动发现返回 manual_entry 提示。
- 手动冒烟：注册 azure 厂商（填 api_version）→ 跳过发现、手填 deployment 名 → 选模型 → 发送请求；配错 key 时「测试连接」给出可读错误。

### 阶段 3：thinking 生命周期（P1，agent 工具循环生死线）

> 原方案只做「抽取展示」，本节重构为完整生命周期：**开启（request）→ 抽取（response）→ 回传（round-trip）→ 展示开关（UI）**。其中「回传」是 Anthropic/Gemini/OpenAI o 系列工具循环的硬约束（缺 signature 回传 → 400），优先级最高。

**3.1 开启（request 侧，按厂商注入）**
- `LLMRuntimeConfig` 增加 `thinking_request: dict | None`（由 capability 的 `thinking_request` 元数据 + 用户「展示开关」选择共同决定）：
  - DeepSeek：`thinking={"type": "enabled", "reasoning_effort": "low"|"high"|"max"}`（新接口形态，旧接口走 `reasoning_effort` 字段，实现时以官方文档为准）。
  - Anthropic：`thinking={"type": "enabled", "budget_tokens": <预算>}`。
  - Gemini：`thinkingConfig={"includeThoughts": True, "thinkingLevel": ..., "thinkingBudget": ...}`。
  - OpenAI o 系列：`reasoning_effort`。
  - Kimi/其它无参数：不注入（响应侧抽取即可）。
- 注入位置：`factory.build_chat_model` 已按 capability 透传；若需请求级动态注入则走 `model_kwargs`。

**3.2 抽取（response 侧，多通道分派）**
- `app/core/workflows/nodes/model_node.py`：`_extract_reasoning_content` 改名/扩展为按 `LLMRuntimeConfig.thinking_channels` 分派：
  - `reasoning_content` → `additional_kwargs["reasoning_content"]`（现状通路，DeepSeek/Kimi/OpenAI 兼容）。
  - `thinking_blocks` → Anthropic：**⚠️ 0.7.0 全包实测 `thinking_blocks` 字段 0 匹配**——唯一已证实的 thinking 通道是 `reasoning_content`（注入块字段名 `{"type":"thinking"}`），Anthropic 抽取大概率复用该通道；`thinking_blocks` 仅作备选，实现时以本地 `.venv` 源码核实为准。
  - `thought` → Gemini：遍历 content 块中 `thought=True` 的块文本。
  - `reasoning` → OpenAI o 系列：`message.reasoning`（`encrypted_content` 不展示，仅摘要）。**⚠️ langchain_litellm 0.7.0 实测无此字段归一通路**（`_convert_dict_to_message` 的 assistant 分支仅处理 `function_call`/`tool_calls`/`reasoning_content`/`provider_specific_fields`，注入的 thinking 块字段名为 `{"type":"thinking"}`，无 `reasoning` 字段处理）——开工先核实 litellm 侧处理；无通路则 o 系列降级为仅摘要展示（见风险清单）。
  - 空通道 → 跳过。
- 事件/前端无需改：`MODEL_THINKING_DELTA` 与 `output` 已有独立通路。

**3.3 回传（round-trip，最高优先级）**
- **目标**：历史 assistant 消息中的 thinking 块（含 Anthropic `signature`、Gemini `thought`+`signature`、OpenAI `encrypted_content`）在下一轮请求中原样透传，不丢、不改。
- **现状已内置**（`langchain_litellm 0.7.0` 源码确认）：`_convert_dict_to_message` 把 `reasoning_content` 存入 `additional_kwargs` 并注入 thinking 块；`_convert_message_to_dict` 转发 `reasoning_content` 让 litellm 为 Anthropic 注入 thinking blocks。
- **当前真实剥离点（已定位，必须先修）**：`model_node.py:423-426` `_collect_chunk_to_ai_message` 对所有模型**无条件** `additional.pop("reasoning_content", None)`（注释为「不应随消息回灌给模型」）。该剥离对 DeepSeek 正确（避免重复思考），但**对 Anthropic（`thinking_blocks`+`signature`）、Gemini（`thought`）、OpenAI o 系列（`reasoning`）会同时剥掉签名/思考块，直接导致下一轮工具调用 400**。剥离点已实锤定位（425-426 行无条件 pop），回传丢失的根因大概率在此；最终以阶段 3.3 链路验证单测闭环确认。
- **本阶段要做的**：
  1. **剥离逻辑按 capability 区分**（P0 核心改动）：`_collect_chunk_to_ai_message` 的 `additional.pop("reasoning_content")` 改为按 `LLMRuntimeConfig.thinking_channels`/provider 判定——`reasoning_content` 通道的厂商（DeepSeek/Kimi/OpenAI 兼容）保持剥离（避免重复思考）；`thinking_blocks`（Anthropic）/`thought`（Gemini）/`reasoning`（OpenAI o 系列）厂商**保留**该字段并经 `RuntimeMessage` 落库，供下一轮经 `_convert_message_to_dict` 原样透传（含 signature/thought）。此改动同时消除 `_extract_reasoning_content`（225-249）与 `_collect_chunk_to_ai_message`（423-426）之间「抽取后又被剥离」的矛盾。
  2. **链路验证**：写验证性单测/冒烟，确认历史消息重建（多轮工具循环）后 Anthropic/Gemini 请求不 400；`_convert_message_to_dict` 侧（`langchain_litellm`）不手改，仅验证其内置转发正确。
  3. **兜底开关**：`LLMRuntimeConfig` 提供 `thinking_roundtrip: bool`（默认 True），供问题排查时临时关闭（宁可丢 thinking 也不 400）。
- **不做**：自研签名机制或自研回传序列化——litellm 已归一，只验证不重造。

**3.4 展示开关（UI）+ 数据流**
- `LLMRuntimeConfig`/前端设置新增三态：`full`（完整思考）/ `summary`（仅摘要：Anthropic 默认、OpenAI o 系列默认）/ `off`（关闭，此时 response 侧不注入开启参数）。
- Gemini 需 `includeThoughts=True` 才有 thought 可展示；Anthropic `budget_tokens` 大小影响可展示的完整度——开关选择映射到 3.1 的注入参数。
- **数据流（明确持久化与注入路径，避免规格空洞）**：展示开关持久化在 **Agent 级 `ModelSettings`**（`app/core/llm/model_settings.py` 新增 `thinking_display: str = "summary"`，随 AgentProfile 存 DB）；`from_model_entry(provider, model_entry)` 扩展签名接收 `thinking_display`（或经 `ModelSettings` 读取），写入 `LLMRuntimeConfig.thinking_display`，再由 `factory.build_chat_model` 依此 + capability 元数据决定 3.1 的注入参数。改动面：`model_settings.py`（加字段）+ `llm_runtime_config.py` `from_model_entry`（加参）+ `factory.py`（读 display 决定注入）。

**修改文件**
- `app/core/workflows/nodes/model_node.py`：多通道抽取（3.2）+ **`_collect_chunk_to_ai_message`（423-426）剥离逻辑按 capability 区分（3.3，P0）**。
- `app/models/llm_runtime_config.py`：加 `thinking_request`、`thinking_roundtrip`、`thinking_display` 字段；**`from_model_entry`（110-146）签名扩展以接收用户展示开关设置（见 3.4 数据流）**。
- `app/core/llm/factory.py`：按 capability `thinking_request` 元数据 + config 注入开启参数。
- `app/core/context/runtime_message_store.py`：验证 thinking 块经 `RuntimeMessage` 落库/重建不丢（3.3 链路验证；仅验证，不手改 `_convert_message_to_dict`）。
- 前端 `ThinkingBlock`/设置面板：展示开关（3.4）。

**验收**
- 单测：`test_model_node_thinking.py`——构造四通道（reasoning_content/thinking_blocks/thought/reasoning）chunk 夹具，断言抽取/合并正确、空通道跳过。
- 单测：`test_thinking_roundtrip.py`——模拟多轮工具循环，历史消息重建后 assistant 消息 thinking 块与 signature 完整保留（Anthropic/Gemini 夹具）；`thinking_roundtrip=False` 时剥离且不抛。
- 冒烟：Anthropic 模型跑一轮带推理的对话，前端 ThinkingBlock 有内容；再跑一轮带工具调用的，确认不 400。

### 阶段 4：错误码归一（P1）

**新增文件**
- `app/core/llm/model_error_mapper.py`：`map_litellm_error(exc) -> ModelErrorInfo(error_code, guidance, retryable)`。
  - 映射 litellm 异常子类（**实测 litellm 1.97.0 异常族**）：`AuthenticationError`→`auth_failed`、`RateLimitError`→`rate_limited`、`ContextWindowExceededError`→`context_length_exceeded`、`BadRequestError`→`invalid_request`、`APIConnectionError`/`Timeout`→`network`（**类名是 `Timeout` 不是 `TimeoutError`**）、`NotFoundError`→`model_not_found`、其余→`unknown`。
  - **余额不足（quota）**：litellm 1.97.0 **没有 `InsufficientQuotaError` 类**（实测 `dir(litellm.exceptions)` 无 Quota 类，余额不足经 HTTP 429 承载）。识别方式：优先看 `RateLimitError` 的 `status_code==429` 且错误消息含 quota/balance/余额 特征 → `insufficient_quota`（国内厂商高频，如 DeepSeek/通义余额不足提示）；否则 `RateLimitError` 保持 `rate_limited`。实现时以异常 `status_code` + 消息特征组合判定，不依赖不存在的类。
  - **内容安全拦截**：国内厂商在 400/特定响应中返回审查码（如通义 `DataInspectionFailed`、DeepSeek 敏感词提示等），无法仅靠异常子类区分——mapper 增加 `map_provider_content_blocked(error_body) -> bool`，命中即映射 `content_blocked`（而不是笼统 `invalid_request`）。**前提风险**：依赖从 litellm 异常中可靠提取原始响应体，`error_body` 可能不可得（见风险清单）。
  - **上下文窗口细分**：`ContextWindowExceededError` 区分「请求超窗」（引导压缩/清历史）与「输出超 max_tokens」（引导调大 max_tokens 或换模型）——从异常消息特征或 litellm `response` 上下文判断。
  - 每个码配用户可读中文 guidance（对齐 `ModelNotConfiguredError` 风格）。所有映射函数带完整 docstring（四段式），遵循规范第四条。

**修改文件**
- `app/models/enums/error_kind.py`：新增模型错误细分（`MODEL_AUTH_FAILED`/`MODEL_RATE_LIMITED`/`MODEL_CONTEXT_WINDOW_EXCEEDED`/`MODEL_INVALID_REQUEST`/`MODEL_NETWORK_ERROR`/`MODEL_NOT_FOUND`/`MODEL_INSUFFICIENT_QUOTA`/`MODEL_CONTENT_BLOCKED`/`MODEL_UNKNOWN`）。
- `app/models/payload/run_failed_payload.py`：新增可选字段 `error_code: str | None = None`（缺省 None 保持向后兼容）。
- `app/core/runtime/runner.py`（398-441）：异常分支调 mapper，`error_code` 写入 payload；`end_reason` 语义保持（`client_disconnected` 等不受影响）。
- 前端 `apps/desktop/src/components/chat/`（StatusBadge/错误提示）：按 `error_code` 展示 guidance；`apps/shared/ts/events.ts` 由脚本生成（勿手改，跑 `scripts/generate_runtime_event_ts.py` 重新生成）。

**验收**
- 单测：`test_model_error_mapper.py`——litellm 各异常子类 → 正确 error_code/retryable/guidance。
- 单测：runner 异常分支 payload 带 error_code。
- 前端 vitest：错误提示按 error_code 渲染。

### 阶段 5：模型窗口表 + 成本统计 + 文档（P2）

**修改文件**
- `app/core/llm/model_catalog.py`：窗口表接入 litellm 目录 `max_input_tokens` 自动回填（`get_model_info` 懒查），仅 deepseek 两条的现状改为「litellm 目录为准，缺省 128k 兜底」；保留深科覆盖。
- **成本统计（litellm 现成能力，不加白不加）**：litellm `model_cost` 价格表含每百万 input/output token 单价，且已归一 `reasoning_tokens`（thinking 计入成本）。新增 `service/` 层成本估算（`estimate_cost(usage, model_name) -> cents`，所有函数带完整 docstring，遵循规范第四条）挂到 `turn_usage_stats`/`RunFinishedPayload`，前端展示每次调用的估算成本与思考 token 消耗。
- `docs/agent-model-provider-design.md`：同步 §5/§7/§11 等段落（厂商枚举、前缀、key 边界、依赖边界）。

**验收**
- 单测：`test_model_catalog.py`——deepseek 命中精确窗口、未知模型回退 128k、litellm 目录查询失败不抛。
- 单测：`test_cost_estimator.py`——known 模型按价表算、未知模型返回 None 不抛、reasoning_tokens 计入。
- 冒烟：跑一轮对话后前端能看到估算成本与 thinking token。

### 阶段 6：端到端回归 + 闭环（全部）

- 全量 `uv run --project apps/backend pytest` + `ruff` + `mypy`（改动文件）。
- **⚠️ 全量 pytest 前置清理**（2026-08-18 实测现状）：3 个测试文件收集期 ImportError（`test_delegate_agent_profiles.py`、`test_file_snapshot_task_seq.py`、`test_file_snapshot_task_boundaries.py`，源于代码与 docstring 漂移）+ 4 个既有失败（`test_analyze_langfuse_replay.py`×3 fixture 缺失、`test_tool_execution_cancellation_and_errors.py`×1 断言漂移）——与本方案无关但会阻塞全量回归，阶段 6 前须先修复或标记 skip。
- 桌面端 `vitest` + `tsc`（仅验证本次相关错误清零）。
- 每阶段完成后启动**独立审查 Agent + 独立测试 Agent**，通过才进下一阶段；不通过回修重跑。

## 五、风险清单

| 风险 | 等级 | 缓解 |
|---|---|---|
| **`model_node._collect_chunk_to_ai_message`（423-426）无条件剥离 `reasoning_content` 导致 Anthropic/Gemini/o 系列签名丢失 → 工具调用 400** | **P0** | 剥离逻辑按 `thinking_channels`/provider 区分（DeepSeek/Kimi 剥、Anthropic/Gemini/o 系列保留），见阶段 3.3；`thinking_roundtrip` 兜底开关 |
| **OpenAI o 系列 `reasoning`（encrypted_content）在 langchain_litellm 0.7.0 无归一通路**（实测 `_convert_dict_to_message` assistant 分支仅处理 `function_call`/`tool_calls`/`reasoning_content`/`provider_specific_fields`，注入块字段名为 `{"type":"thinking"}`） | P1 | 阶段 3.2 对 `reasoning` 通道先核实 litellm 侧处理；若 0.7.0 确无通路，o 系列降级为「仅摘要展示」不回传，或升级 langchain-litellm 后重新核实 |
| Anthropic thinking 通道字段名待定（0.7.0 全包 `thinking_blocks` 0 匹配，唯一已证实通道是 `reasoning_content`，注入块字段名为 `{"type":"thinking"}`） | P1 | 阶段 3 开工前先读本地 `.venv` ChatLiteLLM/litellm 源码核实 Anthropic 抽取通道 |
| thinking 回传链路在历史消息重建时丢 signature → 工具调用 400 | **P0** | 剥离点已定位（`_collect_chunk_to_ai_message`）；阶段 3.3 先写链路验证单测/冒烟，发现剥离则补透传；`thinking_roundtrip` 兜底开关 |
| `map_provider_content_blocked(error_body)` 依赖从 litellm 异常可靠提取原始响应体，`error_body` 可能不可得 | P2 | mapper 对取不到 body 的情况回退 `invalid_request`，不抛；验收覆盖该分支 |
| azure 额外参数（api_base 形如 `https://xxx.openai.azure.com/`）透传细节 | P1 | 阶段 2 冒烟用真实/文档示例验证一次 |
| 前端 `ProviderType` 与后端注册表漂移 | P2 | vitest 契约测试锁死 15 类型集合 |
| `model_node` 到 `LLMRuntimeConfig` 的传递链未通 | P1 | 阶段 3 前先确认 nodes 传递链，改动最小化 |
| Key 加密引入重依赖或破坏桌面端现有 keychain 集成；迁移失败丢原 key | P1 | 阶段 2 以 `apps/desktop/src-tauri` 现有能力为准评估，不强行引入；存量迁移失败不丢原 key，可重试 |
| 代理配置影响既有无代理请求 | P2 | `WEB_PROXY_*` 全可选，None 时不注入，回归既有链路 |
| litellm 后续 minor 升级行为漂移 | P2 | 已锁 1.97.0；升级须跑全量回归 |

## 六、明确不做（防膨胀）

- 不做各厂商原生 SDK 适配——全部委托 litellm（不重复造轮子底线）。
- 不引入新三方依赖（Key 加密优先复用桌面端现有 keychain/OS 能力）。
- 不为「未来可能有」的厂商预建 schema（注册表是 dict，加一行即支持）。
- 不手改 `shared/ts/events.ts`（脚本生成物，变更统一由 `scripts/generate_runtime_event_ts.py` 重新生成，见阶段 4）。
- 不改 Turn/Task 数据模型（`agent_id`/`parent_turn_id` 边界见 MEMORY.md，有意设计）。
- 本方案不实现「未来扩展」章节的条目（见下章），仅记录。

## 七、实施顺序摘要

> **本期交付边界**（2026-08-18 用户决议）：先只做**设计文档 + 代码骨架**。本文件即设计文档；代码骨架清单见 §九。下面顺序为用户审查放行后的实施顺序，**当前未启动实施**。

1. 阶段 1（注册表 + 枚举 15 类）→ 2. **阶段 1.5（Agent profile 无默认模型策略，与阶段 2 同批）** → 3. 阶段 2（schema + factory + 凭据存储明文 SQLite + 连通测试 + 代理）→ 4. 阶段 3（thinking 生命周期）→ 5. 阶段 4（错误码归一）→ 6. 阶段 5（窗口表 + 成本统计 + 文档同步）→ 7. 阶段 6（端到端回归 + 独立审查/测试闭环）。

每个阶段独立提交、独立审查/测试闭环；阶段 1 + 1.5 + 2 为核心地基，先落地；阶段 3（thinking 回传）为 agent 工具循环生死线，紧随其后；阶段 4-5 体验完善；阶段 6 全量闭环。

## 八、代码骨架清单（本期交付物）

> 本节列出本期「设计文档 + 代码骨架」交付物中**新增**的代码骨架文件。每个文件**只含接口定义、dataclass、Type 字段、函数签名 + 完整 docstring（四段式）**，**不含业务实现**——实现留 TODO 注释占位，等用户审查放行后按 §七 阶段顺序逐个补全。骨架提交时必须通过 `ruff format` + `ruff check` + `mypy_new_strict.sh`（新文件无豁免），但允许 `NotImplementedError` 占位与 `# TODO(阶段 N)` 标注。

### 后端骨架（`apps/backend/app/`）

| 路径 | 类型 | 内容摘要 | 阶段归属 |
|---|---|---|---|
| `app/models/provider_capability.py` | 新增 | `ProviderCapability` dataclass + `PROVIDER_CAPABILITIES` 注册表（15 类）+ `get_capability()` + docstring 四段式 | 阶段 1 |
| `app/core/llm/model_error_mapper.py` | 新增 | `ModelNotConfiguredError` 异常类 + `map_model_not_configured()` 函数签名（完整 mapper 见阶段 4，本期仅此一项骨架） | 阶段 1.5 |
| `app/core/llm/model_settings.py` | 修改 | 加 `max_retries`/`drop_params`/`timeout_seconds`/`thinking_display` 字段（None 默认 + docstring） | 阶段 2 |
| `app/models/llm_runtime_config.py` | 修改 | 加 `thinking_request`/`thinking_roundtrip`/`thinking_display`/`thinking_channels`/`api_version` 字段；`from_model_entry` 签名扩展（接收 `provider_type`/`thinking_display`，函数体留 TODO） | 阶段 2 + 3 |
| `app/storage/model/provider_model.py` | 修改 | 加 `api_version: Mapped[str \| None]` 列 + docstring | 阶段 2 |
| `app/models/provider_record.py` | 修改 | 同步 `api_version` 字段；扩展「序列化不输出 api_key」docstring | 阶段 2 |
| `app/storage/crud/provider_crud.py` | 修改 | 透传 `api_version`；扩展 no-leak docstring | 阶段 2 |
| `app/service/provider/provider_service.py` | 修改 | 删 `_KEYLESS_PROVIDER_TYPES`，改读 capability；docstring 更新 | 阶段 1 |
| `app/service/provider/provider_discover_service.py` | 修改 | 删 `PROVIDER_TYPE_PREFIXES`，改读 `get_capability().litellm_prefix`；`discover_models` 函数体留 TODO；幂等 upsert / soft-delete 逻辑骨架 | 阶段 2 |
| `app/service/provider/provider_connection_test_service.py` | 新增 | `test_connection(provider_id) -> ConnectionTestResult` 函数签名 + docstring（函数体留 TODO，阶段 2 实现） | 阶段 2 |
| `app/core/llm/factory.py` | 修改 | `build_chat_model` 签名扩展（读 capability 决定 max_retries/drop_params/api_version/thinking 注入），实现留 TODO | 阶段 2 |
| `app/core/agents/agent_profile.py` | 修改 | `model_name: str \| None = None` + docstring | 阶段 1.5 |
| `app/core/agents/define_agents.py` | 修改 | 5 个 profile 移除 `model_name=...` 硬编码 | 阶段 1.5 |
| `app/models/enums/error_kind.py` | 修改 | 加 `MODEL_NOT_CONFIGURED` 枚举值（完整细分见阶段 4） | 阶段 1.5 |
| `app/models/payload/run_failed_payload.py` | 修改 | 加 `error_code: str \| None = None` 字段 | 阶段 1.5 + 4 |
| `app/service/task/turn_prepare_service.py` | 修改 | 入口前置 `if agent.model_name is None: raise ModelNotConfiguredError(...)` 分支骨架 | 阶段 1.5 |
| `app/api/providers_api.py` | 新增/修改 | `POST /providers`、`GET /providers`、`PATCH /providers/{id}`、`POST /providers/{id}/discover`、`POST /providers/{id}/test`、`GET /models?enabled=true` 路由签名（已有路由由实际代码决定，本骨架补缺失端点） | 阶段 2 |
| `app/api/schemas/request/ProviderCreateRequest.py` | 修改 | `PROVIDER_TYPES` 改读注册表；加 `api_version` 字段 | 阶段 1 + 2 |
| `app/api/schemas/response/ProviderResponse.py` | 修改 | 加 `api_version`；**不含 `api_key` 字段**（no-leak） | 阶段 2 |
| `app/api/schemas/response/ModelEntryResponse.py` | 修改/新增 | 扁平 list 响应（携带 `provider_id`/`provider_name`，无嵌套） | 阶段 2 |

### 前端骨架（`apps/desktop/src/`）

| 路径 | 类型 | 内容摘要 | 阶段归属 |
|---|---|---|---|
| `apps/shared/ts/model.ts` | 修改 | `ProviderType` 扩展为 15 类（与后端注册表键对齐）+ 契约测试 | 阶段 1 |
| `apps/shared/ts/agents.ts` | 修改 | `AgentProfile.model_name: string \| null` | 阶段 1.5 |
| `apps/desktop/src/components/settings/ProviderFormDialog.tsx` | 修改 | 厂商类型下拉扩展；azure 显示 api_version 输入框；`qianfan`/`xfyun` 显示 base_url 提示；`moonshot`/`minimax`/`zai` 显示区域端点提示；加「测试连接」按钮 + 「发现模型」按钮 | 阶段 1 + 2 |
| `apps/desktop/src/components/settings/ProviderDiscoverDialog.tsx` | 新增 | 发现结果列表 + 启用/禁用切换 + 已发现模型展示 | 阶段 2 |
| `apps/desktop/src/components/chat/ModelSelector.tsx` | 新增 | `<select>` + `<optgroup>` 按厂商分组平铺；调 `GET /models?enabled=true`；localStorage 持久化选择 | 阶段 1.5 + 2 |
| `apps/desktop/src/pages/chat/NewTaskPage.tsx` | 修改 | 接入 `ModelSelector`；`agent.model_name=null` 时发送按钮 disabled + tooltip | 阶段 1.5 |
| `apps/desktop/src/components/layout/InputBar.tsx` | 修改 | 发送按钮 disabled 条件加 `!selectedModelId` | 阶段 1.5 |
| `apps/desktop/src/services/api.ts` | 重新生成 | `scripts/generate_api_ts.py` 重新生成；含 `ProviderResponse`/`ModelEntryResponse`/`AgentProfile` 新字段 | 阶段 1.5 + 2 |
| `apps/desktop/src/stores/provider.ts` | 新增/修改 | zustand store：providers 列表 + CRUD actions + discover action + test_connection action | 阶段 2 |
| `apps/desktop/src/stores/model.ts` | 新增/修改 | zustand store：models 平铺列表（按 provider 分组缓存）+ selected_model_id + localStorage 持久化 | 阶段 1.5 + 2 |

### 测试骨架（与代码骨架同批提交，断言留 TODO）

| 路径 | 阶段归属 |
|---|---|
| `apps/backend/tests/test_provider_capability.py` | 阶段 1 |
| `apps/backend/tests/test_agent_profile_no_default_model.py` | 阶段 1.5 |
| `apps/backend/tests/test_model_not_configured_error.py` | 阶段 1.5 |
| `apps/backend/tests/test_provider_record_no_leak.py` | 阶段 2 |
| `apps/backend/tests/test_provider_discover_service.py` | 阶段 2 |
| `apps/backend/tests/test_provider_test_connection.py` | 阶段 2 |
| `apps/backend/tests/test_llm_factory_litellm.py`（扩展） | 阶段 2 |
| `apps/desktop/src/tests/ModelSelector.test.tsx` | 阶段 1.5 |
| `apps/desktop/src/tests/ProviderFormDialog.test.tsx`（扩展） | 阶段 1 + 2 |
| `apps/desktop/src/tests/shared-model-contract.test.ts` | 阶段 1 + 1.5 |

### 骨架提交的验收门禁

- `uv run --project apps/backend ruff format --check apps/backend/app apps/backend/tests`
- `uv run --project apps/backend ruff check apps/backend/app apps/backend/tests`
- `uv run --project apps/backend mypy --config-file apps/backend/mypy.strict.ini apps/backend/app/models/provider_capability.py apps/backend/app/core/llm/model_error_mapper.py apps/backend/app/service/provider/provider_connection_test_service.py`（新增文件无存量豁免，必须通过）
- `cd apps/desktop && pnpm vitest run src/tests/shared-model-contract.test.ts`
- 骨架文件必须含完整四段式 docstring（参数/返回/异常/副作用），函数体允许 `raise NotImplementedError("TODO(阶段 N)")` 占位
- 不允许骨架文件 import 不存在的模块或字段；如需引用未实现的下游函数，用 `from typing import TYPE_CHECKING` + 字符串前向引用

## 九、未来扩展（用户视角缺口，本期不实施，仅记录）

> 以下条目来自 2026-08-18 官方文档 + 用户视角审查，属于「用户会卡住」的真实体验缺口，但超出本期 6 阶段范围，记入方案供未来迭代按优先级取舍。**不得在本期擅自扩大范围实施**；实施时逐条独立评估、独立闭环。

| # | 缺口 | 用户会遇到的痛点 | 建议归属 |
|---|---|---|---|
| 1 | **参数范围提示** | Kimi `temperature∈[0,1]`、thinking 模型禁 temperature、DeepSeek 只有 `max_tokens`（OpenAI o 系列要 `max_completion_tokens`）——用户乱调必报错 | 注册表 `parameter_constraints` 元数据落地后接 UI 提示 |
| 2 | **thinking 展示粒度** | Anthropic 默认只回摘要、Gemini 需开 `includeThoughts` 才能看到 thought——用户要能选「完整/仅摘要/关」 | 阶段 3.4 已含展示开关，剩余「budget_tokens 大小配置」可扩展 |
| 3 | **新模型发现** | litellm 目录远程拉取持续新增，用户不会知道新模型可用 | discover 增「新增模型」角标/提示 |
| 4 | **成本可视化增强** | 阶段 5 只有单次估算，缺「按日/按 task 汇总」 | 聚合报表 |
| 5 | **多工作区密钥隔离** | 不同项目可能用不同厂商 key | providers 与 workspace 绑定 |
| 6 | **并发/配额提示** | 同一 key 多个 task 并发时撞限流，用户不知道为什么慢 | 请求排队 + 限流状态透传 |
| 7 | **厂商文档直链** | 配错参数时用户需要去官方文档确认 | ProviderFormDialog 内嵌官方文档链接 |
| 8 | **流式首 token 延迟监控** | 各家服务端首 token 延迟差异大，用户以为卡死 | 观测指标（TTFT） |
| 9 | **多 key 轮换/备用** | 一个厂商可配多个 key，主 key 限流自动切换 | providers 表支持多 key |
| 10 | **用量告警** | 余额/用量接近上限时应提醒 | 接入阶段 5 成本统计后扩展 |
| 11 | **凭据加密存储（未来）** | 当前明文 SQLite 满足本地单机；若未来支持远程部署/多用户，须升级为 keychain/DPAPI + 加密 | 触发条件：远程部署或多用户场景上线时 |
