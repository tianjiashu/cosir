# delegate_task 工具真实链路端到端测试方案

> 目标：在**无 mock** 前提下，把 delegate_task 工具调用一路真实跑通——从工具入口
> DelegateTaskTool.execute → 真实 DelegationExecutor → 真实 DelegationService
> /TurnService（SQLite 落库）→ 真实 AgentRuntime.run_agent → 真实 LangGraph
> StateGraph（AsyncSqliteSaver checkpoint）→ 真实 DeepSeek LLM → child turn 落库
> → observation 归一化回父工具。
>
> 测试工作区固定使用 G:\code\TradingAgents（真实可读取的 GitHub 项目），child Agent
> 可直接调真实委派对象（delegate_reviewer / delegate_coder / delegate_tester /
> delegate_analyst），以验证其真实行为。

---

## 一、什么算「无 mock」

本方案刻意**不替换**以下任何协作者，全部使用生产真实实现：

| 链路节点 | 真实来源 | 备注 |
|---------|---------|------|
| 工具 handler | apps/backend/app/tools/tool_handler/delegate_task.DelegateTaskTool | 经 build_delegate_task_definition().handler 进入 |
| 委派执行器 | apps/backend/app/core/delegation/delegation_executor.DelegationExecutor | 生产构造签名 |
| Agent 目录 | get_agent_registry()（build_agent_registry 播种的 6 个内置 agent） | delegate_reviewer 等真实存在 |
| 委派 service | get_delegation_service()（真实 DelegationService + DelegationCrud） | SQLite 落库 |
| Turn service | get_turn_service()（真实 TurnService + TurnCrud） | child turn 真实落库 |
| Child runner | ChildAgentRunner(runtime.run_agent, should_cancel=cancellation_registry.is_cancelled) | **完全等价生产装配**（`apps/backend/app/core/runtime/runner.py` 中 DelegationExecutor 即以此签名构造；`ChildAgentRunner.__init__` 见 `apps/backend/app/core/delegation/child_agent_runner.py:18`） |
| 运行引擎 | apps/backend/app/core/runtime/runner.AgentRuntime（无参构造，取全局 service 单例） | child 真实跑 ReAct |
| 编排 | agent.workflow.run → LangGraph StateGraph + AsyncSqliteSaver | checkpoint 真实落盘 |
| 模型 | build_chat_model（DeepSeek，api_key_env 读环境变量） | 真实联网调用 |
| 持久化 | Settings.DATABASE_FILE / CHECKPOINT_FILE / LOG_DATABASE_FILE 全部 override 到临时目录 | 不污染开发库 |

**唯一允许「非真实」的只有外部环境**：LLM 本身无法离线，因此本测试是一条
**需要联网 + DEEPSEEK_API_KEY 的冒烟测试**，用 @pytest.mark.llm 标记，默认不进普通
pytest（符合项目「桌面单用户本地工具」取向，避免无 key 时崩）。

---

## 二、装配顺序与 executor 注入（关键不变量）

### 2.1 全局装配顺序

AgentRuntime.__init__ 在构造时**直接取全局 service 单例**
（get_task_service()/get_turn_service()/get_workspace_service()/get_agent_registry()）。
因此装配顺序必须是：

1. Settings.override(DATABASE_FILE=..., CHECKPOINT_FILE=..., LOG_DATABASE_FILE=..., LOG_DIR=...) → 指向 tmp_path；
2. init_storage()（建表，apps/backend/app/storage/store_engines）；
3. initialize_service_dependencies()（装配全局 service 单例，指向上述临时库）；
4. AgentRuntime()（此时其私有协作者全部指向临时库）；
5. 真实 create_workspace(root_path="G:\code\TradingAgents")；
6. 真实 create_task + create_turn（父 turn 必须 claim_pending_turn 进入 running 态，
   因为 DelegationExecutor 依赖 parent_turn 边界与 parent_turn.parent_turn_id 判定深度）；
7. 测试方**自己构造** DelegationExecutor 并塞进 execution_context（见 2.2）；
8. 调用 handler(...) 真实跑通；
9. teardown：close_service_dependencies() + service_depends reset + Settings.load() 复位。

> 注意：build_checkpointer() 读 Settings.CHECKPOINT_FILE（apps/backend/app/core/runtime/checkpointer），
> 必须在步骤 1 一并 override，否则 LangGraph 会写默认 storage/langgraph_checkpoints.sqlite。

### 2.2 为什么必须自己构造 executor（不走生产自动装配）

生产路径中，DelegationExecutor 由 AgentRuntime._build_operations 在 run_agent 内部自动构造，
**仅当 execution_context 非空时**注入 ToolRuntimeDependencies，服务于 run_agent 内的子工具调用。
但本 e2e 是从 **delegate_task 工具入口**进入（直接调 handler.execute），**不经过 run_agent 的父 turn
装配**——此时 execution_context 是调用方（测试）自己构造并传入的，里面必须自带 executor。

因此步骤 7 的精确做法是：

```python
from apps.backend.app.core.delegation.delegation_executor import DelegationExecutor
from apps.backend.app.core.delegation.child_agent_runner import ChildAgentRunner
from apps.backend.app.core.runtime.turn_cancellation_registry import cancellation_registry
from apps.backend.app.tools.schemas import ToolExecutionContext
from apps.backend.app.tools.schemas.tool_runtime_dependencies import ToolRuntimeDependencies

# 真实 child runner，完全等价生产 runner.py 的 DelegationExecutor 构造
child_runner = ChildAgentRunner(
    runtime.run_agent,
    should_cancel=cancellation_registry.is_cancelled,
)
# 真实 executor（生产构造无 policy 参数）
executor = DelegationExecutor(
    child_runner=child_runner,
    parent_profile=parent_profile,          # registry.resolve("developer")
    parent_turn=parent_turn,                # 步骤 6 已 claim 的父 turn
    parent_task=parent_task,                # 步骤 6 的父 task
)
# execution_context 必须带 workspace 边界 + 真实 task/workspace id + 注入的 executor
execution_context = ToolExecutionContext(
    task_id=parent_task.task_id,
    workspace_id=workspace.workspace_id,
    workspace_root="G:\\code\\TradingAgents",
    is_write_allowed=True,
    is_modify_allowed=True,
    is_delete_allowed=True,
    runtime_dependencies=ToolRuntimeDependencies(delegate_task_executor=executor),
)
# handler 在 delegate_task.py:97 读 execution_context.runtime_dependencies.delegate_task_executor
observation = handler(
    child_agent_id="delegate_reviewer",
    title=...,
    objective=...,
    rules=[...],
    references=[...],
    expected_output=...,
    execution_context=execution_context,
)
```

若 execution_context 缺 workspace_root 或 runtime_dependencies 为 None，则 delegate_task.py:97/98
会直接返回 error observation（executor 为 None），链路根本不会进 DelegationExecutor——这是落地的
关键缺口，必须显式构造带 executor 的 execution_context。

---

## 三、必须覆盖的链路（测试矩阵）

### 链路 A：工具入口 → 委派创建 → child 跑通 → 终态回写（核心链路）

- A1 主路径（completed）：委派 delegate_reviewer 对 G:\code\TradingAgents 做代码审查。
  - 断言：observation.status == "success"、data["status"] == "completed"、
    data["delegation_id"] 非空。
  - 断言：真实 delegations 表记录 status == "completed"，child_turn_id 已回写。
  - 断言：真实 turns 表存在对应 child turn，且 execution_status 为终态。
  - 断言：真实 runtime_events 表存在 delegation_started + delegation_finished。
  - 断言：child turn 在 workspace 内产生了真实 runtime 事件流（至少 RUN_STARTED + RUN_FINISHED）。

### 链路 B：child 行为正确性（针对 TradingAgents 工作区）

- B1 reviewer 行为：委派 delegate_reviewer，objective 指向「审查 TradingAgents 的主入口文件」。
  断言 child 返回的 final_response 内容确实提及了 TradingAgents 相关的文件/模块/风险，
  而非泛泛空话（验证 child 真实读取了 workspace，而非编造）。
- B2 coder 行为：委派 delegate_coder，objective 指向「在 TradingAgents 内新增一个小工具函数」。
  断言 child 真实调用了 write_file/patch 工具（tool_call_started 事件含 write_file/patch），
  且生成文件落在 G:\code\TradingAgents workspace 边界内（由 ProjectPathResolver 约束）。
- B3 tester 行为（弱断言）：委派 delegate_tester，objective 指向「检查某模块的测试覆盖」。
  断言 child 真实产生了 runtime 事件流且走完终态。注意：delegate_tester 的 allowed_tools 为
  **全工具**（define_agents.py 中 tester 未收窄工具权限），「不写文件」靠 LLM 行为而非权限约束，
  因此**不要**把「未调用写工具」作为强断言，仅做观察性记录（如断言存在 read_file/search_files 调用）。

### 链路 C：策略与边界（真实 DelegationPolicy 生效）

- C1 未知 child：child_agent_id="ghost_agent"。
  - 断言：observation.status == "error"、文案含 child not found；
    真实 delegations 表**无**新建记录（解析失败早于创建）。
- C2 深度策略：父 turn 自身已是 child（parent_turn.parent_turn_id 非空，executor 内
  depth = 1 if parent_turn.parent_turn_id else 0 → depth=1）。真实 DelegationPolicyContext.max_depth
  **默认值为 1**（delegation_context.py），depth(1) >= max_depth(1) 成立 → 拒绝。
  - 断言：observation.status == "error"、reason 含策略拒绝信息；
    真实 delegations 表无新建记录（策略拒绝早于创建）。
  - 注：落地测试必须把父 turn 构造为「自身是 child」（parent_turn_id 非空），否则深度判定为 0 不会触发拒绝。
- C3 并发额度：Settings.DELEGATION_MAX_CONCURRENCY = 0 时委派。
  - 断言：observation.status == "error"，reason 含并发额度超限；无 child turn 落库。

### 链路 D：失败与取消（真实终态回写）

> 关键事实：`ChildAgentRunner.run_child` 经 `asyncio.run` 驱动 `_consume_child_events`，后者
> 内部 try/except 吞掉 run_agent 异常，返回 `DelegationResult(status="failed")`——它**几乎不会
> 向上抛异常**。executor 的 try/except 分支（delegation_executor.py:178）只对**同步步骤**抛异常生效：
> create_child_turn / claim_pending_turn / mark_child_started / ChildAgentProfileBuilder.build。
> child 运行本身的失败走 `_finalize_result` 的 `result.status=="failed"` 分支 → mark_failed。

- D1 child 运行时失败（走 _finalize_result，确定性构造）：**不能**依赖真实 LLM 内部崩溃
  （RUN_FAILED 来自 workflow 内部异常，真实 DeepSeek 极少稳定触发）。应采用确定性强、不依赖
  LLM 行为的注入：对 `runtime.run_agent` 包一层薄 spy，使其生成的 AsyncGenerator 在首个 yield
  前直接 `raise RuntimeError("injected child workflow failure")`，令 `ChildAgentRunner.run_child`
  经 `asyncio.run` 驱动的 `_consume_child_events`（child_agent_runner.py:131 起）内部 try/except
  捕获 run_agent 异常后返回 `DelegationResult(status="failed")`（吞异常位置在 `_consume_child_events`，
  不是 run_child 自身）。
  - 断言：observation.status == "error"，delegations 表记录 status == "failed"，
    child turn execution_status == "failed"。路径为 `_finalize_result` 的 `failed` 分支（**不是**
    executor 的 try/except 同步步骤分支）。这是真实回写路径，仅 child 事件源被确定性注入，executor
    落库/归一化逻辑全部真实。
- D2 同步步骤异常（走 executor except 分支）：构造一个让 create_child_turn / claim_pending_turn
  / mark_child_started / ChildAgentProfileBuilder.build 任一同步调用抛异常的场景（例如对 DelegationService
  注入一个会抛异常的薄 spy，或直接令 child turn 的 claim_pending_turn 因并发冲突返回 False 触发
  `RuntimeError("child_turn_claim_lost")`——这是 executor 内显式 raise 的真实路径）。
  - 断言：observation.status == "error"，delegations 表记录 status == "failed"（已由 try 块内的
    mark_failed 回写），child turn 终态为 failed。验证 executor except 分支真实回写。
- D3 取消信号：在 child 运行期间经 cancellation_registry 触发 is_cancelled(child_turn_id)=True。
  - 断言：observation.status == "error"，delegations 表记录 status == "cancelled"，
    child turn 终态为 cancelled（验证 should_cancel 接线真实生效，等价于生产）。

### 链路 E：结构化输入拼装（真实 _build_agent_input_text）

- E1 全字段：传入 rules/references/background/expected_output。
  - 断言：经 `DelegationCrud().get(delegation_id).prompt` 取出的文本（注意：service 层
    `DelegationService` 只暴露 `list_by_parent_turn`/`list_active_by_parent_turn`，单条读取
    由 `DelegationCrud.get` 承担，返回 `DelegationRecord.prompt`），开头为 `# {title}`，
    并含 `## Objective` / `## Rules` / `## References` / `## Background` /
    `## Expected Output` 分段，且原文被完整保留（验证父工具→executor→service 的文本透传无截断/无注入）。

### 链路 F：观察归一化（真实 tool_success / tool_error）

- F1 success 字段形态：A1 的 observation 须含 status/data/content/summary，
  且 content 经脱敏（不泄露 secret），summary 与 data["status"] 一致。
- F2 error 三字段：C/D 类错误的 observation 须含完整 error/reason/retryable，
  且 retryable 与 reason 语义一致（符合 ToolObservation 失败契约）。

---

## 四、需要考虑的情况与风险

1. LLM 成本与稳定性：child 每轮真实跑 ReAct，会消耗 DeepSeek 额度；TradingAgents
   是大项目，child 可能跑多步。建议 objective 限定为「单文件/单模块」的小范围任务，控制步数。
2. 工作区写污染：B2（coder）会真实在 G:\code\TradingAgents 内写文件。测试后应清理
   生成物（或限定 objective 到一个临时子目录，如 G:\code\TradingAgents\.e2e_scratch）。
3. checkpoint 库隔离：必须 override CHECKPOINT_FILE，否则并发跑测试会写开发库。
4. 全局单例污染：initialize_service_dependencies 用 lru_cache 单例，teardown 必须
   close_service_dependencies() + 清 lru_cache，否则影响同进程其它测试。
5. 网络/key 缺失：无 DEEPSEEK_API_KEY 时测试应在 fixture 阶段 pytest.skip，
   而非失败（避免无 key CI 误报）。**本机已就绪**：`apps/backend/.env` 中存在
   `DEEPSEEK_API_KEY`（与 `CODING_AGENT_MODEL_BASE_URL=https://api.deepseek.com` 配套），
   测试 fixture 可直接 `monkeypatch.setenv("DEEPSEEK_API_KEY", os.environ["DEEPSEEK_API_KEY"])`
   复用该 key，无需手动 export。注意 `.env` 被 `.gitignore` 忽略，普通 grep/ripgrep 检索不到，
   方案与测试**不得**把 key 明文写入仓库（脱敏约定，见 F1）。
6. child 真实超时：大模型可能慢，pytest 超时需放宽（@pytest.mark.llm 用例单独给长 timeout）。
7. policy 默认拒绝：delegate_* 默认受 DelegationPolicy 约束（工具收敛）；C2 依赖默认
   max_depth=1，落地时父 turn 必须构造为 child（parent_turn_id 非空）才能触发拒绝。
8. execution_context 注入缺口：务必按 2.2 构造带 runtime_dependencies.delegate_task_executor
   的 execution_context，否则 delegate_task.py:97/98 直接返回 executor 缺失错误，链路不进 executor。

---

## 五、建议测试文件结构

```
apps/backend/tests/test_delegate_task_e2e.py
├── pytestmark = pytest.mark.llm
├── fixture real_runtime_stack(tmp_path, monkeypatch)
│     └─ Settings.override → init_storage → initialize_service_dependencies → AgentRuntime()
├── fixture trading_agents_workspace(real_runtime_stack)
│     └─ create_workspace(root_path="G:\\code\\TradingAgents")
├── fixture parent_turn(real_runtime_stack, trading_agents_workspace)
│     └─ create_task + create_turn + claim_pending_turn
├── fixture injected_execution_context(parent_turn, trading_agents_workspace)
│     └─ 按 2.2 构造带 DelegationExecutor 的 ToolExecutionContext（含真实 child_runner）
├── test_A1_delegate_reviewer_completes_end_to_end(...)
├── test_B2_delegate_coder_writes_file_in_workspace(...)
├── test_B3_delegate_tester_runs_to_terminal(...)
├── test_C1_unknown_child_rejected_before_create(...)
├── test_C2_depth_policy_rejects_nested_delegation(...)
├── test_C3_concurrency_limit_rejects(...)
├── test_D1_child_run_failure_marks_failed(...)
├── test_D2_sync_step_exception_marks_failed(...)
├── test_D3_cancel_signal_marks_cancelled(...)
├── test_E1_structured_prompt_preserved(...)
└── test_F1_F2_observation_shape(...)
```

> pyproject.toml 的 pytest 配置需补 markers = ["llm: 真实调用 LLM 的端到端冒烟测试"]，
> 避免 unknown marker 警告。

---

## 六、与现有测试的分工（避免重复造轮子）

- test_delegate_task_tool.py：纯单元，打桩验证 handler 外壳（executor 缺失分支、参数校验）。
- test_delegation_executor.py：半内存集成，覆盖「拒绝早于创建 + observation 形态」——即
  rejects_unknown_child（C1 等价）、rejects_policy_denial_before_create（C2 等价，断言
  `"delegation_depth_exceeded" in content`）、rejects_when_concurrency_limit_reached（C3 等价）。
  三者均基于 **Fake service**，且**只验证「早于创建即拒绝」，并未验证 delegations 表真实终态回写**
  （fake 根本没建表）：C1/C2 断言 `try_create_pending` **未被调用**；C3 则断言 `try_create_pending`
  **已被调用**但 `create_child_turn` 未被调用（额度裁决走 storage 原子 acquire，fake 仅模拟额度已满信号）。
  使用 `ChildAgentRunner(fake_run_agent)`，不碰 LLM、不碰真实 storage。
- test_delegate_task_e2e.py（本方案）：与上述两者**不重叠**的**唯一新增价值**是验证
  「真实 LLM + 真实 SQLite storage + 真实 AgentRuntime 接线」下的端到端行为——即：
  - A1：真实落库 + 真实 child 跑通（内存测试无法验证）；
  - B 系列：child 真实行为是否符合角色（内存测试无真实 child 行为）；
  - C1/C2/C3：真实 storage 下 delegations 表**确实**无新建记录（内存 fake 无从验证落库）；
  - D2/D3：真实取消信号与同步步骤异常的真实回写路径（真实 service 才落库）；
  - E1：真实 prompt 透传落库（经 `DelegationCrud.get(delegation_id).prompt` 读）。
  这些是内存 fake 无法验证的，均属非零新增覆盖，分工清晰，无平行重写。
