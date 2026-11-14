# 内置浏览器能力缺口：实施循环问题账本

> 对应方案：[browser-capability-gaps-plan.md](browser-capability-gaps-plan.md)；测试计划：[browser-capability-gaps-test-plan.md](browser-capability-gaps-test-plan.md)。
>
> 规则：编号形如 L-阶段-序号，首次出现时分配，之后不变。同一条用例、同一断言，或同一 `文件:行` 的同一种失败算同一问题。出现次数达到 2（修过又复现）即停下报告。

## 问题

| 编号 | 现象 | 首次出现 | 出现次数 | 级别 | 状态 |
|---|---|---|---|---|---|
| L-3-1 | 引擎 E2E「pages long snapshots…」中直接构造的宿主形状 `press`（不带 `repeat`）一次都没按，步骤却结算为 done（`browser-engine.spec.ts:561`，`:566` 期望 title「shortcut」实为「Rich」） | 阶段 3 第 1 轮 | 1 | 高 | 已修复（第 1 轮） |

## 改动断言的依据

| 断言 | 依据 |
|---|---|
| `browser-tab-log.test.ts` 原有 3 处 `toEqual` 整条比较补 `body` 字段；`requestFinished` 调用补第三个参数 | 方案新契约：网络条目带响应体状态；请求结束时要传 `encodedDataLength`（测试计划 R1-1 预计要改） |
| `browser-contract.test.ts` 网络日志夹具补 `body`；两条 logs 参数样例补 `body: null` | 方案新契约：`browser_logs` 新增 `body` 参数，网络条目带响应体状态（R1-5） |
| `test_browser_contract_guard.py` 网络日志逐行断言加「· body: …」 | 方案新契约「列表行显示」（R1-5） |
| `test_browser_tools.py` 的 logs 期望 dict 补 `body: None` | 导出 schema 每个字段必填，handler 必须原样转发 `body`（R1-6） |
| `browser-engine.spec.ts` 的 `WIRE_DEFAULTS["browser.logs"]` 补 `body: null` | 同上，否则宿主按 schema 拒绝（R1-7） |
| `test_browser_tools.py` 元素截图正文从「screenshot of element e7 (the image follows).」改为加上「its pixels are not viewport coordinates」 | 方案改动账本「截图正文：『视口 / 元素』两句合并为按截区说明」；视口截图正文不变（N7） |
| `test_browser_tools.py` 截图用例由按位置传参改为关键字传参；契约样例与 `WIRE_DEFAULTS["browser.screenshot"]` 补 `region: null` | 方案新契约：`browser_screenshot` 新增 `region`，位于 `tab` 之前（R2-1、R2-4） |
| `browser-pure.test.ts` 滚动页面函数参数从 `[{ value: true }]` / `false` 改为 `"down"` / `"up"` | 方案「改造后复用」：`SCROLL_AREA` 参数由「是否向下」改为方向，纵向语义不变（R3-2） |
| `browser-pure.test.ts`、`browser-contract.test.ts` 的 `toAction` 期望补 `repeat`；契约 act 样例 click 改为 `click_count: 3, repeat: 1`、press 加 `repeat: 5`、scroll 改为 `left` | 方案新契约：click / press 新增必填 `repeat`，`click_count` 可取 3，scroll 可取左右；样例要覆盖新取值（R3-1） |
| 单测替身、`browser-native/main.ts` 的 `click`、E2E 的 `clickOn` 与 `ACTION_DEFAULTS` 补 `repeat: 1`（E2E 另补 press、scroll 的缺省字段） | 方案实现约定 1：宿主类型里 `repeat` 必填，替身缺了它由 `tsc` 报错；E2E 的 `clickOn` 不经类型检查，缺了会静默点零次，一并补上 |

## 刻意不做（低级问题）

| 编号 | 现象 | 理由 |
|---|---|---|
| —（阶段 2 N8） | 手机模拟下的区域截图没有单独用例 | 与视口截图共用同一套几何，手机模拟下的视口截图与坐标点击已有 E2E |
| —（阶段 3 O6） | 「嵌套时同一个 ref 往下滚的是纵向可滚的祖先」没有单独用例 | 纵向分支逻辑未改，原有纵向 E2E 全部通过；横向分支由宽表格 E2E 覆盖 |
| —（阶段 3 O4） | 「repeat 中途未知结果之后，同批 dialog 可不带 id 作答」没有单独断言 | 沿用原有规则（`input/actions.ts:44-46`），原有单测覆盖 |

## 范围外

| 来源 | 现象 | 证据 |
|---|---|---|
| 主 Agent，阶段 1a 交付命令 | `npm run lint` 报 97 个错误，全部在 `apps/desktop/temp/` 下的旧探针文件与 `tests/unit/terminal-query-replies.test.ts`；本次改动的文件单独跑 eslint 无错误 | `npx eslint <本次文件>` 无输出 |
| 主 Agent，收尾全量运行 | `tests/test_local_backend_process_cleanup.py::test_timeout_stops_the_command_gently_first` 偶发失败，连跑 3 次 1 次失败；文件未改动，属进程超时类时序测试 | 重跑通过 |
| 测试子 Agent，阶段 3 第 1 轮 | `tests/test_terminal_shell_integration.py::test_match_reports_running_command[/bin/bash]` 全量运行时失败、单独运行通过（依赖执行顺序）；本会话开始前已出现过，未改动终端相关文件 | 单独运行 3 passed |
| 主 Agent，阶段 3 交付命令 | 后端 `ruff check app/tools` 报 `app/tools/mcp/connection_pool.py:61` E501，该文件未改动，HEAD 已有 | `git status` 无改动 |
| 测试子 Agent，阶段 1a 第 1 轮 | `contract/tool-args.schema.json` 在会话开始前已有未提交改动（x 坐标、viewport 说明），来自更早的浏览器提示词修订，与本方案无关 | `git diff --stat` 4 行增删，全为 description |

## 轮次记录

### 阶段 1a

- **第 1 轮**：测试子 Agent 运行全部命令，无失败（后端浏览器 96、宿主浏览器单测 214、全量单测 1008、引擎 E2E 46、后端全量 2850、原生验收 11）。按停止判定 1，本阶段通过。
  - 报告的「未能运行」不是失败，按来源处理：
    - 没有对应测试的 B2（XHR）、B6（关标签后迟到失败）、B7（采集超时）、B10（补采时失败）：主 Agent 补了 4 个单测，`browser-tab-network.test.ts` 共 11 个，全部通过；
    - 依赖真实 Chromium 的待实测项 1、3（导航后正文是否保留、「拿到响应就 alert」能否补采）：由阶段 1b 的引擎 E2E（用例 L）覆盖；
    - 待实测项 2（渲染进程缓冲上限）、6（非 UTF-8 字符集）：没有自动化入口，列入最终报告由用户决定是否实测。
  - 实现时的两处细化，已同步测试计划：B6 不要求「不写已关闭标签的日志」，写回已脱离的日志对象没有副作用，不另设防；非 Fetch / XHR 条目的正文读取返回 `unavailable` 并说明原因，与列表行的 `body: null` 对应。

### 阶段 1b

- **实施中的实测**：「拿到响应就 alert」的引擎 E2E 临时收紧为「弹窗时状态必须是 deferred」连跑 3 次，3 次都是 deferred。真实 Chromium 里采集总是抢不过弹窗，答完后补采这条路径确实生效，P2 的修法成立。断言随后改回「text 或 deferred」：时序竞态不写死成断言。

- **第 1 轮**：测试子 Agent 运行全部命令，无失败（后端浏览器 109、宿主浏览器单测 221、全量单测 1015、引擎 E2E 48、原生验收 11、后端全量 2863）。按停止判定 1，本阶段通过。
  - 报告的「未能运行（没有对应测试）」由主 Agent 补齐：
    - H 二进制条目渲染：`test_browser_tools.py` 新增 1 个用例；
    - I 长正文走通用输出预算：`test_browser_tools.py` 新增 1 个用例，用临时工作区，证明正文截断后全文落在 `.cosir/tool-artifacts/` 且可读回；
    - L 补上约 300 KB 正文与 SSE：真实 Chromium 中 300 KB 正文被渲染进程保留、宿主截断到 256 KB 以内，EventSource 标为 `streaming`。待实测项 2 的「单条上限」一半由此确认，总量上限（8 MB）仍未实测。
  - M 实跑验收需用户配合，列入最终报告。

### 阶段 2

- **实施中**：新增区域截图的引擎 E2E。真实 Chromium 中，滚动后按视口坐标截到的正是画布对应位置的颜色；50 像素高的区域放大 3.2 倍；越界报 `OUT_OF_VIEWPORT`。

- **第 1 轮**：测试子 Agent 运行全部命令，无失败（后端浏览器 118、宿主浏览器单测 228、全量单测 1022、引擎 E2E 49、原生验收 11、后端全量 2872）。按停止判定 1，本阶段通过。
  - 报告中的缺口与处理：
    - N1 有限值：后端参数模型接受 `width=inf`。信任边界在后端参数模型，主 Agent 给四个字段加 `allow_inf_nan=False`，并补了 inf / nan 两个拒绝用例。最大高度不另设上限：宿主按可视视口拒绝越界区域，视口本身就是上限。
    - N5 区域截图遇弹窗：在引擎 E2E 的弹窗用例里补一行，区域截图同样报 `DIALOG_OPEN`，已通过。
    - N8 手机模拟下的区域截图：没有覆盖。区域截图与视口截图共用同一套几何（`page-geometry.ts`），手机模拟下的视口截图与坐标点击已有 E2E，风险低，未补用例。
    - 第四节待实测项 8（clip 越界配 `captureBeyondViewport`）：实现改为截图前就拒绝越界区域，不再把越界 clip 交给 Chromium，这条实测不再需要。
    - N9 实跑验收需用户配合，列入最终报告。

### 阶段 3

- **实施中**：
  - 「第 1 次就没做成时不附次数」最初实现为附「stopped after 0 of N」，与测试计划 O4 不符，按计划改为不附；
  - 引擎 E2E 在真实 Chromium 中确认：横向滚动、到左边缘报 `SCROLL_END`、整页锁定横向时报「cannot scroll sideways」；`repeat: 2` 触发 2 次 click、0 次 dblclick；`click_count: 2` 触发 1 次 dblclick；提交后禁用的按钮连点 3 次只下 1 单；三击选中整段；连按 3 次收到 3 个 keyup。
- **第 1 轮**：引擎 E2E 50 通过 / 1 失败（L-3-1），后端全量 1 失败（终端集成测试依赖执行顺序，记入「范围外」），其余命令全部通过。
  - 复查子 Agent 的结论：根因在契约层，是类型覆盖的缺口，不在执行器。宿主动作形状只有一份定义，`repeat` 必填；线上入口有 ajv 守着；但 E2E 的 `call` 用 `Record<string, unknown>` 擦掉了类型，HEAD 里就有的三处 press（`:332`、`:561`、`:666`）没跟着补 `repeat`，其中 `:561` 静默按了零次。主 Agent 抽查确认：`call` 的签名属实，`:561` 那处 press 在 HEAD 中已存在。
  - 修法：选结构化改造，`call` 的请求改为宿主方法的请求类型，用例通常不关心的 `view`、`newTab` 由 `call` 像 `context` 一样补上。暴露出 5 处不合宿主类型的写法，全部改正。否决了两条：只补 `:561` 的补丁（无类型通道仍在）、执行器运行期拒绝非法次数（宿主内部形状的信任边界在 ajv，执行器再校验属于重复校验，方案实现约定 1 也明确不兜底）。
  - 验证：在一处 `call("act")` 里删掉 `repeat`，`tsc` 报错，恢复后干净；引擎 E2E 51 全部通过。
  - 已同步方案（实现约定 1）与测试计划（O5、R3-5、P8）。
- **第 2 轮**：测试子 Agent 运行全部命令，无失败（后端浏览器 132、宿主浏览器单测 235、全量单测 1029、引擎 E2E 51、原生验收 11、后端全量 2886）。O5 在副本上验证：删掉 `call("act")`、`clickOn`、`browser-native` 中任一处 `repeat`，类型检查都报错。按停止判定 1，本阶段通过。
  - 主 Agent 补齐报告中的两处缺口：宿主契约拒绝 `repeat` 越界、`click_count: 4`、缺 `repeat`、非法方向（`browser-pure.test.ts`）；断言 `SCROLL_END` 提示为「已到尽头」（`test_browser_tools.py`）。
  - 收尾全量运行：单测 1029 通过；后端全量三次中两次全部通过，一次有 1 个进程超时类测试偶发失败（记入「范围外」）。

