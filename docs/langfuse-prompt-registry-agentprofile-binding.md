# Langfuse Prompt Registry 与 AgentProfile 绑定最终方案

> 本文是最终讨论方案，不是底层实现设计。它用于约束后续技术方案与代码开发：AgentProfile 是 Agent 能力边界的一等事实源；Langfuse Prompt Registry 负责远程 prompt 版本管理；System Prompt 只承载模型需要遵守的稳定与运行时文本契约；`workflow` 和 `context_policy` 完全不进入 system prompt；子 Agent 能力摘要通过 `delegate_task` 的 `ToolDefinition.description` 暴露。所有取舍以第零铁律为准：方便项目长期稳定迭代。

## 1. 设计目标

本方案解决五个问题：

1. 让 `AgentProfile` 成为 Agent 能力、权限、prompt 绑定与执行策略引用的一等事实源。
2. 让每个 Agent 绑定独立系统提示词，并支持 Langfuse Prompt Registry 的版本、label 和发布流程。
3. 让本地 fallback prompt 成为可靠保底资产，避免 Langfuse 不可用时阻断正常开发。
4. 让 `delegate_task` 弱化为“选择已注册 Agent + 描述本次任务契约”的工具，不允许父 Agent 临时塑造子 Agent 能力。
5. 让 prompt 构建、Langfuse SDK、工具描述生成、运行时策略之间的边界清晰，避免后续扩展时互相污染。

核心判断：

- prompt 版本管理是通用复杂能力，优先复用 Langfuse Prompt Registry，不自研远程 prompt 管理。
- Langfuse 是远程 prompt 发布源和可观测平台，不是本地 Agent 能否运行的前提。
- `workflow` 和 `context_policy` 是确定性运行时策略，应由 runtime/workflow/context builder 保证，不交给模型阅读和遵守。
- 子 Agent 能力由 `AgentProfile` 预定义，父 Agent 只选择目标 Agent 并提供任务契约。
- 工具描述可以暴露子 Agent 能力摘要，但必须来自稳定、专用、受控的投影，不能把运行时对象或策略字段泄漏给模型。

## 2. 当前代码事实

> 本项目是 0-1 绿地项目，无历史兼容包袱。下列事实是**起点事实**：目标结构直接覆盖这些起点，不需要兼容层或迁移过渡期；凡是目标形态与起点冲突处，一律以目标形态为准，并同步删除/改写起点代码。

以下是当前仓库事实，不代表目标状态：

- `AgentProfile` 位于 `apps/backend/app/core/agents/agent_profile.py`，是一个运行时构造的 `@dataclass`，不是从配置或持久化加载的。
- 当前 `AgentProfile` 字段包括 `agent_id`、`role`、`goal`、`allowed_tools`、`context_policy`、`workflow`、`model_name`、`model_settings`、`max_steps`、`turn`、`context_excluded_turn_ids`、`runtime_event_loop`。其中 `turn` / `context_excluded_turn_ids` / `runtime_event_loop` 是 **child run 的运行时状态**，由 `ChildAgentProfileBuilder.build`（`app/core/delegation/child_agent_profile_builder.py:40`）通过 `dataclasses.replace` 注入，不属于 Agent 能力事实。
- 当前 `AgentProfile` 没有 `prompt_ref` 字段。
- 当前 `AgentProfile.to_dict()` 输出 `agent_id`/`role`/`goal`/`allowed_tools`/`context_policy`/`workflow`/`model_name`/`max_steps`，**不含** `turn` 等运行时字段；它不适合作为 tool description 或 system prompt 的直接数据源。
- 内置委派 Agent 工厂已存在于 `apps/backend/app/core/agents/delegate_agent_profiles.py`，包括 `delegate_reviewer`、`delegate_analyst`、`delegate_coder`，均使用 `goal` 字段。
- 当前 `SystemPromptBuilder` 位于 `apps/backend/app/core/context/system_prompt_builder.py`，`build(agent_profile, workspace_root) -> str`。其内部 section 为 `agent_identity`、`engineering_principles`、`workflow_contract`、`tool_use_policy`。
- **事实校准**：`_workflow_contract()`（`system_prompt_builder.py:127`）是**硬编码的 5 条通用步骤文本**，并不读取 `AgentProfile.workflow`。即：起点状态里 `workflow` **本就没进入 system prompt**，不存在"把 workflow 从 prompt 移出"的动作；方案要求保持的只是"workflow 继续不进 prompt"，且需明确 `_workflow_contract` 静态 section 与 `AgentProfile.workflow` 无关。
- **事实校准（token cache）**：`_agent_identity`（`system_prompt_builder.py:80-89`）每 turn 注入 `date.today()` 和 `workspace_root`。这与本方案 §8「稳定内容放前缀、动态内容放后缀」的 token cache 原则冲突，目标结构必须把这两项归入 dynamic suffix（见 §8 修订）。
- 当前 `delegate_task` 参数模型位于 `apps/backend/app/tools/tool_models/delegate_task_args.py`，字段为 `child_agent_id`、`delegation_type`、`prompt`、`requested_tools`。
- **事实校准（工具能力交集已实现）**：child 可用工具的交集计算已由 `DelegationPolicy.resolve`（`app/service/delegation/delegation_policy.py:12`）+ `DelegationPolicyContext`（`delegation_executor.py:88-104`）实现，四交集为 `parent.allowed_tools ∩ child.allowed_tools ∩ registered_tools ∩ system_policy`。本方案不重新发明，仅收回模型传入入口（见 §3.4）。
- **事实校准（tools→core 依赖方向，已核实）**：经核实 `app/tools` 目录下无任何 `import get_agent_registry` 或 `from app.config.configuration` 语句；`delegate_task.py` 仅 `from app.tools.schemas import ...`、`tool_base`、`delegate_task_args`，其执行器通过 `execution_context.runtime_dependencies.delegate_task_executor` 注入（`delegate_task.py:80`），**工具层当前不反向依赖 core/registry**。本方案 §13「tools 层不 import 注册表」目标与现状一致，无需修正 import，仅需在装配层把 child 摘要投影注入 `ToolDefinition.description`。
- `SystemPromptBuilder.build` 的**唯一调用点**在 `app/core/context/runtime_context.py:270`，仅传 `agent_profile` 与 `workspace_root`。目标结构要在此装配 `PromptBundle`（见 §9 时序）。
- 当前项目规则声明 Langfuse 三方依赖唯一收口在 `apps/backend/app/core/observability/`（实际 `langfuse_tracing.py:147` `from langfuse import Langfuse`）。

本方案升级最后一条边界：Prompt Registry 接入后，Langfuse 不再只是 observability 能力。为长期可维护性，绿地直接以新的 `core/integrations/langfuse` 作为 Langfuse SDK 唯一收口；`core/observability` 目标形态不得再 `import langfuse`，只消费 trace adapter / prompt metadata。落地时必须同步更新 `AGENTS.md` 与相关规则文档中的 Langfuse 收口说明。

## 3. 已确认决策

### 3.1 AgentProfile

- `AgentProfile` 是 Agent 能力边界的一等事实源。
- `AgentProfile.goal` 不保留，直接替换为 `description`，不做兼容过渡。
- `AgentProfile` 增加 `description`、`capabilities`、`recommended_use_cases`、`constraints`。
- `AgentProfile` 增加严格 `prompt_ref`。
- `AgentProfile.to_dict()` 不作为 system prompt 或 `ToolDefinition.description` 的数据源。
- 新增专用 tool-facing projection，例如 `AgentProfileToolSummary` 或 `AgentProfile.to_tool_summary()`，只输出允许给模型看的稳定能力摘要。

### 3.2 Prompt Registry

- 每个 AgentProfile 绑定一个稳定 prompt name。
- fallback prompt 每个 Agent 一个 markdown 文件，必须随代码提交。
- Prompt Registry label 允许通过配置切换。
- 第一版采用 Langfuse 远程优先 + 本地 fallback。
- 远程 Langfuse 获取失败不应中断 turn，应降级到本地 fallback。
- 本地 fallback 缺失是本地配置错误，允许 fail fast，因为仓库缺少必需 prompt 资产。
- Prompt 变量必须有本地 Pydantic schema，测试只能验证 schema 生效，不能替代 schema。

### 3.3 System Prompt

- System Prompt 分为 stable prefix 和 dynamic suffix。
- `workflow` 完全不进入 system prompt（与起点事实一致：当前 `_workflow_contract()` 是静态 section，不读 `AgentProfile.workflow`；目标保持 workflow 不进 prompt）。
- `context_policy` 完全不进入 system prompt。
- `workflow` 和 `context_policy` 只作为运行时结构化策略字段，由 runtime/workflow/context builder 执行。
- stable prefix 放平台契约、通用工程规则、Agent 身份、Agent 能力摘要、工具原则、输出契约、供应商差异说明等相对稳定内容。
- dynamic suffix 放本次任务契约和运行时事实。
- **token cache 自洽约束（修正原矛盾）**：起点 `SystemPromptBuilder._agent_identity` 每 turn 注入 `date.today()` 与 `workspace_root`（`system_prompt_builder.py:85/89`），与「动态内容不进前缀」冲突。目标结构把 `date.today()` 与 `workspace_root` 明确归入 dynamic suffix；若论证二者「对单 workspace 单日稳定」，可保留在 prefix 但须在 docstring 显式声明其为 stable 而非 dynamic。默认迁移到 suffix。
- System Prompt 构建必须注意 LLM token cache，避免把每 turn 变化内容放进前缀。

### 3.4 delegate_task

- `delegate_task` 不采用自由文本 `instruction`。
- `delegate_task` 参数采用结构化任务契约：`child_agent_id`、`title`、`objective`、`rules`、`references`、`expected_output`。
- `title` 可选，只用于 UI 展示，不参与执行决策。
- `objective`、`rules`、`references`、`expected_output` 必填。
- `rules` 和 `references` 必须有数量、单项长度和总长度上限，并在 schema description、工具 description、校验错误中可见。
- `delegate_task` 执行前必须校验 `child_agent_id` 已注册在 `AgentProfileRegistry`。
- 父 Agent 不能通过 `rules`、`references` 或其他参数扩大 child Agent 能力。
- `delegation_type` 不再作为模型可传入参数，应从 child `AgentProfile` 派生，用于 UI 分类、事件和统计。
- `requested_tools` 不再作为模型可传入参数。child 可用工具交集计算**复用既有 `DelegationPolicy.resolve`（`app/service/delegation/delegation_policy.py:12`）+ `DelegationPolicyContext`（`delegation_executor.py:88-104`）**，四交集为 `parent.allowed_tools ∩ child.allowed_tools ∩ registered_tools ∩ system_policy`。本方案不重新发明该逻辑，仅收回模型传入入口（模型不再传 `requested_tools`），交集仍由 `DelegationPolicy` 在 `DelegationExecutor.delegate` 内计算。

### 3.5 子 Agent 能力暴露

- 子 Agent 能力摘要通过 `delegate_task` 的 `ToolDefinition.description` 暴露给父 Agent。
- 摘要来源必须是专用 tool-facing projection，不使用当前 `AgentProfile.to_dict()`。
- **`AgentProfileToolSummary` 定位（定调）**：独立 dataclass，定义在 `app/core/agents/agent_profile_tool_summary.py`，由 `AgentProfile.to_tool_summary()` 投影生成（plain 数据，无 core 反向依赖）。绿地落地：经核实 `delegate_task.py` 当前已通过 `execution_context.runtime_dependencies.delegate_task_executor` 注入执行器，工具层不 import 注册表，符合目标，无需改动 import；装配层在进程启动（`configuration.py` 的 `build_agent_registry()` 已是遍历注册所有 profile 的单一事实来源）时，对每个 profile 调 `to_tool_summary()` 投影成 plain summary dict，注入 `DelegateTaskTool` 的 `ToolDefinition.description`（或构造参数）。工具层只消费注入的 plain summary。
- 第一版 child Agent 数量少，可以直接在 `delegate_task` 工具描述中列出全部可委派 Agent 摘要。
- 摘要只包含稳定字段：`agent_id`、`role`、`description`、`capabilities`、`recommended_use_cases`、`constraints`、必要时包含高层工具能力类别。
- 摘要严禁包含 `workflow`、`context_policy`、`turn`、`runtime_event_loop`、workspace、task、turn、时间、父 Agent 临时规则等动态或运行时字段。
- `ToolDefinition.description` 可以包含摘要，但生成逻辑不能造成 `tools -> core` 反向依赖。

### 3.6 Langfuse SDK 边界

- 当前 `core/observability` 只适合作为 trace/span 语义边界，不再适合作为所有 Langfuse 能力的唯一收口。
- 目标边界升级为 `apps/backend/app/core/integrations/langfuse/`。
- 只有 `core/integrations/langfuse` 可以 import Langfuse SDK。
- `core/prompts` 只依赖项目内 prompt provider 协议，不直接 import Langfuse SDK。
- `core/observability` 只消费 trace adapter 或 prompt metadata，不参与 prompt 构建。
- `service`、`tools`、`storage`、`models` 不直接 import Langfuse。
- 该边界调整属于为长期稳定迭代做的结构性改动，后续落地时必须同步更新 `AGENTS.md` 与相关规则文档中的 Langfuse 收口说明。

## 4. AgentProfile 目标形态

目标字段：

```text
agent_id
role
description
capabilities
recommended_use_cases
constraints
allowed_tools
context_policy
workflow
model_name
model_settings
max_steps
prompt_ref
```

字段含义：

- `agent_id`：稳定 Agent 标识，用于注册、任务记录、事件记录和委派选择。
- `role`：短角色名，用于提示词和 UI 展示。
- `description`：整体能力说明，替代当前 `goal`。
- `capabilities`：这个 Agent 能做什么。
- `recommended_use_cases`：推荐什么时候使用这个 Agent。
- `constraints`：这个 Agent 明确不能做什么，或必须遵守什么。
- `allowed_tools`：这个 Agent 的长期工具权限边界。
- `context_policy`：上下文策略标识，只供运行时使用，不进入 system prompt，不进入 tool-facing summary。
- `workflow`：执行策略，只供运行时使用，不进入 system prompt，不进入 tool-facing summary。
- `model_name` / `model_settings`：模型配置。
- `max_steps`：这个 Agent 的默认最大执行步数。
- `prompt_ref`：这个 Agent 绑定的 prompt 引用。

**运行时字段归属（绿地直接落地，闭合原缺口）**：起点 `AgentProfile` 上的 `turn` / `context_excluded_turn_ids` / `runtime_event_loop` 从 `AgentProfile` 移除，改由新建运行时状态对象 `ChildRunContext`（位于 `app/core/delegation/child_run_context.py`）承载。`ChildAgentProfileBuilder.build`（`child_agent_profile_builder.py:40`）改为返回 `(child_agent_profile, child_run_context)`，或把状态注入 `ChildRunContext` 而非 `dataclasses.replace` 进 profile。这样 `AgentProfile` 保持不可变能力事实源，`ChildRunContext` 承载每次派生的可变状态，职责清晰（单一职责）。

**调用方同步改写清单（必做，否则 delegation 主链路断裂）**：
- `delegation_executor.py:135-141`：`child_profile = ChildAgentProfileBuilder.build(...)` 需解构为 `child_profile, child_run_context = ChildAgentProfileBuilder.build(...)`；随后 `runtime_event_loop` 从 `child_run_context.runtime_event_loop` 取（原 `delegation_executor.py` 中多处 `runtime_event_loop` 局部变量需改为读 `child_run_context`）。
- `delegation_executor.py:142`：`self._child_runner.run_child(child_profile)`——若 `run_child` 内部依赖 profile 上的 `turn`/`context_excluded_turn_ids`/`runtime_event_loop`，需改为同时接收 `child_run_context`（签名扩展为 `run_child(child_profile, child_run_context)`）。
- `delegation_executor.py:110-120`：`delegation_service.create_pending(..., runtime_event_loop=runtime_event_loop)` 与 `:133/:159` 的 `mark_child_started`/`mark_failed` 的 `runtime_event_loop` 参数同样改为从 `child_run_context` 取。
- 上述改动随 §18 路线一步落地，列入「一次性同步改写清单」并确保 `tests/test_delegation_executor.py` 同步更新。

**`goal → description` 重命名（绿地无兼容层）**：同步修改以下调用点——`default_developer_agent` / `default_developer_pro_agent`（`agent_profile.py`）、`delegate_reviewer` / `delegate_analyst` / `delegate_coder`（`delegate_agent_profiles.py`）、`SystemPromptBuilder._agent_identity`（`system_prompt_builder.py:80`，改读 `description`）、以及全部相关测试。一次性改完，不保留 `goal` 字段。

目标 docstring 应明确：

```text
AgentProfile 是 Agent 能力边界的一等事实源。
它描述这个 Agent 是什么、能做什么、不该做什么、推荐何时使用、允许使用哪些工具、采用什么运行时策略、绑定哪个系统提示词。
父 Agent 委派任务时，只能选择已注册的 AgentProfile，并提供本次任务契约；不能通过工具参数临时塑造 child Agent 的长期能力边界。
workflow 和 context_policy 是运行时字段，不属于模型可见提示词内容。
```

## 5. PromptRef 目标形态

`AgentProfile` 不保存完整 prompt 文本，只绑定 `prompt_ref`。

第一版采用严格变量约束：

```text
prompt_ref:
  name: coding-agent/delegate-reviewer
  label: production
  fallback_path: apps/backend/app/core/prompts/templates/delegate-reviewer.md
  variables_schema: delegate_reviewer_v1
```

字段含义：

- `name`：Langfuse Prompt Registry 中的稳定 prompt name。
- `label`：默认 label，可由配置覆盖，例如 `production` 或 `staging`。
- `fallback_path`：仓库内 fallback markdown 文件路径，**定调为包内资源路径**：`LocalPromptProvider` 使用 `importlib.resources` 读取 `app/core/prompts/templates/<name>.md`（以 `app` 包为锚，不依赖运行工作目录，避免 ENOENT）。绿地新建目录 `app/core/prompts/templates/` 随首批 fallback 文件一并创建。
- `variables_schema`：本地 Pydantic schema 标识，用于约束 prompt 渲染变量。

变量约束：

- 每个 `variables_schema` 必须映射到一个本地 Pydantic model。
- prompt 渲染前必须校验必填变量。
- prompt 渲染前必须拒绝未声明的额外变量，除非技术方案明确指定某个 schema 允许 extra。
- schema 校验失败是本地构造错误，应 fail fast，并带可排查日志。
- 测试必须覆盖 schema 校验，但测试不能替代 schema。

不放进 `AgentProfile` 或 `PromptRef` 的内容：

- 当前日期。
- 当前 workspace 路径。
- 当前 turn id。
- 当前 task id。
- 本次用户任务。
- 本次委派目标。
- 本次委派规则。
- 本次委派参考材料。
- 审批状态。
- 工具执行结果。

这些内容属于运行态上下文，由 prompt 构建流程通过 `PromptRenderContext` 注入。

## 6. Langfuse Prompt Registry 使用策略

Langfuse Prompt Registry 定位为远程 prompt 版本管理与发布源。

第一版采用：

```text
Langfuse 远程优先 + 本地 fallback
```

运行策略：

- Langfuse 可用时，按 `prompt_ref.name + effective_label` 获取 prompt。
- 默认 label 使用 `production`。
- label 允许通过配置切换，建议配置名使用当前项目风格：`CODING_AGENT_LANGFUSE_PROMPT_LABEL`。
- `CODING_AGENT_LANGFUSE_ENABLED=false` 时，不访问 Langfuse，直接使用本地 fallback。
- Langfuse 启用但不可用时，自动使用本地 fallback。
- 远程 prompt 获取失败不应中断 turn。
- 远程失败后如果本地 fallback 也缺失，可以中断 prompt 构建和 turn 执行，因为这是本地必需资产缺失。
- 每次构建 system prompt 时，应记录 prompt name、version、label、source、是否 fallback、fallback reason。

建议 metadata：

```json
{
  "prompt_name": "coding-agent/delegate-reviewer",
  "prompt_label": "production",
  "prompt_version": 12,
  "prompt_source": "langfuse",
  "prompt_fallback_used": false,
  "prompt_fallback_reason": null
}
```

Langfuse 能力可以支持该路线：

- Prompt Registry 支持版本和 label。
- SDK 支持缓存，降低远程请求成本。
- SDK 支持 fallback prompt。
- prompt 支持变量编译，可注入运行时变量。

参考文档：

- [Prompt Version Control](https://langfuse.com/docs/prompt-management/features/prompt-version-control)
- [Prompt Caching](https://langfuse.com/docs/prompt-management/features/caching)
- [Guaranteed Availability](https://langfuse.com/docs/prompt-management/features/guaranteed-availability)

## 7. 不强依赖 Langfuse 的降级链路

目标链路：

```text
AgentProfile.prompt_ref
  -> PromptResolver
    -> LangfusePromptProvider via core/integrations/langfuse
    -> LocalPromptProvider
```

降级策略：

- `CODING_AGENT_LANGFUSE_ENABLED=false`：不访问 Langfuse，直接读本地 fallback。
- Langfuse 缺少 public key / secret key：记录配置日志，使用本地 fallback。
- Langfuse SDK 未安装或导入失败：记录原因，使用本地 fallback。
- 网络失败、超时、远程服务异常：记录原因，使用本地 fallback。
- 远程 prompt 不存在或 label 不存在：记录明确 reason，使用本地 fallback。
- 本地 fallback 缺失：fail fast，因为仓库缺少必需 prompt 资产。

日志要求：

- 不输出密钥。
- 记录 `prompt_name`、`prompt_label`、`fallback_path`、失败类型、是否使用 fallback。
- 远程失败只降级，不吞掉诊断信息。
- 本地 fallback 缺失必须有明确错误，方便定位缺失文件或错误路径。

## 8. System Prompt 分层与 Token Cache

System Prompt 按稳定程度分层。稳定内容放前面，动态内容放后面。

推荐结构：

```text
stable prefix:
  platform_contract
  global_engineering_rules
  agent_identity
  agent_capability_profile
  tool_policy
  output_contract
  provider_overlay

dynamic suffix:
  task_contract
  runtime_facts
```

### 8.1 stable prefix

`platform_contract`：

- 本地桌面 Agent 的硬边界。
- workspace 边界。
- 工具必须遵守 schema。
- 不能伪造执行结果。
- 不能越权。

`global_engineering_rules`：

- 所有开发类 Agent 共同遵守的长期工程规则。
- 包括第零铁律、代码整洁、分层依赖、测试审查闭环、日志可排查。

`agent_identity`：

- 当前 Agent 是谁。
- 角色是什么。
- 职责边界是什么。

`agent_capability_profile`：

- 来自 `AgentProfile` 的模型可见能力说明。
- 包括 `description`、`capabilities`、`recommended_use_cases`、`constraints`、`allowed_tools`、`max_steps`。
- 不包含 `workflow`。
- 不包含 `context_policy`。

`tool_policy`：

- 基于 `AgentProfile.allowed_tools` 生成。
- 只写工具使用原则和允许工具集合，不复制工具 JSON schema。

`output_contract`：

- 输出结构和粒度要求。
- 不同 Agent 可以有不同输出契约。

`provider_overlay`：

- 模型供应商差异层，面向**模型可见提示词**的供应商差异说明（例如「DeepSeek 会输出 reasoning 思考流，你不回灌」）。
- **双源冲突约束（定调）**：供应商差异的**运行时行为**（reasoning 是否回灌、工具调用协议差异）实际由 `core/llm/factory.py` + model-node chunk 处理实现（见项目 MEMORY：DeepSeek 流式事实）。system prompt 里的 `provider_overlay` 只是**告知模型的提示**，不是行为控制源。二者必须保持「运行时行为以 `core/llm` 为准，prompt 仅做对齐说明」的关系，禁止在 prompt 里写一套、运行时另写一套导致双源不一致。若运行时行为变更，必须同步评估 prompt 文案是否需要更新。

### 8.2 dynamic suffix

`task_contract`：

- 本次用户任务，或父 Agent 委派给 child Agent 的结构化任务契约。
- 包括 `objective`、`rules`、`references`、`expected_output`。

`runtime_facts`：

- 每次 turn 动态注入。
- 包括日期、OS、workspace root、task id、turn id、parent turn id、delegation id。

### 8.3 明确不进入 system prompt 的内容

`workflow` 完全不进入 system prompt。

原因：

- workflow 是执行策略，应由 runtime/workflow 实现保证。
- 如果把 workflow 解释成 prompt 文本，容易造成策略事实源分裂。
- 同一个 workflow 可以驱动多个 Agent，不应和 Agent 的自然语言 prompt 绑定。

`context_policy` 完全不进入 system prompt。

原因：

- context policy 是上下文选择策略，应由 context builder 实现保证。
- 如果把 context policy 解释成 prompt 文本，容易让模型误以为它自己负责上下文裁剪。
- 上下文裁剪属于确定性运行时能力，不应交给模型遵守。

## 9. Prompt 构建与 Langfuse 集成边界

建议目标结构：

```text
apps/backend/app/core/prompts/
  prompt_ref.py
  prompt_bundle.py
  prompt_provider.py
  prompt_resolver.py
  local_prompt_provider.py
  prompt_render_context.py
  prompt_variable_schema_registry.py
  templates/

apps/backend/app/core/integrations/langfuse/
  langfuse_client_provider.py
  langfuse_prompt_provider.py

apps/backend/app/core/context/
  system_prompt_builder.py
```

职责划分：

- `SystemPromptBuilder`：按固定顺序组合 section，产出最终 system prompt 字符串。
- `PromptResolver`：根据 `AgentProfile.prompt_ref` 获取远程 prompt 或 fallback prompt。
- `PromptProvider`：项目内协议，定义 prompt provider 的返回结构和错误语义。
- `LangfusePromptProvider`：封装 Langfuse Prompt Registry SDK 调用，位于 `core/integrations/langfuse`。
- `LocalPromptProvider`：读取仓库内 fallback prompt。
- `PromptRenderContext`：承载运行时变量，例如 workspace、today、allowed tools、turn id、delegation id。
- `PromptVariableSchemaRegistry`：把 `variables_schema` 映射到本地 Pydantic model。

边界原则：

- Prompt Registry 不放在 `tools` 层。
- `core/prompts` 不直接 import Langfuse SDK。
- `core/prompts` 可以依赖项目内 `PromptProvider` 协议。
- `core/integrations/langfuse` 是唯一 Langfuse SDK import 收口。
- `core/observability` 负责 trace/span/metadata，不参与 prompt 构建。
- `SystemPromptBuilder` 不直接访问 Langfuse，只消费 resolver 返回的 `PromptBundle`。

### 9.1 装配时序（闭合调用链路缺口）

起点代码里 `SystemPromptBuilder.build(self.agent_profile, self.workspace_root)` 的唯一调用点在 `app/core/context/runtime_context.py:270`，只传 `agent_profile` 与 `workspace_root`，**没有 PromptBundle 来源、也没有 PromptRenderContext 组装方**。目标结构必须补上这条数据流，明确责任方：

```text
进程启动（app/config/configuration.py）
  -> 遍历 AgentProfileRegistry，为每个 AgentProfile：
       prompt_ref -> PromptResolver.resolve(prompt_ref) -> PromptBundle
       缓存 PromptBundle 到 AgentProfileRegistry 或独立 PromptBundleCache
  -> 遍历 AgentProfileRegistry 投影 AgentProfileToolSummary，注入 DelegateTaskTool

每次 RuntimeContext 构建（runtime_context.py:270 附近）
  -> 取该 agent_profile 缓存的 PromptBundle
  -> 组装 PromptRenderContext(workspace=workspace_root, today=date.today(),
                              delegation_id=..., turn_id=..., parent_turn_id=...)
       * PromptRenderContext 组装责任方：RuntimeContext / RuntimeContextBuilder（core/context）
  -> SystemPromptBuilder.build(agent_profile, prompt_bundle, render_context) -> str
```

责任划分：

- `PromptResolver` 的调用与 `PromptBundle` 缓存：**进程启动装配期**（`configuration.py`），不放在每次 turn 的热路径，避免重复远程拉取。
- `PromptRenderContext` 组装：**每次 `RuntimeContext` 构建时**由 `core/context` 负责（它已有 `workspace_root` 和 turn 上下文），不在 `tools` 层。
- **职责归属声明（避免误读现状）**：上述 `PromptBundle` 缓存与 `AgentProfileToolSummary` 投影为**方案新增的装配步骤**，将扩展或紧邻现有 `build_agent_registry()`（`configuration.py:78-105`，其职责仅为注册 AgentProfile）调用，而非修改 `build_agent_registry()` 现有职责。当前代码无此两步，落地时需新增装配入口。
- `SystemPromptBuilder.build` 签名扩展为 `(agent_profile, prompt_bundle, render_context)`，内部把 `PromptBundle.rendered`（或带变量的 template + `render_context`）拼入 `agent_identity` / `agent_capability_profile` 等 section，并把 `date.today()` / `workspace_root` 从原 `_agent_identity` 静态注入改为从 `render_context` 取（落入 dynamic suffix，见 §3.3）。

## 10. fallback prompt 文件策略

第一版每个 Agent 一个 fallback markdown 文件。

建议路径：

```text
apps/backend/app/core/prompts/templates/
  developer.md
  developer-pro.md
  delegate-reviewer.md
  delegate-analyst.md
  delegate-coder.md
```

原则：

- fallback 文件是本地运行保底资产，必须随代码提交。
- fallback 文件缺失视为本地配置错误，可以中断 prompt 构建。
- fallback 文件不包含本次任务内容。
- fallback 文件不包含运行态路径、日期、turn id、工具结果。
- fallback 文件可以包含当前 Agent 的稳定身份、输出契约和长期行为要求。
- 动态变量通过 `PromptRenderContext` 注入。

## 11. delegate_task 参数方案

`delegate_task` 的目标是让父 Agent 选择一个已注册 child Agent，并提供本次委派的结构化任务契约。

推荐第一版参数：

```json
{
  "child_agent_id": "delegate_reviewer",
  "title": "审查依赖边界",
  "objective": "审查 DelegationExecutor 依赖边界是否符合第零铁律",
  "rules": [
    "只审查，不修改代码",
    "不运行测试",
    "按 Critical/Important/Minor 输出"
  ],
  "references": [
    "apps/backend/app/core/delegation/delegation_executor.py",
    "docs/Langfuse Prompt Registry 与 AgentProfile 绑定方案.md"
  ],
  "expected_output": "输出通过/不通过；如不通过，列出文件行号、原因和建议"
}
```

字段说明：

- `child_agent_id`：必填。目标 child AgentProfile id。执行前必须校验已注册。
- `title`：可选。只用于 UI 展示，不参与执行决策。
- `objective`：必填。本次委派目标，回答“要完成什么”。
- `rules`：必填。本次委派附加规则，回答“必须遵守什么”。
- `references`：必填。本次委派参考材料，回答“应该参考哪些文件、文档、范围或事实来源”。
- `expected_output`：必填。回答“希望 child Agent 以什么结构返回结果”。

删除或降级：

- `delegation_type`：从 `AgentProfile` 派生，用于 UI 分类和统计，不作为模型传入参数。
- `requested_tools`：不再由父 Agent 传入。child 可用工具由权限交集自动计算。

**落地迁移映射（闭合对 `DelegationExecutor` 旧字段消费的改造，已核实现状）**：当前真实代码与目标冲突，需同步改写——
- `delegate_task_args.py`：当前字段为 `child_agent_id`/`delegation_type`/`prompt`/`requested_tools`（`delegate_task_args.py:6-35`），目标改为 `child_agent_id`/`title`/`objective`/`rules`/`references`/`expected_output`；`prompt`（自由文本）拆为 `objective+rules+references+expected_output` 结构化契约。
- `delegate_task.py:64-71`：`DelegateTaskArgs.model_validate` 当前仍传 `delegation_type`/`prompt`/`requested_tools`，需改为目标字段；execute 签名同步去掉 `delegation_type`/`prompt`/`requested_tools`。
- `delegation_executor.py:115-117`：`create_pending(..., delegation_type=args.delegation_type, prompt=args.prompt, requested_tools=tuple(args.requested_tools))` 需改为目标字段（`prompt`→`objective`，`requested_tools` 改由 `decision.effective_tools` 派生并作记录）；`child_turn.input_text` 从 `args.prompt` 改为 `args.objective`（`delegation_executor.py:123`）。
- `delegation_type` 改为在 `DelegationExecutor` 内从 child `AgentProfile` 派生（如取 `agent_id` 前缀或 `role`），不再来自模型参数。
- 上述改写随 §18 路线一步落地，并同步更新 `tests/test_delegate_task_tool.py` 与 `tests/test_delegation_executor.py`。

约束：

- 父 Agent 不能通过 `rules` 扩大 child 能力。
- 父 Agent 没有的工具，child Agent 不能获得。
- child AgentProfile 允许但当前系统未注册的工具，也不能使用。
- `references` 只是参考材料，不代表 child Agent 自动拥有读取 workspace 外路径的权限。
- 子 Agent 默认不能递归委派。

## 12. delegate_task 参数预算

`rules` 和 `references` 必须有数量、单项长度和总长度上限，并且这些限制必须让父 Agent 可知。

第一版方案确定预算必须有上限，并给出**第一版推荐默认值**（绿地直接落地；日后按模型上下文预算调参，不阻塞实现）。数字为起点推荐值，非硬约束上限。

推荐默认预算：

```text
objective:
  max_chars: 800

title:
  max_chars: 120

rules:
  max_items: 5
  max_item_chars: 300
  max_total_chars: 1000

references:
  max_items: 5
  max_item_chars: 300
  max_total_chars: 1000

expected_output:
  max_chars: 600
```

`references` 单条类型白名单（第一版定调，消除模糊）：每条 `reference` 必须是以下类型之一，由 Pydantic 模型 `ReferenceItem(kind: Literal["file_path","doc_path","url","turn_id","natural_language"], value: str)` 约束——
- `file_path`：workspace 内相对路径（受 `ProjectPathResolver` 边界约束）。
- `doc_path`：项目文档相对路径。
- `url`：外部链接（经 `web/url_safety` 校验）。
- `turn_id`：本 task 内历史 turn 引用。
- `natural_language`：自然语言描述（不指向具体资源）。

预算约束应体现在：

- Pydantic 参数模型。
- 工具 schema description。
- `delegate_task` 工具 description。
- 参数校验错误信息。
- 必要时进入前端表单约束。

## 13. delegate_task 工具描述与子 Agent 暴露

`delegate_task` 的工具描述需要告诉父 Agent 当前有哪些可委派 child Agent，以及它们分别适合什么任务。

第一版 child Agent 数量少，可以通过 `ToolDefinition.description` 直接暴露全部摘要：

```text
Available child agents:
- delegate_reviewer: 只读代码审查；适合发现 bug、风险、遗漏测试；不修改代码，不运行测试。
- delegate_analyst: 只读事实分析；适合代码/文档调查、方案比较；不修改代码。
- delegate_coder: 受限代码开发；适合明确范围内的修复、实现和验证。
```

摘要来源：

- 新增专用 projection，例如 `AgentProfileToolSummary` 或 `AgentProfile.to_tool_summary()`。
- 不使用当前 `AgentProfile.to_dict()`，因为它包含 `workflow` 和 `context_policy`。
- projection 输出 plain data，不暴露 workflow 对象、context policy、turn、event loop 或其他运行时状态。

推荐 projection 字段：

```text
agent_id
role
description
capabilities
recommended_use_cases
constraints
tool_capability_summary
```

`tool_capability_summary` 只能是高层摘要，例如“只读文件与搜索”“代码修改与测试”“Web 调研”。是否暴露原始 `allowed_tools` 列表留给技术方案判断；如果暴露，也必须通过专用 projection 显式选择，不能直接 dump `AgentProfile`。

依赖方向约束（与 §3.5 一致，已核实现状）：

- 现状（已核实）：`delegate_task.py` 只 import `app.tools.*` 和 `delegate_task_args`，执行器经 `execution_context.runtime_dependencies.delegate_task_executor` 注入，工具层不 import `AgentProfileRegistry` 或 core。目标与现状一致。
- `DelegateTaskTool` 接收一个预渲染的 child Agent 摘要字符串（plain data），不自己 import `AgentProfileRegistry`。
- 摘要数据由装配层生成：在 `app/config/configuration.py` 的 `build_agent_registry()` 进程启动装配时，遍历 `AgentProfileRegistry`，对每个 profile 调 `to_tool_summary()` 投影成 `AgentProfileToolSummary`，再序列化为 plain dict 注入 `DelegateTaskTool` 的 `ToolDefinition.description`。
- `ToolDefinition.description` 是模型可见事实，但不是新的能力事实源；它只是 `AgentProfile` 稳定投影的展示载体。

### 13.1 Token cache 取舍

子 Agent 摘要进入 `ToolDefinition.description` 会增加工具描述长度，也可能在 Agent 列表变化时影响 token cache。

第一版仍选择该方案，理由是：

- 父 Agent 选择 child Agent 时，应该在工具语义附近看到可选 Agent 能力，降低误用概率。
- 当前 child Agent 数量少，摘要稳定，变化频率低。
- 摘要来自 `AgentProfile` 专用投影，不复制第二套事实源。
- 工具描述中只放稳定能力摘要，不放 turn、workspace、时间、任务等动态事实。

后续如果支持运行时动态增删 Agent，应给工具描述生成引入稳定版本或缓存策略，避免每 turn 任意变化。

### 13.2 渐进暴露预留

未来子 Agent 过多时，应做渐进暴露：

```text
第一层：只展示分类和简短摘要。
第二层：父 Agent 需要时查询某个 AgentProfile 的完整能力说明。
第三层：支持按任务目标检索推荐 child Agent。
```

第一版只在文档和 `AgentProfile` docstring 中预留该方向，不新增查询型工具。

## 14. 三类内置 child Agent 的 Prompt 方向

### 14.1 delegate_reviewer

定位：

- 只读审查。
- 不修改代码。
- 不运行测试。
- 输出按 severity 排序的 findings。
- 重点发现 bug、架构风险、测试遗漏、违反第零铁律的问题。

Prompt 重点：

- 审查者身份。
- 只读限制。
- 发现问题优先，摘要次之。
- 不能把“没跑测试”伪装成测试通过。

### 14.2 delegate_analyst

定位：

- 事实调查、代码分析、方案比较。
- 不修改代码。
- 可以使用只读工具和 Web 工具。
- 输出结论、依据、风险和开放问题。

Prompt 重点：

- 区分事实、推断、建议。
- 引用具体文件或来源。
- 不把候选方案写成已确认决策。

### 14.3 delegate_coder

定位：

- 在明确范围内做代码修改和验证。
- 必须保持改动聚焦。
- 必须输出 changed files、测试结果、剩余风险。

Prompt 重点：

- 遵守第零铁律。
- 先理解现有实现，再改代码。
- 复用已有机制，不重复造轮子。
- 修改后必须验证，不能自称完成而无测试依据。

## 15. Prompt 版本、环境和发布策略

建议约定：

- 生产默认使用 `production` label。
- 本地开发可配置 `CODING_AGENT_LANGFUSE_PROMPT_LABEL=staging`。
- 测试环境默认使用本地 fallback，除非显式启用 Langfuse。
- 每个 AgentProfile 绑定一个稳定 prompt name。
- Prompt 模板中的变量必须有本地 Pydantic schema。
- Prompt 版本信息必须进入 runtime trace / Langfuse metadata。

第一版不做：

- UI 在线编辑 prompt。
- prompt A/B 实验。
- 多 label 复杂路由。
- 用户在普通对话中动态覆盖 AgentProfile prompt。

## 16. 与 Langfuse Observability 的关系

Prompt Registry 与 Observability 相关，但不是同一职责。

目标关系：

- `core/prompts` 负责 prompt 解析、fallback、变量校验和 metadata 产出。
- `core/integrations/langfuse` 负责 Langfuse SDK 访问，包括 Prompt Registry provider 和后续可能抽取的 trace client provider。
- `core/observability` 负责 Langfuse trace/span/metadata 记录。
- Prompt Resolver 返回 prompt metadata。
- Runtime 或 tracing 层把 prompt metadata 写入 Langfuse trace。
- `SystemPromptBuilder` 不直接创建 trace。
- `service` 和 `tools` 层不直接 import Langfuse。

后续开发重点（绿地直接落地，无迁移过渡期）：

- 绿地以 `core/integrations/langfuse` 为 Langfuse SDK 唯一收口；`core/observability` 目标形态不得 `import langfuse`，只消费 trace adapter / prompt metadata。
- 先建立 Langfuse SDK 单点收口，再接 Prompt Registry。
- prompt metadata 必须能进入 trace，方便复盘 prompt 版本。
- `core/prompts` 不应反向依赖 `core/observability`；`core/observability` 只消费 prompt metadata，不参与 prompt 构建。
- 落地时同步更新 `AGENTS.md` 与 `rules/` 中「Langfuse 收口在 core/observability」的旧表述。

## 17. 风险与技术方案待细化项

风险：

- 如果 prompt 分层过细，第一版实现成本会上升。
- 如果 stable prefix 混入每 turn 变化内容，会破坏 LLM token cache。
- 如果 `delegate_task.rules` 过于自由，父 Agent 仍可能塞入大量自然语言噪音。
- 如果 prompt 版本没有进入 trace metadata，后续很难复盘模型行为。
- 如果复用当前 `AgentProfile.to_dict()` 生成工具描述，会泄漏 `workflow` 和 `context_policy`。
- 如果 `ToolDefinition.description` 动态生成没有缓存或版本策略，未来动态 AgentProfile 可能影响 token cache。
- 如果 Langfuse SDK 收口不重构，Prompt Registry 会和现有 observability 边界冲突。

本方案已定调、落地阶段直接执行的项（不再推迟到技术方案）：

- `AgentProfileToolSummary`：独立 dataclass，定义于 `app/core/agents/agent_profile_tool_summary.py`，由 `AgentProfile.to_tool_summary()` 投影；字段见 §3.5/§13。
- `rules`：第一版为字符串数组（`list[str]`），由 `max_items/max_item_chars/max_total_chars` 约束（§12）。
- `references`：第一版为结构化数组 `ReferenceItem(kind, value)`，类型白名单见 §12（`file_path`/`doc_path`/`url`/`turn_id`/`natural_language`）。
- Prompt variable schema：Pydantic model 置于 `core/prompts/schemas/`，由 `PromptVariableSchemaRegistry` 按 `variables_schema` 名映射（§5/§9）。
- `ToolDefinition.description` 摘要接入：装配层（`configuration.py` 启动期）投影注入，工具层不 import registry（§3.5/§13）。
- 摘要生成时机：**进程启动装配期**一次性生成并注入 `DelegateTaskTool`，非每 turn。
- `CODING_AGENT_LANGFUSE_PROMPT_LABEL`：由 `Settings` 类级命名空间读取（与现有 `Settings.LANGFUSE_*` 一致），测试用环境变量覆盖。
- prompt metadata 传递：PromptResolver 返回 `PromptBundle.metadata`，由 `core/observability` 的 tracing 层在 turn trace 中写入；`core/prompts` 不反向依赖 observability。
- Langfuse 收口：绿地直接以 `core/integrations/langfuse` 为唯一收口，无迁移顺序问题（§16）。

仍留给落地实现的工程细节（非决策模糊）：具体 Pydantic 字段命名、fallback 文件内容文案、测试 fixtures 组织。

## 18. 第一版推荐路线

第一版推荐路线：

1. 将 `AgentProfile` 明确为 Agent 能力边界的一等事实源，并更新中文 docstring。
2. 移除 `AgentProfile.goal`，替换为 `description`；绿地一次性同步修改全部调用点（`agent_profile.py` 两个工厂、`delegate_agent_profiles.py` 三个工厂、`system_prompt_builder.py:80` 的 `_agent_identity`、以及全部相关测试），不保留 `goal` 字段（详见 §4）。
3. 为 `AgentProfile` 增加 `capabilities`、`recommended_use_cases`、`constraints`。
4. 为 `AgentProfile` 增加严格 `prompt_ref`，绑定 `name`、`label`、`fallback_path`、`variables_schema`。
5. 新增 `AgentProfileToolSummary` 或 `to_tool_summary()`，只输出 tool description 允许暴露的稳定字段。
6. 新建 `core/prompts` 边界，支持 provider 协议、本地 fallback、变量 schema 校验和 prompt metadata。
7. 新建或规划 `core/integrations/langfuse`，作为 Langfuse SDK 唯一 import 收口。
8. 保持 `SystemPromptBuilder` 负责最终组合，按 stable prefix + dynamic suffix 输出 system prompt。
9. 保证 `workflow` 和 `context_policy` 完全不进入 system prompt，也不进入 child Agent tool-facing summary。
10. 为 developer、developer_pro、delegate_reviewer、delegate_analyst、delegate_coder 各准备一个 fallback markdown prompt。
11. 将 `delegate_task` 参数调整为 `child_agent_id + title + objective + rules + references + expected_output`。
12. 执行 `delegate_task` 前校验 `child_agent_id` 已注册在 `AgentProfileRegistry`。
13. 为 `delegate_task` 参数设置预算上限，并在 schema/description 中让父 Agent 可知。
14. 通过 `ToolDefinition.description` 暴露可委派 child Agent 摘要，摘要来自专用 projection。
15. 由装配层生成并注入 child Agent 摘要，避免 `tools -> core` 反向依赖。
16. 将 prompt metadata 写入 runtime trace / Langfuse metadata。

第一版不做：

- 不做 UI prompt 在线编辑。
- 不做 prompt A/B 实验。
- 不做复杂 provider prompt 矩阵。
- 不让 service/tools 层直接依赖 Langfuse。
- 不把工具 schema 复制进 system prompt。
- 不让父 Agent 通过工具参数临时扩大 child Agent 能力。
- 不把 `rules` 当成绕过 child AgentProfile 限制的机制。
- 不做大规模子 Agent 渐进检索，只预留方向。

## 19. 原审查问题闭合清单（绿地校准）

本方案经第零铁律审查后修订，下列原问题已在本方案内闭合（非推迟到技术方案）：

| 原问题 | 闭合条款 |
| --- | --- |
| `AgentProfile` 目标形态剥离 `turn`/`context_excluded_turn_ids`/`runtime_event_loop` 后无归宿，`ChildAgentProfileBuilder` 无法工作 | §4 新增 `ChildRunContext`（`core/delegation/child_run_context.py`），`build` 返回 `(profile, run_context)`；并附调用方同步改写清单（`delegation_executor.py:135-141` 解构、`run_child` 签名扩展、`:110-120/133/159` 的 `runtime_event_loop` 改从 `child_run_context` 取）；`AgentProfile` 保持不可变事实源 |
| `SystemPromptBuilder` 事实误述（误称 workflow 从 prompt 移出） | §2/§3.3 校准：`_workflow_contract` 为静态 section，workflow 起点也没进 prompt；目标保持不进 |
| `prompt_ref` 装配链路断裂（谁调 PromptResolver、谁组 PromptRenderContext） | §9.1 给出完整装配时序：`configuration.py` 启动期缓存 `PromptBundle`，`RuntimeContext` 构建时组装 `PromptRenderContext`，调用点 `runtime_context.py:270` |
| 工具能力交集被当成新决策重复发明 | §3.4 显式复用既有 `DelegationPolicy.resolve` + `DelegationPolicyContext`，仅收回模型传入入口 |
| `tools→core` 依赖方向误述（假设 `delegate_task.py:8` 已破现状） | §2/§3.5/§13 已核实：tools 层经 `execution_context.runtime_dependencies` 注入执行器，不 import registry，与现状一致，无需改 import |
| token cache 自相矛盾（`date.today()`/`workspace_root` 在 prefix） | §3.3 明确这两项归入 dynamic suffix |
| `AgentProfileToolSummary` 定位模糊（dataclass 还是方法） | §3.5/§13 定为独立 dataclass（`core/agents/agent_profile_tool_summary.py`）+ `to_tool_summary()` |
| `rules`/`references` 预算与类型全推迟到技术方案 | §12 给推荐默认值 + `references` 类型白名单 `ReferenceItem` |
| `provider_overlay` 与 `core/llm` 运行时行为双源 | §8.1 定调：运行时行为以 `core/llm` 为准，prompt 仅做对齐说明 |
| Langfuse 边界升级「甩给技术方案」 | §16 绿地直接以 `core/integrations/langfuse` 为唯一收口，无迁移期 |
| `fallback_path` 目录不存在、解析根含糊 | §5/§10 明确 `core/prompts/templates/` 包内资源路径，随首批文件创建 |
| `goal→description` 兼容性过渡含糊 | §4/§18 绿地一次性重命名并列出全部调用点，无兼容层 |
| `delegate_task` 参数重构（`prompt`/`delegation_type`/`requested_tools`→结构化契约）未列 `DelegationExecutor` 消费迁移 | §11 附迁移映射：`delegate_task_args.py` 改字段、`delegate_task.py:64-71` 校验同步、`delegation_executor.py:114-117/123` 旧字段消费改写、`delegation_type` 改由 child `AgentProfile` 派生 |
