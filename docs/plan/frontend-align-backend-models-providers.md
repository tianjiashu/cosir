# 前端对齐后端改造方案：模型选择器与厂商配置

> 状态：提案（待独立审查子 Agent 闭环验收）
> 约束：后端为契约唯一事实源，**不改后端任何文件**；以第零铁律（长期稳定迭代为尺）为准绳，以 Impeccable 前端设计理念（用户体验优先、诚实状态、不制造噪音、不重复造轮子）为落点。
> 事实来源：代码探查（code-explorer 子 Agent，2026-08-28）+ 后端路由全目录搜索 + `ModelEntryResponse.py` / `models_api.py` / `providers_api.py` 逐文件读取。

---

## 一、现状与核心矛盾（基于代码事实）

### 1.1 后端真实现状（不动）

| 端点 | 方法 | 后端现状 | 证据 |
|---|---|---|---|
| `/models` | GET | ✅ 唯一模型数据源，返回 `provider_id(int)/provider_name/model_name/supports_thinking/supports_image/supports_video/enabled/api_key_configured/reasoning_effort/sort_order` | `models_api.py:18,48-59` |
| `/providers` | POST | ✅ 新建厂商 | `providers_api.py:20` |
| `/providers/{id}` | PUT/DELETE | ✅ 启停/删除 | `providers_api.py:61,104` |
| `/providers/{id}/test` | POST | ✅ 连通性测试 | `providers_api.py:129` |
| `/providers` | GET | ❌ 不存在 | 全目录搜索 0 结果 |
| `/providers/{id}/discover` | POST | ❌ 不存在 | 无路由 |
| `/providers/{id}/models` | POST | ❌ 不存在 | 无路由 |
| `/models/{id}` | PUT/DELETE | ❌ 不存在 | `models_api.py` 仅 1 个 GET |
| `/agents` | GET | ❌ 已删除，无 HTTP 出口 | 无 `agents_api.py` |

### 1.2 前端真实矛盾（探查证据）

1. **ModelSelector 残缺可见**（体验硬伤，非降级问题）：
   - React key 用 `model_id`（`ModelSelector.tsx:193`）→ 后端不返回 → `key=undefined`，多模型时 React 告警/复用错乱。
   - 展示名用 `display_name`（`ModelSelector.tsx:199`）→ 后端不返回 → 空字符串。
   - 窗口徽标用 `max_context_window`（`ModelSelector.tsx:201`）→ 后端不返回 → 显示空/0。
   - 结论：下拉「能打开但每行空白、key 错乱」——这是必须修复的**数据契约断裂**，不是体验取舍。

2. **厂商配置 UI 露死按钮**：`ProviderSettingsDialog:73` 调 `GET /providers`（必 404 红字）、`ProviderModelsSection` 的 discover/import 按钮调不存在端点（必 404 静默错误）。用户点击得到「失败」却无任何可用路径。

3. **agentStore / AgentSelector 是 0 字节空文件**，无源码引用（仅测试注释提到）。用户确认前端不需要，直接删。

4. **`workspace_id` 类型错位**：`CreateTaskRequest.workspace_id: number`（`api.ts:38`）但前端 store/参数全为 `string`（`workspaceStore.ts:53`、`taskStore.ts:316-318` 等）。运行时靠 JSON 序列化兜底，属隐性技术债。

5. **协议文件类型错配（生成物，本方案不手改）**：`events.ts:1-8` 已正确声明「由 `scripts/generate_runtime_event_ts.py` 从后端 Pydantic payload 生成，勿手改」；`model.ts:4-6` 是另一份手写文件，二者不矛盾。`events.ts` 当前把 `workspace_preparing/ready/degraded` 塞进 `RuntimeEvent`（强制 `task_id`），而后端 workspace 事件走独立 `WorkspaceEvent`（无 `task_id`）→ **真实类型错配**。但该文件是生成物（AGENTS.md §八禁止手改），且本方案约束「不改后端」——二者构成死锁，故本方案**不处理** events.ts/logs.ts 的生成物修正，相关错配登记于 §3.8 并移交「后端契约修订」议题。

---

## 二、设计原则（第零铁律 + Impeccable）

1. **后端是事实源，前端适配而非假设**：凡后端无端点/无字段，前端不臆造、不挂死按钮、不崩溃。
2. **无 UI 降级，体验优化优先**：`GET /models` 是「已配置厂商的可用模型」**唯一且充分的体验数据源**。围绕它的真实字段做**体验增强**（清晰分组、唯一 key、可读展示名、能力徽标），而非「降级隐藏」。信息本就不存在的（如窗口大小）则**诚实省略**，不显示伪造的 0。
3. **不重复造轮子 / 不引依赖**：用现有 cmdk/Popover 底座、现有 store 结构、现有 token；档位名复用后端 `effort_map` 原始键（已在上一轮落地）。
4. **单一职责 / 目录清晰**：契约收敛到 `shared/ts/model.ts`（手写、明确人工对齐）；删除空文件与死 API 封装减噪音。
5. **可排查**：配置失败、连通性失败有清晰面向用户的错误文案与日志，不静默吞错。

---

## 三、改造内容（聚焦、可审查）

### 3.1 契约收敛 `shared/ts/model.ts`（手写文件，安全修改点）

> 证据：`GET /models`（`models_api.py:48-59`）仅投影 `provider_id/provider_name/model_name/supports_thinking/supports_image/supports_video/enabled/api_key_configured/reasoning_effort/sort_order`；`ModelEntryResponse.py:39` 中 `provider_id: int`。`model.ts` 头部（`model.ts:4-6`）明言手写、人工对齐，是安全修改点。

- **`ModelEntryRecord`**：删除后端 `GET /models` 不返回的字段 `model_id` / `display_name` / `max_context_window` / `temperature` / `top_p` / `max_tokens` / `created_at` / `updated_at`。保留并明确后端真实字段：`provider_id: number`（由 string 改 number，对齐 `ModelEntryResponse.py:39`）、`provider_name` / `model_name` / `supports_thinking` / `supports_image` / `supports_video` / `enabled` / `api_key_configured` / `reasoning_effort` / `sort_order`。
- **注记**：后端 `ModelEntryService` / `ModelEntryRecord` 模型表**存在** `max_context_window` 字段（`model_entry_record.py`、`model_entry_service.py:184`），但 `GET /models` 端点不投影它，故前端不消费——属「端点不返回」而非「后端无此能力」，避免后续维护者误引。
- **`ProviderRecord.provider_id`**：`string` → `number`（对齐后端 int）。`ProviderCreateRequest`/`ProviderUpdateRequest` 入参保持 `name/type/base_url/api_key/enabled/sort_order`（后端 `POST /providers` 真实接收），`model_count/created_at/updated_at` 等响应聚合字段保留（若 `GET /providers` 前端不再调用，则 `ProviderRecord` 仅作 `POST` 回参参考，可缩为最小集）。
- **删除死契约**：`ModelCreateRequest`（含 `temperature/top_p/max_tokens?`，后端不接收）、`ModelBulkImportRequest`、`ModelUpdateRequest`、`ModelCandidate`、`ModelImportResult` —— 均绑定不存在的 `discover/import/PUT|DELETE /models/{id}` 端点，前端无 UI 调用，整组删除。
- **`ReasoningEffortInfo`**：保留（已对齐后端，上轮落地）。
- 同步 `apps/shared/ts/model.ts` 顶部注释：明确「本文件手写对齐 `ModelEntryResponse` 真实字段；`GET /models` 为唯一数据源，不假设未返回字段」。

### 3.2 路径常量 `MODEL_PROVIDER_PATHS`（`model.ts:16-24`）

- 保留 `PROVIDERS` / `PROVIDER_DETAIL` / `PROVIDER_TEST` / `MODELS`（对应后端存在端点）。
- **删除** `PROVIDER_DISCOVER` / `PROVIDER_MODELS` / `MODEL_DETAIL`（无后端端点，避免诱惑后续开发者误用）。

### 3.3 `services/api.ts` 删除死封装

- 删除 `listProviders`（`GET /providers` 404）、`discoverProviderModels`、`importProviderModels`、`updateModel`、`deleteModel`（绑定不存在端点）。
- 保留 `createProvider` / `updateProvider` / `deleteProvider` / `testProviderConnection` / `listModels`。

### 3.4 `ModelSelector.tsx` —— 体验增强（核心）

围绕 `GET /models` 真实字段重做渲染（**增强而非降级**）：

1. **唯一 key**：`key={model.model_name}`（模型路由名全局唯一，天然稳定 key，比臆造 id 更可靠，符合第零铁律「不重复造轮子」——直接用后端给的唯一路由名）。
2. **展示名**：优先 `model_name` 去前缀美化（去掉 `provider/` 前缀部分）作为可读标签；不依赖不存在的 `display_name`。**跨厂商重名兜底**：若去前缀后的标签在同一 `provider_name` 分组内出现重复（如 `openai/gpt-4o` 与 `azure/gpt-4o` 去前缀均为 `gpt-4o`），回退展示完整 `model_name`（或 `provider_name/model_name`），保证可读且可区分——避免两行标签相同致用户无法抉择。
3. **分组**：按 `provider_name` 分组（后端已返回 `provider_name`，无需 `GET /providers` 即可分组展示——这是「配置即分组可见」的体验落点，直接满足用户「配置后的厂商模型即可见」意图）。**可见性边界（诚实告知，非降级）**：`GET /models` 仅返回「已启用且 `api_key_configured=true`」的厂商模型（`models_api.py:39` `list_providers(enabled=True)` + `api_key_configured` 过滤），故未启用或未配 Key 的厂商模型**天然不出现在任何 UI**；这是后端数据契约的诚实呈现，不是 UI 隐藏，UI 文案须明确提示「启用并配置 Key 后模型方出现在选择器」。
4. **能力徽标**：`supports_thinking` → Sparkles；`supports_image` → 图片徽标；`supports_video` → 视频徽标（后端已返回，新增展示，体验增强）。
5. **推理强度**：`reasoning_effort?.supported` 为真时渲染档位行（上轮已落地，保留）。
6. **窗口徽标**：后端 `GET /models` 响应不投影 `max_context_window`（后端 `ModelEntryService` 模型表确有该字段，但 `ModelEntryResponse` 未投影，见 §3.1 注记）→ 前端**诚实省略**窗口大小徽标（不显示伪造 0、不显示占位）。这是「信息不存在则不说」的诚实设计，非降级，且勾稽于后端契约（非前端漏实现）。
7. **启用态**：`enabled=false` 或 `api_key_configured=false` 的模型，行内以 muted 样式 + 角标「未启用/未配置 Key」呈现（引导用户去厂商配置），而非直接隐藏造成「配置了却看不见」的困惑。本段「看得见」特指**配置清单层（providerConfigStore）中已被用户配置的条目**；模型选择层（GET /models 子集）未必可见属预期，详见 §3.5 两类列表职责边界。
8. 折叠态标签：`model_name`（去前缀）+ 档位后缀（若选），保持紧凑 `max-w-selectorLabel` token。

### 3.5 `ProviderSettingsDialog` / `ProviderModelsSection` —— 配置即生效 UX

- **保留可用路径**：新建（`POST /providers`）、启停/编辑（`PUT /providers/{id}`）、删除（`DELETE /providers/{id}`）、测试连通性（`POST /providers/{id}/test`，补一个「测试连接」按钮，后端已支持，当前未渲染）。
- **移除不存在端点依赖**：删除 `GET /providers` 列表加载（打开对话框不再 404）；删除 `discover` / `import` 按钮与 `ProviderModelsSection` 组件（后端无，露死按钮违反 Impeccable）。
- **已配置厂商列表（关闭刷新态丢失与信息盲区）**：厂商创建/编辑/删除成功后，**将回参写入独立持久化 store `providerConfigStore`**（新建 `apps/desktop/src/stores/providerConfigStore.ts`，localStorage 键 `coding-agent.configuredProviders`，参照 `taskStore` 的 `persistSelectedModelName` 单出口模式；**不并入 agentStore**——agentStore 为空文件将删除）。`ProviderSettingsDialog` 直接读 `providerConfigStore` 渲染「已配置厂商清单」——**不依赖 `GET /providers`**（后端无此端点），也不依赖易失内存正向态（刷新/重启后仍可见）。
- **两类列表职责边界（消除交叉处信息盲区）**：
  - **配置清单**（来源 `providerConfigStore`，本地全量）：展示用户「已配置的所有厂商」（含未启用、未配 Key），供配置管理。
  - **模型列表**（来源 `GET /models`，后端子集）：仅展示「已启用且 `api_key_configured=true`」厂商下的模型，即 ModelSelector 实际可选模型。
  - 二者天然可能不一致（清单有某厂商但该厂商模型不在 ModelSelector）——这不是 bug，是后端数据契约的诚实分层。UI 须在配置清单中对该厂商标注「未启用/未配 Key，模型未出现在选择器」，使「配置了却看不见模型」从困惑转为可解释状态，闭合 §3.4.7 的「看得见」语义（特指配置清单层，非模型选择层）。
- **配置即生效引导文案**：对话框说明改为「配置厂商并启用、且配置 Key 后，其模型将自动出现在模型选择器；未启用或未配 Key 的厂商不出现在选择器」——把「无列表端点」转化为清晰配置闭环，而非错误红字。测试连接结果用 `ProviderConnectionTestResult`（`success/elapsed_ms/error_message`）做即时反馈。

### 3.6 删除 `agentStore.ts` / `AgentSelector.tsx`（0 字节空文件）

- 直接删除两文件。可复现核查证据（供独立测试 Agent 验证无残留引用）：在 `apps/desktop/src` 下搜索 `from "@/stores/agentStore"` / `from "@/components/chat/AgentSelector"` / `useAgentStore` / `selectedAgentId` 的 `.ts/.tsx` **源码 import** 命中数为 **0**（仅 `*.test.tsx` 注释文字提及，无编译依赖，删除空文件不影响构建）；`NewTaskPage.tsx:8-9`、`TaskHeaderBar.tsx:11-14,85,154`、`ModelSelector.tsx:19,149`（仅注释「与 AgentSelector 对齐」）等非 import 引用，删文件后仅余注释，需同步修订文案避免误导。
- 删除前额外执行 `grep -r "vi.mock(\"@/stores/agentStore\")" apps/desktop/src/tests` 确认无测试对空文件做 `vi.mock`（若有则同步移除该 mock 行）。
- 测试文件里对已删空文件的注释断言（如 `taskHeaderBarExtraction.test.tsx` 断言「不应 import AgentSelector」）删除空文件后该反向断言仍成立，可保留，但建议统一清理提及 agentStore 的注释。

### 3.7 类型收敛 `workspace_id`

- 统一为 `number`（对齐后端 int 与 `CreateTaskRequest`）：改 `WorkspaceRecord.workspace_id`（`workspace.ts:11`）、`workspaceStore` / `taskStore` 的 `Record<string,...>` key、`api.ts` 的 `workspaceId: string` 参数、`useTask.createTask(workspaceId: string)`。
- 波及面（`taskStore.ts:316-318,419-421,463-491,696-703`、`workspaceStore.ts:53-72`、`workspaceEventStore.ts:38,42-44`、`api.ts:320,321,363-372,386-395,541-551` 等）一次性收敛，消除隐性 string→number 错位技术债（第零铁律：长期稳定迭代，不累积隐性债）。
- 若评估波及过大可拆为独立 PR，但本方案主张一次性收敛（第零铁律优先于「最小改动」）。

### 3.8 生成物类型错配（移出本方案，登记后端契约修订议题）

> 死锁说明：本方案约束「不改后端」，而 `events.ts` / `logs.ts` 是 pre-commit 从后端 payload 自动生成的**生成物**（AGENTS.md §八禁止手改）。其类型错配只能由「改后端 payload registry 重生成」解决，与本方案约束冲突，故**本方案不触碰这两个生成物**，相关项移交独立议题。

- **`events.ts` workspace 事件错配（登记，不改）**：`workspace_preparing/ready/degraded` 被塞进 `RuntimeEvent`（强制 `task_id`），后端 workspace 事件走独立 `WorkspaceEvent`（无 `task_id`）。正确修复在后端 `models/payload/registry/` 调整 `RuntimeEventType` 生成源后重生成——属「后端契约修订」议题，不在本前端方案范围。
- **`workspaceEvent.ts` `WorkspaceState` 补 `failed`（本方案处理，手写文件）**：`workspaceEvent.ts` 是手写文件（非生成物），可安全改。当前 `WorkspaceState = ready|preparing|degraded|error|idle` 缺 `failed`。后端 `WorkspacePrepareResponse.state` 合法值含 `failed`（另含 `unavailable|unreachable|timeout` 已由 `WorkspaceDegradedState` 承载）。**处置**：将现有 `error` 归一态对齐为 `failed`（SSE 收到 `failed` 不再落默认分支），并加注释说明来源；`accepted` 是 prepare 请求的**同步即时态**（`WorkspacePrepareResponse` 初值），非 SSE 流出的事件态，**不并入** `WorkspaceState` 事件状态机，仅在 `prepareWorkspace` 请求-响应处作为即时快照字段处理。
- **`logs.ts` `LogError` 臆造结构（登记，不改）**：`LogError extends Record<string, unknown>` 已兼容后端自由 dict，其 `type/message/stack` 可选字段属生成物冗余，须走 `scripts/generate_api_ts.py` 源修订，移交后端契约议题。

---

## 四、验证闭环（独立审查 + 独立测试子 Agent）

主 Agent 只调度，不充当裁判。改造落地后：

1. **独立开发 Agent**：按本方案 §3 落地，零后端改动、零新依赖。完成后跑 `tsc --noEmit` + `vitest run` 相关用例。
2. **独立审查 Agent（A）**：逐条对照本方案与项目编码规范（单一职责、不引依赖、docstring、分层、不重复造轮子、日志），输出「符合/不符合」+ 问题列表（文件:行号）。
3. **独立审查 Agent（B）**：目标一致性审查——确认「前端真正对齐后端、无 UI 降级、体验优化优先」三目标达成；确认未误改后端、未引死按钮、未露 404 路径。
4. **独立测试 Agent**：边界验证——
   - ModelSelector：`GET /models` 字段下 key 唯一（model_name）、展示名非空、能力徽标正确、窗口徽标诚实省略、强度控件门控；
   - 厂商配置：新建/启停/删除/测试连通性调用后端存在端点；无 `GET /providers`/`discover`/`import` 调用残留；
   - 删除 agentStore/AgentSelector 后构建通过、无引用断链；
   - `workspace_id` 全链路 number 一致；
   - `events.ts` workspace 事件移出 RuntimeEvent、workspaceEvent 补 failed/accepted。
5. 审查/测试不通过则修复后重跑，直到双通过。主 Agent 不自行宣布完成。

---

## 五、待用户拍板的两点

1. **`workspace_id` 收敛**（§3.7）：是否并入本次 PR？建议并入（消除隐性债），若想缩小改动面可拆独立 PR。
2. **厂商「已配置列表」展示**（已据审查修订）：本方案 §3.5 已改为「创建/编辑/删除成功回参写入持久化 store（localStorage），`ProviderSettingsDialog` 读该 store 渲染已配置清单，不依赖 `GET /providers`、刷新不丢失」；ModelSelector 仅展示后端 `GET /models` 已启用+已配 Key 的模型，UI 文案告知用户启用并配 Key 方可见。**无需再拍板极简态**——信息盲区已闭合。
3. **生成物错配移交**：events.ts workspace 事件错配、logs.ts LogError 结构属「后端契约修订」议题（生成物禁手改 + 本方案不改后端），不在本次前端方案执行范围，已登记 §3.8。

---

## 六、改动文件清单（预估）

- `apps/shared/ts/model.ts`（契约收敛 + 路径常量）
- `apps/shared/ts/workspace.ts`（workspace_id 类型）
- `apps/shared/ts/workspaceEvent.ts`（WorkspaceState 补 failed，手写文件可改；`accepted` 不入事件状态机）
- `apps/shared/ts/api.ts`（workspace_id 参数类型）
- **不改动（已移出本方案，登记后端契约修订议题）**：`apps/shared/ts/events.ts`、`apps/shared/ts/logs.ts` —— 二者为 pre-commit 生成物（AGENTS.md §八禁手改），其 workspace 事件错配 / LogError 结构须由后端 payload registry 修订后重生成解决，本方案不改后端故不触碰。
- `apps/desktop/src/services/api.ts`（删死封装、workspace_id 类型）
- `apps/desktop/src/stores/taskStore.ts`（workspace_id key、删 agent 引用注释）
- `apps/desktop/src/stores/workspaceStore.ts`（workspace_id 类型）
- `apps/desktop/src/stores/workspaceEventStore.ts`（workspace_id 类型、WorkspaceState 消费）
- `apps/desktop/src/components/chat/ModelSelector.tsx`（体验增强核心）
- `apps/desktop/src/components/settings/ProviderSettingsDialog.tsx`（移除死按钮、补测试按钮、配置即生效文案）
- `apps/desktop/src/components/settings/ProviderModelsSection.tsx`（删除，绑定不存在端点）
- `apps/desktop/src/components/chat/AgentSelector.tsx`（删除，0B）
- `apps/desktop/src/stores/agentStore.ts`（删除，0B）
- 相关测试文件（契约断言、ModelSelector、taskStore、useTask 同步更新）
