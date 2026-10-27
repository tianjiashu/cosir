# Task 命令去回执与 Task 视角对话：验收标准

对应设计：`task-command-fire-and-forget-design.md`（第一期已实施，本标准覆盖剩余全部：第二期对话链路整体切换、第三期 fork 按消息切分，以及第一期遗留的 HTTP 回执删除）。验收由独立子 Agent 执行，**只读代码、跑命令，不改代码**；每条给出判定方法，任何一条不满足即判定不通过。

## 验收基线

- 基线提交：`a4c09a76`，外加未提交的第一期改动、`run_driver.py` 惰性导入修复与基线修正（见下）。
- 基线修正后，后端完整全量测试 **0 失败**（2334 passed、46 skipped、2 xfailed、1 xpassed），不再排除任何用例；`docs/plan/task-command-fire-and-forget-baseline-failures.txt` 因此为空。F1 据此要求改造后仍为 0 失败。
- 基线修正记录：
  - 测试偏移 73 个：`UserInputConsumedEvent` 只带消息身份（40）、落库 user 消息必须带 `kind`（23）、上下文服务替身缺 `session` / `message_id` 参数（10）。新增共享测试辅助 `tests/transport_fixtures.py`。
  - 代码缺陷 9 个：作用域配置目录对失效符号链接与隐藏文件的处理、`normalize_workspace` 把 `None` 字符串化、apply_patch 文档样例缺换行、`web_search` 前置检查吞掉具体原因与 `web_extract` 的死代码、空广播仍装配调度层、model 节点漏了「加载上下文后、开请求前」的取消检查（导致卡死用例）。
  - 测试偏移 5 个：配置测试误跑完整 lifespan、取消检查已移到 `prepare_input`、冷重建夹具缺 `message_id`、并发用例未容忍并发 `drop_scope` 后注销的 `KeyError`、跨作用域同名用例把「system 注册被 workspace 同名项拒绝」当成缺陷（该行为是确认保留的规则，用例改为断言拒绝，规则写入 `AgentProfileRegistry` 类 docstring）。

## A. snapshot wire 契约（Task 视角）

| # | 标准 | 判定方法 |
|---|---|---|
| A1 | 顶层键恰为 `messages`、`activity`、`error`，可选 `queued_messages`；不再有 `runs`、`current_run_id`、`approvals` | 读 `assistant_transport/state/conversation_state_snapshot.py` 的键集合与 `validate_snapshot`；前端 `lib/assistant/snapshot-validation.ts` 同步 |
| A2 | `messages[]` 扁平、按 context 序号排列；每条消息带 `runId`；每轮回复最后一条助手消息在 Run 终态后带 `outcome = { status, endReason, error, usage }`，未结束时没有 `outcome` | 读状态类型与 rebuilder；后端测试覆盖「终态写 outcome」「运行中无 outcome」 |
| A3 | `activity` 为最新 Run 摘要 `{ runId, status, endReason, error, usage, contextWindowTotal, launch }`，Task 无 Run 时为 `null`；`activity.status` 只取后端五个状态值 | 读状态类型；后端测试 |
| A4 | 增量 mutation 路径只出现 `("messages", …)`、`("activity", …)`、`("queued_messages", …)`、`("error",)`；不再出现 `"runs"` | `grep -rn '"runs"' apps/backend/app/assistant_transport` 无结果 |
| A5 | 旧 Run 的迟到状态 / 结果事件不覆盖 `activity`，只更新其消息的 `outcome` | 后端测试：两个 Run 依次结束后，回放第一个 Run 的状态事件，`activity` 不变 |
| A6 | 顶层 `error` 只承载命令被拒，由 actor 写入与清空；run 事件投影不再写它 | 读 `run_event_projection.py`；后端测试覆盖写入与清空 |
| A7 | 删除死字段与死分支：`approvals`、`RunInitializedEvent.replace_existing`、帧元数据 `source_run_id` / `current_run_id` / `current_run_status` / envelope `target_run_*` | `grep -rn "replace_existing" apps/backend/app` 与 `grep -rn "source_run_id\|current_run_status\|target_run_\|approvals" apps/backend/app/assistant_transport apps/desktop/lib apps/desktop/components` 均无结果（`core/context` 里工具调用配对的 `target_run_id` 是另一个领域概念，不在范围内） |

## B. 命令去回执

| # | 标准 | 判定方法 |
|---|---|---|
| B1 | 发送、重跑、恢复、审批决定、取消命令投递后返回 202，响应体不含 snapshot；输入解析错误仍同步 400，Task 不存在仍 404 | 读 `assistant_api.py`；后端 API 测试 |
| B2 | 编辑排队消息不经 actor，消息已并入时仍同步 409 `MESSAGE_CONSUMED` | 后端测试 |
| B3 | actor 拒绝的命令（`RUN_NOT_RESUMABLE`、`RUN_NOT_STOPPED`、`EDIT_TARGET_STALE`）写入顶层 `error`，下一次成功处理用户命令时清空 | 后端测试 |
| B4 | 信号与入口不再有回执：`task_signals.py` 无 `receipt` 字段、无 `SessionReceipt`、`resolve` / `reject` / `abandon`；`WorkflowSession` 无 `_request` / `_receipt` / `post_async` / `wait_run_settled` | `grep` 无结果 |
| B5 | `WorkflowSession` 改名为 `TaskActors`，只承担登记表与投递 | 读文件；`grep -rn "WorkflowSession" apps/backend/app` 无结果 |

## C. 长订阅

| # | 标准 | 判定方法 |
|---|---|---|
| C1 | Task 没有 Run 时 attach 不再 409，推送 full 帧后保持连接 | 后端测试 |
| C2 | Run 完成、失败、取消、挂起都不结束连接；最新 Run 变化时同一条连接继续推送 | 后端测试：一条连接上依次经历两个 Run |
| C3 | 连接只在客户端断开、`resync_required`、Task 删除时结束 | 读 `transport_stream_service.py` |

## D. 前端

| # | 标准 | 判定方法 |
|---|---|---|
| D1 | 对话界面不再按 runId 工作：`targetRunId`、`targetRunStatus`、`cancelRequestedRunIdRef`、`reconcileTerminalSnapshot`、`findTransportRun`、`getTransportRunId`、`toMessageStatusForRun`、`resumeOnMount`、取消确认轮询全部删除 | `grep` 无结果 |
| D2 | 「一轮结束」由 frame store 的 `onActivitySettled` 判定：`activity.status` 从活动态（`running` / `suspended`）进入终态时触发一次；首次导入不触发；重连拿到 full 帧时补触发一次；挂起不触发 | Vitest |
| D3 | 运行中 = 有待提交命令或 `activity.status` 为活动态（含 `suspended`） | Vitest |
| D4 | Task 页面常驻一条订阅；命令返回后不再按回执决定是否 attach；意外断开进入有限重连 | Vitest |
| D5 | 历史回复的已取消 / 失败 / 用量显示来自消息 `outcome`；fork 入口挂在带 `outcome` 的消息上 | Vitest |
| D6 | Workbench 子 Agent 只读查看常驻订阅子 Task，`AssistantAttachController` 不再按 runId 去重 | 读代码；Vitest |
| D7 | 取消与业务恢复改为 Task 级标记，不比对 runId | 读代码；Vitest |
| D8 | 顶层 `error` 在对话区显示；pending 消息在 snapshot 出现该消息或顶层 `error` 写入时清除，不在 202 时清除 | Vitest |
| D9 | 状态联合收敛到后端五个值，删除 `idle`、`interrupted`、`pending` 状态值 | `grep` 与 `tsc` |

## E. fork 按消息切分

| # | 标准 | 判定方法 |
|---|---|---|
| E1 | `POST /tasks/{id}/fork` 请求为 `{ messageId }`，不再接受 `runId` | 读 schema；后端测试 |
| E2 | 复制序号不晚于该消息的 context 与拥有这些条目的 Run；最后一个 Run 只带切分点之前的条目；被复制的 Run 未终态时 409 `RUN_NOT_READY` | 后端测试 |
| E3 | 前端 fork 入口按消息调用 | 读代码；Vitest |

## F. 质量门禁

| # | 标准 | 判定方法 |
|---|---|---|
| F1 | 后端全量测试的失败集合是基线失败集合的子集（不新增失败；被本改造重写而修好的可以减少） | 运行全量（排除卡死用例），与基线文件对比 |
| F2 | 改动涉及的后端模块通过 ruff；新增与重写的 `service/conversation_run/`、`assistant_transport/` 模块通过 `mypy.strict.ini` | 运行命令，只看改动文件 |
| F3 | 前端 `tsc --noEmit` 通过；`vitest run tests/unit` 全部通过；`eslint` 对改动文件无错误 | 运行命令 |
| F4 | 受影响的 Playwright E2E 在 WebKit 下通过（本机未安装 Chromium），失败只允许出现在基线已失败的工作区文件浏览用例 | 运行 `--project=webkit` |
| F5 | 关键流程保留结构化日志：命令被拒、订阅打开 / 关闭、`onActivitySettled` | 读代码 |

## G. 文档

| # | 标准 | 判定方法 |
|---|---|---|
| G1 | `AGENTS.md` 中「返回 JSON 回执」「回执 snapshot 只用于首次导入」「`/assistant/attach` 只订阅 Task 当前 Run」「前端只把 Run 用于渲染分组」「`WorkflowSession` … 等回执」等描述已按新契约改写 | 读文件 |
| G2 | 设计稿状态更新为已实施，并补实施记录 | 读文件 |
| G3 | 改动模块的 docstring 与代码事实一致，不残留对已删除概念（回执、`runs[]`、`wait_run_settled`、`targetRun`）的描述 | `grep` 与抽读 |

## 不在范围内

- 任务列表 API 的 `execution_status` 与 Workbench 轮询（`current_run_id` 已在第二轮修复中从响应去掉）。
- 上下文观测页的按 Run 查看。
- 修复基线里与本改造无关的既有失败。
