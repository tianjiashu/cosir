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
| 1 | context-observer.spec.ts:193 缓存命中率 | 测试漂移 | `1cdb454e` 删文案，漏删断言 | 高（85%） | 测试 |
| 2 | frontend-regressions.spec.ts:46 截图 | 环境差异（抗锯齿） | 无代码改动；本机浏览器缓存 10-28 重装 | 中高（85%） | 测试（加容差或重生成基线） |
| 3 | fullscreen.spec.ts:19 「Team 设置」 | 测试漂移 | `6a1ac503` 对话框改为「Team 属性」面板，切画布默认全屏 | 高（95%） | 测试 |
| 4 | new-conversation.spec.ts:55 clientCancelCount=1 | 开发期副作用，非生产缺陷 | `157a888a` 让 attach 的 fetch 同步发出，叠加 StrictMode | 高 | 二选一，见下 |
| 5 | new-conversation.spec.ts:227 attach 计数=2 | 同 #4 | 同 #4 | 高 | 同 #4 |
| 6 | new-conversation.spec.ts:307 resume_rejected 日志 | 测试漂移 | `6406cf80` 恢复改为同步 409，删了旧事件 | 高 | 测试 |
| 7 | new-conversation.spec.ts:441 点「关闭」超时 | 测试漂移 | `d46ca1da` 改用 `showErrorDialog`，按钮名为「知道了」 | 高 | 测试 |
| 8 | system-configuration.spec.ts:21 落到 appearance | 测试漂移（或待确认的产品取舍） | `b42c5b24` 把「外观」插到标签首位 | 高（90%） | 测试；若默认页应是子 Agent 则改标签顺序 |

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
  - 但测试守的「挂载只建立一条订阅、执行中不掉订阅」在开发模式下被打破了。
- **两条路线（需要你选）**：
  - **A 改代码**：`open()` 登记 handle 后先让出一次（`await Promise.resolve()`），再判断 `if (this.active !== handle) return`，docstring 写明「被替换的 open 不应打出网络请求（StrictMode 清理再挂载、连续 open 互相替换）」。
    - 优点：恢复 `157a888a` 之前的行为，并把意图显式写进代码；连续重连时也少一次无用请求。
    - 缺点：依赖「StrictMode 清理是同步的」这一时序事实。
  - **B 改测试**：
    - :227 只统计没有失败的 attach，排除 `requestfailed` / ERR_ABORTED；
    - :55 的 `clientCancelCount` 要在测试服务里按「是否已推完 full 帧、连接存活时长」过滤，比较牵强。
  - **倾向 A**：B 要在测试服务里做时序过滤，等于为开发模式的副作用加特例；A 一处改动就让两条用例回到原有语义。

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

1. **#4/#5 走 A（改 `open()`）还是 B（改测试）**。倾向 A。
2. **#8 设置页默认标签**：外观（改测试）还是子 Agent（改标签顺序）。
3. **#2 截图**：加容差，还是在本机重新生成基线。
4. **#1 命中率**：确认是有意删除（删断言），还是要在详情页加回来。

其余（#3、#6、#7）是纯测试漂移，可以直接按建议修。

## 共性观察

- 7 个用例中有 5 个，是改版时同步改了「姊妹 spec」却漏了另一个 spec（#1、#3、#6、#7，加上 #8 的新增标签）。这类漂移靠「改界面文案 / 交互时 `grep` E2E 中的旧可访问名称」就能拦住。
- 建议在每次改界面后跑全量 E2E，而不是只跑直接相关的 spec。
