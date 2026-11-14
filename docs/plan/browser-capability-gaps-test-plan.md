# 内置浏览器能力缺口：测试计划与回归测试计划

> 2026-11-14。对应方案：[browser-capability-gaps-plan.md](browser-capability-gaps-plan.md)。由一个不带对话上下文的子 Agent 只读方案与代码后写出，主 Agent 抽查了关键论断（第五节 P1、P2，以及 E2E 窗口与设计稿路径两处事实）。尚未运行任何测试。
>
> 第五节列出的 P1–P12 已在方案修订版中逐条处置（方案文末「问题处置表」）。本计划已按修订版契约同步：响应体状态表与上限分层、`untracked` 记账隔离、弹窗补采、`response_body` 结果、找不到条目报 `INVALID_PARAMS`、区域越界报 `OUT_OF_VIEWPORT`、`repeat` 必填、「stopped after k of N」。
>
> 实施循环的问题账本见 [browser-capability-gaps-loop-log.md](browser-capability-gaps-loop-log.md)。

## 运行命令

| 范围 | 命令 |
|---|---|
| 后端 | `cd apps/backend && uv run pytest <文件> -q` |
| 导出 schema | `cd apps/backend && uv run python scripts/export_browser_tool_schema.py` |
| 宿主与前端单测 | `cd apps/desktop && npx vitest run tests/unit/<文件>`；全量 `npm run test:unit` |
| 类型检查 | `cd apps/desktop && npx tsc --noEmit && npx tsc -p electron` |
| 桌面引擎 E2E | `cd apps/desktop && npx playwright test -c playwright.desktop.config.ts browser-engine`（`global-setup.mjs:16-25` 自己打包 harness，仍会用 cargo 构建 Terminal Worker） |
| 原生验收（不经 Playwright） | `node apps/desktop/scripts/test-browser-native.mjs` |

每个阶段交付前都跑一遍：

```
cd apps/backend && uv run python scripts/export_browser_tool_schema.py \
  && uv run pytest tests/test_browser_tools.py tests/test_browser_contract_guard.py tests/test_browser_api.py tests/test_system_prompt_builder.py -q
cd apps/desktop && npx vitest run tests/unit/browser-tab-log.test.ts tests/unit/browser-contract.test.ts \
  tests/unit/browser-pure.test.ts tests/unit/browser-action-settlement.test.ts tests/unit/browser-operation.test.ts \
  tests/unit/browser-host.test.ts tests/unit/browser-initialization.test.ts tests/unit/browser-deadlines.test.ts \
  tests/unit/browser-page-tool.test.ts
cd apps/desktop && npx tsc --noEmit && npx tsc -p electron && npm run lint
```

最后再跑一次全量 `npm run test:unit` 和 `uv run pytest -q`。

---

## 一、测试计划

### 阶段 1a：标签日志承载响应体，请求结束时采集

**A. TabLog 存储规则**

- 层级：宿主 Vitest。
- 文件：扩展 `apps/desktop/tests/unit/browser-tab-log.test.ts`。
- 方法：直接调用 `requestStarted / responseReceived / requestFinished` 和新增的写入响应体接口，不经 CDP。

| 用例 | 关键断言 |
|---|---|
| A1 文本响应体挂在对应条目上 | Fetch 200 `application/json` 采下后，正文读取给出 `{state:"text", mime, size, text, truncated:false, reason:null}`；条目原有字段不变 |
| A2 4xx / 5xx 也采 | 500 的 JSON 正文能读到（核心场景就是「保存失败」）；`digest().failedRequests` 仍只计 1 次 |
| A3 宿主单条上限 256 KB | 刚好 256 KB 不截断；超过时截断到 256 KB 并 `truncated:true`；用中文正文，确认按 UTF-8 字节计、截断不切坏多字节字符 |
| A4 每标签 2 MB 先进先出挤出 | 累计超过 2 MB 后，最早的条目还在、状态为 `evicted`、`text:null`；内部字节计数等于剩余正文之和，不漂移 |
| A5 条目被 1000 条容量挤掉 | 正文随条目一起释放，字节计数相应减少；沿用 `browser-tab-log.test.ts:134` 的 1200 条写法，每条带正文 |
| A6 重定向只有最后一跳带正文 | 沿用 `:117`：302 那一跳 `body` 为 null，最终跳可采；同一 requestId 不串到上一跳 |
| A7 二进制 | `image/png`、`application/octet-stream` 状态为 `binary`，`size` 为 `encodedDataLength`，不发采集 |
| A8 流式 | `resourceType: "EventSource"` 的条目状态为 `streaming`；分块返回、最终结束的 Fetch 照常采集 |
| A9 跨导航保留 | `pageStarted()` 之后，按 seq 仍能读到上一页的正文；不带 `since` 的默认读取只列当前页，行为不变 |
| A10 其余状态 | 在途与采集中为 `pending`；`loadingFailed` 的 Fetch `body` 为 null；非 Fetch / XHR 类型 `body` 为 null（两者的正文读取返回 `unavailable` 并说明原因）；空正文为 `empty`（`text:""`）；超过渲染进程单条上限为 `too_large`；采集失败为 `unavailable` 且带原因 |
| A11 读取不消费 | 读正文不推进 `digest` 的游标，`undeliveredNotes` 不变（与 `:9` 同一个不变量） |
| A12 找不到条目 | seq 不存在、指向 console 条目、已被 1000 条容量挤掉，正文读取都报 `INVALID_PARAMS` |
| A13 补采登记 | 记为 `deferred` 的条目，`TabLog` 交出待补采的 requestId；补采成功后转为 `text`，补采时报「页面已离开」则转为 `unavailable` |

**B. BrowserTab 的采集触发链**（方案漏了这一层）

- 层级：宿主 Vitest。
- 文件：新建 `apps/desktop/tests/unit/browser-tab-network.test.ts`，套用 `browser-initialization.test.ts:3-14` 的写法：mock electron 的 `contents.debugger`，捕获 `debugger.on("message")`，手工派发 CDP 事件。

| 用例 | 关键断言 |
|---|---|
| B1 初始化参数 | `Network.enable` 带 `maxTotalBufferSize: 8 MB` 与 `maxResourceBufferSize: 1 MB`（现有 `browser-initialization.test.ts:69` 只核对方法序列，不看参数） |
| B2 触发条件 | `loadingFinished` 只对 Fetch / XHR 加文本 mime 发一次 `Network.getResponseBody`，时限 5 秒；Document、Image、二进制 mime 不发 |
| B3 base64 | 返回 `base64Encoded:true` 的文本先按 UTF-8 解码 |
| B4 页面停住 | `pausedBy` 非空时 `CdpSession.send` 立即以 `DIALOG_OPEN` 拒绝（`cdp-session.ts:82-85`），在途时被 `interrupt` 打断；条目记为 `deferred`，不抛成未处理异常 |
| B5 **关键**：记账隔离 | 采集命令带 `untracked`，`CdpSession` 不把它交给标签级的 `onCommand`（`browser-tab.ts:161-163` → `operation.ts:74-77`）。断言：有 `operation` 时采集命令不进 `pending`；act 期间结束的 fetch 不推迟 `operationSettled`；不带 `untracked` 的命令照旧记账 |
| B9 弹窗答完后补采 | `deferred` 的条目在 `resumeAfterDialog()` 时重发采集；成功转为 `text`；`resumeAfterDialog()` 仍补设设备模拟并调用 `operation.resumeCleanup()`（原有两件事不变） |
| B10 补采前已导航 | 补采命令失败（如找不到资源）时记为 `unavailable`，原因写明页面已离开或资源已不在 |
| B6 采集中关标签 | getResponseBody 迟到的拒绝不成为未处理异常（标准同 `browser-initialization.test.ts:81`）；写回已关闭标签的日志对象无副作用，不另设防 |
| B7 CDP 超时 | 10 秒命令时限（`cdp-session.ts:6`）超时后条目有明确状态，不留悬挂的 Promise（`vi.getTimerCount()` 归零） |
| B8 删除被取代的旧代码 | `browser-tab.ts:37-38` 的 1 KB 常量与「从不取响应体」注释已删除（grep 确认） |

### 阶段 1b：`browser_logs` 的 `body` 参数、列表行状态、wire 与渲染

| 用例 | 层级与文件 | 关键断言 |
|---|---|---|
| C 参数模型 | 后端 pytest，`apps/backend/tests/test_browser_tools.py`，扩展 `test_logs_reject_filters_of_the_other_kind`（`:997-1008`）的参数表 | 拒绝：`body` 配 `kind:"console"`；配 `since`、`level`、`pattern` 任一；配 `failed_only:true`；`body` 小于 1 或不是整数。接受：`{"kind":"network","body":20}`，以及同时给了非默认 `limit` 的情形（`limit` 不起作用，不报错） |
| D 请求形状 | 同上 | `test_every_tool_sends_exactly_the_exported_wire_shape`（`:655`）加一组带 `body` 的参数；`test_logs_sends_every_field_and_renders_entries`（`:940`）的期望 dict 加 `body: None` |
| E 契约导出 | 契约守卫 | 改模型后重跑导出脚本，`test_exported_schema_matches_the_arg_models`（`test_browser_contract_guard.py:65`）通过 |
| F 宿主读参数 | 宿主 Vitest，`tests/unit/browser-contract.test.ts:143-161` | 两条现有 `browser_logs` 样例补 `body:null`，再加一条 `body:20`（其余过滤为 null）；`parseLogs` 产出新的查询形状；键集合等于 schema 属性 |
| G wire 样例 | 两侧契约，`electron/main/browser/contract/wire-samples.json` 与 `browser-contract.test.ts:75-103` | `network_logs` 的 Fetch / XHR 条目带 `body: {state, mime, size}`，其他类型为 null；新增 `response_body`（`kind: "response_body"`，带 `entry` 与 `body`，不带翻页字段）、`response_body_truncated`、`response_body_unavailable` 三份样例 |
| H 渲染 | 后端 pytest，`test_browser_contract_guard.py` | `:186-201` 的逐行断言改成带状态的新行；正文读取放在边界标签里，正文里出现闭合标签时只剩一个闭合标签（仿 `test_browser_tools.py:996`）；`truncated` 与 `unavailable` 的说明文字；二进制只给类型和大小 |
| I 长正文走通用输出预算 | 后端 pytest，`test_browser_tools.py` 或复用 `tests/test_tool_output_budget_truncation.py` 的写法 | 256 KB 正文经执行管线后，超过 20,000 字符（`constant.py:347`）写入 `.cosir/tool-artifacts/` 并给出路径，正文里有截断标记 |
| J 交接中可读日志 | 宿主 Vitest，`tests/unit/browser-host.test.ts`（目前没有 logs 用例） | 请求带 `handoffTabs` 时 `logs` 照样返回，不抛 `HANDED_OFF`（`browser-host.ts:186-194` 不调 `assertAgentMayModify`） |
| K 工具描述 | 后端 pytest，`test_browser_tools.py` | 描述写明「请求结束时采下；导航后仍可读；直到被挤出或标签关闭」 |

**L. 引擎 E2E**（`tests/desktop-e2e/browser-engine.spec.ts`）

- 服务端：`:115-136` 的路由补上以下接口。

  | 接口 | 返回 |
  |---|---|
  | `/api/save` | 500 JSON |
  | `/api/big` | 约 300 KB |
  | `/api/bin` | PNG |
  | `/api/redirect` | 302 跳到 JSON |
  | `/sse` | EventSource |

- 页面：新增 `/save-fail`，点击后先 fetch 再 alert。
- 断言：
  - 列表行状态正确；
  - 按 seq 读到 500 的正文；
  - 导航到 `/second` 后，带 since 仍能读到；
  - 页面停在 alert 时也能读；
  - 重定向链上只有最后一跳有正文。

**M. 实跑验收**（真实模型，在桌面应用里由用户配合发起）

- 本地小应用：保存接口返回 500 `{"error":"quota exceeded"}`，页面只显示「保存失败」。
- 成功标准：
  - Agent 的回复里说出 `quota exceeded`；
  - 过程中没有用 `browser_evaluate` 重发请求，也没有挨个 seq 试错。
- 专门测「请求返回后立刻 alert」这个变体（P2）。

### 阶段 2：区域截图

| 用例 | 层级与文件 | 关键断言 |
|---|---|---|
| N1 参数互斥与形状 | 后端 pytest，扩展 `test_browser_tools.py:530` | `region` 与 `full_page`、与 `ref` 都互斥；宽高为正、坐标非负、有限值；最大高度建议与 `FULL_PAGE_MAX_HEIGHT=4000` 对齐 |
| N2 截区计算 | 宿主 Vitest，**新建** `tests/unit/browser-page-capture.test.ts`（现有单测完全没覆盖 `page-capture.ts`） | fake 的 `send` 记录 `Page.captureScreenshot` 参数；区域换成文档坐标时加上 `visual.x/y`（滚动后）；`scale = zoom / deviceRatio`；不加边距；小区域按 160 像素、至多 4 倍放大 |
| N3 越界 | 同上 | 区域任何一部分落在可视视口外时报 `OUT_OF_VIEWPORT`，不发截图命令；不新增错误码 |
| N4 不占修改槽位 | `browser-host.test.ts` | 带 region 时不调 `tab.modify`；交接中允许（与视口截图一致，`browser-host.ts:176` 只对 ref 检查交接） |
| N5 页面停在弹窗 | `browser-host.test.ts` 或 N2 | 返回 `DIALOG_OPEN`，与现有截图一致 |
| N6 契约 | `browser-contract.test.ts:146`；后端 `test_browser_tools.py:771` | 键集合加 `region`；样例填非默认值；导出 schema 一致 |
| N7 正文与说明 | 后端 `test_browser_tools.py:771` 与守卫 `:220` | 区域截图的正文写明「像素与视口坐标不一致」；视口截图的正文不变；`ref` 参数说明不再以「chart」为例 |
| N8 引擎 E2E | `browser-engine.spec.ts`，新页面 `/chart`（Canvas） | `region` 截出的尺寸符合放大规则；`scroll` 之后截的仍是视口坐标那块；手机模拟下按 980 宽排版也正确（`page-geometry.ts:8-9`） |
| N9 实跑 | 真实模型 | Agent 截取并核对 Canvas 图表上的数值 |

### 阶段 3：横向滚动、三击、连点与连按

| 用例 | 层级与文件 | 关键断言 |
|---|---|---|
| O1 后端参数 | `test_browser_tools.py:503-527` 的拒绝表与 `:309` 的原样转发 | 拒绝：`click_count: 2` 与 `repeat: 2` 同时给；`repeat` 为 0 或 21。接受：`click_count: 3`、`repeat: 20`、`press.repeat`、scroll 的 `left` / `right` |
| O2 契约 | `browser-contract.test.ts:163-197`；`browser-pure.test.ts:116` | click、press 样例加 `repeat`（宿主不校验跨字段规则，`params.ts:20`）；`toAction` 产出 `repeat` 与三击的 `clickCount`；拒绝 `repeat: 21`、`click_count: 4` |
| O3 事件序列 | `browser-pure.test.ts` 的 `runActions` 组 | 三击：先 `moved`，再依次 pressed / released，clickCount 为 1、2、3（仿 `:599`）。连点 `repeat: 3`：三组 pressed / released，clickCount 都是 1，`mouseMoved` 只发一次，落点只取一次。带 ref 的连按：`DOM.focus` 只发一次，键事件 N 组 |
| O4 结算矩阵 | `browser-action-settlement.test.ts:59-126`，加 `repeat` 组 | 第 2 次 `mousePressed` 已派发后失败：`unknown`，消息末尾为 ` (stopped after 1 of 3)`。第 2 次派发前被闸门拒绝（第 1 次打开了弹窗）：因 `effectStarted` 已置位（`actions.ts:41`），仍是 `unknown`，消息写明 `stopped after 1 of N`；同批之后的 `dialog` 可以不带 id 作答（`actions.ts:46`）。第 1 次就失败且未派发：`failed`，不附「stopped after」。连按第 k 次 keyDown 失败：同上规则 |
| O5 `repeat` 必填 | 宿主类型检查：`npx tsc --noEmit` | 宿主 `PageAction` 的 click / press 带必填 `repeat`；直接构造宿主形状动作的地方（单测替身、`browser-native/main.ts`、引擎 E2E 的 `call("act")` 与 `clickOn`）都带宿主类型，缺了 `repeat` 即类型检查失败；执行器不做「缺省当 1」的兜底。验收：在任一 `call("act")` 里删掉 `repeat`，`npx tsc --noEmit` 必须报错 |
| O6 横向滚动页面函数 | `browser-pure.test.ts:722` 一组 | `left` / `right` 走 `overflowX`、`scrollWidth`、`scrollLeft`；到边返回 `SCROLL_END`，结算 `failed`；嵌套时带 ref 往右滚的是表格容器而不是整页，同一个 ref 往下滚的是纵向可滚的祖先 |
| O7 引擎 E2E | `browser-engine.spec.ts` | 宽表格：新页面 `/wide-table`（一个只横向滚动的容器，加一个整页横向溢出），往右滚后 `viewport` 快照里出现原本看不见、带 ref 的列；到最右报 `SCROLL_END`；`overflow-x: hidden` 时报「不可滚」。计数按钮：复用现有 `/counter`（`:68-70`），`repeat: 2` 计数为 2；「提交后 disable 按钮」变体在 `repeat: 2` 下只计 1 次；`click_count: 2` 只触发一次 dblclick，`repeat: 2` 不触发 dblclick。三击：在 `/rich` 的段落上三击后，`getSelection` 等于段落文字 |
| O8 实跑 | 真实模型 | Agent 测出本地小应用「快速点两次重复下单」，并给出依据（订单数为 2） |

---

## 二、回归测试计划

### 阶段 1

| 项 | 文件 | 预计要改 | 必须不变 |
|---|---|---|---|
| R1-1 TabLog 现有行为 | `browser-tab-log.test.ts` 全部 | `:24-26`、`:124-128` 用 `toEqual` 比整条记录，要补响应体状态字段 | 序号与游标；`moreBefore` / `moreAfter` / `droppedBefore`（`:134-159`）；`digest` 只消费一次、只计当前页、取消的请求不算失败（`:9-56`）；导航在途与放弃（`:97`）；失败只计一次（`:164`） |
| R1-2 console 不受影响 | `browser-tab-log.test.ts`；引擎 E2E `:869` | 无 | console 条目形状与 500 字符截断；三类容量 50 / 1000 / 1000（`tab-log.ts:5`）；`:879` 的 `toMatchObject` 是部分匹配，应保持通过 |
| R1-3 初始化 | `browser-initialization.test.ts:32-96` | 无 | 方法序列（`:73-76`）；各阶段的取消与失败清理；不多出计时器 |
| R1-3b 弹窗之后的恢复 | `browser-host.test.ts` 中调用 `resyncEmulation` 的用例（改名为 `resumeAfterDialog`） | 方法名 | 同进程标签补设设备模拟、`operation.resumeCleanup()` 照常执行 |
| R1-4 动作结算与收尾（**最需要盯**，P1） | `browser-operation.test.ts`、`browser-action-settlement.test.ts` 全部；`browser-host.test.ts:75-97` 的回执预算 | 新增：act 期间有 Fetch 结束并触发采集 | 回执与 `operationSettled` 的时序同未采集时；`OPERATION_DRAIN_MS` 内能收尾；标签不因采集变成 `unknown` |
| R1-5 契约链 | `test_browser_contract_guard.py` 全部；`browser-contract.test.ts`、`browser-deadlines.test.ts` | 网络日志逐行断言（守卫 `:193-197`）、网络日志样例、两条 logs 参数样例补 `body:null` | 方法时限镜像（`:74`，`browser.logs` 仍是 5 秒）；错误码对照（`:80`，方案不新增错误码，必须原样通过） |
| R1-6 后端工具 | `test_browser_tools.py` | `:940` 的期望 dict、`:655` 的 logs 参数加 `body` | `:996`「边界只出现一次」 |
| R1-7 引擎 E2E | `browser-engine.spec.ts` | `WIRE_DEFAULTS`（`:189-197`）的 `browser.logs` 必须加 `body:null`，否则所有 logs 调用都会被宿主 schema 拒成 `INVALID_PARAMS`（导出时每个属性都标为必填） | `:869` 的 console 与失败请求用例 |

### 阶段 2

| 项 | 文件 | 预计要改 | 必须不变 |
|---|---|---|---|
| R2-1 三种截图模式 | 后端 `test_browser_tools.py:771`、守卫 `:220`；引擎 E2E `:615`、`:1076`、`:1116` | `execute(False, None, None, …)` 按位置传参：新参数加在末尾，或测试改为关键字传参 | 视口截图 1280×800 CSS 像素；元素截图放大、跨站 iframe、`REF_STALE` |
| R2-2 冻结帧 | 引擎 E2E `:383`；新建的 `browser-page-capture.test.ts` | 无 | `freeze` 仍走 `devicePixels: true` 与 1 秒时限（`browser-host.ts:299-303`），`scale` 为 1 |
| R2-3 截图不占修改槽位 | `browser-host.test.ts:113` | 无 | 区域截图不把标签标成 `agentBusy`，不拿修改槽位 |
| R2-4 契约 | 导出 schema、`browser-contract.test.ts:146`、引擎 E2E `WIRE_DEFAULTS["browser.screenshot"]`（`:196`） | 补 `region:null` | 前端目录 `tool-catalog.ts:135` 只读 `full_page`，不受影响 |

### 阶段 3

| 项 | 文件 | 预计要改 | 必须不变 |
|---|---|---|---|
| R3-1 click 与双击 | `browser-pure.test.ts:599`、`:85`；`browser-contract.test.ts:163-188`；引擎 E2E `:984` | `toAction` 映射与契约样例的期望加 `repeat` | 双击的五条鼠标事件序列逐字不变；右键、双击带 Shift、拖放、`OUT_OF_VIEWPORT` |
| R3-2 scroll 纵向与 SCROLL_END | `browser-pure.test.ts:722-736`；`browser-action-settlement.test.ts:117-126`；引擎 E2E `:488`、`:521`、`:545`、`:558`；后端 `test_browser_tools.py:550` | 页面函数参数从布尔值改为方向字符串：`arguments: [{value:true}]` 与 `.call(document.documentElement, false)` 两处断言要改；`SCROLL_END` 的 `status_hint` 从「已到底」改为「已到尽头」（方案实现约定 3 明确改变） | 纵向往下、往上的语义；scroll 组的结算；`scrollEnd` 替身按 `is already at the` 匹配，横向文案保留这个前缀 |
| R3-3 key_by_key 与 press | `browser-pure.test.ts:677`、`:693`；引擎 E2E `:1003`；结算矩阵 type 与 press 组（`:90-104`） | 无 | 快捷键修饰位不产生字符；逐键输入；keyup 计数为 3 |
| R3-4 动作数与时限 | `browser-deadlines.test.ts`；`method-deadlines.json` | 无 | `browser.act` 仍为 330 秒；20 次连按加至多 10 个动作仍远小于预算（`browser-host.ts:58、151`） |
| R3-5 直接构造的宿主形状动作 | `browser-native/main.ts` 的 `click`、`browser-engine.spec.ts` 的 `clickOn` 与全部 `call("act")` | `call` 的请求改为宿主请求类型，补上三处 press 的 `repeat` 与 `modifiers` | `repeat` 必填，缺失由类型检查拦截（方案实现约定 1） |

---

## 三、窗口与需要用户配合的用例

| 用例 | 命令 | 窗口情况 |
|---|---|---|
| 桌面引擎 E2E（L、N8、O7 与回归 R1-7、R2-1、R2-2、R3-1、R3-2、R3-3） | `npx playwright test -c playwright.desktop.config.ts browser-engine` | harness 窗口 `show: false`（`browser-harness/main.ts:21`），**不弹窗**，但会启动 Electron 进程 |
| 原生验收（含第四节待实测项） | `node apps/desktop/scripts/test-browser-native.mjs` | `show: false` 且隐藏 Dock 图标（`browser-native/main.ts:59-61`），不弹窗 |
| desktop-smoke | `npm run test:e2e:desktop -- desktop-smoke.spec.ts` | 打开真实主窗口，跑前要征得用户同意；本方案只间接涉及它（`:39` 宿主连上工具链路），**可以不跑** |
| 三个阶段的实跑验收（M、N9、O8） | 无自动化入口：pytest 的 `llm` 标记下没有浏览器用例，浏览器工具又必须有宿主 | 由用户在 `npm run electron:dev` 里手工发起任务 |

能用单测覆盖的部分：
- TabLog 全部规则（A1–A11）；
- 采集触发链（B1–B8，含停住、被修改操作接管、关标签、超时；mock electron debugger，先例 `browser-initialization.test.ts`）；
- 区域截图的截区与放大（N2、N3）；
- 三击、连点、连按的事件序列与第 k 次结算；
- 横向滚动页面函数的参数与派发路径。

只能在真实 Chromium 里验证的：
- 响应体是否真的被采到、mime 与 base64；
- 导航与进程切换、Service Worker、渲染进程缓冲上限；
- 真实的 dblclick 和选区；
- `overflowX` 的真实判定、Canvas 截图像素。

---

## 四、待实测项的最小验证手段

都放进原生验收（`browser-native/main.ts`，经 `scripts/test-browser-native.mjs` 运行），各加一个临时 check 组。原因是它不经过 Playwright，不会像 Playwright 那样替页面打开焦点模拟（`browser-engine.spec.ts:6-11`）。每组直接用 `tab.session.send` 调 CDP，配一个本机 http 服务。

1. **导航后响应体是否保留**（方案的动机）：先 fetch `/api/a`，在 `Network.loadingFinished` 拿到 requestId；分别在同源导航到 `/b` 后、跨站导航（换进程）后调用 `Network.getResponseBody(requestId)`，记录成功与否和错误文字。
2. **单条与总量缓冲上限**（决定 P3 的分界）：以 `maxResourceBufferSize = 256 KB` 启用 Network，分别取 200 KB 和 300 KB 的响应体；再连发 25 个 200 KB 请求，读最早那条，看是否超出 4 MB 总量。
3. **页面停在弹窗时能否采到**（P2）：页面执行 `fetch('/api/save').then(r => r.json()).then(() => alert('保存失败'))`，在 `loadingFinished` 回调里立刻调 getResponseBody，记录它是先于弹窗返回还是被闸门打断；重复 20 次统计成功率。再测一个变体：同进程的另一个标签此时正停在弹窗上。
4. **Service Worker**：注册一个 SW，分别从缓存返回、返回 `new Response(JSON.stringify(...))`；看主会话上有没有 `requestWillBeSent` / `loadingFinished`（及 `response.fromServiceWorker`），getResponseBody 能否拿到。
5. **流式与 SSE**：EventSource 的 `resourceType`，以及是否永远收不到 `loadingFinished`；分块返回、最终会结束的 fetch 在结束后能否取到全文。据此确定「流式无正文」的判定（P4）。
6. **base64 与字符集**：分别返回 `application/json; charset=gbk` 和 UTF-8，看 `base64Encoded` 的取值与解码结果。
7. **焦点模拟下滚轮是否仍会挂起**（方案问题清单 2）：视图不挂在窗口上，分别开、关焦点模拟（新建标签，跳过 `Emulation.setFocusEmulationEnabled`），各发 `Input.dispatchMouseEvent { type: "mouseWheel", deltaY: 400 }`（`timeoutMs: 3000`），记录是否返回、`scrollTop` 是否变化；再挂上窗口（`host.place`）各测一次。Playwright 驱动的引擎 E2E 证明不了这一条。
8. **区域截图越界**：`Page.captureScreenshot` 的 clip 超出可视视口，分别配 `captureBeyondViewport` 的两个取值，看输出，用来定 N3 的规则。

---

## 五、方案问题清单

以下由子 Agent 发现；P1、P2 与 P10 中的两处事实已由主 Agent 抽查确认。

| 编号 | 级别 | 问题 | 证据 | 建议 |
|---|---|---|---|---|
| P1 | 高 | **后台采集会被当前修改操作接管**。标签把会话上的每条 CDP 命令都交给当前操作追踪，act 期间结束的 fetch 发出的 getResponseBody 会进入 `operation.pending`：一是改变输入单元的释放条件 `pending.size === 0`；二是 `drain` 要等它完成；三是慢的时候超过 `OPERATION_DRAIN_MS`，标签会被标成 `unknown`。方案的行为账只写了「多一次 CDP 往返」 | `browser-tab.ts:161-163`；`operation.ts:74-77、104、144-156` | 采集走一条不被操作追踪的发送路径；配 B5、R1-4 |
| P2 | 高 | **「请求结束时采集」在核心场景里有竞态**。页面停住时 CDP 命令立即失败或被打断，而「保存失败」这类页面通常一拿到响应就 alert，采集很可能被打断。方案只把「停在弹窗时失败」算作按需读取的缺点，以为本方案能免疫 | `cdp-session.ts:84-85、99`；`browser-tab.ts:63、101` | 补「不可用，原因：页面停住」这个状态，用第四节第 3 条实测；若失败率高，要重新评估路线 |
| P3 | 中 | **上限口径打架**。渲染进程单条 256 KB 意味着超过的正文 Chromium 根本不保留，取回来只能是「不可用」，于是宿主没有更小的单条上限时，wire 的 `truncated` 永远到不了 true；宿主单条存多少只写了「按上限」；每标签 2 MB 与渲染进程总量 4 MB 的关系也没说 | 方案「行为账」与「新契约」 | 写清哪一层决定截断、哪一层决定不可用 |
| P4 | 中 | **列表行只有四种状态，不够用**。漏了在途 / 采集中、`loadingFailed`、非 Fetch / XHR 类型、采集失败、空正文；「流式」的判定没写（分块 fetch 结束时同样收到 `loadingFinished`，SSE 一直在途）；WebSocket 根本不进列表（只订阅了四个 Network 事件）；二进制的大小用编码后还是解码后字节没定 | `browser-tab.ts:533-546` | 补全状态表，写明流式判定与大小口径 |
| P5 | 中 | **「其余过滤条件必须为空」没法照字面实现**。`limit` 默认 50 且不可为 null，`failed_only` 默认 false，`kind` 必填；seq 不存在、指向 console 条目、条目已被挤掉时返回什么也没定 | `browser_args/logs.py:15、34-44` | 定义 `limit` 必须是默认值还是直接忽略；未找到的情形走 `INVALID_PARAMS` 还是「不可用 + 原因」 |
| P6 | 中 | **body 读取结果的 wire 形状前后不一致**。一处说返回「该条目加响应体」，另一处只列了 `{seq, mime, size, text, truncated, unavailable}`；现有 `logsWire` 带 entries、next、more_before 等游标字段 | `wire.ts:42-57` | 先定形状，再写样例 |
| P7 | 中 | **区域截图的规则缺口与文案漂移**。越界（负值、超出视口、超过 4000 高）怎么处理没写；是否沿用 8px 边距没写。现有 `ref` 的说明写着「e.g. an icon, chart or captcha」，但 ref 只发给可交互元素，与方案「图表截不了特写」的前提矛盾 | `page-capture.ts:9、46-55`；`browser_args/screenshot.py:19-20`；`tool_handler/browser/screenshot.py:36-44`；`snapshot/outline.ts:9-11、83-93` | 补越界规则；阶段 2 一并改掉这处说明 |
| P8 | 中 | **缺省值可能导致静默点零次**。有测试和验收脚本直接构造宿主形状的动作、不经 `toAction`；若实现写成 `for (i < action.repeat)`，缺省时一次都不点也不报错。实施中确认（L-3-1）：`browser-native/main.ts` 经 `host.act` 调用、受类型检查；引擎 E2E 的 `call` 经 `app.evaluate` 序列化、请求类型是 `Record<string, unknown>`，是唯一不受检查的通道 | `browser-engine.spec.ts` 的 `call` 与其中三处 press | 宿主类型里 `repeat` 必填，`call` 的请求改为宿主请求类型（O5、R3-5）；不在 `toAction` 或执行器补默认值 |
| P9 | 低 | **「第 k 次」的语义没定**。指完成了 k 次，还是在第 k 次失败；派发前被闸门拒绝时，因前面已有副作用，结果是 `unknown` 而不是 `failed`，方案没写 | `actions.ts:41` | 定语义并写进回执说明 |
| P10 | 低 | **几处证据与事实不符**：设计稿路径写成了简称；`input/pointer.ts` 的循环行号；阶段 2 说的 page-capture 单测目前不存在；方案说引擎 E2E「会弹出真实窗口」，实际 `show: false` | — | 已在方案文档中更正 |
| P11 | 低 | **横向滚动沿用 `SCROLL_END` 的提示**。`status_hint` 是「已到底」，向左、向右到头时说法不准 | `tool_handler/browser/errors.py:98-101` | 不新增错误码的话，至少改这条提示 |
| P12 | 支线 | **docstring 漂移**（按约定只说明、不改）：`browser_args/actions/keyboard.py:48` 写「宿主时限按动作数估算」，实际宿主方法总时限固定为 330 秒 | `contract/method-deadlines.json`；`deadlines.ts:16-18` | 待用户决定是否修正 |
