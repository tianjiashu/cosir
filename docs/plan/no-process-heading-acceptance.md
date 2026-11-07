# 无过程完成回复的标题验收（2026-11-07）

## 改造前确定的范围

正常完成且没有可展示过程的助手回复不显示耗时标题、分隔线及占位。运行中/挂起反馈、失败/取消/重启中断、过程开合、正文 DOM、用量与操作栏保持现有行为；不改变后端协议和耗时记录，不提交 Git。

根因是 `apps/desktop/lib/assistant/turn-heading.ts:17` 把耗时存在当作标题存在的理由（展示策略，高置信度）。过程归属继续由 `turn-layout.ts:86` 投影，标题规则仅在 `projectTurnHeading` 调整；`components/assistant-ui/turn/turn-heading.tsx:24` 和 `message-height-estimate.ts:109` 消费同一结果。无需 CSS 隐藏、额外状态或后端字段。静默工具的可见性继续遵守 `visibleCall`，不以工具数量或模型曾进入 reasoning 阶段替代真实过程。

## 验收清单（实施前固定）

| 编号 | 场景与通过标准 | 验证方式 |
| --- | --- | --- |
| H1 | 正常完成、仅最终回复，耗时 0、正数均没有标题、分隔线和残留占位；正文、操作栏仍存在 | 标题单测、浏览器布局断言、Electron |
| H2 | 过程说明、可见工具保留展开入口；无 outcome 的历史中间消息显示“查看过程”，终态耗时必填；quiet 成功工具不产生入口，quiet 异常/待用户操作仍可见 | 标题单测、浏览器；Electron 验证真实过程 |
| H3 | 无工具也保留失败、取消、重启中断状态；子工具结局不能覆盖父 Run 结局 | 标题单测、浏览器 |
| H4 | 首输出等待、运行中、挂起反馈保持原样；纯回复流式完成后标题消失，正文 DOM 不重建，保留手动回到底部及顶部锚定；从底部完成后无残留底部空隙 | 单测、浏览器 HTTP/SSE 转换 |
| H5 | 过程按钮 Enter/Space 开合正常，最终回复 DOM 不重建，浅深色均通过 | 既有浏览器回归、Electron |
| H6 | 纯回复估高不因耗时变化；真实过程/异常状态仍计入标题；历史滚动和分页行为不退化 | 估高单测、历史滚动 E2E |
| H7 | 本机真实 Electron 宿主 + app:// 构建产物 + 真实 Python HTTP/SQLite：纯回复无标题，带过程有入口，浅深色和刷新结果正确，截图可检查 | 隔离临时数据根运行桌面 E2E；不调用付费模型，不触碰用户真实数据 |
| H8 | TypeScript、相关 ESLint、diff whitespace 检查通过；子 Agent 在改造后按 H1–H7 独立复核并实际执行测试 | 检查输出与子 Agent 验收报告 |

## 验收记录

H1–H8 验收通过，子 Agent `heading_acceptance` 独立复核无阻塞项。未提交 Git。

- 31 项相关单测通过：turn-heading、message-height、turn-layout、current-activity。
- 13 个不同 Chromium 用例通过：标题 8 项、历史滚动 2 项、历史分页 3 项。主 Agent 使用 slot 51，子 Agent 使用 slot 52 独立执行；H4 校正后双方分别复跑通过，不把重复执行计入用例数量。浏览器使用受控 localhost HTTP/SSE。
- 真实 Electron H7 用例通过：主 Agent 10.5 秒，子 Agent 独立复跑 8.0 秒。均启动本机 Electron + app:// 构建页面 + 真实 Python 后端，使用临时隔离 SQLite；断言真实 HTTP 快照中的 finalReply 和数值耗时，检查纯回复/有过程、两种主题、刷新、键盘开合与正文身份。没有调用远端模型。
- 双方逐张检查 4 张真机截图，主 Agent 归档至 `target/acceptance/no-process-heading/heading-{plain,process}-{light,dark}.png`；子 Agent 独立截图在 `/tmp/cosir-heading-electron-review/desktop-smoke-H7-real-Elec-1bb4c-oth-themes-and-after-reload/`。
- `npm run build`、TypeScript、相关 ESLint、fixture Ruff、`git diff --check` 均通过。构建仍提示现有 CSS `::highlight` 优化器识别警告及大 chunk 警告，本次未修改相关样式或打包策略。

复现命令（前端命令在 `apps/desktop` 执行）：

```sh
npx vitest run tests/unit/turn-heading.test.ts tests/unit/message-height.test.ts tests/unit/turn-layout.test.ts tests/unit/current-activity.test.ts
COSIR_E2E_SLOT=51 npx playwright test tests/e2e/turn-heading.spec.ts tests/e2e/thread-scroll-stability.spec.ts tests/e2e/history-window.spec.ts --reporter=line
npm run build
npx playwright test -c playwright.desktop.config.ts tests/desktop-e2e/desktop-smoke.spec.ts -g 'H7 real Electron' --output=/tmp/cosir-heading-electron --reporter=line
```

### 调查后的验收口径校正

H4 初稿的“底部跟随正常”措辞不准确：`apps/desktop/components/assistant-ui/elements/thread.aui.tsx:134` 明确使用 `turnAnchor="top" autoScroll={false}`。运行中追加正文不持续追底是现有交互，不能把开启自动追底夹带进本次改造。新增测试实际复现小幅追加后视口保持原 scrollTop，故按现有契约验证：正文增长不强制移动阅读位置、用户可手动回到底部、随后完成移除标题不重建正文且底部无残留空隙。滚动实现不改。

标题投影原有 `_cancelling` 参数从未消费，ESLint 报 unused；同步删除其在 Turn → LiveTurnHeading 的无效透传，取消反馈仍由 CurrentActivity 消费 Task 级状态，行为不变。
