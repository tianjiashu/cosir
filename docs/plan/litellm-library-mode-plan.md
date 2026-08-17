# 全面切 litellm 库模式方案：core/llm 单一收口

> 状态：待评审（plan 先行）
> 日期：2026-08-15
> 本方案**推翻**同文件旧版「双路径并存 + 向后兼容」设计，改为**单一路径**：删除全部 OpenAI 兼容直连，收口到 `langchain_litellm.ChatLiteLLM`。

## 一、目标与三条决策

删除整条「OpenAI 兼容直连」路径（`llm_provider/` 子包 + `model_http_pool`），把 `core/llm` 收口为单一入口 `build_chat_model`，内部直接实例化 `ChatLiteLLM`。模型名带 provider 前缀透传（`deepseek/deepseek-v4-flash`），路由/连接/`reasoning_content`/`usage_metadata` 全交 litellm。

**三条不可动摇决策（已拍板）**：
1. **缺 Key 报错、请求失败再报错**——删 `GenericFakeChatModel` 回退，缺 Key 构建期抛 `ValueError`。
2. **删 `langchain-openai` 依赖**。
3. **范围**——仅「`core/llm` 全面切 litellm + 内置 profile 迁移 + 真实调 Key 冒烟」，不含前端选择器 / turn 级覆盖 / `GET /models`。

## 二、代码事实（改造基线）

### 2.1 消费契约（不可破坏）
`workflow.py:138` 调 `build_chat_model(model_name, model_settings) -> BaseChatModel`，随后 `bind_tools(schemas, strict=True)` + `astream`。**返回对象仍是 `BaseChatModel` 即编排层零改动**。

`model_node.py` 流式消费固定读取：`chunk.additional_kwargs["reasoning_content"]`、`usage_metadata`（优先）/`response_metadata.token_usage`（兜底）、`_extract_text` 只认 `type=="text"` 块。**这是唯一硬约束**。

### 2.2 删除面（6 文件）
`llm_provider/` 整个子包（`base.py` / `openai_compatible.py` / `deepseek_provider.py` / `qwen_provider.py` / `__init__.py`）+ `model_http_pool.py`。

### 2.3 改造面（9 文件）
| 文件 | 动作 |
|---|---|
| `factory.py` | 重写：单一 ChatLiteLLM 收口，缺 Key 报错 |
| `model_settings.py` | 6 字段保留，语义微调 |
| `model_catalog.py` | `max_context_window` 归一化 `rsplit("/",1)[-1]` |
| `define_agents.py` | 5 profile 迁移带前缀 model_name + 删 `base_url`（`developer_agent_pro` 悬空引用收口为 Task 3 前置，见 §4.4） |
| `agent_profile.py` | 默认 `model_name` 改 `"deepseek/deepseek-v4-flash"` |
| `app/app.py` | 删第 40 行 import + 第 118-119 行 `close_shared_model_http_clients()` |
| `workflow.py` | 处置 line 146-156 `try/except NotImplementedError` 死兜底（决策 1 连锁，见下注） |
| `model_node.py` | `_extract_reasoning_content` docstring 同步（`DeepSeekChatOpenAI` → `ChatLiteLLM`，规范四） |
| `pyproject.toml` | 增 litellm + langchain-litellm，删 langchain-openai |

> **行为变更声明**：决策 1（删 `GenericFakeChatModel`、缺 Key 构建期抛 `ValueError`）落地后，`build_chat_model` 在 `workflow.py:138` 即抛错，专为 fake 模型设计的 line 146-156 `try/except NotImplementedError`（注释明写 "expected when no real API key is configured"）成为**死路径**，必须处置（删除，或改写注释保留给「真实模型不支持 bind_tools」的合法场景）。同时「本地无 Key 起服 + 无工具模式」能力被移除——本地开发/调试需真实 Key 或单测 mock。这是有意取舍（fake 掩盖配置错误、构建期显式报错更可排查），属**行为变更**，需在使用方与测试中同步声明。

### 2.4 关键实现细节
- `factory.py` 现状：缺 Key 回退 `GenericFakeChatModel`（事件 `llm_fallback_fake_model`），否则 `DeepSeekProvider().build()`。
- `model_settings.py`：`ModelSettings` 6 字段 `temperature/top_p/max_tokens/thinking/base_url/api_key_env`，含 `_FIELDS`/`to_dict`/`from_dict`。
- `model_catalog.py`：`_DEFAULT_CONTEXT_WINDOWS = {deepseek-v4-flash/pro: 1M}`，兜底 128K。
- `define_agents.py`：当前磁盘仅 5 profile 构造器（developer/reviewer/analyst/test/coder，全 `deepseek-v4-flash`），`developer_agent_pro` 已被未提交工作区改动删除；但 `configuration.py:104-106,112` 与 `test_delegate_agent_profiles.py:6,192` 仍引用 → 当前 `build_agent_registry()` 处破损中间态（ImportError）。各 profile 硬编码 `base_url="https://api.deepseek.com"` + `api_key_env="DEEPSEEK_API_KEY"`。
- `openai_compatible.py` thinking 映射：`extra["thinking"]={"type":"enabled"}`。
- `llm_provider/qwen_provider.py`：已实现未接线（`__init__.py` 为空、无任何导入方），删除面已含，属防膨胀正当删除。

### 2.5 依赖兼容（已实测）
`langchain 1.3.1` / `langchain-core 1.4.9`（要求 `>=1.4.7`）/ `httpx 0.28.1`（要求 `>=0.28.1,<0.29`）/ Python 3.11 全兼容；需新增 `litellm>=1.83.14,<2.0.0`。wheel 已下载 `temp/llm-wheel/langchain_litellm-0.7.0-py3-none-any.whl`。

## 三、官方文档/源码事实

**已核实（litellm 文档 + langchain-litellm GitHub main）**：DeepSeek 加 `deepseek/` 前缀；`reasoning_content` 透传；思考用 `thinking={"type":"enabled"}`；ChatLiteLLM 流式 `_convert_delta_to_message_chunk` 已把 `reasoning_content` 写 `additional_kwargs`；`usage_metadata` 由 `_create_usage_metadata` 生成；**无 `http_async_client` 参数**。

**待核实（审查必须解压 wheel 读 0.7.0 真实源码，不得只看 main）**：

| # | 待核实项 | 风险 |
|---|---|---|
| W1 | 0.7.0 wheel 流式是否把 `reasoning_content` 落 `additional_kwargs` | main 已修 #29513，但 0.7.0 发布包可能仍丢 |
| W2 | ChatLiteLLM 构造参数精确列表（`api_base`/`model_kwargs`/`request_timeout`/`streaming`） | 参数名猜错 → 静默丢配置 |
| W3 | 采样参数 `temperature/top_p/max_tokens` 传法（构造参数 vs `model_kwargs`） | 传错 → 采样失效 |
| W4 | `thinking` 传法 | 传错 → 推理模型不思考 |
| W5 | `usage_metadata` cache 字段映射（`cache_read_input_tokens` vs `input_token_details.cache_read`） | 字段差异 → cache 计数错 |

## 四、总体设计

### 4.1 factory.py（单一收口）
```python
def build_chat_model(model_name, model_settings=None) -> BaseChatModel:
    # Key：api_key_env 显式配置但环境变量缺失 → 抛 ValueError（不回退 fake）
    # thinking=True → model_kwargs {"thinking":{"type":"enabled"}}
    # 组装 ChatLiteLLM(model=model_name 透传, api_key, api_base, 采样, request_timeout)
```
缺 Key 两层：`api_key_env` 配置缺失 → 构建期 `ValueError`（含缺失变量名+模型名，写 error 日志）；未配置 → `api_key=None` 交 litellm 按前缀自动解析，请求期失败 litellm 抛错。

### 4.2 model_settings.py
6 字段保留（避免动 `_FIELDS`）：`temperature/top_p/max_tokens`→采样；`thinking`→`thinking={"type":"enabled"}`；`base_url`→可选 `api_base` 覆盖；`api_key_env`→Key 来源。

### 4.3 model_catalog.py
`max_context_window` 归一化 `rsplit("/",1)[-1]`，裸名与带前缀名均命中，未收录 128K 兜底（`test_context_usage.py` 零回归）。

### 4.4 profile 迁移
按磁盘事实：5 profile（developer/reviewer/analyst/test/coder）`model_name` 全改 `deepseek/deepseek-v4-flash`；删 `base_url`（litellm 内置）；保留 `api_key_env="DEEPSEEK_API_KEY"`。`agent_profile.py` 默认值同步。

**前置收口（Task 3 门禁）**：`developer_agent_pro` 已被未提交改动删除，但 `configuration.py:104-106,112` 与 `test_delegate_agent_profiles.py:6,192` 仍引用 → 迁移前必须先收口这两处悬空引用（删除注册与断言），否则当前工作区 `build_agent_registry()` 即 ImportError；若后续需恢复 pro profile，应先恢复构造器再迁移。

### 4.5 app/app.py
删 `close_shared_model_http_clients` import 与 lifespan `finally` 调用（连接由 litellm 管）。

## 五、测试牵连

| 文件 | 问题 | 处理 |
|---|---|---|
| `test_langchain_bridge_assistant_content.py` | ① `_serialize_to_provider` 用 `langchain_openai._convert_message_to_dict`（删除后 import 失败）；② import `_ADDITIONAL_KWARGS_DROP_KEYS` 但当前 `langchain_bridge.py` 无此符号（既有不一致，当前已 import 失败） | ①② 同文件合并处理：整体重写为仅断言 `sanitize_assistant_messages` 后的字段，不依赖 `langchain_openai` 私有符号 |
| `test_delegation_recovery.py` | monkeypatch `close_shared_model_http_clients` | 删相关 monkeypatch |
| `test_context_usage.py` | 裸名查表 | 无需改（归一化兼容） |
| `test_analyze_langfuse_replay.py` | `assert "DeepSeekChatOpenAI" in content` | 无需改（字符串来自历史 replay JSON，非代码类名） |

## 五·五、W1-W5 源码核实结论（2026-08-16 已闭环）

> 核实对象：`.venv/Lib/site-packages/langchain_litellm/chat_models/litellm.py`（0.7.0 发布包，非 GitHub main），与 wheel 同源。

| # | 待核实项 | 结论 | 证据 |
|---|---|---|---|
| W1 | 0.7.0 流式是否把 `reasoning_content` 落 `additional_kwargs` | **成立** | `_convert_delta_to_message_chunk` line 260-261：`if "reasoning_content" in delta: additional_kwargs["reasoning_content"] = delta["reasoning_content"]` |
| W2 | `ChatLiteLLM` 构造参数精确列表 | **成立** | 独立 Pydantic 字段含 `model`/`api_base`/`api_key`/`model_kwargs`/`request_timeout`/`streaming`/`max_retries`；`_client_params` 按名透传 |
| W3 | 采样参数传法 | **成立** | `temperature`/`top_p`/`max_tokens` 均为独立构造字段，非 `model_kwargs` 包裹 |
| W4 | `thinking` 传法 | **成立** | `_thinking_config()` 从 `model_kwargs.get("thinking")` 读取（line 452-454）；`bind_tools` 对 Claude+thinking+强制 tool_choice 有降级保护 |
| W5 | `usage_metadata` cache 字段映射 | **成立** | `_create_usage_metadata` 优先 `cache_read_input_tokens`/`cache_creation_input_tokens`，兜底 `input_token_details.cache_read`/`cache_creation_tokens`（双路径兼容） |

**决策**：W1 成立 → **R1 适配子类不落地**（Task 2 门禁放行）。`model_settings.thinking=True` 经 `model_kwargs={"thinking": {"type": "enabled"}}` 传入。

## 六、风险

| 风险 | 缓解 |
|---|---|
| R1：0.7.0 流式丢 `reasoning_content`（W1 不成立） | 冒烟先证实并**断言 `reasoning_content` 非空**（`model_node._extract_reasoning_content` 对字段缺失静默返回空串，仅"不抛异常"不足为证）；若丢则 `ChatLiteLLMWithReasoning` 适配子类覆写 chunk 转换，**R1 落地为 Task 2 门禁** |
| R2：usage 字段差异（W5） | 冒烟对照 `model_node` 消费；必要时适配 |
| R3：`bind_tools(strict=True)` 兼容 | 冒烟覆盖 tool_calls；按 model 降级 |
| R4：`_ADDITIONAL_KWARGS_DROP_KEYS` 既有不一致 | 与切 litellm 正交但删除 langchain-openai 会暴露；实施时判定「补齐剥离实现」还是「剥离到独立任务」 |
| R5：litellm 依赖较大 | 桌面单用户可接受；锁版本 |

## 七、落地步骤

- **Task 1（先行）**：改 `pyproject.toml` + `uv lock`；解压 wheel 核实 W1-W5；真实调 Key 冒烟 `ChatLiteLLM(deepseek/deepseek-v4-flash).astream`，打印并**断言 `additional_kwargs["reasoning_content"]` 非空** + 记录 `usage_metadata`，定论 reasoning_content/usage 透传事实。W1 不成立即触发 R1 适配子类方案。
- **Task 2**：`factory.py` 重写 + `model_settings.py` + `model_catalog.py` 归一化；删 `llm_provider/` 子包 + `model_http_pool.py` + `app/app.py` 清理；处置 `workflow.py` line 146-156 死兜底 + 更新 `model_node._extract_reasoning_content` docstring。**门禁：若 W1 不成立，R1 适配子类必须随本任务落地**。
- **Task 3**：profile 迁移（`define_agents.py` + `agent_profile.py`）；**前置**：收口 `configuration.py:104-106,112` 与 `test_delegate_agent_profiles.py:6,192` 对 `developer_agent_pro` 的悬空引用。
- **Task 4**：测试牵连修复（`test_langchain_bridge_assistant_content.py` / `test_delegation_recovery.py`）+ 新增路由/归一化/缺 Key 报错单测。
- **Task 5**：端到端一轮 turn 冒烟（thinking 事件 / tool_calls / usage / checkpoint）+ 回归。

## 八、验收清单

- [ ] `uv run --project apps/backend ruff check` 与 `mypy` 通过（新代码零错误）
- [ ] 全部 pytest 通过（既有零回归 + 新增单测）
- [ ] W1-W5 全部有基于 wheel 源码 + 官方文档的闭环结论
- [ ] 真实调 Key 冒烟：reasoning_content 断言非空 / usage_metadata 透传事实明确
- [ ] 缺 Key 时构建期报错（无 fake 回退）、请求失败有结构化 error 日志
- [ ] `pyproject.toml` + `uv.lock` 提交，版本锁定
- [ ] `llm_provider/` 子包与 `model_http_pool.py` 已物理删除，无残留 import
- [ ] `workflow.py` line 146-156 死兜底已处置，无「本地无 Key 起服」残留行为
- [ ] profile 悬空引用（`configuration.py` / `test_delegate_agent_profiles.py`）已收口，`build_agent_registry()` 可正常构建
