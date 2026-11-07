# 输入边界分叉改造验收

> 2026-11-08：切点已改为回合边界（见 [agent-fork-task-design.md](agent-fork-task-design.md)）。本文是输入边界版本的验收记录，其中「输入切点」「首条输入之前得到空对话」已不适用。

验收日期：2026-11-07（含整体 review 收口复验）。实现说明见 [agent-fork-task-design.md](agent-fork-task-design.md)。本次未提交 Git，也未清理或修改用户现有运行数据库。

## 整体 review 收口复验（最新）

- 删除独立 `InheritedTurn`、`HISTORY_VIEWS` 和浏览器历史专用解析器；继承与本地记录共用布局、工具注册表、展开状态和虚拟估高。仅在活执行能力边界禁止订阅/操作，配置提案也不读取来源的保存状态。
- 删除 `TaskFork.tool_call_id` 及透传；Task API fork 只投影侧栏树父节点。`TaskExtra.team_node` 收窄为 `TaskNodeConfig` 的四项实际执行配置，不复制 Team 目标、身份、工具与模型引用。
- 继承上下文的单条用量可读，但不把 `run_id=null` 当成同一 Run 计算缓存率差分。
- **更正 review 第 2 项**：先前仅复现了快照保留失效模型 ID，漏读消息准备的现有校验，不能据此推断消息会入队卡住。真实 `send_message → prepare_agent_message → check_launch_config` 在入队前拒绝失效模型；本轮补回归证明没有入队，也通过真实 HTTP 验证用户换模型后继续。没有增加重复校验或 JSON 删除同步。

本轮结果：

| 检查 | 结果 |
| --- | --- |
| fork / TaskExtra / Agent 消息 / Team 节点配置与后台 / 命令链相关后端回归 | 127 passed |
| 前端全部单元测试 | 130 文件，910 passed |
| fork、统一时间线展示与侧栏 Chromium 验收 | 29 passed |
| 重新构建后真实 Electron + FastAPI + SQLite + SDK 链路 | 1 passed；另验证删除模型时 400、无 Run/排队消息，选择新模型后完成 |
| 生产构建与 TypeScript | 通过；仍有 CSS 与包体大小提示 |
| TaskExtra / task_launch / TaskResponse 类型检查及相关 Ruff | 通过 |
| diff 空白检查 | 通过 |

浏览器新增用例实际验证文件路径链接、行号与截断提示、失败工具仍显示输出、终端阅读器、quiet 调用保持隐藏、配置提案不误标保存状态，以及滚出虚拟窗口使消息卸载后再滚回仍保留展开状态；监控来源 Task / TeamRun 请求，确认未访问来源。截图在 `apps/desktop/test-results/slot-12/` 对应 `agent-fork` 用例目录，`fork-inherited-tools.png`。

本轮日志：`/tmp/fork-review-backend.log`、`/tmp/fork-review-unit.log`、`/tmp/fork-review-browser-final.log`、`/tmp/fork-review-desktop.log`、`/tmp/fork-review-build.log`。以下旧批次结果作为历史记录，不能与本轮重复累计。未提交 Git，未清理用户数据库。

## 已实现的用户行为

- 真人输入和 Agent 任务说明都能作为切点，严格继承切点之前的保留上下文；切点本身及之后内容不复制。
- Agent 自动采用调用之前最近的本地有效输入作为边界，显示轻量来源提示；新的任务说明必须自足。手动分叉完整展示原本可见的继承历史，等待用户输入。
- 手动分叉 Agent 子任务时，原先隐藏的继承历史重新可见。首条输入之前也能分叉，得到空对话。
- 历史只归目标 Task 所有，不克隆可执行 Run；历史工具不能继续审批、取消或恢复，也不订阅来源任务的活状态。
- 初始模型、推理档位与工具选项独立冻结；来源任务删除、后端重启不破坏新分支。

## 真实桌面验收

用例：`apps/desktop/tests/desktop-e2e/desktop-smoke.spec.ts:346`，`fork context: input boundary, presentation, restart and source deletion`。

执行命令（`apps/desktop`）：

```sh
npx playwright test -c playwright.desktop.config.ts tests/desktop-e2e/desktop-smoke.spec.ts -g 'fork context' --workers=1
```

结果：**1 passed，10.5 秒**。不是只测前端假数据：启动隔离数据目录中的 Electron、真实 FastAPI、SQLite、Task actor、fork 工具及 OpenAI SDK。仅模型输出由本机确定性服务提供，不调用付费模型。

| 场景 | 实际断言 |
| --- | --- |
| 背景 A → 触发 fork 的用户输入 B → 子任务说明 C | 捕获子任务实际模型请求：有 A 及其回复，有 C，无 B，无触发 fork 的工具调用 |
| 打开 Agent 子任务 | 页面有来源提示和子任务回复，没有继承用户气泡；snapshot 不投影 A |
| 点击 Agent 任务说明上的分叉按钮 | 页面重新显示 A 及其回复，不继承 C；新分支没有活动 Run，继承输入没有编辑重跑入口 |
| 真人输入切点及第一条输入切点 | 前者显示 A、不含 B；后者消息为空 |
| 后端重启 | generation 增长且 ready 后重新取状态；手动分支仍无活动 Run，Agent 隐藏策略仍成立 |
| 删除来源后通过真实输入框发送 | 捕获实际请求仍含 A、不含 B；使用被冻结的模型及 low 推理档位，而不是模型目录中排在第一位的另一模型 |

截图（Playwright 产物，后续运行可能覆盖）：

- [Agent 浅色界面](../../apps/desktop/test-results/desktop-smoke-fork-context-0f05f-restart-and-source-deletion/fork-agent-light.png)
- [Agent 深色界面](../../apps/desktop/test-results/desktop-smoke-fork-context-0f05f-restart-and-source-deletion/fork-agent-dark.png)
- [手动分叉完整历史](../../apps/desktop/test-results/desktop-smoke-fork-context-0f05f-restart-and-source-deletion/fork-manual.png)

模型请求在用例中逐项断言，并作为 `actual-model-requests` 测试附件提供。截图只证明展示，边界正确性由实际请求断言证明。

## 自动回归结果

| 检查 | 结果 |
| --- | --- |
| fork、消息、上下文、恢复、snapshot、删除、命令链、事实模型、派生与 TaskType 回归 | 322 passed，1 deselected |
| 前端全部单元测试 | 130 文件、910 项通过 |
| fork 与 Task 类型消费方浏览器测试 | 20 passed，含历史截图、静态操作隔离、运行/挂起中分叉、迟到响应不抢页面、侧栏、跨工作区切换及只读子任务 |
| 上一轮侧栏、布局隔离、子任务和通用前端扩大检查（本轮未重跑） | 26 passed、7 failed；7 项均在通用 `frontend-regressions.spec.ts`，见下文 |
| TypeScript 检查及生产构建 | `npx tsc --noEmit`、`npm run build` 通过；构建仍有 CSS/包体大小提示 |
| task_extra、task_kind、fork_history、task_launch | strict mypy、Ruff 通过 |
| 工作区 diff 空白检查 | `git diff --check` 通过 |

后端重点覆盖：继承上下文中同号工具按出现位置配对、继承记录不能被本地结果回写或编辑重跑修改、缺失工具结果闭合、孤儿结果事务回滚、跨仅有结果的 Run 寻找输入、不可退回继承输入、来源删除、首输入与 Agent 输入、请求取消与工作区闸门；新增中断输出经 Agent 隐藏后手动再分叉不丢失，以及五种 TaskType 对齐能力表与响应 schema、TaskExtra 的数据库往返、类型与元数据错配拒绝、旧 created_by 拒绝、改挂不改来源、Team 运行配置不投影。

## 未通过的扩大检查及边界

不能据此宣称全仓测试全绿。当前工作区还包含其他未提交改造，本次保留这些改动；以下是上一轮扩大检查记录，本轮没有重复运行这些已定位的无关失败：

1. 前一轮后端全量尝试曾在 `test_agent_team_background.py` 公共 fixture 被阻塞。本轮为验证 Team 节点实体化，给 fixture 补齐当前协议要求的 `intent`；Team 后台回归已通过并计入 322 项。未再跑全量套件，不能把相关回归通过表述成全量通过。
2. `test_task_workspace_deletion.py:537` 仍断言 `background_registry.drop_task(task) == []`，当前返回的是映射；相关 322 项批次显式排除这一项，不隐瞒为通过。
3. 通用前端 7 项失败位于 `frontend-regressions.spec.ts` 的 47、71、111、139、196、243、267 行，涉及思考项、工具分组展开、当前动作和深色过程区。实际症状为旧选择器不存在、过程内调用行隐藏或点击超时；均使用本地对话 fixture。当时的继承独立渲染路径现已在 review 收口中删除。139 行用例已把旧 fork 按钮完成信号改为 canonical activity，但仍在展开后调用行不可见处失败。未建立整个混合工作区的干净基线，因此不把这些结果表述为已经证明的历史失败。

最终复验日志留在本机 `/tmp/task-five-regression.log`、`/tmp/task-five-unit.log`、`/tmp/task-five-browser.log`、`/tmp/task-five-desktop.log`、`/tmp/task-five-build.log`、`/tmp/task-five-tsc-final.log`、`/tmp/task-five-mypy.log`。上一轮通用前端扩大检查日志是 `/tmp/fork-consumer-ui.log`。

## 数据与范围

最终方案已移除 `InheritedHistoryModel`、`inherited_history_id` 和消息级 history 协议。**不新增数据库表或 context 字段。** Agent fork 使用现有 `project_to_transport=False`；继承副本的 `run_id=None`，首次本地执行才创建 Run。来源 Run 的结局、总用量、耗时不再额外复制。五种 Task 类型统一由 `TaskType` 定义，fork 不再保存 created_by，`TaskRecord.extra` 使用严格的 `TaskExtra` 实体。

五种 Task 类型与实体化 extra 改变了持久化值和 JSON 契约，旧类型/旧元数据及旧 fork 请求不设兼容分支；不应把表结构未变理解为旧运行数据可直接使用。所有真实验收使用隔离数据目录，未重置用户现有数据库。

不保证模型永远不会主动再次分工；保证旧触发输入不会通过继承自动进入新子任务。没有禁止嵌套 fork，也没有用前端隐藏代替模型上下文边界。
