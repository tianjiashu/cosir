# Assistant 用量与上下文占用改造验收文档

**对应方案**：[assistant-usage-context-display-design.md](./assistant-usage-context-display-design.md)  
**状态**：主Agent验收通过  
**验收方式**：代码事实 + 自动化测试 + 主Agent最终复核

> 实现已经落地。本次按用户最新指示由主Agent完成最终验收；此前独立审查提出的问题已逐项修复，并以当前代码和最新自动化结果为准。

## 1. 验收范围

验收只针对以下两个用户可见指标：

- `ConversationStateUsage`：一次 Conversation Run 的累计 input/output/cache/reasoning token。
- `context_usage`：Task 当前有效 context 相对当前模型 context window 的占用比例，并能提供 absolute used/window 详情。

不把费用、provider billing、真实 tokenizer 精度和历史所有 run 的总 token 纳入本次必须交付；它们只能作为后续扩展，除非实现改动明确包含并有独立证据。

## 2. 必须通过的契约验收

### A. 后端快照

- [x] `ConversationStateSnapshot` 明确定义 `usage_run_id`、`context_usage_used`、`context_window_total`，并增加 `context_revision`。
- [x] `usage` 六个字段中五个计数为非负整数，`cache_miss_tokens` 在 provider 缺少明细时为 null；新增关联字段与事件字段类型一致。
- [x] context ratio、used、window 对 null/0/超额/非有限值的语义明确，backend/frontend 校验一致。
- [x] old snapshot 读取不会因新增字段导致历史任务无法打开，并记录兼容迁移日志。

### B. run usage 更新

- [x] 每个完整模型调用最多按一次 usage metadata 累加；流式 chunk 不重复累加。
- [x] `UsageUpdatedEvent` 到达后，snapshot usage 是该 run 的完整累计替换值，而不是前端自行累加的增量。
- [x] `RunInitializedEvent` 后 usage 已清零且 `usage_run_id` 指向新 run。
- [x] completed、failed、cancelled、resume 路径均不会把上一 run 的 usage 留在新 run 上；终态小 usage 不能覆盖大累计值。
- [x] provider 没有 usage 时 UI 显示“统计中/—”，不把 0 文案解释成真实消耗 0 token。
- [x] `cache_miss_tokens` 有可靠细节时按累计 input-cache_hit 推导；细节缺失时保持未知口径，UI 不宣称精确值。

### C. context usage 更新

- [x] `RuntimeContextManager` 的 context 变更按完整有效 entries 重算，不发生重复累加；resume 会重新投影。
- [x] context event 把 `used_tokens` 和有效 context window 一起投影到 snapshot；前端能展示 used/window。
- [x] `context_usage` 仍由 backend 计算，允许超额；前端只做视觉 clamp，不改事实比例。
- [x] Task DB 的 `context_usage_used` 与实时 snapshot 的最终一致性策略有失败安全和日志。
- [x] context 未测量与确实为 0 可区分。

## 3. 必须通过的 UI/UX 验收

### A. Composer context meter

- [x] 位于 composer rail/footer，不占用消息正文空间。
- [x] 正常状态、65% 预警、85% 高风险、100%/超额状态的视觉和文案可区分。
- [x] hover 与 keyboard focus 都能打开详情；详情包含 used、window、remaining/headroom 和估算/未知提示。
- [x] 无有效 measurement 时显示 `—` 或等价空状态，不显示误导性绿色 0%。
- [x] 窄窗口隐藏文本标签保留可访问 button，详情不造成横向溢出。
- [x] 不把 run token 总量与 context window 百分比相加。

### B. Run usage footer

- [x] 只显示当前 assistant message 对应 run 的用量，不显示 Task context ratio。
- [x] running 显示“统计中”或等价状态；收到增量累计后可以更新；终态保留最终已知值。
- [x] 详情可查看 input、output、total、cache hit、cache miss、reasoning。
- [x] run 切换后旧数字不会出现在新 assistant message 上。
- [x] 历史任务重开时，展示的是持久化 snapshot 的最后已知值，并在不完整时有明确提示。

### C. Assistant UI 集成

- [x] 使用 `useAuiState` 订阅最小的 thread state selector；不在组件外创建第二份业务 store。
- [x] converter 透传 Transport state，不把顶层自定义 usage 伪装成未经证实的 assistant-ui `metadata.steps`。
- [x] 视觉模式遵循 assistant-ui Context display / Context breakdown / Composer context 的层级、阈值、可访问性和“无 usage 不造数字”原则。

## 4. 必须执行的自动化验证

### Backend

至少覆盖：

```text
RunInitializedEvent -> usage reset
UsageUpdatedEvent(step 1) -> cumulative usage
UsageUpdatedEvent(step 2) -> replacement cumulative usage
completed / failed / cancelled -> final known usage
new run -> old usage isolation
ContextUsageUpdatedEvent -> ratio + used + window
zero window / overage / invalid finite values
cache details and repeated model calls
snapshot reload after backend restart
TaskResponse / WorkspaceTask context fields and window fallback
legacy snapshot read-time migration and strict validation
```

### Desktop

至少覆盖：

```text
parse new snapshot contract
format null / 0 / 1k / 1M / overage
65% / 85% / 100% thresholds
usage run_id mismatch is hidden or marked stale
context updates rerender meter
usage updates rerender footer
keyboard focus and ARIA meter semantics
two sequential runs do not leak usage
```

### 当前实现验证记录

以下命令均已在当前工作区通过：

```text
apps/backend\.venv\Scripts\python.exe -m pytest apps/backend/tests --basetemp apps/backend/temp/pytest-final-acceptance -q
208 passed

cd apps/backend
.\.venv\Scripts\ruff.exe check app tests
All checks passed
```

桌面端命令应从 `apps/desktop` 执行：

```text
cd apps/desktop
npm.cmd run test:unit
53 passed
npm.cmd run build
TypeScript + Vite build passed
npm.cmd run lint -- --max-warnings=0
passed
npm.cmd run test:e2e -- tests/e2e/frontend-regressions.spec.ts
10 passed
npm.cmd run test:e2e
16 passed
```

回归测试新增了终态 usage 乱序保护、resume context 重投影、阈值显示一致性，以及两次连续 Run 的实际 UI meter/footer 更新和旧 Run 隔离；Popover 使用可聚焦 button 与 ARIA progressbar，窄屏隐藏文字标签但保留图标和可访问名称。

## 5. 主Agent最终验收记录

本次根据用户最新指示，由主Agent读取本验收文档、对应方案和当前代码事实完成最终复核。独立子 Agent 不作为本次完成门禁；此前独立审查中发现的问题已在实现和测试中修复。

```text
验收结论：PASS
实现状态：COMPLETE

代码事实核对：
- [PASS] 现状描述准确
- [PASS] usage 更新链路与缺陷定位准确
- [PASS] context 更新链路与缺陷定位准确
- [PASS] 前后端边界与数据源描述准确

方案完整性核对：
- [PASS] composer context meter 方案
- [PASS] run usage footer 方案
- [PASS] unknown/overage/accessibility 方案
- [PASS] 契约、代码清单、测试和完成定义

测试证据：
- backend pytest：208 passed
- backend Ruff：All checks passed
- desktop unit：53 passed
- desktop build：passed
- desktop lint：passed
- desktop Playwright：16 passed（关键 frontend-regressions：10 passed）

文档问题：
- 无阻断性事实错误、需求遗漏或无法执行的验收项
```

以下情况必须给实现 FAIL 或 BLOCKED：

- 实现把当前不存在的 UI、字段或事件写成已经完成；
- 没有为真实 used/window 提供契约来源，却要求 UI 展示精确 absolute token；
- 方案把估算值标成精确 provider token；
- 方案没有覆盖新 run usage 隔离、未知 usage、超额 context 或恢复边界；
- 文档中的文件路径、事件名、字段名与代码事实明显不一致；
- 验收结论无法从代码或测试证明，只给“看起来可以”。

## 6. 当前实现的最终通过标准

1. 当前实现代码、测试证据和最终复核结论同时满足 `PASS`。
2. Backend full tests、desktop unit/build/lint 和关键 E2E 全绿。
3. 方案中列出的 P0/P1 缺陷已修复，任何延期项不影响当前 UI 对 run/context 生命周期的正确解释。

验收结果：**PASS（主Agent最终验收，2026-09-09）**。
独立子Agent结果：**未作为本次最终门禁**。
