# 想法需求文档

> 用途：沉淀 UI 重构讨论过程中的原始想法、已确认小结论、默认假设、开放问题与风险。
> 非正式 PRD，不设边界；重要更新记录来源与时间。
> 相关计划：`docs/ui-refactor-plan.md`；规范：`rules/global-interaction-clarification.md`、`rules/Agent客户端UI规范.md`。

## 当前主题

coding-agent 桌面端 **UI 整体重构**（交互范式 + 界面），对标 `deepseek-harness` 的 UI 范式，在影子仓 `feat/ui-refactor` 分支上实施（尚未开始开发）。

## 已确认想法

1. **目标已确认**：整体对标 harness 的 UI 范式。对标对象是**信息架构 + 交互模型**（单一壳、情境化 details、slash 命令、Cmd+K、trajectory spine、StateDot/TerminalBlock 等模式），不是代码级复刻（harness 包与 Tauri/cordis 不兼容，只能在本栈重写）。
2. **起步方式**：先走查现状 → 归纳痛点 → 立项排序（已对现有 8 个"面"完成首轮走查清单）。
3. **重构路线文档已定稿并细化**：`docs/ui-refactor-plan.md`（5 个 Phase，已由审查 Agent 按真实代码纠偏）。
4. **【第一性结构决策 · 老板已拍板】采纳 harness 的「单一壳 + 情境化 details」模型**：
   - 废除整页切换（chat / new-task / logs 的整页替换），改为单一壳 + 内联引导 / overlay / 命令触发；
   - 右栏从固定 3 tab（Outputs/Sources/Subagent，且为 Mock 数据）改为「选中什么显示什么」的单一情境化 details 面。
5. **【老板已拍板】new-task = 内联 onboarding 空态**：中央主区在"无活跃任务"时显示引导态（复用现有 `NewTaskPage` 资产：大标题 / 4 快捷卡片 / 工作区选择器 / 输入框），创建任务后平滑进入会话；侧栏"新建任务"按钮 / Cmd+K 回到引导态。无整页切换。

## 用户原话

- 「我期望对标harness。」
- 「你先开启一个影子仓库。」（已完成：`H:/coding-agent/.worktrees/ui-refactor`，分支 `feat/ui-refactor`）
- 「读取rules 下的 agents 开发规范，了解最零铁律，给出你的建议。」（第零铁律：一切以「方便项目长期稳定迭代」为最终目标）
- 「我们先讨论一下应该如何进行重构呢？」→ 走查先行。
- 「可以先寄一份。」（同意建立本想法需求文档）

## 默认假设

- **视觉层面仍守 `rules/ui-guidelines.md` 克制基调**（4px 灰阶、无阴影、彩色面积<5%）——除非老板明确要求连视觉一起对齐 harness（harness 是 12px 大圆角 + 高饱和风，与克制基调直接冲突）。
- 重构在影子仓 `feat/ui-refactor` 分支进行，主仓 `main` 不受影响。

## 候选建议（均未最终确认）

1. **slash 命令组合框 + Cmd+K 全局面板**：命令注册表单一数据源，两者共用；**Cmd+K 命令面板用 cmdk**（`rules/Agent客户端UI规范.md` §1 钦定生态，勿手写）。
2. **trajectory spine**：合并现有分散时间线（TurnTimeline / DelegationTimeline / trace）为一条权威执行轨迹视图。
3. **StateDot 四态指示器**（done/warning/ongoing/error）替换/增强现有 thinking-dot。

## 开放问题

1. 右栏 details 面具体承载哪些 surface（工具 I/O、diff、子 agent 时间线、plan、上下文）？——我的推荐已给：**工具 I/O + 变更 diff + 子 agent 三面**，plan/上下文先不进；Mock 数据接真实或砍，不留占位。**待老板确认。**
2. LogsPage 是否并入 trajectory，还是保留为诊断模态（Cmd+K 进入）？
3. `ui-refactor-plan.md` §5 六个决策点：右栏 slim rail、StateDot 令牌化、LogsPage 走向、是否第四列、Tauri 快捷键冲突、slash 初版范围。

## 风险与冲突

- **视觉冲突**：harness 的 12px 圆角 / 彩色化 vs 本项目克制基调（`ui-guidelines.md` §三/§七）。
- **架构不兼容**：`@deepseek-ai/dsh-*` 强耦合 cordis 运行时，不能 import，只能借鉴模式在本栈重写。
- **【已核实 · 老板认可】整体"拿 harness 前端过来改"不可行**：壳层包（ui-layout/ui-sidebar/ui-conversation/ui-trajectory/ui-input-trigger/ui-slots 等）全部是 cordis 插件（ui-layout inject `dsh-client-runtime`+`dsh-client-ui-theme`；ui-slots peerDep `@deepseek-ai/cordis`），整体移植 = 引入平行运行时 + 双 token 体系（CSS Modules/`--dsw-*` vs Tailwind/`tokens.ts`）+ 视觉冲突（12px vs 4px）。结论：**借模式、借 `ui-primitives` 原子组件实现思路（官方标注 zero cordis，MIT），不借包、不借运行时**；原子组件落本项目时须翻译为 Tailwind + `tokens.ts`。
- **生态纪律**：命令面板必须用 cmdk（规范钦定），手写即违反铁律二「不重复造轮子」。
- **闭环要求**：rules 第八节强制开发-审查-测试独立闭环，重构每 Phase 需独立审查 + 测试 Agent，开发不得自验。

## 被推翻或替换的想法

- 早期诊断称"右栏 6+ 常驻 tab"——已由审查 Agent 按真实代码纠偏：实际 3 tab（Outputs/Sources/Subagent），Changes 已迁到 ChangesDrawer，Context/Mcp 是占位块；且 Outputs/Sources 目前是 Mock 数据。
- 早期认为"四条时间线分散"——已纠偏：`projector.ts` + `eventStore` + `TurnTimeline` 已是统一管线。

## 后续可整理方向

- 走查结果逐面归纳 → 重构需求清单（痛点 → 期望 → 对应 Phase）。
- 结构决策确认后，更新 `ui-refactor-plan.md` 的 Phase 1/2/3 范围。
- 决策确认后，将本文件已确认项同步到正式计划。

## 变更记录

- 2026-08-15：创建。沉淀首轮讨论（对标 harness 目标、走查起点、影子仓、默认假设、候选建议、开放问题）。
- 2026-08-15：**老板拍板第一性结构决策**——采纳「单一壳 + 情境化 details」模型（候选建议转正为已确认想法，从开放问题移除）。
- 2026-08-15：**老板认可**"不整体移植 harness 前端"结论——记入风险与冲突（附已验证事实：壳层 cordis 耦合、ui-primitives zero cordis）。
- 2026-08-15：**老板拍板 new-task = 内联 onboarding 空态**（转正为已确认想法，从开放问题移除）。
