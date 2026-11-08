# 验收方案：Run 对前端不可见、快照以回合为身份

目标：前端不读取、不比对任何对话 Run 身份；Transport snapshot 以回合（turn）为回复身份与渲染单位；Run 只在后端承载状态。诊断与上下文观测按回合。TeamRun（`team_run_id`）不在范围内。

验收由独立子 Agent 执行，逐条给出「通过 / 不通过 + 证据（文件:行、命令输出）」。任一条不通过即不算完成。

## A. 契约（wire）

1. 快照消息不含 `runId` 键；用户消息与回复都带 `turn: int`（≥1）与 `inherited: bool`。前后端快照校验（`conversation_state_snapshot.py`、`snapshot-validation.ts`）的精确键集合与此一致。
2. `activity` 不含 `runId`，带 `turn: int | null`（当前 Run 正在写的、最后一个可见回合）与 `resumable: bool`。
3. 回复 id 只有 `assistant-turn-{n}` 一种；用户消息 id 仍是队列行 id。
4. fork 请求的 `replyMessageId` 只接受 `assistant-turn-{n}`；指向已撤回回合时被拒（不切到已撤回的回合）。
5. `/context-messages` 按 `task_id` + 可选 `turn` 过滤；响应行带 `turn`、不带 `run_id`；按回合查询时附所属 Run 的状态事实（不含 run id）。
6. `AgentTeamRunResponse` 不再含 `conversation_run_id`。

## B. 后端结构（删除项必须不存在）

用 grep 核对以下已删除：`leading_reply_id`、`ForkReply`（`models/fork_reply.py`）、`_plan_historical_status`、`last_assistant_index`、`InputConsumedEvent.opens_turn`、`is_current_run` / `active_run_id` 中的 Run 号比对、`finalReply` 的置 False 扫描循环、用户输入投影里「替换末尾空回复占位」的逻辑、`rebuild_state` 中沿用 `modelActivity` 的分支、看板里的 `runId is None` 判断、`/context-messages` 的 `run_id` 参数与 run 反查。

核对新增：`InputConsumedEvent.turn` 与三类模型流事件的 `turn`；纯函数 `turn_withdrawn`（`models/conversation_task_context.py`，fork 直接用；恢复与冷重建经 `run_last_turn_withdrawn` 共用）；投影器维护 `activity.turn` 与 `activity.resumable`，实时与冷重建同一规则。

## C. 前端结构

1. `apps/desktop/lib`、`components`、`src` 中不再出现对话 Run 身份：grep `runId` / `run_id` 的命中只允许是 TeamRun（`teamRunId` / `team_run_id` / Agent Team API 的 TeamRun 路径），日志与上下文观测里也不再有对话 run id。
2. 已删除：`lastAssistantIdsByRun`、`outcomeStatusOf`、Run 切换补发 settled 的分支、`isRunEnd`、用户消息 `custom.runId`、`canResumeStoppedRun` 的推断逻辑（改读 `activity.resumable`）、command-request-store 的 runId 对账。
3. 继承判断读 `inherited`；当前回复 / 临时活动槽 / 流式合帧按 `activity.turn` 与回复 `turn` 判断；脚注与诊断在 `outcome !== null` 或为当前回复时显示。
4. 诊断复制内容为 `task_id` + 回合号 + 状态；观测链接为 `?task_id&turn=`。

## D. 行为（须有测试覆盖并通过）

1. 实时投影与冷重建一致（`test_live_cold_parity`），新增覆盖：恢复已取消 / 失败的 Run；同一 Run 内连续两次立即发送（由输入开启的回合各自保留回复）；多回合 Run 的结局只落在最后一个可见回合的回复上。
2. 结局、恢复清空结局、结构化结果更新都落到 `activity.turn` 的回复；Run 没有可见回合时不写结局。
3. `finalReply` 只按 `sourceSequence` 置真，不清除历史 Run 的标记。
4. `activity.resumable`：cancelled / failed 为真，最后一个回合被撤回后为假，running / suspended / completed 为假。
5. 「停止中」在活动离开活动态时结束；对话订阅断开（含背压重新同步）时这个 Task 的停止转为待确认（可重试），不依赖 Run 身份。
6. fork 按回合定位，来源与分支中同一条回复 id 相同。

## E. 质量门槛

1. 后端：`uv run pytest` 全量只允许改动前即存在的 5 个失败（`test_live_cold_parity::test_a_failure_before_the_response_is_committed_settles_its_calls`、`test_model_request_cancellation` 的两个 timeout 参数、`test_tool_call_chain_audit` 的两个 `test_t2_disabled_*`）。
2. 前端：`npx tsc --noEmit -p .`、`npx eslint lib components src tests` 无新增错误；`npx vitest run tests/unit` 全过；无头 E2E `npx playwright test` 全过。桌面 E2E 不运行（需用户许可）。
3. 改动文件中的 docstring / 注释与代码事实一致；AGENTS.md 中 snapshot、回合、诊断相关描述已更正且篇幅不增。
4. 不夹带与本目标无关的改动。

## F. 刻意不做（验收时不应出现）

快照嵌套为 `turns` 结构；为「只有结局」的回合单独建回复；把 turn 写进日志上下文；改动 TeamRun 契约；修改「隐藏通知单独开回合」的回合规则。
