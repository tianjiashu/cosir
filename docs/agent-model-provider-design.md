# 输入栏 Agent + 模型双选择器 & 模型厂商配置中心（设计文档）

- 状态：已确认决策，待实施
- 日期：2026-08-17
- 影响范围：后端 `core/llm`、`api`、`storage`、`models`、`service`；前端 `InputBar`、`AgentSelector`、store、services；共享协议 `apps/shared/ts`
- 关联文档：`docs/上下文折叠.md`（上下文窗口解析）、`AGENTS.md`（目录与既有决议）

---

## 1. 背景与目标

当前模型硬编码在 `AgentProfile.model_name`（`define_agents.py`，如 `deepseek/deepseek-v4-flash`），前端无法选择模型、无法配置模型厂商。本次目标：

1. 输入栏支持 **Agent + 模型双选择器**：Agent 决定「怎么执行」，模型决定「用什么推理」，二者解耦。
2. 提供 **模型厂商配置中心**：以 SQLite 动态表承载厂商（Provider）与模型（Model），替代「静态模型目录 + 环境变量直配」的现状。
3. 模型可用性以 **DB 配置为唯一事实来源**；无可用厂商/模型时**发送前校验报错**（前端拦截 + 后端兜底），不做发送后失败。
4. API Key 继续走 **.env 环境变量**（`api_key_env` 引用），不引入加密存储。

## 2. 现状盘点（已核对代码）

| 项 | 现状 |
|---|---|
| Agent 选择 | ✅ `AgentSelector.tsx` → `GET /agents` → `taskStore.selectedAgentId` → 创建 task/turn 传 `agent_id` |
| 模型选择 | ❌ 无；模型名硬编码在 `AgentProfile.model_name`（`define_agents.py` 5 处） |
| 厂商配置 | ❌ 仅 `ModelSettings.api_key_env` + `base_url`（env 引用），无 UI、无持久化 |
| 模型构建 | ✅ `core/llm/factory.py::build_chat_model(model_name, model_settings)` → `ChatLiteLLM` 单一收口；唯一调用点 `workflow.py:140` |
| 上下文窗口 | ✅ `ModelCatalog` 静态事实表 + `context_window_resolver`（`min(模型窗口, 全局软上限)`） |
| 请求契约 | `CreateTaskRequest{text, agent_id, workspace_id}`、`CreateTurnRequest{input_text, agent_id}`——**无 model 字段**；`POST /tasks` 经 `task_service.create_task_with_initial_turn` **自动建首个 pending turn**（前端新 task 场景不另发 turn 请求） |
| Turn 持久化 | `TurnRecord` 有运行期 `agent_id`，**无 `model_name`**；`turns` 表刻意无 `agent_id` 列（既有决议） |
| Schema 迁移 | ✅ `init_schema.py`：`APP_MODELS` 建表 + `_ensure_model_columns` 加列迁移机制，新增表/列零成本接入 |
| 前端 UI 库 | 基础组件齐（popover/button/tabs/switch…），缺 Command/Combobox/Dialog/Select/Form |

## 3. 已确认决策（用户拍板，2026-08-17）

| 编号 | 决策 |
|---|---|
| D1 | `model_name` **落库 `turns` 表**（时间线可追溯每轮所用模型）。此决策独立于既有「turns 不加 agent_id」决议，需新建列 |
| D2 | API Key 存储用 **.env 环境变量**：provider 记录 `api_key_env`（环境变量名），运行时 `os.environ` 读取；不引入 keyring |
| D3 | 模型自动发现统一走 **litellm model list**（litellm 内置模型目录），不直连 `{base_url}/v1/models` |
| D4 | **没有可用厂商/模型时，发送 turn 之前校验报错**（前端发送动作前拦截 + 后端解析期兜底），不做发送后失败 |

## 4. 核心语义

- **Agent = 怎么执行**（提示词 + 工具集 + workflow）；**Model = 用什么推理**。输入栏双选择器。
- 模型下拉**首项固定 "Auto · 跟随 Agent 默认"**：选中 Agent 后自动使用其 `AgentProfile.model_name`；显式选择具体模型则覆盖本次 turn。
- **DB 是模型的唯一事实来源**：`providers` / `models` 两张表承载全部厂商与模型配置。现 `.env` 的 `CODING_AGENT_MODEL_*`（provider/base_url/model/thinking）**不再作为模型来源**，仅 `DEEPSEEK_API_KEY` 作为 Key 来源保留。首次使用需在配置中心添加厂商（提供「从 .env 一键导入 DeepSeek」引导，见 §7.4）。
- Agent 默认模型（`AgentProfile.model_name`）**保持字符串不改 schema**：Auto 语义下由解析链查 DB；DB 未收录该模型 → 校验报错（与 D4 一致）。
- **`AgentProfile.model_settings` 处置**（审查修订，闭合死配置/双源歧义）：主路径（DB 解析 → `from_model_entry`）以 **DB model 行为唯一事实源**（采样参数/thinking/api_key_env 均取自 provider+model 行），`profile.model_settings` **不再作为运行时事实源**——`define_agents.py` 5 处 `model_settings=ModelSettings(...)` 参数**移除**；`ModelSettings` 与 `LLMRuntimeConfig.from_profile` 仅保留为 env 直配兼容路径（测试/未迁移场景，D2/D16）。

## 5. 数据模型

### 5.1 `providers` 表（新增）

| 列 | 类型 | 说明 |
|---|---|---|
| id | TEXT PK | UUID |
| name | TEXT NOT NULL | 显示名（如 "DeepSeek 官方"），`UNIQUE` |
| type | TEXT NOT NULL | `deepseek` / `openai-compatible` / `anthropic` / `ollama` / `custom`，决定 litellm 前缀与默认 base_url |
| base_url | TEXT NULL | 可空；空时交 litellm 按前缀内置解析 |
| api_key_env | TEXT NULL | 环境变量名（D2） |
| enabled | BOOL NOT NULL DEFAULT 1 | 启用开关 |
| sort_order | INT NOT NULL DEFAULT 0 | 排序 |
| created_at / updated_at | DATETIME | 审计 |

- 模型表 `models` 经 `provider_id` FK 级联删除。
- **不预置内置厂商**（D4 逻辑推导：预置则「没有厂商报错」永不触发）；首次使用靠配置中心引导。

### 5.2 `models` 表（新增）

| 列 | 类型 | 说明 |
|---|---|---|
| id | TEXT PK | UUID |
| provider_id | TEXT NOT NULL FK→providers(id) ON DELETE CASCADE | 归属厂商 |
| model_name | TEXT NOT NULL | litellm 路由名，如 `deepseek/deepseek-v4-flash`，`UNIQUE(provider_id, model_name)` |
| display_name | TEXT NOT NULL | 下拉展示名（可省略前缀） |
| max_context_window | INT NOT NULL | 上下文窗口（token），discover 预填 litellm 已知值，可改 |
| supports_thinking | BOOL NOT NULL DEFAULT 0 | 推理模型标识 |
| temperature / top_p / max_tokens | REAL/REAL/INT NULL | 默认采样参数 |
| enabled | BOOL NOT NULL DEFAULT 1 | 启用开关（下拉只显示启用项） |
| sort_order | INT NOT NULL DEFAULT 0 | 组内排序 |

### 5.3 `turns` 表加列（D1）

- `TurnModel` / `TurnRecord` 增加 `model_name: str | None`。
- 迁移复用 `init_schema.py` 既有机制：`TurnModel` 加列即被 `_ensure_model_columns` 自动 `ALTER TABLE ADD COLUMN` 补齐，**历史行（升级前已存在的 turn）填 NULL**——NULL 仅表示「当时未落库」，与 Auto 轮次落解析结果（非 NULL，D11）语义不同，前端时间线展示区分（§10.3）。
- **落库语义：存「解析后的实际所用模型」**（默认决策 D11）。创建期预解析（§6.4）得到 `final_model_name` 后即写入 turn（Auto 时写 `profile.model_name` 命中 DB 的解析结果，而非 NULL）；运行期兜底解析若因配置竞态修正模型名，经 **`turn_service.update_model_name` 服务端口回写**（core 不直写 storage）。保证时间线每轮展示真实所用模型，D1「可追溯」对 Auto 轮次同样成立。
- **只落 model_name，不落 provider_id**（默认决策 D7）：历史回看只需展示模型名；provider/base_url/key 等属「随时可变」的配置态，不固化进历史行。

### 5.4 Schema 注册

- 新 model 加入 `APP_MODELS`（`init_schema.py`），沿用现有建表 + 索引补齐流程，无新增迁移框架。

## 6. 模型解析链（核心改造）

**两段式解析**（审查修订，消除错误等级歧义）：

```
[CreateTurnRequest（追加 turn）或 CreateTaskRequest（新 task 自动建首 turn）+ model_name?]  ── ① service 层创建期预解析 ──┐
        │                                                  │ 命中失败 → HTTP 422 + 修复指引
        │ final_model_name 写入 TurnRecord.model_name       │ （错误等级：请求级）
        ▼                                                  │
[workflow.run()]  ── ② 运行期兜底解析 ──────────────────────┘
    resolve(profile, turn.model_name) → LLMRuntimeConfig      命中失败 → 父 turn RUN_FAILED（错误等级：运行期终态）
        ↓
factory.build_chat_model(llm_config)   ← workflow.py:140 唯一调用点
```

**child 例外**（无 HTTP 上下文）：`delegation_executor` 入口同样执行 ① 预解析（§6.4），失败**无 422 出口** → 与 ② 同为运行期终态语义——**child task/turn 均不落库**（预解析前置在 `create_child_task` 之前，无孤儿任务）、delegation 置 failed、父 turn 收 `DelegationResult(status="failed")`，不影响父 turn 其余流程，错误等级不混同（D12）。

- **① 创建期预解析**（`service/task` 内，turn 创建时；`CreateTaskRequest.model_name` 经 `create_task_with_initial_turn` 透传首 turn，见 §8.3）：以 `requested_model or profile.model_name` 计算 final，校验 DB 命中；DB 命中后经 `provider_service.api_key_configured` 做 **Key 存在性检查**（只读 env，非构建期缺失校验），任一失败即 422（`ModelNotConfiguredError`），不入队不落库。此段承载 D4「发送前校验」的后端拦截。
- **② 运行期兜底解析**（`workflow.run()` 内，`workflow.py:101`）：再次 resolve 以应对「创建后配置被删/禁用」竞态；失败 → `RUN_FAILED` + error 日志（`model_resolve_failed`），与缺 Key 同等级可排查。若因竞态修正了实际模型名，经 **`turn_service.update_model_name(turn_id, final)` 服务端口回写**（core 不直写 storage，§6.4）。
- 两段共用同一 `ModelResolverService.resolve` 核心逻辑，仅错误出口不同（422 vs RUN_FAILED），不重复实现。
- **Key 校验双出口语义**（修订 D13，闭合唯一收口歧义）：① 经 `provider_service.api_key_configured` 做存在性检查（配置检查，失败 → 422，与前端校验同源）；`factory.build_chat_model` 构建期做最终缺失校验（实际读取 env，失败 → `ValueError` → ② RUN_FAILED）。两者分属「配置检查 vs 构建校验」两个阶段，互不重复、不冲突。

### 6.1 `LLMRuntimeConfig` 值对象（`models/llm_runtime_config.py`）

- **归属层**（审查修订）：`LLMRuntimeConfig` 由 service 层 `ModelResolverService` 构造、被 core 层 `factory` 消费——放 `core/llm/` 会形成 **service → core 反向依赖**（不在 AGENTS.md 合法方向内）；放 `models/`（业务值对象层）后 `core → models` 与 `service → models` 均合法，与「值对象自带 `from_xxx` 工厂」约定兼容。
- 字段：`model_name`（带前缀 litellm 路由名）、`base_url: str | None`、`api_key_env: str | None`、**`max_context_window: int | None`**、`temperature/top_p/max_tokens`、`thinking: bool | None`。

- **不承载 `api_key` 明文值**（审查修订）：Key 的 env 读取与缺失校验**唯一收口在 `factory.build_chat_model` 构建期**（复用现有 `ValueError` 逻辑），`LLMRuntimeConfig` 只携带 `api_key_env`，避免 Key 解析双路径漂移。
- `from_profile(profile)`：兼容既有 env 直配形态（D2），供未迁移/测试场景构造（`max_context_window` 取自 `ModelCatalog`）。**分层约束**（审查修订）：入参类型用 `TYPE_CHECKING` + 鸭子类型——运行时只读 `profile.model_name` / `profile.model_settings` 字段，**禁止运行时 import `core/agents` / `core/llm` 类型**，避免新造 `models → core` 反向依赖（现 `models/` 层 `from_xxx` 工厂均不引用 core）。
- `from_model_entry(provider, model_entry)`：从 DB 构造（主路径），**`max_context_window` 取自 `models.max_context_window`**（问题 2 修复：为 `resolve_context_window` 的 `db_window` 提供来源，DB 窗口不落空）。

### 6.2 `ModelResolverService`（`service/llm/model_resolver_service.py`）

- **依赖方向**（审查修订）：解析需查 DB，属业务编排，故 `model_resolver` 上移到 **service 层**（`service/llm/`），经 `storage/crud/model_entry_crud` 只读查询；`core`（workflow）→ `service` 为合法方向（AGENTS.md 契约 `core → service`），**不新增 `core → storage` 依赖**。
- **依赖清单**（修订，闭合实施缺口）：`ModelResolverService` 注入 `model_entry_crud`（模型只读）+ `provider_service`（取 provider 的 `base_url` / `api_key_env` / `api_key_configured`，供 ① 预解析 Key 存在性检查）；二者在 `service/depends.py` 装配（§12），不直接从 storage 另拉 provider，注入关系实施时无需猜测。
- 只读查询启用模型命中 → 组装 `LLMRuntimeConfig`；未命中 → 抛 `ModelNotConfiguredError`（含模型名与修复指引：去配置中心添加厂商或选择已配置模型）。
- `ModelCatalog` 静态表**保留为上下文窗口兜底**：models 表**未收录的模型**（`from_profile` env 直配/测试路径）→ `ModelCatalog`（修订：DB 行 `max_context_window NOT NULL`，正常解析必有窗口，兜底仅在模型不在表中时触发）。**兜底不进入 `ModelResolverService` 校验路径**（与 D10「DB 未收录即报错」不冲突）：主解析链上 DB 未命中即 422/RUN_FAILED，ModelCatalog 仅服务于窗口解析层，防止实施时误在 resolver 主路径加窗口兜底。
- `context_window_resolver` 签名改为 `resolve_context_window(model_name, db_window: int | None = None)`（修订，闭合双源优先级落地）：`db_window` 非空优先（正常解析必有），`None` 时回退 `ModelCatalog`；**两源均仍经 `min(窗口, 全局软上限)` 收敛**（既有语义不变，仅入参增加 db_window 优先项）。
- **兜底分支服务对象**（建议采纳）：主解析链 `db_window` 恒非空（`from_model_entry` 来自 NOT NULL 列、`from_profile` 来自 ModelCatalog 恒有值），`resolve_context_window` 的 ModelCatalog 兜底分支实际服务于 **`tasks_api` / `ContextUsageMeter` 等无 `db_window` 的调用**（问题 2，§12 改造清单）——实施时勿误判为死代码删除。

### 6.3 `factory.build_chat_model` 改造（向后兼容）

- 新签名接受 `LLMRuntimeConfig`；为兼容既有调用形态，保留原 `(model_name, model_settings)` 入口，内部折叠为 `LLMRuntimeConfig`。
- **Key 解析唯一出口**：`api_key_env` 配置但环境变量缺失 → 构建期 `ValueError`（现有逻辑原样保留，不迁移到 service 层）；`api_key_env` 为 None 的 provider（ollama/custom 等）→ `api_key=None` 交 litellm 按前缀解析。

### 6.4 运行时接入点

- `workflow.py:140`：`build_chat_model(agent_profile.model_name, ...)` → `build_chat_model(resolved_llm_config)`（② 运行期解析结果）。
- `workflow.py:203` 的 `model_name_provider` / 上下文窗口：改用解析后的 `llm_config.model_name` 与 **`resolve_context_window(llm_config.model_name, db_window=llm_config.max_context_window)`**（db_window 由 `LLMRuntimeConfig.max_context_window` 提供，§6.1；两段解析共用，无重复查询）。
- **`context_window_total` 语义（turn 级选择引入后的任务级口径）**（审查修订）：`tasks_api.py:62`（`TaskResponse.context_window_total`）原按 `profile.model_name` 计算，turn 级显式选择模型后该值可能与实际不一致——改为按 **该 task 最近一次 turn 的 `model_name`** 计算（首 turn 即 task 创建透传值），无 turn 时回退 `profile.model_name`；`ContextUsageMeter` 无 `db_window` 的调用维持 `resolve_context_window(model_name)` → ModelCatalog 兜底（§6.2 建议 1）。
- **① 预解析落点（双路径，审查修订）**：
  - **既有 task 追加 turn**：`turn_service.create_turn`（`turns_api.py:98` 入口）创建 turn 时调用 `ModelResolverService`，`final_model_name` 写入 `TurnRecord.model_name`（§5.3）；拒绝即 422，不入队不落库。
  - **新 task 首 turn**（已核实 `task_service.create_task_with_initial_turn` 直连 `turn_crud.create`、绕过 turn_service）：① 在 `create_task_with_initial_turn` 内、**task 与 turn INSERT 之前**调用 `ModelResolverService`——失败即 422，不产生孤儿 task、无需补偿；成功后透传 `model_name` 至首 turn 落库。
  - 两路径均满足「先解析后落库」，与 §6 流程图承诺一致。
- **日志级别**：两路径 ① 拒绝均写 **`warn` 级 `model_resolve_rejected`**（turn/task 上下文、请求 model、原因；业务拒绝非系统异常），与 422 响应同出；运行期兜底失败为 `error` 级 `model_resolve_failed`（§6），两级不混用。
- **child 预解析 profile 获取**（建议采纳）：child profile 经 `get_agent_registry`（`app/config/configuration.py`）解析，`service → config` 为合法方向；`workflow.run()` 回写端口经既有的 `TurnRunner`/operations 注入取 `turn_service`，不新增全局访问。
- ② 运行期兜底若因配置竞态修正模型名：`workflow.run()` 经 **`turn_service.update_model_name(turn_id, final)` 服务端口回写**（core → service 合法方向，core 不直写 storage）；**回写补 info 日志 `turn_model_updated`**（旧名→新名、turn 上下文），保证「运行期模型被修正」可排查。
- child 委派子 Agent（reviewer/analyst/tester/coder）同走此链：无前端选择，Auto 语义即 `profile.model_name`；`delegation_executor` 在 `create_child_task` **之前**执行 ① 预解析（解析结果随 `turn_service.create_turn` 写入 child turn），保证 D11 对 child 成立（不落 NULL），且失败不产生孤儿 child task（见下）。
- **child 预解析失败出口**（修订，闭合 422 无 HTTP 上下文缺口）：child 预解析发生在 `workflow.run()` 运行期内部、无 HTTP 请求上下文，不存在 422 出口——child 预解析失败（模型未收录/Key 未配置）→ **child task/turn 均不创建**（① 前置在 `create_child_task` 之前，无孤儿任务），delegation 记录置 failed，结果归入父 turn 的 `DelegationResult(status="failed")`，**不影响父 turn 其余流程**；与 D12「错误等级不混同」兼容（child 本就走运行期终态语义，不产生携带 RUN_FAILED 的 child turn）。
- **child ① 日志事件名**（审查修订）：child 预解析失败是「创建期拒绝」而非「运行期竞态」，拒绝点日志用 **`warn` 级 `model_resolve_rejected` + data 标注 `child=true`**（复用父路径事件名，data 区分阶段），不用 `error` 级 `model_resolve_failed`，避免与运行期兜底同名同级别导致排查误判阶段。**但 child 预解析失败最终表现为一次失败的委派**（delegation 置 failed、父 turn 收 `DelegationResult(status="failed")`，无 child turn 承载 RUN_FAILED），仅 warn 不足以支撑运行期失败排查——child 委派收口处（`delegation_executor` 归并 failed 结果时）需**再记一条 `error` 级 `child_delegation_model_resolve_failed`**（含 child task/turn 上下文、模型名、拒绝原因），与拒绝点 warn 事件成对出现，保证「child 预解析失败」可按 error 级检索；该 error 记录与本小节「child 预解析失败出口」互补——前者记录失败原因、后者记录终态语义。

## 7. 模型自动发现（D3：统一 litellm model list）

### 7.1 流程

```
POST /providers/{id}/discover
  → litellm 模型目录（model_list / get_model_cost_map，按 type 前缀过滤）
  → 候选列表：{model_name, display_name, max_context_window?, supports_thinking?, already_imported}
  → 前端勾选 → POST /providers/{id}/models 批量入库
```

**日志与失败语义**（审查修订）：discover 属外部依赖调用，必须可排查——
- 成功：info 日志 `provider_discover_succeeded`（provider_id、type、候选数、耗时）；
- 失败：error 日志 `provider_discover_failed`（provider_id、type、litellm 异常类型/信息、耗时、是否可重试）；API 返回 502 级「目录读取失败」+ 引导重试，不静默返回空列表。
- providers/models 的关键状态变更（创建/更新/删除/导入）一律 info 日志（`provider_created` / `provider_updated` / `provider_deleted` / `models_imported`），沿用 `app.config.logging.logger.log` 单例 + 稳定 snake_case event。

### 7.2 type → litellm 前缀 / 默认 base_url 映射（内置预设）

| type | 前缀过滤 | 默认 base_url |
|---|---|---|
| deepseek | `deepseek/` | `https://api.deepseek.com` |
| openai-compatible | `openai/` | 留空（用户填） |
| anthropic | `anthropic/` | `https://api.anthropic.com` |
| ollama | `ollama/` | `http://localhost:11434` |
| custom | 不限 | 留空 |

- `custom` 类型不按前缀过滤，discover 返回全目录由用户搜索勾选。
- **入库前必须勾选**（默认决策 D6）：litellm 目录数千条，不全量导入；候选带 litellm 已知上下文窗口信息预填，用户可改。

### 7.3 Ollama 边界

- 走 litellm 目录（D3），但 litellm 目录 =「litellm 支持的所有 ollama 模型」，≠「本机已 pull 的模型」。文档明确：ollama 场景**推荐手动添加实际已 pull 的模型**（模型名即 `ollama/模型名`）；discover 结果仅作候选参考。如需本地 `ollama list` 探测属后续增强（本期不做）。

### 7.4 「从 .env 一键导入 DeepSeek」引导（增强，建议做）

- 首次打开配置中心且 providers 为空时，检测 `DEEPSEEK_API_KEY` / 旧 `CODING_AGENT_MODEL_*` 环境变量，提供「一键导入 DeepSeek 官方厂商 + deepseek-v4-flash 模型」按钮，降低既有用户迁移成本。
- 与 D4 不冲突：导入后即「有厂商」，未导入时发送前照常报错引导。

## 8. API 契约

### 8.1 Provider CRUD（`providers`，无 `/api` 前缀，与既有 `workspaces`/`tasks` 路由一致）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/providers` | 厂商列表（聚合 models、`api_key_configured`、`enabled`） |
| POST | `/providers` | 新建厂商（`name/type/base_url/api_key_env/enabled`） |
| PUT | `/providers/{id}` | 更新（启停即时生效，下次请求重新解析） |
| DELETE | `/providers/{id}` | 删除（级联删 models） |
| POST | `/providers/{id}/discover` | litellm 目录发现候选（§7） |

### 8.2 Model 管理（`models`，无 `/api` 前缀）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/models` | **启用模型扁平列表**（下拉数据源，按厂商分组；含 provider 名、窗口、thinking、`api_key_configured`） |
| POST | `/providers/{id}/models` | 单条/批量导入（discover 勾选或手动添加） |
| PUT | `/models/{id}` | 更新（display_name/窗口/采样/thinking/enabled/排序） |
| DELETE | `/models/{id}` | 删除 |

### 8.3 请求模型扩展（D1 配套）

- **模型选择作用于 turn 级，经两个入口透传**（审查修订，闭合「`POST /tasks` 自动建首 turn」事实）：
  - `CreateTurnRequest` 增加 `model_name: str | None`（None = Auto）——**既有 task 追加 turn** 的入口；
  - `CreateTaskRequest` 增加 `model_name: str | None`（None = Auto）——**新 task 场景**：已核实 `workspaces_api` 的 `POST /tasks` 经 `task_service.create_task_with_initial_turn` **自动创建首个 pending turn**，前端不另发 turn 请求，故 `model_name` 必须随 task 请求透传给首 turn（否则显式选择的模型在首 turn 无通道，D4/D11 对首 turn 落空）。
  - 两入口共用同一字段，首 turn 与非首 turn 语义一致。
- 改造清单：`CreateTaskRequest` / `CreateTurnRequest` + `shared/ts/task.ts` / `api.ts` 同步（`events.ts` 为生成物，本次无事件变更，不触发生成器）；`workspaces_api` / `task_service.create_task_with_initial_turn` 透传 `model_name` 至首 turn（§12）。

### 8.4 错误语义

- `GET /models` 返回空列表 ≠ 错误：前端据此驱动空态引导。
- **两段式错误等级**（审查修订）：
  - ① service 创建期预解析未命中 → **HTTP 422**（`ModelNotConfiguredError`），错误体含「模型名 + 修复指引」，不入队不落库；**child turn 无 HTTP 上下文，例外出口见 §6 流程图/§6.4**（delegation 置 failed 而非 422）；
  - ② workflow 运行期兜底解析未命中（配置变更竞态）→ **RUN_FAILED 终态事件**，非 HTTP 错误；
  - 二者**永不混同**：创建期失败前端可即时引导，运行期失败走既有失败终态展示。
  - **错误体指引差异**（建议采纳）：「模型未收录」指引「去配置中心添加/导入该模型」；「Key 未配置」指引「在 .env 配置对应环境变量后重启/刷新」，二者文案不同，避免误导。
- Provider 保存**不校验** `api_key_env` 环境变量存在（允许先配置后填 Key）；`GET /providers` / `GET /models` 返回 `api_key_configured` 供前端状态展示与发送前校验（D4 兜底）。
- **`api_key_configured` 语义**（审查修订）：无 `api_key_env` 的 provider（ollama/custom 等，本不需 Key）→ 恒为 `true`（「无需 Key」）；仅「配置了 `api_key_env` 但 `os.environ` 未命中」→ `false`。前端只对 `false` 拦截，避免对无需 Key 的厂商误报「环境变量 X 未配置」。

## 9. 发送前校验（D4）

触发点：InputBar 发送按钮点击——**turn 创建前**。新 task 场景：模型选择随 `CreateTaskRequest.model_name` 经 `create_task_with_initial_turn` 透传首 turn，校验发生在 `POST /tasks` 请求发出前；既有 task 追加 turn 场景：校验发生在 `POST /tasks/{id}/turns` 请求发出前（两入口均满足 D4「发送前校验」，§8.3）。

1. **本地模型缓存校验**：前端启动时 `GET /models` 拉取启用模型缓存（taskStore）。缓存为空 → 拦截发送，内联提示「未配置任何模型，请先在配置中心添加模型厂商」，并打开 `ProviderSettingsDialog`。
2. **Auto 语义校验**：选中 Auto 时，校验 Agent 默认模型 `profile.model_name` 是否在缓存中——**直接复用 `AgentProfileResponse.model_name` 既有字段**（审查修订：不再新增 `default_model_name`，避免同数据双字段）；不在 → 拦截并提示「该 Agent 默认模型未配置，请选择模型或配置厂商」。
3. **Key 校验**：仅当目标模型 provider「配置了 `api_key_env` 且 `api_key_configured=false`」时拦截并提示「环境变量 X 未配置」；`api_key_env` 为空的 provider 跳过此校验（§8.4）。
4. **后端兜底**：① 创建期预解析（§6.4）与前端校验结果天然一致，API 错误透传为明确引导文案；② 运行期竞态由 RUN_FAILED 终态承载，不做发送后无声失败。
5. **缓存刷新**：`ProviderSettingsDialog` 保存/删除成功后刷新 taskStore 模型缓存（重新 `GET /models`），保证校验与下拉即时反映配置变更，避免误拦截/误放行。

## 10. 前端 UI 设计（impeccable 风格）

### 10.1 `ModelSelector`（输入栏，AgentSelector 右侧，同排 h-7）

- 基于 **cmdk**（shadcn Command 底座）+ 现有 Popover。
- 分组列表：按厂商分组，组头厂商名；首项固定「Auto · 跟随 Agent 默认」。
- 行内信息：`display_name` + 厂商 badge + 上下文窗口（`128k`/`1M`）+ thinking 标识徽标（推理模型）。
- 键盘优先：`↑↓` 导航、输入即过滤、`Enter` 选中；空态给「暂无可用模型，点击配置」引导。
- 底部固定 footer：「配置模型 / 管理厂商」→ 打开 `ProviderSettingsDialog`。
- 选中态 check 标记 + 高亮；视觉权重低于输入框，不抢主操作。

### 10.2 `ProviderSettingsDialog`（配置中心）

- 厂商列表卡片：启用 switch（即时生效）、模型数量、base_url、`api_key_configured` 状态徽标（未配置 Key 显示告警）、编辑/删除（二次确认）。
- 新增/编辑表单（Dialog + react-hook-form + zod）：
  - 名称、类型（预设 5 类 + 自定义）、base_url；
  - `api_key_env`：环境变量名输入 + 「检测是否已设置」按钮（读 `GET /providers` 的 `api_key_configured`）；附 .env 配置说明文案（**不提供写 .env 能力**，默认决策 D9，避免破坏用户文件）；
  - **「测试并发现模型」** → `POST /providers/{id}/discover` → 候选列表勾选导入（预填窗口/thinking，可改）；
  - 手动添加模型行（model_name/display_name/窗口/thinking/采样参数）。
- 首次打开且无厂商：展示「从 .env 一键导入 DeepSeek」引导（§7.4）。

### 10.3 InputBar / store 集成

- 布局：`[AgentSelector] [ModelSelector] [输入框] [发送]`。
- `taskStore` 增加 `selectedModelName`（`localStorage` 持久化最近选择）；`useTask` 创建 task/turn 时透传 `model_name`（Auto 时传 `null`）。
- 时间线（TurnTimeline）在 turn 元信息展示「所用模型」，来源为 `turns.model_name` 落库字段（解析后的实际模型，§5.3）；**`model_name` 为 NULL（升级前历史 turn）显示「—」占位**——NULL 仅表示「当时未落库」，**不显示「Auto」**（Auto 轮次已落解析结果、非 NULL，D11；避免误导历史 turn 走了 Auto 语义但未解析）。
- Auto 语义校验复用 `AgentProfileResponse.model_name` 既有字段（审查修订：不新增 `default_model_name`）。

## 11. 开源依赖选型（第零铁律：复用成熟，不造轮子）

| 层 | 依赖 | 用途 | 版本策略 |
|---|---|---|---|
| 前端 | `cmdk`（shadcn Command 底座，成熟搜索菜单） | ModelSelector 搜索式选择器 | 锁定 |
| 前端 | `react-hook-form` + `zod` | 厂商/模型表单校验 | 锁定 |
| 前端 | shadcn `command/dialog/select/switch/form`（补齐现有 ui 库） | 配置中心 + 选择器基础件 | 已有 shadcn 体系，直接补齐 |
| 后端 | **零新增**：discover 复用 litellm（已有） | 模型目录发现（D3） | — |

> 说明：原方案候选 `keyring`（API Key 加密存储）因 D2 决策（.env）**不再引入**，后端保持零新增依赖。
>
> **litellm 依赖边界**（建议采纳）：litellm 仅存两处入口——`core/llm/factory`（ChatLiteLLM 构建）+ `service/provider/provider_discover_service`（模型目录发现）；其余模块不直接 import litellm，防依赖四处散落。

## 12. 目录结构规划（新增/变更）

```text
apps/backend/app/
  core/llm/
    factory.py                # 改：接受 LLMRuntimeConfig，保留旧入口；Key 读取/缺失校验唯一收口
  core/delegation/
    delegation_executor.py    # 改：入口处（create_child_task 之前）执行 ① 预解析（Auto = profile.model_name），失败 → 不建 task/turn、delegation 置 failed、父收 DelegationResult(failed)（§6.4）
  models/
    llm_runtime_config.py     # 新增：LLMRuntimeConfig 值对象（from_profile / from_model_entry / max_context_window），models 层（§6.1）
    provider_record.py        # 新增：ProviderRecord 值对象
    model_entry_record.py     # 新增：ModelEntryRecord 值对象（models 表对应）
  storage/
    model/
      provider_model.py       # 新增：providers ORM
      model_entry_model.py    # 新增：models ORM
    crud/
      provider_crud.py        # 新增：providers 增删改查
      model_entry_crud.py     # 新增：models 增删改查（含按名查启用模型，供 resolver）
  service/
    llm/
      model_resolver_service.py   # 新增：resolve(profile, requested_model) → LLMRuntimeConfig；ModelNotConfiguredError
    provider/                    # 新增：厂商/模型领域服务
      provider_service.py        # 仅 CRUD + api_key_configured 状态聚合（单职责）
      provider_discover_service.py # 新增：litellm 目录读取 + 前缀过滤 → 候选列表（外部交互独立成文件）
      model_entry_service.py     # 模型 CRUD + 批量导入
  api/
    providers_api.py          # 新增：/providers + /providers/{id}/discover
    models_api.py             # 新增：/models
    schemas/request/  ProviderCreateRequest.py / ProviderUpdateRequest.py / ModelBulkImportRequest.py
    schemas/response/ ProviderResponse.py / ModelEntryResponse.py / ModelCandidateResponse.py
    schemas/request/  CreateTaskRequest.py / CreateTurnRequest.py   # 改：+ model_name（§8.3，task 透传首 turn）
    schemas/response/ WorkspaceResponse.py / TaskResponse.py        # 改：如需在首 turn 元信息暴露 model_name 时
    api/workspaces_api.py  # 改：POST /tasks 透传 model_name → task_service.create_task_with_initial_turn → 首 turn（§8.3）
    api/tasks_api.py       # 改：TaskResponse.context_window_total 改用最近 turn 的 model_name 计算（§6.4/问题2）
```

- 命名统一（审查建议）：models 表相关文件一律 `model_entry_*`（`model_entry_record` / `model_entry_model` / `model_entry_crud` / `ModelEntryResponse` / `model_entry_service`），避免与 `models/` 目录、Python `models` 模块名混淆。
- `api/schemas` 沿用 PascalCase 文件名（`CreateTaskRequest.py` 等），属 **AGENTS.md 既有已知偏差 D12**（api schemas PascalCase 命名，与本文档 §15 的 D12「解析错误等级」**撞号、两套独立编号体系**），本次新增文件一并遵循，待该偏差统一解决（不在本方案范围内）。
- **装配步骤**（审查修订，落实项目约定）：新增 service 在 `service/depends.py` 注册单例（`get_provider_service` / `get_provider_discover_service` / `get_model_entry_service` / `get_model_resolver_service`），并**同步加入 `reset_service_dependencies` 的 `cache_clear()` 清单**（测试切库依赖）；`app/app.py` **模块级**（`app` 单例定义后）`importlib.import_module("app.api.providers_api")` / `importlib.import_module("app.api.models_api")` 触发路由注册（网关约定，不能用 `import app.api.xxx`）；lifespan 内完成 service 依赖初始化。

- `turn_crud.py` / `TurnModel` / `TurnRecord`：加 `model_name`（§5.3）。
- `workflow.py` / `context_window_resolver` 调用点：改用解析结果（§6.4）。
- `shared/ts/task.ts` / `api.ts`：加 `model_name`。
- 前端：`components/chat/ModelSelector.tsx`、`components/settings/ProviderSettingsDialog.tsx`（新目录 `components/settings/`）、`stores/taskStore.ts`、`hooks/useTask.ts`、`components/layout/InputBar.tsx` 集成、`TurnTimeline.tsx` 展示。

## 13. 分阶段实施计划

| Phase | 内容 | 验收 |
|---|---|---|
| 1 数据层 | 建表（providers/models）+ CRUD + 值对象 + turns 加列迁移 | 单测覆盖建表/CRUD/级联删除/加列迁移 |
| 2 解析链 | `LLMRuntimeConfig` + `ModelResolverService` + `factory`/`workflow` 改造 + `ModelNotConfiguredError` + 首 turn 透传（`workspaces_api`/`create_task_with_initial_turn`）+ `delegation_executor` 入口 ① 预解析（§6.4，create_child_task 之前）+ `define_agents.py` 移除 `model_settings`（D16） + `tasks_api` context_window_total 改最近 turn 口径 | 单测：命中/未命中/Auto 语义/缺 Key 构建期报错；child 预解析命中（child turn 落解析模型）/未命中（delegation 置 failed + 父收 DelegationResult(failed)、无孤儿 task/turn）；回归既有模型构建 |
| 3 API | providers/models/discover 端点 + 请求模型扩展 + `api_key_configured` 聚合 + **depends.py 装配 + app.py 路由注册** | API 测试 + 错误语义（422 区分 500）+ 装配/路由可启动验证 |
| 4 前端选择器 | 补 shadcn 组件 + `ModelSelector` + store/useTask/InputBar 集成 + 发送前校验 | vitest：Auto/显式/空态/Key 缺失拦截路径 |
| 5 前端配置中心 | `ProviderSettingsDialog` + discover 勾选导入 + .env 导入引导 + 时间线展示 | vitest + 手工验收全链路 |
| 6 闭环 | 独立审查 Agent + 独立测试 Agent 全量过 | 审查「符合」+ 测试全绿 |

**验收通项**（审查建议采纳）：每个 Phase 新增/修改的函数均须有完整 docstring（参数/返回/异常/副作用），且随签名变更同步更新，纳入审查 Agent 检查项。

## 14. 风险与边界

- **迁移影响**：升级后首次使用需先配厂商（D4 有意为之）；用 §7.4「.env 一键导入」缓解迁移摩擦。
- **litellm 目录 vs 实际可用性**：目录 =「litellm 支持」，≠「厂商/本地实际提供」（尤其 ollama，§7.3）。discover 仅候选，最终以请求期结果为真。
- **ModelCatalog 与 DB 窗口双源**：`resolve_context_window(model_name, db_window)` 以 DB `max_context_window` 优先，ModelCatalog 仅在「模型不在 models 表」（env 直配/测试路径）时兜底；两处不一致以 DB 为准。
- **turns 只落 model_name**：厂商配置变更不影响历史可读性；不做 provider 快照（D7）。
- **并发**：provider/models 为纯配置读多写少，与既有「task 间 turn 并发」无共享冲突；配置变更即时生效，无需缓存失效机制（每次解析实时查 DB，天然一致）。
- **配置变更竞态**（审查建议补）：运行中 task 正使用的 provider/model 被删除/禁用 → 该 turn 已解析完成、不受影响；**新 turn 经①创建期预解析报 422**（配置变更即时生效语义，见 §6 两段式）。
- **不引入写 .env 能力**：避免破坏用户文件；仅校验 + 引导（D9）。
- **discover 外部依赖失败**：litellm 目录读取异常/超时 → `provider_discover_failed` error 日志 + 502 语义，不静默返回空列表（§7.1）。

## 15. 决策记录

> **编号消歧**：本文档 D1–D16 为**本方案独立编号**，与 AGENTS.md / 目录组织规范中的偏差编号 D1–D15（如 D12 = api schemas PascalCase 命名偏差）**非同一体系**；引用本项目既有偏差时一律带限定词（如「AGENTS.md 偏差 D12」）。

### 已确认（用户拍板）

| 编号 | 决策 |
|---|---|
| D1 | turns 落库 model_name |
| D2 | API Key 存 .env（api_key_env 引用） |
| D3 | 模型自动发现统一 litellm model list |
| D4 | 无厂商/模型时发送前校验报错（前端拦截 + 后端兜底） |

### 默认决策（本方案给出合理默认，未单独拍板；用户有异议可随时推翻）

| 编号 | 默认 |
|---|---|
| D5 | `.env` 的 `CODING_AGENT_MODEL_*` 不再作模型来源，DB 为唯一事实来源；仅 `DEEPSEEK_API_KEY` 保留为 Key 来源 |
| D6 | discover 候选需勾选入库，不全量导入 |
| D7 | turns 只落 model_name，不落 provider_id |
| D8 | 不预置内置厂商；首次使用经配置中心引导（含「从 .env 一键导入 DeepSeek」） |
| D9 | 配置中心不提供写 .env 能力，仅校验环境变量是否已配置 + 引导文案 |
| D10 | Agent 默认模型保持 `AgentProfile.model_name` 字符串，Auto 语义解析时查 DB，未收录则校验报错 |
| D11 | turns.model_name 落**解析后的实际模型**（Auto 轮次写命中 DB 的解析结果，不落 NULL），保证时间线可追溯 |
| D12 | 解析分两段：service 创建期预解析（有 HTTP 上下文 → 422）+ workflow 运行期兜底（RUN_FAILED），错误等级不混同；child turn 无 HTTP 上下文，创建期预解析失败与运行期兜底同为**运行期终态语义**——child task/turn 均不落库（预解析前置在 `create_child_task` 之前）、delegation 置 failed、父 turn 收 `DelegationResult(status="failed")`，不产生携带 RUN_FAILED 的 child turn（§6 流程图/§6.4） |
| D13 | `LLMRuntimeConfig` 只携带 `api_key_env` 不承载 Key 明文；Key 实际读取与缺失校验唯一收口在 `factory.build_chat_model` 构建期（`ValueError` → ② RUN_FAILED）；创建期 ① 仅经 `provider_service.api_key_configured` 做存在性检查（配置检查 → 422），两者分属「配置检查 vs 构建校验」互不冲突 |
| D14 | `CreateTaskRequest` 增加 `model_name` 并透传首 turn（`create_task_with_initial_turn`）：`POST /tasks` 自动建首 turn，前端不另发 turn 请求，首 turn 的模型选择必须有通道（§8.3/§2） |
| D15 | `LLMRuntimeConfig` 承载 `max_context_window`（`from_model_entry` 取自 DB），供 `resolve_context_window` 的 `db_window` 使用，DB 窗口永不落空（§6.1/§6.4） |
| D16 | `AgentProfile.model_settings` 不再作为运行时事实源（DB model 行为唯一）；`define_agents.py` 移除 5 处 `model_settings` 参数，`ModelSettings`/`from_profile` 仅保留 env 直配兼容路径（测试/未迁移） |
