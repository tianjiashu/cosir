# Langfuse 后续能力接入规划

> 状态：未来规划
> 日期：2026-08-10
> 范围：本项目后端 Agent runtime、prompt 构建、质量评估、数据集回归与前端观测入口
> 目标：把 Langfuse 从单纯 trace 面板升级为 coding-agent 的质量迭代工作台，同时保持本地运行时事实源不变。

## 一、定位

Langfuse 在本项目中的定位是外部观测、Prompt 版本管理、实验评估和质量数据分析平台。

它不作为运行时事实源，不替代本地 SQLite、`runtime_events`、JSONL 日志、LangGraph checkpoint、工具审批状态或 SSE timeline。所有 Agent 执行的权威状态仍以本地数据为准，Langfuse 只接收经过脱敏和预算控制后的外部投影。

## 二、适合接入的能力

### 2.1 Agent 执行观测

当前项目已经有 Langfuse trace 接入雏形，覆盖 turn 根 observation、LLM generation 和工具 tool observation。

后续应先完成现有观测链路加固：

- Langfuse 初始化、flush、context exit 失败不得影响 turn 成功或失败语义。
- 工具 span 的 contextmanager 不能吞掉或改写工具执行异常。
- 增加统一 Langfuse payload sanitizer，覆盖工具输入、工具输出、错误信息、metadata 与 LLM generation 原文捕获策略。
- 增加 recorder 安全工厂，runner 不直接构造具体 Langfuse recorder。
- 验证 `asyncio.to_thread` 工具执行路径下 tool observation 能归入同一 turn trace。

对应现有文档：

- `docs/Langfuse可观测性集成技术方案.md`
- `docs/Langfuse Agent执行观测修复计划.md`

### 2.2 Runtime Event 投影

在现有 LLM 和工具 trace 稳定后，可以增加 runtime event 到 Langfuse 的轻量投影层，使一次 turn 能在 Langfuse 中按时间线复盘。

优先投影：

- `RUN_STARTED`
- `STEP_STARTED`
- `MODEL_CHUNK` / `MODEL_FINISHED`
- `TOOL_CALL_REQUESTED`
- `TOOL_CALL_FINISHED`
- `HUMAN_INPUT_REQUESTED`
- `RUN_CANCELLED`
- `RUN_FAILED`
- `RUN_FINISHED`

约束：

- 本地 `runtime_events` 仍是事实源。
- Langfuse 投影失败只写日志，不影响落库、SSE 或 turn 状态。
- 不在第一版强行把 tool span 嵌入对应 generation span；先用 `step_id`、`tool_call_id`、`turn_id` 建立关联。

### 2.3 Prompt Management

适合放入 Langfuse 管理的内容：

- Agent mission prompt。
- DeepSeek / OpenAI 协议 provider overlay。
- 工具使用策略提示。
- context compaction prompt。
- LLM-as-a-judge evaluator prompt。
- 实验用 prompt 变体。

不适合放入 Langfuse 的内容：

- 项目长期开发规范全文。
- `rules/` 下的硬性工程规则。
- 安全边界、工具权限、审批逻辑。
- 必须随代码审查和测试一起版本化的运行时契约。

接入建议：

- 本地 `SystemPromptBuilder` 继续提供默认 prompt fallback。
- 新增 `PromptProvider` 协议，默认实现读取本地 prompt，Langfuse 实现负责按 prompt name + label 拉取。
- 默认使用 `production` label；开发实验可使用 `staging`、`experiment-*` label。
- Langfuse prompt 拉取失败时使用本地 fallback，不中断 Agent 执行。
- trace metadata 中记录 prompt name、version、label，方便按 prompt 版本分析质量、成本和延迟。

建议 prompt 命名：

```text
agent/developer/system
agent/developer_pro/system
provider/deepseek/overlay
workflow/react/tool-use-policy
workflow/context-compaction/summarizer
eval/code-task-quality/judge
eval/rule-compliance/judge
```

### 2.4 Dataset 与 Experiment

这是最适合本项目长期迭代的 Langfuse 能力。

数据集来源：

- 真实失败 turn。
- 用户手动标记为高价值或低质量的 turn。
- 工具调用失败但最终恢复的 turn。
- 回归测试失败案例。
- prompt / workflow / tool 改造前后的代表性任务。

dataset item 建议字段：

- `task_id`
- `turn_id`
- `agent_id`
- `model_name`
- `workspace_summary`
- `user_input`
- `runtime_event_summary`
- `tool_call_summary`
- `final_response`
- `failure_reason`
- `expected_behavior`
- `tags`

实验运行方式：

- P1 只做离线 SDK 实验，不接入主运行链路。
- 用固定 dataset 重放新 prompt、新模型或新 workflow。
- 实验结果写回 Langfuse score。
- 通过 score 对比不同版本的质量、成本、延迟和失败率。

### 2.5 Evaluation 与 Score

第一阶段优先使用确定性 evaluator，避免一开始过度依赖 LLM-as-a-judge。

推荐确定性 score：

- `turn_success`: boolean，turn 是否成功完成。
- `runtime_failed`: boolean，是否出现 `RUN_FAILED`。
- `tool_error_count`: numeric，工具错误次数。
- `tool_retry_count`: numeric，同类工具失败后的恢复次数。
- `approval_interrupt_count`: numeric，审批中断次数。
- `latency_ms`: numeric，turn 总耗时。
- `input_tokens`: numeric。
- `output_tokens`: numeric。
- `estimated_cost`: numeric。
- `tests_passed`: boolean，可从终端工具输出或后续测试守卫提取。

第二阶段再引入 LLM-as-a-judge：

- `task_completion_quality`
- `rule_compliance`
- `change_scope_control`
- `verification_quality`
- `final_response_clarity`

LLM-as-a-judge 的 prompt 本身应纳入 Langfuse Prompt Management，并通过 dataset 做回归验证。

### 2.6 前端入口

后续桌面端可以增加观测入口，但不要把 Langfuse UI 当作核心产品界面。

推荐入口：

- turn 详情中显示 `langfuse_trace_id`。
- 提供“打开 Langfuse Trace”外部链接。
- task 级别提供 session 链接。
- prompt 实验入口仅作为开发者设置或调试工具，不放在普通用户主路径。

## 三、分期路线

### P0：修稳现有 Trace 接入

目标：

- 可观测性失败不影响 Agent 主流程。
- 外部观测 payload 有统一脱敏边界。
- LLM generation 和工具 observation 能稳定归入同一 turn trace。

涉及区域：

- `apps/backend/app/core/observability/`
- `apps/backend/app/core/runtime/runner.py`
- `apps/backend/app/service/tool_execution/`
- `apps/backend/tests/`

### P1：Runtime Event 轻量投影

目标：

- Langfuse trace 中能复盘 turn 主要执行阶段。
- 本地 `runtime_events` 仍为事实源。

建议新增：

- `core/observability/langfuse_runtime_event_projector.py`

### P2：Prompt Management

目标：

- 支持从 Langfuse 拉取可实验 prompt。
- 本地 prompt fallback 始终可用。
- trace 中记录 prompt 版本。

建议新增：

- `core/context/prompt_provider.py`
- `core/context/local_prompt_provider.py`
- `core/observability/langfuse_prompt_provider.py`

是否把 Langfuse prompt provider 放在 `core/observability` 还是单独 `core/prompt_management`，应在实施前按当时目录组织规则确认。原则是：Langfuse 三方 import 不扩散到业务层。

### P3：Dataset 沉淀

目标：

- 支持把当前 turn 导出为 Langfuse dataset item。
- 支持失败 case、人工标记 case 和高价值 case 沉淀。

建议入口：

- 后端 service 方法：按 `task_id` / `turn_id` 生成 dataset item payload。
- 前端调试入口：标记“加入回归集”。

### P4：Experiment Runner

目标：

- 使用固定 dataset 对比 prompt、模型、workflow 改动。
- 将实验结果写回 Langfuse score。

建议实现：

- 先做独立脚本或开发命令，不接入用户主流程。
- 实验运行产生独立 task / turn，避免污染真实任务历史。

### P5：Online Evaluation

目标：

- 对真实生产 / 日常使用 trace 自动打分。
- 发现低质量 case 后沉淀回 dataset。

约束：

- 在线 evaluator 失败不得影响 turn。
- evaluator 调用成本必须可控。
- 默认先对失败 turn、长耗时 turn、高成本 turn 抽样评估。

## 四、边界原则

1. Langfuse 是外部投影，不是事实源。
2. 本地 fallback 必须始终存在。
3. 所有发往 Langfuse 的 payload 必须脱敏、截断、预算控制。
4. Langfuse 失败只能影响外部观测完整性，不能影响 Agent 执行。
5. prompt 可以由 Langfuse 管理，但工程规则和安全边界仍由仓库版本化。
6. 先接确定性 score，再接 LLM-as-a-judge。
7. dataset 和 experiment 应服务回归验证，不应污染真实任务历史。

## 五、官方参考文档

- Langfuse Overview: https://langfuse.com/docs
- Prompt Management Overview: https://langfuse.com/docs/prompt-management/overview
- Prompt Management Get Started: https://langfuse.com/docs/prompt-management/get-started
- Prompt Version Control: https://langfuse.com/docs/prompt-management/features/prompt-version-control
- Prompt Variables: https://langfuse.com/docs/prompt-management/features/variables
- Prompt Caching: https://langfuse.com/docs/prompt-management/features/caching
- Prompt Config: https://langfuse.com/docs/prompt-management/features/config
- Prompt Management Data Model: https://langfuse.com/docs/prompt-management/data-model
- Evaluation Core Concepts: https://langfuse.com/docs/evaluation/core-concepts
- Scores Overview: https://langfuse.com/docs/evaluation/scores/overview

