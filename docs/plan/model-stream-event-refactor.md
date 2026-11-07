# 模型流事件与工具呈现职责收敛

## 目标与已确认的行为

用户已批准：统一模型流阶段与正文事件管理，停止发射推理原文，保留思考状态与上下文观测；执行门禁和工具呈现解耦；修复真实快照 HTTP 响应的 500。开发不提交 Git。

## 调查结论

- 原 model_node 嵌套 publish_phase 与 StreamingPartStateMachine 各自维护同一次请求的阶段，退出路径需要两处同步。根因是职责分散，按结构整理处理。
- 原推理内容经历状态机缓冲、事件发布后被 Transport 丢弃。推理原文已有 canonical context 事实源，事件链无需第二份内容。
- 原 create_tool_call 根据 admitted_tools 决定是否发布身份，然后经 ToolBatch.projection、was_projected、ToolMessageMetadata.hidden 和历史重建共同维护不可见。真正准入在 ToolAccessGate；界面过滤侵入执行和持久化链。禁用调用现在在过程详情保留失败记录，无 running 事件、无 handler 执行。
- 原 ToolPresentation 仅在 TYPE_CHECKING 下导入 wire TypedDict，FastAPI 实际响应序列化无法解析，空 snapshot 和历史分页同样 500。此前直接调用路由函数的测试没有走响应序列化，不能证明接口正常。

## 终态结构

- ModelStreamStateMachine 每个模型请求一个实例，管理等待、实际分片阶段、正文合并与收口、已注册工具调用身份去重及流中取消占位。阶段只由当前 delta 决定；累计工具快照与最终身份补齐不重设阶段。混合帧优先级为 tool_call、reasoning、responding；正文仍完整保留。finish 在任何退出路径清除阶段，并先刷新正文。
- 状态机 receive 仅接受 reasoning 的存在性，不接收原文。对话内容事件只允许 text；原文仍经 SDK snapshot 保存，观测读取 canonical context。
- model_node 保留 SDK 请求、响应保存、取消 Run 与路由职责。ToolBatch 只保存执行交接，不保存 UI 投影记账。工具创建只检查身份完整性与注册表（与 canonical assistant_message 过滤一致）；执行准入只由 ToolAccessGate 判断。事件传递完整结果，呈现交给 Transport 和前端。
- ToolPresentation 在无依赖 core/tool/presentation.py 定义，工具定义与 wire 运行时正常导入，避免 schemas 聚合包循环；不通过 response_model=None 或延迟 rebuild 掩盖错误。
- unknown/不完整工具身份仍按现有模型协议过滤，不人为补成有效工具调用。quiet 是默认安静呈现，不是删除事实；失败、取消、用户待办、后台工作仍可见。

## 数据与范围

删除 ToolMessageMetadata.hidden、ToolBatch.valid_tool_calls 和旧状态机，不保留兼容分支。按绿地项目约定，旧运行数据含这些字段时需离线清理或重建；本轮不删除用户会话、不自动迁移用户数据库。测试使用隔离数据根。旧数据库直接用于新版本会因严格契约校验失败，这与本次已修复的类型解析 500 是两个不同原因，交付时须明确说明。

不改工具授权规则、不改变模型上下文推理保存、不增加通用策略引擎、不改终端输出所有权。终端已有最终 output 覆盖实时输出的语义不在本轮调整范围。

## 验收要求

1. 真实 FastAPI HTTP：空会话与真实存储、三类呈现的嵌套工具 part、历史分页、类型 schema 解析均成功。
2. 实际 SDK 流与状态机：等待、推理、正文、工具、混合通道、累计快照不重放、最终补齐、取消、超时与异常清理。
3. 禁用注册工具：发创建和失败，handler 未运行，无 running；取消与冷重建保留一致事实。
4. UI：真实 Electron + Python，临时数据根，创建工作区/会话、进入和刷新，不出现后端错误；浏览器 fixture 回归等待/思考/quiet/独立卡片。
5. gpt-6-sol 基于本文和 AGENTS.md 只读复核。单测、真实 HTTP、真实 UI 结果分开记录，不能以直接调用路由或仅打开空壳替代。

## 验证记录

- 第一批真实 HTTP、SDK 取消和 HITL 测试：44 passed。
- 最终合并定向后端回归：424 passed，包含真实 HTTP 序列化、SDK 取消/超时、投影、冷重建、门禁、HITL、并发与事件契约。
- 两处原终端测试与既有 docs/plan/terminal-output-experience-refactor.md 的终态 output 所有权契约相反，已同步测试断言；没有改变终端输出生产逻辑。
- 前端生产构建、TypeScript 检查和新增桌面 E2E 文件 ESLint 通过；新增两个生产模块 strict mypy 通过；本轮核心生产文件 Ruff 通过；OpenAPI 导出成功。
- 真实 Electron + Python：隔离数据根创建工作区和会话，真实 HTTP snapshot/history 均 200，进入和刷新会话后编辑框可见且无后端错误，1 passed。
- Chromium UI（fixture Transport，非在线模型端到端）：10 passed，覆盖实际思考状态、无助手消息的状态、quiet 工具、取消与过程折叠。
- gpt-6-sol 基于本文只读复核，独立运行 60 项测试通过。发现并修正旧测试注释；主路径未发现额外阻断。

## 本轮不扩展的问题

- P2（已解决：持久化入口保证 Task 内唯一）：模型产生重复 tool_call_id 时，message_codec 保留重复项，tools_node 执行两次，而实时/历史 UI 按 ID 只显示一张卡。当前 test_model_output_contract 和 test_tool_call_pipeline 的既有契约明确允许保留重复项，不是本轮引入；应另行在模型协议输入边界确定拒绝/去重策略。不能在 UI 里掩盖或单独修补。根因置信度高，重复副作用尚未做真实工具复现，本轮记录。
- 真实 Electron 截图在当前宿主环境捕获到空白合成层，不能作为视觉验收截图；DOM 进入/刷新和真实 HTTP 断言通过。可查看 Chromium fixture 的浅深色截图检验样式。

- 扩展回归发现终端批次测试缺少 execution_scope，已按生产作用域补齐；gpt-6-sol 独立复跑相关 HTTP 和终端测试 23 passed。最终 424 项已包含这些用例，不重复计数。

### 可复核证据

- 后端最终日志：`/tmp/stream-final-tests.log`（424 passed；5 项 snapshot HTTP/schema 契约用例包含在内）。
- 真实桌面日志：`/tmp/stream-electron-test.log`（1 passed）。
- 浏览器 UI 日志：`/tmp/stream-browser-tests.log`（10 passed）；样式截图 `apps/desktop/test-results/slot-48/model-thinking.png` 与 `model-thinking-dark.png`。
- 构建和类型检查：`/tmp/stream-desktop-build.log`、`/tmp/stream-tsc.log`、`/tmp/stream-mypy.log`；OpenAPI 验证输出 `/tmp/stream-openapi.json`，仓内 OpenAPI 存在其他既有变更的生成差异，本轮未覆盖它。
- 未执行 Git 提交，未操作用户真实会话数据。
