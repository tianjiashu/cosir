# 对话活动与工具呈现改造

## 已确认目标

隐藏对话中的推理原文，但不能隐藏运行状态。ToolDefinition 声明 activity / standalone / quiet 呈现类别；前端拥有文案、图标与阅读状态。Team、委派、fork 独立折叠。保留现有执行、取消、后台、任务与 TeamRun 事实源，不提交 Git。

## 调查与路线

问题属于展示契约和布局职责：前端 tool-catalog.control 决定静默；reasoning part 同时承载内容与状态；Team 位于产物区而 child/fork 没有展开体。Snapshot 已有 activity、工具 part、background_tasks、foreground_waits、handoffs，缺少每次模型调用活动，不能将 Run.running 或 execution_data.next_node 解释为思考。

结构化路线：模型调用边界发布不含正文的 ModelActivityEvent（step_id + phase），Transport 投影到 activity.modelActivity；开始 waiting，按实际输出通道更新 reasoning/responding/tool_call，finally 清空。阶段仅供显示、不进入调度或数据库；Run 离开 running 清空，旧 step 的结束不得清新 step。运行期间保留工作快照；同进程 canonical 重建在同锁下保留同 Run 的活动，重启不恢复。编辑重跑入队后再重建存在执行已启动窗口，不能假定重建总是静止。

静态工具策略在 ToolDefinition 唯一声明，Transport 实时创建与冷重建消费同一映射，已删除工具默认 activity。不保存每次调用的历史样式，不复用 metadata.hidden（原义为未准入/未建 part）。模型 schema 不包含 UI 策略。

对话 wire 删除 reasoning 原文；上下文数据库、模型续跑保留 reasoning_content。独立观测 API 和详情提供思考原文。Thread 活动槽独立于消息，后台结果驱动且没有助手消息时仍能显示；Turn 保留内容和历史结局。前端统一投影状态，不建并行 store；停止请求和连接异常沿用既有宿主状态。

普通活动按连续片段聚合，正文与独立条目打断，静默等待不打断。独立条目不进工具组，但属于整轮过程。正常完成且有明确最终回复时默认收起过程；无最终回复保持展开，用户显式开合优先。Team 在时间线呈现审批或轻量状态，执行详情复用后台看板，不重复渲染审批表单。具体验收见 `timeline-disclosure-acceptance.md`。

## 验收

- 模型首次等待、推理、正文、工具参数、工具后再次模型请求，以及取消/异常/挂起/恢复。
- 实时、重连、冷重建、历史分页一致；无 reasoning 原文进入对话 wire；观测和模型上下文保留。
- quiet 正常隐藏但异常可见；后台和审批不丢；standalone 不进入普通工具组。
- Team 审批/执行、child/fork、真实命令输出与复制、展开与滚动、深浅色。
- gpt-6-sol 基于本文独立复核；记录真实测试边界，不将 fixture 测试声称为在线模型测试。

## 验证记录

2026-11-06 完成实现与复核，未创建 Git 提交。

| 检查 | 结果与边界 |
| --- | --- |
| 前端全量 Vitest | 126 个文件、847 项通过；最终卡片箭头补充后另跑 24 项卡片单测通过 |
| 后端相关 pytest | 157 项通过，包含模型 SDK 读取/取消、活动投影、冷重建、快照生命周期、工具注册表、上下文与命令链 |
| Chromium UI | 37 项不同用例通过（多次隔离运行），含首输出 3、后台/Team 2、fork 1、子 Agent 工作台 3、上下文观测 7、对话呈现 7、头部 5、真实命令输出 9 |
| 真实桌面 | Electron + Python 后端 smoke 4 项通过，临时数据根；宿主启动、工具链路、隐藏窗口继续运行、退出清理 |
| 编译与静态检查 | 最终 `npm run build`、TypeScript、相关生产文件 ESLint/Ruff、`git diff --check` 通过 |
| 新增后端文件类型 | 3 个新增生产文件 strict mypy（`--follow-imports=silent`）通过；展开依赖的 strict 检查仍报告 41 个既有依赖文件的 75 项错误，不声称全仓 strict 通过 |

浏览器测试经过真实 React、Chromium、HTTP/SSE；模型/Team 生命周期由受控 fixture 驱动，并非在线模型或真实 Team 作业。终端的 9 项测试先经 Python 执行器和 Rust Terminal Worker 实际运行命令，再将真实结构化输出送入 UI，覆盖成功、超时、取消、ANSI、复制与独立滚动。观测用例实测推理页签与剪贴板；模型状态用例包含无助手消息、工具后再次等待/思考、quiet 等待、深浅色与减少动画。未调用付费远端模型。

后端新增断言证明：实时和冷重建对 wait、delegate、fork、Team、普通工具与已删除工具使用一致呈现类别；呈现策略不进入模型函数 schema；推理正文留在上下文和观测响应、不会进入重建快照；同进程重建保留同 Run 模型活动，取消与进程缓存清空后不恢复；模型通道变化只在阶段变化时发活动事件，异常退出清空。

两位 gpt-6-sol 子 Agent 分别按本文复核后端契约与前端渲染。发现并修复：每个 token 重建工具目录、Team 审批落定自动收起、等待提示重复、自动展开缺少滚动锁定、尾部文本刷新晚于活动清除。当时只读复核未发现阻断，但后续真实 HTTP 暴露该结论不完整：ToolPresentation 仅 TYPE_CHECKING 导入导致 FastAPI 响应序列化 500。已改为无依赖契约模块的运行时导入，并补 HTTP 回归；见 model-stream-event-refactor.md。

截图（测试产物，不作为持久业务数据）：

- 深色思考状态：`apps/desktop/test-results/slot-38/model-thinking-dark.png`。
- Team 独立执行卡：`apps/desktop/test-results/slot-39/team-running.png`。

静态 OpenAPI 导出存在早于本次的全仓漂移；本次仅从生成结果同步 ContextMessageResponse.reasoning_content 字段，未夹带其他 API 变更。Vite 构建仍提示大 chunk，本次没有扩展为包拆分。

## 本次影响与保留边界

- 契约有意改变：删除对话 reasoning part；activity 增加 modelActivity；tool-call part 必填 presentation；观测响应增加 reasoning_content。前后端同步升级，不保留旧 wire 兼容分支，无数据库迁移。
- 具体实现入口：`core/workflow/nodes/model_node.py` 与 `core/events/model_activity_event.py` 产生模型阶段；`assistant_transport/projection/model_activity_projection.py` 投影；`core/tool/schemas/tool_definition.py` 声明工具类别；`assistant_transport/projection/tool_presentation.py` 解析当前工具类别；`lib/assistant/current-activity.ts` 只读状态并在 Thread 末尾呈现；`turn-layout.ts` 同时供渲染和估高使用。
- 删除被替代的 ReasoningItem、reasoning 渲染/开合 key、tool-catalog.control 和 Team 产物区重复路径。保留已有模型上下文、运行生命周期、后台结果、取消、TeamRun 查询、子 Task 定位与终端阅读器。
- 不修改模型提示词、调度/执行语义、数据库结构、Team 固定拓扑或窗口生命周期；既有历史分页锚点问题仍按原方案文档记录，未借本次改造扩大范围。
