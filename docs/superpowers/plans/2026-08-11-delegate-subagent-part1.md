# Delegate Subagent Part 1: Child Agent Transformation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> 本计划是「Langfuse Prompt Registry 与 AgentProfile 绑定方案」的**第一部分**，与第二部分（接入 prompt 管理）解耦，可独立交付、独立测试、独立审查。

**Goal:** 把 `delegate_task` 从「父 Agent 强塑造子 Agent（自由 prompt + requested_tools + delegation_type）」改造为「父 Agent 选择已定义能力的子 Agent（结构化任务契约）」。让 `AgentProfile` 成为子 Agent 能力的**事实源**，并把可用子 Agent 的能力摘要投影进 `delegate_task` 工具的 description，供父 Agent 在调用时知悉可选子 Agent 及其能力边界。

**与第二部分（prompt 管理）的接缝：** 本计划在 `AgentProfile` 上新增 `prompt_ref: PromptRef | None` 字段，但**第一部分不消费它**——`PromptRef` 仅作为纯数据类定义（name/label/fallback_path/variables_schema），不含任何 Langfuse 依赖。第二部分才引入 `core/prompts` 的 `PromptResolver`/`LocalPromptProvider` 去读 `prompt_ref.fallback_path`、接 Langfuse。这样第一部分功能完整可跑，第二部分是「换 prompt 供给源」的增强，互不阻塞。

**Tech Stack:** Python 3.11 / FastAPI / pytest / uv；React + TypeScript + Vite（仅同步 schema 生成类型）。

---

## Global Constraints

- 第零铁律：所有取舍以“方便项目长期稳定迭代”为最终目标。禁止为了小改动留下并行运行时、全局 mutable handler 或不可观测黑盒。
- 后端依赖方向：`tools -> config/models/utils/trace_infra`，`tools` 必须**不导入** `service` 或 `core`。子 Agent 摘要投影必须落在 `core/agents/`（core 层纯函数），由 `config/configuration.build_agent_registry` 调用（config→core，合法）投影后存入 `config/configuration` 单例，再由 `tools/tool_system.py` 的 `ToolSystem.build_tool_system` 读取单例并注入 `build_delegate_task_definition(agent_summary=...)`（tools→config，合法）。`DelegateTaskTool` 增加构造参数 `description: str | None = None` 以覆盖类属性，确保注入生效，避免 `tools` 反向依赖 `core/agents` 注册表。
- `AgentProfile` 改动必须同步 `to_dict()` 与 `AgentProfileResponse` schema（及 `apps/shared/ts/agents.ts` 生成类型），保持 API 契约一致。
- 新字段/方法必须有完整中文 docstring（参数/返回/异常/副作用）与类型注解；`ruff` + `mypy` 通过。
- 日志使用 `from app.config.logging.logger import log`，英文 snake_case 事件键 + 中文 `msg` + `data` 承载业务 ID。
- 预算上限：通用一套合理默认值（见 Task 2）。上限机制本版落地，具体数字后续可经配置调整。
- 不修改委派执行链路的已落地骨架（delegation_service / child_agent_runner / 事件 / SSE / 前端 timeline）；只调整参数形态、child 输入拼装、policy context、system prompt 来源。

---

## File Structure

Backend files to create:

- `apps/backend/app/core/agents/prompt_ref.py`: `PromptRef` 纯数据类（name/label/fallback_path/variables_schema），零 Langfuse 依赖。
- `apps/backend/app/core/agents/agent_profile_tool_summary.py`: 从 `AgentProfileRegistry` 投影子 Agent 能力摘要字符串的 core 层纯函数（含 `AgentProfileToolSummary` 值对象 + `project_child_agent_summary(registry) -> str`）。

Backend files to modify:

- `apps/backend/app/core/agents/agent_profile.py`: 删 `goal`，加 `description`/`capabilities`/`recommended_use_cases`/`constraints`/`delegation_type`/`prompt_ref`；新增 `to_tool_summary()`；同步 `to_dict()`。
- `apps/backend/app/core/agents/delegate_agent_profiles.py`: 三个子 profile 改用新字段形态，补 `delegation_type` 与稳定能力字段。
- `apps/backend/app/core/agents/agent_profile.py` 内的 `default_developer_agent()` / `developer_agent_pro()`: 补 `capabilities`/`constraints`（供 `SystemPromptBuilder` 消费，避免 None）。
- `apps/backend/app/config/configuration.py`: `build_agent_registry()` 注册后投影摘要 → 新增 `set_delegate_agent_summary` / `get_delegate_agent_summary`；`build_delegate_task_definition(agent_summary=...)` 注入点落在 `ToolSystem.build_tool_system`（`tools/tool_system.py`），该方法读取 `get_delegate_agent_summary()` 后注入，不新增 `configuration.build_tool_system` 方法。
- `apps/backend/app/tools/tool_models/delegate_task_args.py`: 参数改 `child_agent_id`/`title`/`objective`/`rules`/`references`/`expected_output`；删 `delegation_type`/`requested_tools`/`prompt`；加预算校验 validator。
- `apps/backend/app/tools/tool_handler/delegate_task.py`: `DelegateTaskTool` 增加 `description` 构造参数覆盖类属性；`build_delegate_task_definition` 由现状无参改为接收 `agent_summary: str = ""` 生成 description（现状无参，本计划新增加入参）；`DelegateTaskHandler.execute` 透传结构化字段。
- `apps/backend/app/core/delegation/delegation_executor.py`: 用结构化字段拼 child `input_text`；`delegation_type` 从 child profile 派生；去掉 `requested_tools` 入参；child `agent_id` resolve 失败时写日志并返回错误观察。
- `apps/backend/app/service/delegation/delegation_context.py`: 删 `DelegationPolicyContext.requested_tools` 字段。
- `apps/backend/app/service/delegation/delegation_service.py`: `create_pending` 同步删除 `requested_tools` 入参。
- `apps/backend/app/core/context/system_prompt_builder.py`: `_agent_identity` 消费 `description` + `capabilities` + `constraints`（替代 `goal`）。
- `apps/backend/app/api/schemas/response/AgentProfileResponse.py`: 字段同步 `to_dict()`（`goal` -> `description` + 新增稳定字段）。
- Generated: `apps/shared/ts/agents.ts`（运行 `scripts/generate_api_ts.py` 重新生成，勿手改）。

Tests to create/modify:

- `apps/backend/tests/test_agent_profile.py`（新建或扩展）：字段契约、`to_tool_summary()` 不泄漏 workflow/prompt、`prompt_ref` 可选。
- `apps/backend/tests/test_delegate_agent_profiles.py`：子 profile 新字段形态与 `delegation_type`。
- `apps/backend/tests/test_agent_profile_tool_summary.py`（新建）：投影只含子 Agent、字段裁剪正确。
- `apps/backend/tests/test_delegate_task_tool.py`：新参数契约、预算校验、摘要注入 description。
- `apps/backend/tests/test_delegation_executor.py`：结构化字段拼装、`delegation_type` 派生、去 `requested_tools`、policy 仍按父/子/系统交集。
- `apps/backend/tests/test_delegation_policy.py`：删 `requested_tools` 入参后的策略测试。
- `apps/backend/tests/test_system_prompt_builder.py`（新建或扩展）：`_agent_identity` 消费 `description/capabilities/constraints`。

---

### Task 1: AgentProfile 成为能力事实源（纯模型改造，零依赖）

**Files:**
- Create: `apps/backend/app/core/agents/prompt_ref.py`
- Modify: `apps/backend/app/core/agents/agent_profile.py`
- Modify: `apps/backend/app/core/agents/agent_profile_registry.py`（如有 `DEFAULT_AGENT_ID` 无关，仅确认 `register`/`resolve`/`list` 不受影响）
- Test: `apps/backend/tests/test_agent_profile.py`

**Interfaces:**
- `PromptRef`: `@dataclass` with `name: str`、`label: str`、`fallback_path: str | None = None`、`variables_schema: dict[str, str] = field(default_factory=dict)`。无方法、无 import 外部依赖；`__post_init__` 做基本非空校验（`name` 必填）。
- `AgentProfile` 字段变更：
  - 删除 `goal: str`。
  - 新增 `description: str`（注入模型上下文的运行目标/职责，替代 goal）。
  - 新增 `capabilities: list[str] = field(default_factory=list)`（该 Agent 能做什么，稳定事实）。
  - 新增 `recommended_use_cases: list[str] = field(default_factory=list)`（何时该选它）。
  - 新增 `constraints: list[str] = field(default_factory=list)`（边界/禁止项）。
  - 新增 `delegation_type: str = ""`（reviewer/analyst/coder；运行时分类标签，进 `to_dict()` 不进 `to_tool_summary()`）。
  - 新增 `prompt_ref: PromptRef | None = None`（第二部分接缝，本计划不消费）。
- 新增 `to_tool_summary(self) -> AgentProfileToolSummary`：只输出 `agent_id`/`role`/`description`/`capabilities`/`recommended_use_cases`/`constraints`/`tool_capability_summary`，**严禁包含** `workflow`/`context_policy`/`turn`/`runtime_event_loop`/`prompt_ref`。其中 `tool_capability_summary` 由 `allowed_tools`（AgentProfile 既有字段）渲染为可读字符串（例如 `"tools: read_file, write_file, ..."`），不从新增字段派生。
- `to_dict()` 同步：删 `goal`，加 `description`/`capabilities`/`recommended_use_cases`/`constraints`/`delegation_type`/`prompt_ref`（prompt_ref 序列化为 dict 或 None）；保留 `workflow` 等既有展示字段。

- [ ] **Step 1: Write failing field tests**

  断言 `AgentProfile` 不再有 `goal` 属性；`to_tool_summary()` 返回对象不含 `workflow`/`prompt_ref`，且 `tool_capability_summary` 由 `allowed_tools` 渲染；`prompt_ref` 默认值 `None` 且可传入 `PromptRef`。

- [ ] **Step 2: Run failing tests**

  预期 FAIL（字段尚未改）。

- [ ] **Step 3: Implement PromptRef + AgentProfile 改造**

  实现 `prompt_ref.py`；改写 `agent_profile.py` 字段、`to_dict()`、`to_tool_summary()`（`tool_capability_summary` 从 `allowed_tools` 渲染）；更新 `default_developer_agent()`/`developer_agent_pro()` 补 `capabilities`/`constraints`（可留空列表或合理默认，确保 `SystemPromptBuilder` 不读到 None）。父 profile 的 `delegation_type` 默认 `""`。

- [ ] **Step 4: Verify**

  `cd apps/backend && uv run pytest tests/test_agent_profile.py -v` 预期 PASS；`ruff`/`mypy` 通过。

- [ ] **Step 5: Commit**

  `git commit -m "refactor: make AgentProfile the capability source of truth"`

---

### Task 2: delegate_task 参数弱化（结构化任务契约 + 预算）

**Files:**
- Modify: `apps/backend/app/tools/tool_models/delegate_task_args.py`
- Modify: `apps/backend/app/tools/tool_handler/delegate_task.py`
- Test: `apps/backend/tests/test_delegate_task_tool.py`

**Interfaces:**
- `DelegateTaskArgs` 字段：
  - `child_agent_id: str`（必填，选定子 Agent）。
  - `title: str | None = None`（可选任务标题）。
  - `objective: str`（必填，任务目标）。
  - `rules: list[str]`（必填，约束/规则）。
  - `references: list[str]`（必填，背景/参考路径）。
  - `expected_output: str`（必填，期望产出）。
  - 删除 `delegation_type` / `requested_tools` / `prompt`。
- 预算默认值（通用一套，落地上限机制）：
  - `objective` ≤ 2000 字符。
  - `title` ≤ 200 字符。
  - `rules` ≤ 10 项；每项 ≤ 500 字符；合计 ≤ 2000 字符。
  - `references` ≤ 10 项；每项 ≤ 500 字符；合计 ≤ 2000 字符。
  - `expected_output` ≤ 2000 字符。
  - 实现：Pydantic `model_validator(mode="after")` 统一校验；超限返回明确中英文错误（英文 error + 面向模型的 reason）。校验失败时通过 `log.warning("delegate_task_args_over_budget", data={"field": ..., "limit": ..., "actual": ...})` 记录超限字段与实测长度，便于排查父 Agent 调用问题。
- 现状 `DelegateTaskTool` 的 `description` 为类属性（delegate_task.py:17），`build_delegate_task_definition()` 无参（delegate_task.py:118），`to_definition` 用 `self.description`。本计划改为：`DelegateTaskTool` 增加构造参数 `description: str | None = None`：传入时覆盖类属性 `description`，否则回退类属性（兼容现有无参构造）。`build_delegate_task_definition(agent_summary: str = "")`（计划新增入参）用注入的 `agent_summary` 拼出完整 description（含「可选子 Agent 能力摘要」+ 结构化字段 schema 说明 + 预算提示），以 `DelegateTaskTool(description=built_description)` 注入；`agent_summary` 为空时降级为通用描述（仍构造实例注入）。
- `DelegateTaskHandler.execute`：解析 `DelegateTaskArgs`，透传结构化字段给 executor（不再拼 `prompt`）。

- [ ] **Step 1: Write failing arg/validator tests**

  断言：缺 `objective`/`rules`/`references`/`expected_output` 校验失败；`rules` 超 10 项校验失败；`objective` 超 2000 字符校验失败；`child_agent_id` 必填。

- [ ] **Step 2: Run failing tests**

  预期 FAIL（旧模型仍用 `prompt`/`requested_tools`）。

- [ ] **Step 3: Implement args + handler**

  改写 `delegate_task_args.py`；`build_delegate_task_definition` 接收 `agent_summary`；`delegate_task.py` 的 `execute` 透传结构化字段（不在这里拼 child input，拼装在 executor）。

- [ ] **Step 4: Verify**

  `cd apps/backend && uv run pytest tests/test_delegate_task_tool.py -v` 预期 PASS；`ruff`/`mypy` 通过。

- [ ] **Step 5: Commit**

  `git commit -m "refactor: weaken delegate_task to structured child-agent contract"`

---

### Task 3: 子 Agent 能力暴露（tool description 投影）

**Files:**
- Create: `apps/backend/app/core/agents/agent_profile_tool_summary.py`
- Modify: `apps/backend/app/config/configuration.py`
- Modify: `apps/backend/app/tools/tool_system.py`（仅 `build_delegate_task_definition()` 调用处注入单例）
- Modify: `apps/backend/app/api/schemas/response/AgentProfileResponse.py`
- Generated: `apps/shared/ts/agents.ts`
- Test: `apps/backend/tests/test_agent_profile_tool_summary.py`

**Interfaces:**
- `AgentProfileToolSummary` dataclass：`agent_id`/`role`/`description`/`capabilities`/`recommended_use_cases`/`constraints`/`tool_capability_summary`。
- `project_child_agent_summary(registry: AgentProfileRegistry) -> str`：遍历 registry，**只筛 delegation 类子 Agent**（约定 `agent_id` 以 `delegate_` 前缀，或显式名单），用 `to_tool_summary()` 投影，拼成「Available child agents:\n- delegate_reviewer: ...\n- delegate_analyst: ...\n- delegate_coder: ...」格式字符串。
- `configuration` 新增：`_DELEGATE_AGENT_SUMMARY: str | None`、`set_delegate_agent_summary(s: str)`、`get_delegate_agent_summary() -> str`（未注入抛 RuntimeError）。`build_agent_registry()` 在 `return registry` 前投影并 `set_delegate_agent_summary(project_child_agent_summary(registry))`。
- 注入点（计划改动）：现状 `tools/tool_system.py` 的 `ToolSystem.build_tool_system(cls, client=None)` 在注册时调用无参的 `build_delegate_task_definition()`（delegate_task.py:118 现状无参）。本计划改为在该处调用 `build_delegate_task_definition(agent_summary=get_delegate_agent_summary())`（捕获 `RuntimeError` 降级为空串），经 `DelegateTaskTool(description=...)` 注入生效。`configuration` 不新增 `build_tool_system` 方法。
- `AgentProfileResponse` 同步 `to_dict()` 新字段（`goal` -> `description`，加 `capabilities`/`recommended_use_cases`/`constraints`/`delegation_type`/`prompt_ref`），并同步更新该 schema 的类 docstring（将 `goal` 描述替换为 `description` 等新字段说明，满足改模型须同步 docstring）；运行 `scripts/generate_api_ts.py` 重生成 `apps/shared/ts/agents.ts`。

- [ ] **Step 1: Write failing summary/registry tests**

  断言 `project_child_agent_summary` 只含 `delegate_*` 子 Agent、不含 developer；摘要字符串含三个子 Agent 的 `role` 与 `description`；不含 `workflow`。

- [ ] **Step 2: Run failing tests**

  预期 FAIL（模块/函数不存在）。

- [ ] **Step 3: Implement projection + injection + schema sync**

  实现 `agent_profile_tool_summary.py`；改 `configuration.py`；改 `ToolSystem.build_tool_system`（`tools/tool_system.py`）注入；改 `AgentProfileResponse`（含 docstring）；重生成前端类型。

- [ ] **Step 4: Verify**

  `cd apps/backend && uv run pytest tests/test_agent_profile_tool_summary.py -v` 预期 PASS；`ruff`/`mypy` 通过；确认 `apps/shared/ts/agents.ts` 已更新且未被手改。

- [ ] **Step 5: Commit**

  `git commit -m "feat: expose child agent capabilities into delegate_task description"`

---

### Task 4: 委派执行链适配（结构化契约落进 child turn）

**Files:**
- Modify: `apps/backend/app/core/delegation/delegation_executor.py`
- Modify: `apps/backend/app/service/delegation/delegation_context.py`
- Modify: `apps/backend/app/service/delegation/delegation_service.py`（`create_pending` 同步删除 `requested_tools` 入参）
- Modify: `apps/backend/app/core/context/system_prompt_builder.py`
- Test: `apps/backend/tests/test_delegation_executor.py`
- Test: `apps/backend/tests/test_delegation_policy.py`
- Test: `apps/backend/tests/test_system_prompt_builder.py`

**Interfaces:**
- `DelegationPolicyContext` 删 `requested_tools` 字段；`DelegationPolicy.resolve` 仅按 `parent_allowed_tools ∩ child_allowed_tools ∩ system_allowed_tools` 计算 `effective_tools`（保持既有交集语义，去掉 requested 维度）。
- `DelegationService.create_pending` 同步删除 `requested_tools` 入参（executor 调用处改为不传）；其余签名（`parent_agent_id`/`child_agent_id`/`delegation_type`/`prompt`/`effective_tools` 等）不变。`prompt` 入参保留，由 executor 传入拼装后的 child `input_text`。
- `DelegationExecutor.execute`：
  - 用 `args.objective` + `args.rules` + `args.references` + `args.expected_output` 拼装 child `input_text`（结构化文本模板，替换原 `args.prompt`）。
  - 先 `agent_profile_registry.resolve(args.child_agent_id)` 解析 child profile；解析为 None 时 `log.warning("delegate_child_resolve_failed", data={"child_agent_id": args.child_agent_id})` 并返回错误观察（不崩溃）。
  - `delegation_type` 从解析出的 child `AgentProfile.delegation_type` 派生，填入 `create_pending(...)`（去掉原 `args.delegation_type`）。
  - 构造 `DelegationPolicyContext` 时不传 `requested_tools`。
- `SystemPromptBuilder._agent_identity`：用 `agent_profile.description` + `capabilities` + `constraints` 拼装身份 section（替代 `goal`）；父/子 Agent 共用此路径，父 profile 已补 `capabilities`/`constraints`。

- [ ] **Step 1: Write/adjust failing tests**

  - `test_delegation_executor`: 断言 `delegation_type` 等于 child profile 的 `delegation_type`；child `input_text` 含 `objective`/`rules`/`references`/`expected_output` 片段；无 `requested_tools` 入参；`create_pending` 调用不再接收 `requested_tools`；`child_agent_id` 无法 resolve 时返回错误观察。
  - `test_delegation_policy`: 去掉 `requested_tools` 入参后策略仍按交集拒绝/放行。
  - `test_system_prompt_builder`: `_agent_identity` 输出含 `description`/`capabilities`/`constraints`，不含 `goal`。

- [ ] **Step 2: Run failing tests**

  预期 FAIL（executor 仍用 `args.prompt`/`args.delegation_type`；policy 仍要 `requested_tools`；builder 仍用 `goal`）。

- [ ] **Step 3: Implement executor/policy/builder adaptation**

  改 `delegation_executor.py`、`delegation_context.py`、`system_prompt_builder.py`。

- [ ] **Step 4: Verify**

  `cd apps/backend && uv run pytest tests/test_delegation_executor.py tests/test_delegation_policy.py tests/test_system_prompt_builder.py -v` 预期 PASS；`ruff`/`mypy` 通过。

- [ ] **Step 5: Commit**

  `git commit -m "refactor: adapt delegation execution to structured contract"`

---

### Task 5: 全量验证、格式化与审查闭环

**Files:**
- 仅修复测试/审查发现的问题。

- [ ] **Step 1: Backend 全量验证**

  ```bash
  cd apps/backend && uv run pytest
  cd apps/backend && uv run ruff format .
  cd apps/backend && uv run ruff check .
  cd apps/backend && uv run mypy app
  ```

- [ ] **Step 2: 前端生成类型校验**

  ```bash
  cd apps/backend && uv run python scripts/generate_api_ts.py
  ```
  确认 `apps/shared/ts/agents.ts` 与 `AgentProfileResponse` 一致，无手改。

- [ ] **Step 3: 启动集成自检（可选）**

  启动后端，调用 `GET /agents` 确认返回含新字段、子 Agent 摘要已注入 `delegate_task` 工具 description（通过工具系统单例读取 `build_delegate_task_definition` 的 `description`）。

- [ ] **Step 4: 请求独立审查子 Agent**

  派发只审查不改码不测试的审查 Agent，对照：
  - `rules/Agent代码开发规范.md`
  - 本计划 `docs/superpowers/plans/2026-08-11-delegate-subagent-part1.md`
  - 原方案 `Langfuse Prompt Registry 与 AgentProfile 绑定方案.md` 的第一部分（§3.1/§3.4/§3.5/§4/§13）
  重点：第零铁律、分层依赖（`tools` 不反向依赖 `core`）、`to_tool_summary()` 不泄漏 prompt/workflow、`prompt_ref` 不引入 Langfuse 依赖、预算校验机制、child `input_text` 拼装正确、policy 交集语义不变。

- [ ] **Step 5: 修复审查发现并复验**

  若有 findings，先补测试再改码，重跑验证，再次派发审查 Agent，直到通过。

- [ ] **Step 6: Final commit**

  ```bash
  git add <all part1 implementation and test files>
  git commit -m "feat: part1 child agent transformation for delegate_task"
  ```

---

## Self-review

- Spec coverage: 覆盖 AgentProfile 能力事实源（Task 1）、delegate_task 结构化弱化 + 预算（Task 2）、子 Agent 能力投影进 tool description（Task 3）、执行链适配 + SystemPromptBuilder 消费稳定字段（Task 4）、全量验证与审查闭环（Task 5）。
- 接缝清晰：本计划不引入 Langfuse、不改 `SystemPromptBuilder` 的 prompt 渲染来源（仍用稳定字段）、不碰 `core/prompts`；`prompt_ref` 仅作数据类预留，供第二部分消费。
- Placeholder scan: 预算具体数字已定（通用一套默认值），无 TBD/TODO。
- Type consistency: `child_agent_id`/`delegation_type`（派生）/`objective`/`rules`/`references`/`expected_output` 在 args、executor、child input_text、policy context 间一致；`AgentProfile` 新字段在 model/`to_dict()`/`AgentProfileResponse`/生成前端类型间一致。
