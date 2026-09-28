# 系统配置中心方案与验收标准

> 状态：已按本方案落地第一期实现，验收以当前代码与测试结果为准。
>
> 范围：第一期只管理系统级配置，不纳入 workspace 级 Agent 配置和 workspace `AGENTS.md`。

## 1. 目标与非目标

### 1.1 目标

在桌面应用中提供一个专门的系统配置界面，统一管理以下四类现有后端配置事实：

1. 系统级子 Agent JSON 配置：`<DATA_DIR>/.cosir/agents/*.json`。
2. 主 Agent 系统提示词：`<DATA_DIR>/.cosir/main_agent_system_prompt.md`。
3. 系统级全局指令：`<DATA_DIR>/.cosir/AGENTS.md`。
4. 系统级运行环境配置：`<DATA_DIR>/.cosir/.env`（单文件，配置中心与手写配置共用）。

配置界面应当让用户知道配置的来源、校验状态和生效时机；保存失败必须可定位，不能静默降级。

### 1.2 非目标

- 不把文件配置复制到 SQLite，也不建立第二套配置事实源。
- 不在第一期管理 workspace 下的 `.cosir/agents`。
- 不在第一期管理 workspace 层级的 `AGENTS.md`。
- 不把任意环境变量编辑器暴露给用户。
- 主 Agent 只开放系统提示词编辑，不开放其工具权限、角色、模型设置等内置契约字段。
- 不在本次配置界面中管理 Provider CRUD、模型目录、Provider API Key 或全局默认模型。
- 允许在单个子 Agent 配置中引用已有 Provider/model；这只是 Agent 的可选模型覆盖，不改变 Provider
  和模型配置域的事实所有权。
- 不为第一期引入认证、多用户、远程配置、云端同步或新的配置服务进程。
- 不通过 assistant-ui 的消息、Thread state 或生成式 UI 保存系统配置。

## 2. 当前代码事实

### 2.1 路径与数据根

系统数据根由 Tauri 通过 `CODING_AGENT_DATA_DIR` 注入，macOS/Windows 使用用户主目录，后端在
[`apps/backend/app/utils/paths.py`](../apps/backend/app/utils/paths.py) 中推导
`SYSTEM_COSIR_DIR`；绕过 Tauri 运行时 macOS/Windows 同样使用用户主目录，其他平台回落到仓库根目录。`.cosir` 基名和系统/工作区子目录由
[`apps/backend/app/utils/cosir_paths.py`](../apps/backend/app/utils/cosir_paths.py) 集中定义。

当前相关路径为：

```text
<DATA_DIR>/.cosir/agents/
<DATA_DIR>/.cosir/main_agent_system_prompt.md
<DATA_DIR>/.cosir/AGENTS.md
<DATA_DIR>/.cosir/.env
```

`cosir_paths.py` 是路径计算模块，不创建目录、不读写文件、不依赖业务模型。这个边界必须保持：
配置读写由 service 层负责，API 层只做 HTTP schema、依赖注入和错误映射。

### 2.2 系统级 Agent 配置

后端启动期在 [`apps/backend/app/lifespan.py`](../apps/backend/app/lifespan.py) 中依次完成：

1. 初始化系统默认 Agent 文件。
2. 构建代码内置的 Agent Registry。
3. 加载系统级 `.cosir/agents` JSON。
4. 加载已登记 workspace 的 `.cosir/agents` JSON。
5. 读取主 Agent prompt 配置并注入内置 profile。
6. 将 Registry 注入进程级配置。

JSON 字段契约和单文件校验由
[`apps/backend/app/core/agents/agent_profile.py`](../apps/backend/app/core/agents/agent_profile.py)
持有；目录加载、作用域解析和冲突裁决由
[`apps/backend/app/core/agents/agent_profile_registry.py`](../apps/backend/app/core/agents/agent_profile_registry.py)
持有。

因此，配置页不能在前端重新实现 Agent schema，也不能在 API 层复制一套校验规则。新增/编辑 Agent
必须调用已有契约或将其抽取为明确的可复用配置 service。保存磁盘文件后，当前进程内 Registry 不会
自动更新；主 Agent prompt 通过专用配置 service 在保存成功后更新 Registry，并从下一次 Run 开始生效。

当前代码内置的准确清单是：`main_agent`（`MAIN`）和 `general-assistant`（代码维护的
`CHILD`）。当前没有由 `define_agents.py` 注册的 `HIDDEN` Agent。系统 `.cosir/agents` 中的
JSON 文件实际都被解析为 `CHILD`，其文件字段仍包含 `system_prompt`、`provider_id`、`model_name`
和 `model_settings`。

代码内置 Agent 不属于用户可编辑的系统级 JSON 配置；但主 Agent 的系统提示词通过独立配置文件和专用 API 编辑。
校验、编辑和删除。主 Agent prompt 保存成功后立即更新进程内 Registry，并从下一次 Run 开始生效；
已经运行的 Run 不会被改写。

当前加载器尚未强制文件名与 `agent_id` 一致。第一期配置 service 应建立明确的规范：新建/编辑只允许
使用 `<agent_id>.json`，不接受客户端提交任意路径；列表读取时若发现已有文件名不匹配，必须报告可
定位的校验状态，不得静默重命名或覆盖。Agent ID 在第一期不可变；改名使用“新建目标 Agent + 删除
旧 Agent”的显式操作。

### 2.3 全局 `AGENTS.md`

[`apps/backend/app/core/context/system_prompt_builder.py`](../apps/backend/app/core/context/system_prompt_builder.py)
将系统级 `AGENTS.md` 作为跨 workspace 的全局提示词层：

- 文件缺失时尝试创建空文件。
- 文件读取失败时记录 warning 并降级为空内容。
- 内容会经过系统提示词的 token 预算限制。
- 该层与 workspace 指令层是两个不同来源，不能在配置界面混为一个文件。

保存全局指令只影响后续构建的系统提示词；已经创建并运行中的 Run 不应被强行改写。它不需要重启
后端，但应在界面上明确“对新 Run 生效”。

### 2.4 主 Agent 系统提示词

主 Agent prompt 的唯一事实源是用户配置文件 `<system_cosir_dir>/main_agent_system_prompt.md`，不再有
随应用分发的内置模板。配置 service 负责文件创建、读取、token 预算、路径边界和原子写入校验：文件
不存在时创建空白文件并返回空正文（读取侧不阻断启动），写入侧仍拒绝空白文本和超预算正文。
`SystemPromptBuilder` 把有效正文放在 `<agent_layer>`，正文为空时整层不出现，不会让子 Agent 继承
主 Agent 专属协议；未配置时主 Agent 的基础身份与运行期事实由动态变量层提供。

系统 prompt 是 Task 级内存事实。配置更新时不修改正在运行的 `RuntimeContextManager`；下一次 Run
取得 Task context 时比较 profile 来源签名，发现主 Agent prompt 变化后重新装配 system entry。

### 2.5 env 配置

[`apps/backend/app/config/settings.py`](../apps/backend/app/config/settings.py) 通过
`paths.env_file()` 读取系统级 `.env`：

- 只有一个系统级 env 文件：配置中心与手写配置共用 `<数据根>/.cosir/.env`。
- 已存在的进程环境变量优先于文件值（`os.environ.setdefault` 语义）。
- `Settings.load()` 在启动阶段加载配置并填充类级静态属性。

> 早期设计把配置拆成「基础 `.env` + 本地覆盖 `.env.local`」，但两个文件都在数据根下、都不进版本
> 控制，覆盖层没有实际用途，却带来「清除 override 后基础值又出现」的困惑，故合并为单文件。配置
> 中心写入只重写白名单内的键，注释、未知键与未管理行原样保留。

第一期只暴露后端明确声明的白名单字段：

```text
DEFAULT_LANGUAGE
FIRECRAWL_API_KEY
FIRECRAWL_API_URL
LANGFUSE_ENABLED
LANGFUSE_PUBLIC_KEY
LANGFUSE_SECRET_KEY
LANGFUSE_BASE_URL
```

`WEB_BACKEND` / `WEB_SEARCH_BACKEND` / `WEB_EXTRACT_BACKEND` 不在白名单内：当前内置 Web Provider
只有 `firecrawl` 一个实现，三者留空即由回退优先级命中同一实现，显式赋值不改变结果；因此它们作为
固定值保留在 `Settings` 中，既不经环境变量覆盖也不暴露给配置界面。接入第二个实现后再开放。

Provider CRUD、模型目录和 Provider API Key 不属于本次配置界面；它们继续由现有 Provider service、
模型 API 和模型选择器管理。子 Agent 配置可以引用已有的 Provider/model 作为可选覆盖：
`provider_id=null` 与 `model_name=null` 表示该 Agent 未配置模型覆盖，保存本身允许为空；运行时是否
能执行仍遵循现有 Agent/Provider 校验和无模型配置错误语义。当前 `AgentProfile` 解析器对无效引用
会将两项降为 `None`；配置 service 不应沿用这种对用户不可见的静默丢弃，而应在保存前对“只填一项”
或引用不存在的 Provider/model 返回明确错误。

env 保存后由后端就地把新配置重载进内存，不需要重启：`Settings.load()` 刷新 `Settings` 与进程环境后，
重装配工具系统（工具是否注册在装配期按当时的 `Settings` 判定，例如 Web 工具要求本地已配置 Provider
凭证）并刷新运行时持有的执行器。重载只影响尚未开始的 Run，运行中的 Run 保持原有行为。Agent Registry
不参与重载，它只由 Agent profile 文件决定。

## 3. 总体架构

采用“文件事实源 + 配置 service + 专用设置页”的结构：

```text
React SettingsPage
  ├─ AgentsSettings
  ├─ GlobalInstructionsSettings
  └─ EnvironmentSettings
          │
          ▼
现有 lib/http/client.ts
          │ localhost HTTP + X-Trace-Id
          ▼
app/api/configuration/（按子域拆分）
          │ schema / DI / error mapping
          ▼
configuration services
  ├─ AgentConfigurationService
  ├─ MainAgentPromptConfigurationService
  ├─ InstructionConfigurationService
  └─ EnvironmentConfigurationService
          │
          ▼
系统 .cosir 文件
  ├─ agents/*.json
  ├─ main_agent_system_prompt.md
  ├─ AGENTS.md
  └─ .env
```

### 3.1 后端目录建议

```text
apps/backend/app/service/configuration/
├─ __init__.py
├─ agent_configuration_service.py
├─ main_agent_prompt_configuration_service.py
├─ environment_configuration_service.py
└─ instruction_configuration_service.py

apps/backend/app/api/configuration/
├─ __init__.py
├─ errors.py                 # 三个子域共用的领域异常 → HTTP 错误映射
├─ agents.py                 # /configuration/agents
├─ main_agent_prompt.py      # /configuration/main-agent-prompt
├─ global_instructions.py    # /configuration/global-instructions
└─ environment.py            # /configuration/environment

apps/backend/app/api/schemas/request/...
apps/backend/app/api/schemas/response/...
```

各 service 只负责一种配置事实：

- Agent service：文件枚举、复用 Agent 校验、序列化、原子写入、状态汇总。
- Main Agent prompt service：默认模板安装、正文校验、原子写入和有效 prompt 投影。
- Instruction service：固定文件的读取、token 预检查、原子写入。
- Environment service：白名单 schema、来源解析、敏感字段脱敏、系统 `.env` 写入。

`cosir_paths.py` 可以补充以下纯路径函数，使配置目录布局清晰且集中：

```python
system_env_file()
system_env_local_file()
system_instruction_file()
system_main_agent_prompt_file()
system_agent_config_dir()
```

其中 `system_instruction_file()` 和 `system_agent_config_dir()` 已存在；env 路径应逐步统一到
该模块的路径命名，不在新的 service 中手工拼接 `.cosir`。

### 3.2 文件写入规则

所有写入均由后端 service 执行，并遵守：

1. 先解析和校验完整输入。
2. 在目标目录中写临时文件。
3. flush 并执行 `fsync`。
4. 使用原子替换覆盖目标文件。
5. 使用原子替换覆盖目标文件，并按目标文件所在目录执行必要的目录同步。
6. 失败时清理临时文件、保留原文件，不把半成品暴露给启动流程。

安全边界必须由 service 明确定义，而不是只依赖“原子替换”：

- 客户端不得提交 `path` 作为写入目标；目标路径只能由 `cosir_paths.py` 和受限的 `agent_id`
  生成。
- Agent 配置目录本身、目标文件和临时文件不得是符号链接；解析后的目标必须仍位于系统 Agent
  配置目录内。
- `agent_id` 只能包含规范化后可安全映射为单个文件名的字符，拒绝路径分隔符、`.`、`..`、空白
  和平台保留名。
- 临时文件必须创建在目标目录内，设置受限权限，并在异常路径清理。
- 同一配置类型的写入应串行化；并发保存时后到请求不能静默覆盖一个已经变更的版本，API 至少要
  提供版本号或修改时间条件。

Agent JSON 保存失败不能产生部分更新；env 保存失败不能改变当前生效配置；Markdown 保存失败不能
清空原有指令。

## 4. 后端 API 方案

建议新增 `app/api/configuration/` 包，按子域拆分路由模块（`agents` / `global_instructions` /
`environment`，共用 `errors`），由 API 层完成 schema、依赖注入和错误映射；API 不直接调用
`Path.read_text()`、`Path.write_text()` 或 CRUD。

新增 API 模块后，必须在 [`apps/backend/app/app.py`](../apps/backend/app/app.py) 中按现有
`importlib.import_module("app.api...")` 方式显式注册；未完成注册的接口不算交付。

配置错误应使用稳定错误码并映射到明确 HTTP 状态：输入 schema/字段校验为 `400`，目标 Agent 不存在
为 `404`，版本条件或并发冲突为 `409`，文件权限/编码/系统 I/O 失败为 `500` 并记录结构化日志，
符号链接或路径安全拒绝按配置输入错误返回 `400`。

### 4.1 Agent API

```text
GET    /configuration/agents
POST   /configuration/agents
PUT    /configuration/agents/{agent_id}
DELETE /configuration/agents/{agent_id}
```

Agent API 必须区分两个 DTO：

- `AgentDocument`：磁盘 JSON 的完整编辑模型，包含所有可持久化字段，用于后端读写和无损保存。
- `AgentRuntimeSummary`：面向列表和委派展示的运行时投影，不能反向用于保存。

不得直接使用现有 `AgentProfile.to_dict()` 作为编辑响应，因为该方法明确不导出
`system_prompt` 和 `model_settings`。

列表响应至少包含：

- `agent_id`
- `role`
- `description`
- `allowed_tool_groups`
- `max_steps`
- `provider_id`（可为空）
- `model_name`（可为空）
- `model_settings`
- `source`：`user_file` 或 `builtin`
- `path`
- `editable`
- `validation_status`
- `validation_error`（不含敏感内容）
- `file_name`
- `file_name_matches_agent_id`

编辑接口请求体使用 Agent JSON 中的用户可编辑字段：`agent_id`、`role`、`description`、
`system_prompt`、`allowed_tool_groups`、`max_steps`、可选的 `provider_id`、`model_name` 和
`model_settings`。工具组由配置 API 按主 Agent 当前工具目录展开为工具名，再写入 JSON 和
Registry；读取时再从工具名反向聚合为工具组。`provider_id` 与 `model_name` 允许同时为空，表示不设置 Agent 级模型覆盖；如果
只填写其中一项，后端应按现有 Provider/model 引用校验返回明确错误，不静默改写成另一项。API 不能
返回当前进程 Registry 中的临时副本作为唯一编辑对象；读取和保存的事实对象必须对应系统配置文件。

新增/编辑时由后端根据 `agent_id` 生成文件名并执行文件名一致性校验；已有文件名不匹配时，列表必须
显示错误状态，编辑必须要求用户通过新建/删除完成迁移。未知字段、未知工具名或非法配置必须沿用
现有校验语义并返回稳定错误码。

### 4.2 全局指令 API

```text
GET /configuration/global-instructions
PUT /configuration/global-instructions
```

响应包含：

- `content`
- `path`
- `token_length`
- `max_tokens`
- `effective_on: next_run`

配置 service 必须复用 `Constant.SystemPrompt.GLOBAL_INSTRUCTION_MAX_FILE_TOKENS` 作为保存前校验的
单一来源。超过该预算时由后端拒绝并返回可读错误，原文件保持不变；这是配置写入边界的额外保护，
不改变当前 `SystemPromptBuilder` 对历史超限内容的运行时预算实现。

### 4.3 环境配置 API

```text
GET /configuration/environment
PUT /configuration/environment
```

读取响应只返回结构化元数据：

- 字段名
- 类型
- 当前磁盘配置的有效值（Secret 不返回原文）
- 当前进程实际值（Secret 只返回 `configured` 和 `masked`）
- `configured`
- `source`：`process`、`env`、`env_local` 或 `default`
- `masked`：是否存在已保存但不回显的 Secret
- 默认值

写入请求只接受白名单字段，每个字段必须明确使用以下一种操作语义：

- `replace`：写入新值；Secret 的原文只存在于请求处理和文件写入过程，不进入响应或日志。
- `clear`：删除 `.env` 中的该键（写回时该行被移除）；如果进程环境仍有同名值，清除后不会变成未配置。
- `unchanged`：不修改 Secret，适用于表单未重新输入 Secret 的更新请求。

所有由设置页管理的值写入系统 `.env`，写入只重写白名单内的键，用户手写的注释、未知键与未管理行原样
保留；若当前有进程环境变量覆盖文件，页面必须显示“进程环境优先，重启后仍可能不使用文件值”的警告。

## 5. 前端界面方案

### 5.1 路由与入口

当前 [`apps/desktop/src/App.tsx`](../apps/desktop/src/App.tsx) 只有 `/` 和 `/tasks/:id` 逻辑。
需要增加设置入口和可寻址的 `/settings` 状态，但不能简单地把设置作为会替换
`WorkspaceShell` 的独立页面：当前 `WorkspaceShell` 直接挂载 `TaskPage`，Assistant runtime 位于
任务表面内部。推荐让 `WorkspaceShell` 成为稳定外壳，在其内部使用并行内容区或 overlay 展示设置，
保留当前任务页、runtime 和 SSE attach 的挂载；从对话进入设置再返回时，应保持原有任务路由、订阅
状态和未发送交互状态。

### 5.2 页面布局

```text
系统配置
├─ 子 Agent
│  ├─ Agent 列表
│  ├─ 新建/编辑表单
│  └─ 校验与重启提示
├─ 全局指令
│  ├─ AGENTS.md 编辑器
│  ├─ Token 预算提示
│  └─ 保存状态
└─ 环境配置
   ├─ 非敏感字段
   ├─ Secret 字段
   ├─ 来源/覆盖关系
   └─ 保存并重启
```

所有请求使用现有 `lib/http/client.ts`，自动复用动态后端地址、`no-store` 和 `X-Trace-Id`。
前端日志使用 `frontendLog`，后端使用结构化日志；日志中不得记录 API Key、Secret、完整 Agent
System Prompt 或完整 Markdown 正文。

### 5.3 assistant-ui 边界

assistant-ui 官方架构将 UI、runtime、backend/agent、protocol 和 persistence 分层；运行时负责
对话状态，组件通过 `AssistantRuntimeProvider` 访问 runtime。当前项目已经使用 assistant-ui 的
Thread、模型选择器和工具 UI。

因此本设置页应当是普通 React 页面，通过 REST API 访问配置 service：

- 不把设置保存为消息。
- 不把设置放入 assistant runtime state。
- 不用 assistant transport 表达配置文件更新。
- 不为配置页创建第二套对话状态机。

只有未来明确需要“让 Agent 通过对话修改配置”时，才考虑使用 assistant-ui 的 tool UI；那会是另一
个需要审批和权限确认的功能，不属于第一期设置页。

参考官方文档：

- [assistant-ui Architecture](https://www.assistant-ui.com/docs/architecture)
- [Custom Runtime Overview](https://www.assistant-ui.com/docs/runtimes/custom/overview)
- [AssistantRuntimeProvider](https://www.assistant-ui.com/docs/api-reference/context-providers/assistant-runtime-provider)

## 6. 生效与重启策略

| 配置 | 保存后 | 当前 Run | 是否重启 |
| --- | --- | --- | --- |
| 系统子 Agent JSON | 写入文件，标记待重启 | 不修改 | 需要 |
| 全局 `AGENTS.md` | 写入文件 | 不修改 | 不需要，对后续 Run 生效 |
| 系统 env `.env` | 写入白名单键 | 不修改 | 不需要，就地重载后对后续 Run 生效 |

env 保存由后端就地重载（`Settings.load` + 重建工具系统 + 刷新运行时执行器），不需要重启；前端不应
在保存 Agent 后私自重启，用户点击“保存并重启”后才调用 Tauri 现有重启控制面。
重启期间沿用当前 boot gate 的 `starting / ready / failed` 状态。后端重启失败必须回到现有失败页面和
日志链路。界面要区分“文件已保存”和“新配置已生效”，不能把保存成功显示成服务已使用新配置。

配置状态至少分为四种：

| 状态 | 含义 |
| --- | --- |
| 磁盘已保存 | 文件写入成功，但不代表当前进程已加载 |
| 当前进程生效 | 当前 Registry/Settings 已使用该配置 |
| 待重启 | 文件已保存，当前进程仍使用旧配置 |
| 重启失败 | 文件已保存，但新进程未 ready，不能宣称新配置生效 |

## 7. 错误、并发与可排查性

- 配置目录缺失：由 service 创建需要创建的系统配置目录；创建失败返回可定位错误。
- JSON 无效：保留原文件，返回字段级错误；不修改 Registry。
- 文件权限/编码错误：返回配置文件路径和稳定错误码，不返回堆栈或敏感正文。
- env 类型错误：后端按 `Settings` 语义校验，例如布尔值只能接受受支持的布尔文本。
- 保存期间发生替换失败：保留旧文件，并记录结构化 error 日志。
- 多次保存：后一次请求不能覆盖一个已知失败的中间文件；临时文件命名和替换必须可清理。
- 配置变更与 Run 并发：正在运行的 Run 继续使用已装配的 Agent/Settings；新配置只在声明的生效边界
  产生影响。

日志事件应至少能区分：读取开始/成功/失败、校验失败、写入成功/失败、待重启状态。日志只记录
配置类型、文件路径、Agent ID 和错误类别，不记录 secret、完整 prompt 或完整配置正文。

## 8. 实施分期

### Phase 1：配置读取与只读展示

- 增加 `cosir_paths.py` 的配置路径函数。
- 增加配置 service 的读取和脱敏模型。
- 增加 `/configuration/*` 的 GET 接口。
- 增加 `/settings` 页面和三个只读面板。
- 验证路径、来源、敏感字段和生效提示准确。

### Phase 2：安全写入

- 增加 Agent JSON 新建/编辑/删除。
- 增加全局 `AGENTS.md` 原子保存。
- 增加系统 `.env` 白名单写入与键清除。
- 增加后端单元测试、API 测试和前端模块测试。

### Phase 3：重启闭环

- 接入保存并重启。
- 验证 boot gate、backend generation、旧运行态收敛和设置页恢复。
- 增加 Playwright/WDIO 场景，覆盖“保存配置后重启失败”和“重启成功后重新读取配置”。

workspace 级配置、配置历史、导入导出、实时 Agent Registry 热更新均不属于以上第一期交付范围。

## 9. 验收标准

### 9.1 路径与事实源

- [ ] 桌面模式下所有配置读取路径都来自 `CODING_AGENT_DATA_DIR/.cosir`。
- [ ] 直跑后端时 macOS/Windows 使用用户主目录，其他平台回落到仓库根目录。
- [ ] 新增代码没有手工拼接第二套 `.cosir` 路径。
- [ ] Agent、主 Agent prompt、全局指令、env 的持久化事实仍分别是 JSON、Markdown、Markdown、env 文件。
- [ ] 没有新增 SQLite 配置表或前端 localStorage 配置事实。
- [ ] 配置 API 不接受客户端任意绝对路径；所有目标路径由后端路径函数生成。
- [ ] Agent 配置目录、目标文件和临时文件的符号链接、路径越界和路径穿越均被拒绝。

### 9.2 Agent 配置

- [ ] 列表能区分内置 Agent 与系统 JSON 文件 Agent。
- [ ] 内置主 Agent 的工具权限、角色和模型设置不可编辑；主 Agent 系统提示词通过专用配置面板可编辑。
- [ ] `main_agent` 和 `general-assistant` 的内置/CHILD 分类与当前代码一致；当前没有把不存在的
      HIDDEN Agent 展示为可编辑对象。
- [ ] 编辑模型包含 `system_prompt`，不会使用 `AgentProfile.to_dict()` 导致字段丢失。
- [ ] 子 Agent 表单可以引用已有 Provider 和 model；不提供 Provider CRUD、模型目录维护或 API Key
      编辑。
- [ ] `provider_id` 与 `model_name` 可以同时为空；为空表示不设置 Agent 级模型覆盖。
- [ ] 只有一项为空时返回明确校验错误，不静默清空或补全另一项。
- [ ] 保存时 `provider_id`、`model_name`、`model_settings` 的值按表单无损持久化。
- [ ] Agent ID 在第一期不可变；新建/编辑文件名始终是 `<agent_id>.json`。
- [ ] 现有文件名与 Agent ID 不匹配时报告校验状态，不静默重命名或覆盖。
- [ ] Agent JSON 保存前复用现有字段和工具名校验规则，并明确补充文件名安全校验。
- [ ] 无效配置不会覆盖原文件。
- [ ] 原子写入失败后原文件内容保持不变。
- [ ] 临时文件在目标目录内创建，权限受限；失败后临时文件被清理，必要的目录同步已验证。
- [ ] 并发保存不会静默覆盖较新的配置；版本条件失败返回 `409`。
- [ ] 删除行为符合当前默认初始化标记语义，不会静默恢复或覆盖用户文件。
- [ ] 保存后界面明确显示“重启后生效”。
- [ ] 重启前正在执行的 Run 不被新配置重写。

### 9.3 全局 `AGENTS.md`

- [ ] 页面编辑的是系统级 `<DATA_DIR>/.cosir/AGENTS.md`，不是 workspace `AGENTS.md`。
- [ ] 页面显示 token 预算。
- [ ] 超过 token 预算的保存被后端拒绝，原文件保持不变；校验复用 `Constant.SystemPrompt`。
- [ ] 保存后后续新 Run 可以读取新内容。
- [ ] 已运行中的 Run 不被强行修改。
- [ ] 读取/保存失败有稳定错误提示和结构化日志。

### 9.4 env 配置

- [ ] 页面只允许编辑明确白名单字段。
- [ ] Secret 不会在 API 响应、前端日志或后端日志中明文出现。
- [ ] Secret 响应只包含 `configured`/`masked`/`source` 等元数据。
- [ ] Secret 更新能区分 `replace`、`clear`、`unchanged`，空输入不会误清除已有值。
- [ ] 保存写入系统 `.env`，只重写白名单键，用户手写内容不被破坏。
- [ ] 清除后该键回到默认值或进程环境值。
- [ ] 布尔值和其他类型按后端 Settings 语义校验。
- [ ] 保存后就地重载，页面不提示“需要重启”。
- [ ] 页面能区分磁盘值与当前进程值。
- [ ] Provider CRUD、模型目录、Provider API Key 和全局默认模型不出现在本次配置界面；现有 Provider
      配置域仍保持原有入口和行为。

### 9.5 前端与 assistant-ui

- [ ] `/settings` 是普通 React 路由，不通过对话消息或 assistant runtime 保存配置。
- [ ] `/settings` 通过稳定 `WorkspaceShell` 的并行内容区或 overlay 展示；进入/离开设置页不会卸载
      `WorkspaceShell`、`TaskPage`、Assistant runtime 或 SSE attach。
- [ ] HTTP 请求复用 `lib/http/client.ts` 的动态地址、`no-store` 和 trace 规则。
- [ ] 配置页未知字段、未知 Agent 状态或后端扩展不会导致整个页面崩溃。
- [ ] 现有 Provider 配置入口不被本次设置页替换或重复实现。

### 9.6 测试验收

- [ ] 后端路径函数有单元测试，覆盖桌面数据根、直跑回落和系统 `.env` 路径。
- [ ] Agent service 有有效配置、未知字段、未知工具、冲突、删除和原子写入失败测试。
- [ ] Instruction service 有空文件、预算超限、编码/权限失败和保存后读取测试。
- [ ] Environment service 有白名单、来源优先级、secret 脱敏、类型校验和键清除测试。
- [ ] API 测试覆盖成功、400、404、409、文件读写失败映射。
- [ ] API 模块已加入 `apps/backend/app/app.py` 的显式路由注册，并有路由可达性测试。
- [ ] 前端测试覆盖加载、保存、校验错误、secret 不回显、待重启状态和重启失败提示。
- [ ] 至少有一个端到端场景验证设置页保存后通过现有 boot gate 重启，并重新读取配置。

## 10. 方案结论

采用文件事实源、按配置类型拆分 service、统一 HTTP 配置 API、独立 React 设置页的结构。
`cosir_paths.py` 只承担路径事实；配置 service 承担文件读写与校验；React 设置页承担编辑和生效
提示；assistant-ui 继续只承担对话 runtime 和对话 UI。

该结构满足项目第零铁律：职责边界清晰、避免重复造轮子、保持现有事实所有权、为后续 workspace
配置和热更新留下明确扩展点，同时不把第一期复杂度扩张到运行时重构。
