# Assistant Run 生命周期调研简报

> 本文是基于当前代码的改造建议，不是已经确认的架构决策。调研范围为本地桌面应用的 Assistant Transport、ConversationRun、RuntimeContextManager、LangGraph checkpoint 与 assistant-ui runtime。

## 结论

`/assistant` 已经是创建新 run 的入口，但还不是“创建 / 继续 / 编辑重跑”的统一入口。现有 `/assistant/resume` 只是在 API 层绑定已有 run 并重新订阅，执行层仍调用同一个无模式的 runtime；工作流会重新写入用户消息，且 `begin_run()` 会删除当前 run 的上下文，因此不能保证真正从 checkpoint 继续。

建议把 `/assistant` 设计成两种合法执行请求：

1. `commands=[add-message]`：创建新 run，执行模式为 `fresh`。
2. `commands=[] + runId`：继续同一个可恢复 run，执行模式为 `resume`。

编辑消息不建议修改旧 run。建议使用 `sourceId` 定位待编辑的最后一条用户消息，重建有效上下文并创建一个新的 run/checkpoint；旧 run 保留为审计事实。

## 当前调用链事实

- `apps/backend/app/assistant_transport/assistant_api.py` 的 `/assistant` 强制寻找 `add-message`，调用 `ConversationRunCommandService.start_or_attach()`，创建 run 后启动 `ConversationRunExecutor`。
- `/assistant/resume` 接受 `runId`，但只允许数据库状态为 `pending/running`，并调用同一个 `runtime.execute_run`。
- `ConversationRunExecutor` 的 runner 只接收 run，没有 `fresh/resume` 执行模式；`claim_or_resume_run()` 也只允许 pending 或已经 running。
- `RuntimeContextManager.begin_run()` 无条件删除该 run 的已有 context entries；React workflow 随后无条件追加 `run.input_text`。
- LangGraph 1.2.10 在存在 checkpoint 时，`graph.astream(None, config)` 才是继续已有 checkpoint 的语义；传入新的 state dict 会走新的输入路径。
- assistant-ui 已经会生成 `sourceId`，但当前前端关闭了 `capabilities.edit`，后端也没有消费 `sourceId`。

## 推荐的最小分阶段改造

### 第一阶段：统一创建与继续

- 请求模型允许 `commands=[]`，增加可选 `runId`。
- wire 校验：有 add-message 时必须有模型；无命令时必须有 runId；不允许无命令创建新 run。
- `/assistant` 内部分派 fresh/resume；`/assistant/resume` 暂时保留兼容转发，待前端切换后删除。
- 从 executor 到 runtime、workflow 显式透传 `execution_mode: fresh | resume`，默认 fresh 以兼容 delegation 调用方。
- `fresh` 保留当前“清理同 run context + 写入用户消息”的行为。
- `resume` 保留当前 run context，不重复写用户消息；workflow 使用 `input_state=None` 继续 LangGraph checkpoint。
- 用户停止建议引入非终态 `paused`：停止本地 task 并保存 checkpoint，不能把工具调用永久投影为 cancelled；恢复时允许 `paused -> running`。
- Assistant UI 的 `resumeApi` 改为 `/assistant`。`resumeStateApi` 可以继续作为只读的活动 run 发现接口；因此“唯一入口”应理解为唯一执行/写入入口。

### 第二阶段：编辑最后一条用户消息并重跑

- 开启 `capabilities.edit`，把 `sourceId` 传入 command service。
- 首版只支持最后一条用户消息、无 active run、无 delegation 分支；其余返回明确的不可编辑错误。
- 以 `sourceId` 定位旧用户消息，删除或标记旧 run 的有效 context entries，并通过 snapshot owner 投影一次历史 rebase/truncate。
- 创建新的 run 和新的 checkpoint thread，写入编辑后的用户消息；旧 run 不回写、不重新打开。
- 明确提示：重新运行会再次执行工具副作用，不能把“编辑重跑”当作文件修改自动回滚。

## 不建议的方案

- 只在 `/assistant` 里根据是否有 `runId` 分支：会漏掉 executor、workflow 和 LangGraph 的恢复语义。
- 直接把已 cancelled 的 run 改回 running：会与已投影的 cancelled tool parts、快照状态机和审计语义冲突。
- 修改旧 run 的 `input_text` 并复用旧 checkpoint：会混淆运行事实，并可能把旧 assistant/tool 输出带入新输入。
