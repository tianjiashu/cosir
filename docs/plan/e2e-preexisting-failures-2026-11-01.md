# 8 个既有 E2E 失败用例排查报告（2026-11-01）

## 范围与方法

- **对象**：`apps/desktop` Playwright E2E 中，在精简前基线 `60b26751` 上就已失败的 8 个用例。当前工作树重跑结果相同：8 failed / 139 passed。
- **方法**：
  - 按用例分 5 组，各由一个只读子 Agent 独立调查：读失败快照与 trace，看断言依赖的代码路径，用 `git log -S` 定位哪一边后改。
  - 主 Agent 抽查了每组的关键论断，包括提交 diff、`文件:行` 和报错原文，均与代码事实一致。
- **未做**：没有修改任何测试或代码，也没有回到旧提交逐个重跑验证「当时是通过的」。这一点靠 git 历史推断。

## 总览

| # | 用例 | 分类 | 引入点 | 置信度 | 建议改哪边 |
|---|---|---|---|---|---|
| 1 | context-observer.spec.ts:193 缓存命中率 | 测试漂移 | `1cdb454e` 删文案，漏删断言 | 高（85%） | 测试（已删断言；之后暴露一个真实缺陷，见下方第 1 节补充） |
| 2 | frontend-regressions.spec.ts:46 截图 | 环境差异（抗锯齿） | 无代码改动；本机浏览器缓存 10-28 重装 | 中高（85%） | 测试（已修：该断言加 `maxDiffPixels: 20`） |
| 3 | fullscreen.spec.ts:19 「Team 设置」 | 测试漂移 | `6a1ac503` 对话框改为「Team 属性」面板，切画布默认全屏 | 高（95%） | 测试 |
| 4 | new-conversation.spec.ts:55 clientCancelCount=1 | 开发期副作用，非生产缺陷 | `157a888a` 让 attach 的 fetch 同步发出，叠加 StrictMode | 高 | 测试（已修，见下） |
| 5 | new-conversation.spec.ts:227 attach 计数=2 | 同 #4 | 同 #4 | 高 | 同 #4 |
| 6 | new-conversation.spec.ts:307 resume_rejected 日志 | 测试漂移 | `6406cf80` 恢复改为同步 409，删了旧事件 | 高 | 测试 |
| 7 | new-conversation.spec.ts:441 点「关闭」超时 | 测试漂移 | `d46ca1da` 改用 `showErrorDialog`，按钮名为「知道了」 | 高 | 测试 |
| 8 | system-configuration.spec.ts:21 落到 appearance | 测试漂移（或待确认的产品取舍） | `b42c5b24` 把「外观」插到标签首位 | 高（90%） | 测试（已修：断言落在导航第一项，再点进子 Agent；默认页保持外观） |

**结论**：
- 没有发现违反 AGENTS.md 契约的生产代码缺陷。
- 6 个是测试没跟上有意的界面或契约变更。
- 1 个是像素级环境差异。
- 1 组（#4、#5）是开发模式下多发一个立即被 abort 的订阅请求。生产构建不受影响，但它是一次无意的行为变化。
- 测试服务（`tests/e2e/test-server.mjs`）与当前后端契约对齐：resume 被拒返回 409，cancel 返回 202，attach 是长订阅。不存在测试服务漂移。

---

## 1. context-observer.spec.ts:193「详情用量…」：测试漂移

- **现象**：断言 `toContainText("缓存命中率 75%")` 失败。详情区实际显示「输入 1,200 / 输出 50 / 缓存读取 900 / 总计 1,250」。
- **证据**：
  - 现在的详情页只渲染四格 `<dl>`，见 `components/context-observer/message-detail.tsx:210-225`。全仓已没有「缓存命中率」文案。
  - `51de5a74`（10-17）同时加了这句说明和这条断言。`1cdb454e`（10-17 22:49）在 `message-detail.tsx` 删掉了这句说明，连同底部的只读、查找提示条一起删除。同一提交也改了这个 spec，但第 201 行没删。
  - 命中率计算 `cacheUsage` 仍在：`message-model.ts:152-173`，列表行在用（`message-list.tsx:242-243`），单测覆盖。
  - 测试数据形状 `input_token_details.cache_read` 与后端 `openai_model.py:36-42` 一致。
- **反向证据**：`1cdb454e` 的提交说明主题是 composer，没提删这句文案，不能完全排除误删。`message-detail.tsx:82` 还在算 `rate`，只是没用上。
- **建议**：
  - 删掉 `context-observer.spec.ts:201`，或改为断言现有四格（如「缓存读取 tokens」「900」）。
  - 这条用例在第 201 行之后的步骤从没跑到过，修完可能暴露后续失败。
  - 如果希望详情页恢复命中率，就改产品代码，由作者决定。
- **处置与补充（已删断言）**：
  - 删掉第 201 行后，用例停在末尾的「窄窗口返回保位」。`1cdb454e` 删掉「定位序号」输入框时，把操作从「定位到 #80」改成「点击 #2」，却仍断言 `#80 User` 可见。已改为断言 `#2 Assistant` 可见。
  - 改完后仍失败，而且是真实缺陷：
    - 现象：宽度 900 的窗口里，列表滚动位置为 0，点 #2 打开详情（列表被隐藏），点「返回列表」后滚动位置变成 637，可视区从 #6 开始，#2 不可见。
    - 测得的数据：点击前第一行是 #0、滚动位置 0；返回后 300ms 滚动位置 637，前三行是 #6/#7/#8。
    - 初判（置信度中）：窄窗口用 `hidden`（`display:none`）隐藏列表（`observer-results.tsx:79-80`），隐藏期间虚拟列表测得各行高度为 0；重新显示时按尺寸变化修正滚动位置，结果偏移了约 7 行。返回时没有恢复滚动位置的意图（`backToList` 只关详情、聚焦列表）。
    - 原先没暴露：旧写法「定位 #80」会留下一个待执行的滚动意图，返回时列表滚到 #80，恰好掩盖了这个问题。
  - 这条用例现在因这个缺陷失败，没有为通过而放宽断言。
- **缺陷修复（已修）**：
  - 证实：在列表上挂探针抓到滚动来源是 TanStack `resizeItem → applyScrollAdjustment`，连续 7 次各 +91px。隐藏期间行高被测成 0 写进缓存；重新显示时按「重新测量」规则（行顶 + 旧高度 ≤ 滚动位置即补偿），旧高度全为 0，于是逐行补偿。
  - 同类问题：对话区在浏览器铺满时同样被 `display:none` 隐藏（`app-layout.tsx:260`）。模拟隐藏再显示，停在顶部时滚动位置从 0 跳到 1664；停在中间不跳。隐藏期间列宽读成 0，还会把按列宽分桶的行高记忆整桶换掉（`message-height-cache.ts:18`）。
  - 修法：新增 `lib/virtual/measure-rendered-element.ts`，行没有被渲染（`checkVisibility()` 为 false）时不采用读数，沿用缓存高度，没有缓存用估计高度。上下文观测列表与对话列表都经它测量；对话列表只在真正测得时写行高记忆。文件搜索列表是固定行高、从不测量，不接入。
  - 测试：新增单测 `measure-rendered-element.test.ts`；`history-window.spec.ts` 新增「对话区被隐藏再显示后，停在顶部的视口不跳」。对照实验中撤掉修复，这条和 context-observer:193 都失败，恢复后都通过。
- **后续：attach 被拒后重连用例（原 `:491`，现 `:471`，已修）**：
  - 先前判断混入了环境干扰：另一个会话同时在跑 E2E，测试服务固定 8000 端口、内存状态共用，对方的 reset 清掉了本用例的任务，对方结束时还关掉了服务（`ECONNREFUSED`）。以下证据都在确认无其他运行时取得。
  - 干净环境 6/6 失败：挂载时约 5ms 内两次 attach，第一次 `ERR_ABORTED`，第二次 200；页面从未收到 503，前端没有断开或重连日志。临时关掉 StrictMode 后 4/4 通过：网络 503 → 200，日志依次为订阅断开、安排重连、订阅建立。产品重连逻辑正确；注入的 503 被 StrictMode 多发、随即中止的那次 attach 吃掉，用例从未走到重连，以前「通过」是假通过。
  - 这是同一根因第 4 次出现（:55、:227、:471、`workbench-subagent.spec.ts` 按响应计数的潜在不稳定）。共同点是把「挂载期间的请求」当作注入或计数目标。定为测试规则：注入与计数都放在订阅建立之后，以「订阅已建立」日志为同步点。不改 E2E 跑开发构建（前端日志只在开发模式暴露、5 个 spec 导入开发服务器模块），也不改订阅的发出时机。
  - 改动：
    - 新增 `tests/e2e/frontend-logs.ts`（`frontendLogs`、`openedSubscriptionCount`，可按事件与 taskId 过滤），两个 spec 的重复实现删除。
    - `test-server.mjs` 新增 `__test__/drop-subscriptions`（断开某 Task 的长订阅，模拟后端连接中断）；`fail-next-attach` 加注释写明只能在订阅建立后注入。
    - 重写该用例：等订阅建立 → 注入 503 → 断开订阅 → 断言 503 → 200、两次断开、未耗尽重连预算 → 发送后照常收到流式输出。
    - `workbench-subagent.spec.ts` 改按 `workbench_agent_subscription_opened` 日志按子 Task 计数，删掉已被证伪的「按响应计数可排除中止请求」注释。
  - 验证（确认运行期间无其他测试插入）：两个 spec 共 12 条全过；该用例、:233 与 workbench 两条各重复 5 次共 15 次全过。
  - 范围外（未处理）：多个会话共用 8000 端口的测试服务会相互干扰，建议测试服务端口可配置、端口被占用时直接失败而不是静默复用。

## 2. frontend-regressions.spec.ts:46「工具追踪视觉回归」：环境差异

- **现象**：`toHaveScreenshot` 报「2 pixels (ratio 0.01) are different」。
- **证据**：
  - 子 Agent 逐像素比对 expected 和 actual：
    - 元素、文案、图标、展开收起状态完全一致；
    - 约 1.5k 个像素有轻微灰度变化，散布全图；
    - 各元素重心偏移不超过 0.17px；
    - 是整体重新光栅化造成的抗锯齿差异，不是布局或图标变化。
  - 基线最后更新于 `01025141`（10-16）。此后截图涉及的组件没有可见改动。`lucide-react` 1.37.0、Playwright 1.62.1、chromium-1234 前后都没变。
  - 本机 `~/Library/Caches/ms-playwright/chromium-1234` 的安装时间是 10-28，晚于基线，是环境侧变动的线索。具体哪个环境因素导致，没有定位。
  - 截图前的 DOM 断言（:51-56，图标各 1 个、完成后收起）全部通过。
- **反向证据**：Fork 图标边缘有 1 个像素差到 61 级灰度。但同类差异遍布每个元素，更符合全图重新光栅化。
- **建议**：
  - 首选给这条截图断言加小容差（如 `maxDiffPixels: 20`）。这条用例要守的「有图标、完成后收起」已由 DOM 断言兜住。
  - 也可以在确认本机就是标准 E2E 环境后，只针对这个用例重新生成 darwin 基线。
  - 截图后的展开断言（:59-62）这次没跑到。

## 3. fullscreen.spec.ts:19「画布全屏覆盖…」：测试漂移

- **现象**：点「画布」后等「Team 设置」按钮超时。
- **证据**：
  - 失败时的页面快照：画布已经全屏，顶栏有「Team 属性」「退出全屏画布」等按钮。右侧 `region "Team 属性"` 已展开（Team ID、名称、最大轮数、用途说明），没有「Team 设置」按钮。
  - 代码：
    - 入口按钮是 `aria-label="Team 属性"`（`components/agent-team/graph-editor.tsx:719`）；
    - 新建 Team 时属性面板默认打开（`:134-135`）；
    - 切到画布即全屏（`:169-173`）；
    - `graph-inspector.tsx:36` 的 docstring 写明「取代原先……『Team 设置』对话框」。
  - `6a1ac503`（10-28）做了这次改版，并同步改了 `agent-team-graph.spec.ts`，漏了 `fullscreen.spec.ts`。后者最后一次修改在 `b691c036`（10-25）。
- **建议**：按新交互重写 `fullscreen.spec.ts` 第 30-49 行。
  - 在 `region "Team 属性"` 里填名称和用途说明。
  - 全屏验证改为：先「退出全屏画布」，再给父级加 transform，然后「全屏画布」；或者在切画布前就给父级加 transform。
  - 去掉对 Team 设置对话框的断言。
  - 「保存失败」弹窗那一段仍然有效。

## 4 & 5. new-conversation.spec.ts:55 / :227：StrictMode 下多发一个被 abort 的 attach

- **现象**：
  - :55 的 `telemetry.clientCancelCount` 期望 0，实际 1。
  - :227 的 `attachRequests.length` 期望 1，实际 2。
- **机制**：
  - `src/main.tsx:28` 包了 `<React.StrictMode>`，E2E 跑 Vite dev，effect 会执行「挂载 → 清理 → 再挂载」。
  - `use-task-assistant-transport-runtime.ts:99-103` 在 effect 里 `session.open()`，清理时 `close()`。
  - `TaskTransportSession.open()`（`lib/assistant/task-transport-session.ts:84-96`）经 `requestOk → requestRaw`。`lib/http/client.ts:42` 在第一个 await 之前就同步调用了 `fetch`。所以第一次 open 的请求已经发到网络，随后被清理阶段 abort 掉。
  - trace 证实：:227 在 5ms 内有两次 `POST /tasks/102/assistant/attach`，第一次是 `net::ERR_ABORTED`。挂载日志成对出现。`/assistant/state` 和 `subscription_opened` 都只有一次。
  - :55 中，被 abort 的那次发生时 run 1 仍有执行者。测试服务 `test-server.mjs:614-617` 把「执行中有订阅断开」计为 `clientCancelCount`。
- **哪一边后改**：
  - 断言和「effect 里 open」都来自 `b691c036`（10-25）。
  - 当时 `open()` 写的是 `headers: await this.callbacks().headers()`，fetch 被推迟到微任务之后，清理先 abort，请求根本没发出。
  - `157a888a`（10-29，模型选择组件重构）改成同步的 `requestOk`，这个「顺带的推迟」就没了。主 Agent 已核对该 diff。提交说明没提订阅行为，属于无意的行为变化。
- **判断**：
  - 不违反契约：AGENTS.md 规定订阅断开不取消 Run，生产构建没有 StrictMode，只建一条订阅。
  - 测试守的语义是「已建立的订阅只有一条、执行中不掉订阅」，却按「发出的 attach 请求数」和「服务端连接关闭数」计数，把开发模式的副作用也算了进去。
- **定案：改测试（B），不改代码**：
  - 不选 A（`open()` 先让出一拍再发请求）：生产代码会多一段只为抵消开发模式副作用的逻辑，还依赖 StrictMode 清理时序；`157a888a` 之前只发一次是 `await headers()` 碰巧推迟了请求，不是设计。React 官方立场是开发期 effect 跑两次属预期，应保证清理正确，而不是阻止重复执行；本仓清理已正确（`task-transport-session.ts:104` 被中止的 open 不触发重连）。
  - 计数口径统一为「已建立的订阅」= 前端 `assistant_transport_subscription_opened` 日志条数：被替换的连接不回调 `onResponse`，所以不会记这条日志。
  - 曾试过按 Playwright `response` 事件计数，重复跑 5 次失败 3 次：被中止的那次 attach 在约 5ms 的窗口里有时已拿到响应头（trace 中状态为 200 且 `net::ERR_ABORTED`），所以按响应计数同样不稳定。`workbench-subagent.spec.ts:26` 也按响应计数，有同样的潜在不稳定（本次未改）。
  - 改动：
    - :227 新增 `openedSubscriptionCount(page)`，替换三处 attach 计数；页面重载后日志从零计，所以重载后的期望是 1，不再是累计的 2。
    - :55 删除 `clientCancelCount` 断言。原有的 opened=1、closed=0 日志断言已覆盖「运行中掉线再重连」。
    - `test-server.mjs` 删除只服务这条断言的 `clientCancelCount` 计数。
  - 验证：两条用例各重复 8 次，16 次全部通过；整份 `new-conversation.spec.ts` 9 条全部通过。

## 6. new-conversation.spec.ts:307「不可恢复时继续运行被拒」：测试漂移

- **现象**：`composer_resume_rejected` 日志期望 2 条，实际 0 条。「当前运行不可恢复」的可见性断言仍然通过（来自 toast）。
- **证据**：
  - 现在 409 由 `use-business-resume.ts:35-43` 抛出。`thread.aui.tsx:397-415` 接住后弹 toast，并记 `composer_resume_failed`（`:411`）。
  - `6406cf80`（10-27）把 resume 改为同步 409、不进 snapshot，符合 AGENTS.md「命令被拒不进 snapshot」。同时删掉了旧的「按顶层 error.seq 认拒绝」effect 和 `composer_resume_rejected` 事件，测试服务也一起改了，唯独 spec 没跟上。
- **建议**：事件名改为 `composer_resume_failed`，或改为断言 toast 出现两次。用例标题「顶层错误显示在对话区」和「以顶层 error 投影」等注释已过时，一并修正。
- **范围外发现（本次不处理）**：预期内的 409 记成 ERROR 级，和 `lib/http/client.ts` 里「4xx 记 WARNING」的约定不一致。

## 7. new-conversation.spec.ts:441「编辑重跑失败」：测试漂移

- **现象**：在弹窗里点 `{ name: "关闭" }` 超时。
- **证据**：
  - 失败快照中弹窗存在，标题「发送失败」，正文「编辑重跑被测试后端拒绝」，唯一按钮是「知道了」。编辑框已恢复为 `edit-failure`，顶部草稿仍是 `keep draft`，被测行为本身正确。
  - `use-runtime-transport.ts:100` 调用 `showErrorDialog`。告知式弹窗的默认按钮是「知道了」（`lib/confirm/confirm-store.ts:117-126`），且 `showCloseButton={false}`（`components/confirm/confirm-dialog.tsx:76`）。
  - `d46ca1da`（10-29，统一为通知式提示）做了这次替换，测试没更新。
- **建议**：`new-conversation.spec.ts:482` 改为点击 `{ name: "知道了" }`。

## 8. system-configuration.spec.ts:21「从 settings 路由打开」：测试漂移

- **现象**：`toHaveURL(/\/settings\/agents$/)` 失败，实际是 `/settings/appearance`。
- **证据**：
  - `/settings` 重定向到 `SYSTEM_SETTINGS_TABS[0]`（`src/router.tsx:53-55`）。
  - 数组首项现在是 `appearance`（`components/settings/settings-tabs.ts:24`），`agents` 在第 25 行。
  - `b42c5b24`（10-16）新增外观设置时把它插在首位。断言写于更早的 `03351d24` / `01025141`。
  - 用例后续第 107-138 行（编辑 reviewer）也依赖停在「子 Agent」标签。只改第 103 行的正则不够。
- **反向证据**：`b42c5b24` 的提交说明没提「默认标签改为外观」，可能只是随手放在了首位。这是产品取舍，不是代码缺陷：重定向逻辑本身正确。
- **建议**：
  - 如果默认页就是外观，改测试：断言重定向到第一个标签，再点导航「子 Agent」进入。仓内先例是 `agent-team-session.spec.ts:8-9`。
  - 如果默认页应是子 Agent，把 `settings-tabs.ts` 里的 appearance 挪到后面，测试不用改。
  - `agent-team-graph.spec.ts` 中有 4 处 `goto("/settings")`（第 42、278、326、351 行）。它们没有失败，说明不依赖默认落点，但没有逐个核对。

---

## 需要你决定的点

1. ~~**#4/#5 走 A 还是 B**~~：已定 B（改测试），已修。
2. ~~**#8 设置页默认标签**~~：已定保持外观，只改测试，已修。
3. ~~**#2 截图**~~：已定加小容差，已修。
4. ~~**#1 命中率**~~：已定不加回来，删断言。
5. **#1 后续暴露的缺陷**（新）：窄窗口点开消息再「返回列表」，列表往下跳约 7 行，刚点的消息不在可视区。是否修、怎么修待定。

其余（#3、#6、#7）是纯测试漂移，可以直接按建议修。

## 共性观察

- 7 个用例中有 5 个，是改版时同步改了「姊妹 spec」却漏了另一个 spec（#1、#3、#6、#7，加上 #8 的新增标签）。这类漂移靠「改界面文案 / 交互时 `grep` E2E 中的旧可访问名称」就能拦住。
- 建议在每次改界面后跑全量 E2E，而不是只跑直接相关的 spec。

## 补记（2026-11-01 对话渲染重构后）

- `workbench-boundaries.spec.ts:152`「新对话页选择其他工作区…」：点工作区 8 后断言 `aria-current="true"` 失败。在干净的 HEAD（53392a29）worktree 上用同一条用例复现，与对话渲染重构无关，待另查侧栏工作区树的选中态。
