# 对话时间线改造与验收

## 范围与事实归属

2026-11-07。本轮不提交 Git。后端拥有模型响应、Run、TeamRun 与工具状态；Transport 派生最终回复语义；前端拥有分组、开合与视觉。Team/委派/fork 不进入普通工具 group，但属于过程时间线。只有正常完成且有明确最终回复才默认收起；取消、失败、中断、无最终回复保持展开。用户显式开合优先，悬停不产生偏好。推理原文仍只供上下文观测。

最终回复取正常完成、非 forked Run 的最后一次已提交且可投影的 assistant 响应，核验无工具、非空正文、正常 finish_reason；不使用 Run.final_output（结构化支线会改它），不猜末尾文本。实时以现有 context sequence 关联文本段，冷重建从同一事实投影。无需数据库迁移。

## 开发前验收清单

| 编号 | 场景与操作 | 必须成立 |
|---|---|---|
| A1 | 普通工具、正文、delegate/team/fork 混排 | 保持顺序；独立条目不在 group 内，能随过程折叠；仅独立条目也有展开入口 |
| A2 | 运行中鼠标经过工具，随后正常完成并收到 finalReply | 默认收起，最终回复与结果入口可见；最终回复 DOM 不重建，正在读的段落留在原位 |
| A3 | 结束后主动展开，再收到状态刷新 | 保持展开；group/详情开合不写父过程偏好 |
| A4 | 无最终回复、空回复、length、取消、失败、runtime_restarted、forked 历史 | 不自动收起；已有内容保留；终态无运行动画；错误/恢复入口可见 |
| A5 | 首 token 等待、reasoning、responding、工具执行、挂起、停止中 | 模型内部阶段统一为「正在思考」，输出正文时不另起状态行（停止中除外）；运行文字左至右扫光，无运行旋转图标；挂起和终态静态；减少动态效果时静态 |
| A6 | Team pending 审批编辑、收起再展开/虚拟卸载 | 输入保留；只有一份审批表单；pending 不误进后台；提交中不可重复决定 |
| A7 | Team 批准→运行→完成/失败，或驳回 | 时间线轻量；批准后可定位后台详情；驳回仍可见；后台节点和子 Agent 打开入口不退化 |
| A8 | delegate/fork 查看入口 | 分别打开现有工作台/侧栏任务；不把创建成功当作子任务完成 |
| A9 | 实时 SSE 与刷新重建，含同 Run 多次响应、后台等待草稿 | 最终标记一致；只标实际最终响应，不将中途文本当答案；复制回复不混入进展 |
| A10 | 深浅色、窄窗口、键盘 Enter/Space、滚动历史、底部跟随 | 无横向溢出；开合状态可访问；不丢焦点、不强拉历史到底部；虚拟估高与可见性同源 |

## 验证分层与完成门槛

1. 后端投影/状态契约测试与真实 localhost HTTP：检验最终回复实时、重建和异常分支；不以 UI fixture 代替后端验证。
2. 前端类型检查、相关单测及构建。
3. Playwright 驱动真实 Chromium 操作上述 UI，记录截图/trace。测试服务驱动的场景明确标为 fixture，不声称真实模型集成。
4. 真实 Python 后端 + 浏览器/桌面 UI smoke，确认实际 snapshot/页面链路；若模型不可用，用隔离模型服务驱动真实 backend，不修改用户模型配置或数据。
5. 明确指定 gpt-6-sol 子 Agent 按本文复核代码及验收证据。发现问题修复后重验。真实 UI 验收未完成，不能宣称任务完成。

## 加减法与不做

删除末尾正文启发式、standalone 常显例外、hover 写父开合、重复可见性判断和 Team 重复查询包装。复用现有 disclosure store、查询缓存、后台看板、Collapsible、虚拟列表及 tw-shimmer；不引入新依赖、数据库状态或通用渲染框架。不处理重复工具 ID、工具执行/调度和全应用加载样式。

## 实施与验收结果

本轮实施完成，未提交 Git。保留原有工作区改动，不将此前已暂存改造计为本轮成果。

### 实施结果

- 后端沿现有 context sequence 标识已提交模型响应，实时投影与冷重建共同判断最终回复；没有新增数据库列或第二套运行状态。
- 前端删除末尾文本猜测与 standalone 绕过过程折叠的例外；渲染和估高复用可见性函数。只有正常完成且有最终回复才默认收起。
- 删除 hover 写父开合状态；用户显式开合优先。自动收起时若焦点在过程内，焦点移到过程标题（不再延后收起：镜像焦点的状态收不到已卸载按钮的 blur）。
- Team 复用现有查询与后台面板：审批表单唯一、草稿跨折叠和任务切换保留；执行态仅显示摘要和查看入口。子任务状态不再与工具调用创建成功混为一谈。
- 运行使用现有文字扫光，等待/终态静态；最终回复复制排除进展，未完成时文案为“复制已生成内容”。无新增依赖。

### 已执行的验证

| 层次 | 结果及覆盖 |
|---|---|
| 后端定向回归 | 最终一轮 181 项通过：事件契约、模型输出、流状态机、实时投影、冷重建、最终回复、Task 生命周期与 HTTP 契约。 |
| 前端单元测试 | 126 个文件、847 项通过。首次受限环境因 localhost listen EPERM 导致 supervisor 用例失败，允许本机监听后全量通过。 |
| 类型、构建与静态检查 | TypeScript 与生产构建通过；本轮前端变动 ESLint 通过，后端涉及文件 Ruff 检查；git diff --check 通过。构建仍有既有 CSS/产物大小提示。 |
| Chromium UI | 29 个不同用例通过：timeline-disclosure 14、conversation-presentation 7、agent-fork/workbench-subagent/first-output 共 7、thread-scroll-stability 1。深浅色、窄窗口、键盘焦点、减少动画、审批草稿与提交中禁用、Team 终态、后台定位和子任务导航均覆盖。 |
| 真实 Electron + Python + SQLite + HTTP | desktop-smoke 的 A9 用例通过：隔离数据目录，真实 API 建立工作区/Task；写入真实 canonical context，真实 state/attach 与页面验证 stop 收起、length 不收起，以及刷新一致性。已等待启动遮罩退出并检查实际截图。 |
| gpt-6-sol 独立验收 | acceptance_audit 对照本文复核实现与后端/前端测试，无阻断；ui_acceptance 独立执行首轮 17 项和补充 5 项真实 Chromium 操作，并查看截图，无阻断。重复运行不叠加到上述 29 项数量。 |

长对话滚动实测：首次遍历累计修正 1306px，二次遍历 96px，切走再返回 0px，均在现有回归门槛内；不宣称修复历史分页锚点问题。

### 证据与边界

- 浏览器截图：`apps/desktop/test-results/slot-21/timeline-disclosure-A1–A3--9c659-d-manual-preference-survive-chromium/timeline-light.png`，深色同目录命名 `17fc0` 的用例内；Team 后台为 slot-21 的 A6-A7 用例截图。
- 真实桌面截图：`apps/desktop/test-results/desktop-timeline/desktop-smoke-A9-real-Pyth-c7da7-ponse-truncation-stays-open/real-timeline-stop.png` 与 `real-timeline-length.png`。
- Chromium 状态矩阵使用 fixture 服务驱动，不等于真实模型调用。Electron 用例使用真实后端持久化事实，覆盖 HTTP/attach/冷重建/页面链路；没有发起在线模型请求。模型流到页面的完整链路由分层测试覆盖，本轮不声明在线模型端到端验收。
- Task5 历史兼容语义测试使用空工具目录，验证历史工具无当前定义时的默认呈现；不再同时装配第二套后端存储。滚动测试删除已移除的 reasoning wire part，并明确最终回复标记。
- 代码复核发现的投影字段校验缺失、测试响应序号缺失均已修正并回归。没有保留新旧两套最终回复判断路径。
