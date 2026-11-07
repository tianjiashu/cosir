# 工具调用意图与当前活动：实施及验收

## 实施前确认（2026-11-07）

按已批准方案分三阶段：统一调用意图契约；统一活动位置与组合状态；真正折叠与完整验收。保持未提交 Git。使用隔离测试数据；本轮不删除用户真实数据库，不静默升级旧 Task 的冻结 schema。新契约要求新建 Task，旧开发数据处理另行明确范围，无兼容分支。

根因与终态：工具调用的展示意图归统一调用契约，业务 Args/handler 不接收；原始 function.arguments.intent 为唯一持久事实源，实时和冷重建显式投影为 tool part.intent。前端只有一处组合当前活动及位置，渲染与估高共用布局；工具生命周期、后台工作、模型阶段保持各自事实归属。终端旧 description 展示字段及旧状态/折叠路径同步删除。

## 验收清单

| 编号 | 通过标准 |
| --- | --- |
| I1 | 全部注册工具模型 schema 的根 intent 恰好一次且必填；业务字段、strict、required、$defs/$ref 不变；显式平台 schema 同样覆盖；保留字冲突明确失败 |
| I2 | 统一入口提取 intent 后，原业务校验/coercion/Hook 保持契约；普通及进程 handler、宿主浏览器、远端 MCP 不接收 intent kwargs；执行上下文能取得意图 |
| I3 | 终端前台、立即后台、转后台沿用同一 intent 标签；删除两平台 description、handler 透传、前端读取特例；配置领域 description 和实体标签保留 |
| I4 | 正常、被禁用、Hook 拒绝、非法业务参数、完整参数后取消：实时/刷新/冷重建/历史 intent 一致；不完整 JSON 不猜意图；HITL 重入复用原调用 |
| I5 | 同一组状态仅解释一次；后台 pending 不再显示“准备执行”；用户等待、取消、失败、unknown 及后台收尾各自正确，不从描述推断状态 |
| I6 | 无助手消息、思考→工具→思考→正文→下一工具组，有且仅有一个主活动入口；入口在当前阅读位置、用量之前；模型和后台并行不互相覆盖，历史组不冒充当前阶段 |
| I7 | 普通工具组关闭时真正隐藏活动调用及详情；用户显式开合不被状态切换覆盖；审批/交接可操作，停止/转后台可发现；只读面不引入写操作 |
| I8 | 正文、工具详情及终端 Reader 身份稳定，关闭后焦点有效；估高同步；纯回复无标题、历史分页锚点、顶部锚定不自动追底回归通过 |
| I9 | 活动文字复用扫光，终态/用户等待不扫光，reduced-motion 生效；浅深色、窄栏长意图可读；普通正文追加不导致全历史工具扫描或全部组重绘 |
| I10 | 相关 Python/TypeScript 检查及回归通过；本机真实 Electron + app:// 构建 + Python/SQLite 完整链路测试，保存截图；子 Agent 按 I1–I9 独立复核和执行测试 |

## 分阶段与边界

1. 后端统一 schema、入口拆分、意图发布/重建；终端迁移及前端契约/标题同步。不留下 description/intent 双轨。
2. 统一调用状态、线程活动选择和 TurnLayout 落点，删除输入框前的独立状态路径；首输出保留线程级落点。
3. 同步组/调用详情折叠、操作和焦点规则、估高；浏览器与 Electron 验收及独立复核。

刻意不做：Hook 改写参数审计持久化、工具调度重写、背景任务生命周期重写、新的滚动基础设施。已知 Hook 后业务参数实时/历史差异另列问题；intent 始终表示原始模型声明，不是系统验证后的操作承诺。

## 实施及验收记录

三阶段已完成，未提交 Git，未删除用户数据。

### 代码加减法

- 统一契约：新增 `core/tool/call_intent.py`，只在模型 schema 出口加根字段，在执行入口拆分；复用现有业务校验、Hook、执行上下文及原始模型响应持久化。新增独立意图事件，实时和冷重建共用解析规则，没有新增意图表或第二份持久事实源。
- 删除终端两平台 Args 的调用 `description`、执行方法透传及前端专有读取；删除未消费的校验结果 `model` 字段。实体与配置的 description 保留。
- 统一展示：`call-status.ts` 组合调用、用户等待及后台状态；`current-activity.ts` 只读当前消息与后台登记，`TurnLayout` 同时服务渲染和估高。删除后台状态重复标签、输入框上方重复活动入口和“未结束就强制显示调用行”的路径。首个助手消息出现前的反馈仍保留。
- 工具区间使用稳定位置键，普通工具组及时间线终端详情默认折叠；显式选择、过程内焦点保护、审批、Reader、只读限制继续生效。用户开启后的组不会因完成而丢失开合偏好。
- 回归夹具同步新契约；没有保留 description/intent 双读，也没有增加状态持久化或新滚动设施。

### 自动化与真机结果

| 检查 | 结果与范围 |
| --- | --- |
| 前端全部单元测试 | `npm run test:unit`：130 个文件、900 项通过 |
| 最终后端相关批次 | 17 个文件，330 项通过、6 项平台条件跳过；覆盖 intent、model_node 异常/取消、事件契约、executor、registry、终端及 snapshot |
| 后端独立验收 | 子 Agent 相关批次 437 项通过、6 项跳过；另 14 项已有夹具失败见下表，不计为通过 |
| 新边界静态检查 | `call_intent.py` strict mypy 通过；新增 Python 文件 Ruff 与前端相关文件 ESLint 通过；TypeScript 检查及最终 `npm run build` 通过 |
| 核心浏览器批次 | `tool-intent-activity`、`timeline-disclosure`、`hidden-tool-details`、`turn-heading` 共 31 项通过 |
| 扩展浏览器批次 | `terminal-output`、`conversation-presentation`、`background-tasks`、`thread-scroll-stability` 共 20 项通过；`turn-render-isolation` 修正测试前提后由子 Agent 独立复跑 1 项通过 |
| 补充独立验收 | 新活动浏览器 3 项、历史分页 3 项、相关单测 51 项通过（与其他批次有重叠，不累加计数） |
| 真实 Electron | 主 Agent 最终构建后 I10 通过（15.3 秒）；子 Agent 独立执行 I10 通过（12.6 秒），并检查浅色、深色、完成态截图 |

I10 使用真实 Electron 宿主、`app://` 构建产物、真实 Python/SQLite、真实文件读取和后台终端进程。仅模型端由受控本机 OpenAI 协议服务驱动；不声称验证了真实远端模型的措辞或服务兼容性。验证了模型导出 schema、调用意图展示、后台与模型并行、折叠/停止、后台收尾以及宿主重启后从 SQLite 冷重建意图一致。隔离测试数据与真实用户数据分开。

截图保存在 `target/acceptance/tool-intent-activity/`：

- [浅色运行态](../../target/acceptance/tool-intent-activity/intent-native-light.png)（组与详情由测试显式展开）
- [深色运行态](../../target/acceptance/tool-intent-activity/intent-native-dark.png)
- [完成态](../../target/acceptance/tool-intent-activity/intent-native-completed.png)

### 独立复核与未纳入事项

`activity_state_facts`、`intent_persistence` 复核 I1–I4；`tool_activity_catalog` 复核 I5–I9；`heading_acceptance` 执行 I6–I10 的独立浏览器、单测、类型检查及真机验收。结论：当前无本轮生产代码阻塞项。

| 发现项 | 证据、归属与处理 |
| --- | --- |
| 扩展后端测试 2 项后台投影夹具失败 | `tests/test_background_task_projection.py:144,258` 未装配 registry；工作前基线已存在 rebuilder 读取 registry 的改动。判为已有测试依赖缺口（高置信），本轮记录、不夹带修复 |
| 扩展后端测试 12 项模型取消夹具失败 | `tests/test_model_request_cancellation.py` 的 `save_model_response` 替身返回 None，而工作前基线已消费 `response.sequence`。判为已有夹具契约未同步（高置信），本轮记录；本轮新意图取消测试使用实际 model_node 路径并已通过 |
| 渲染隔离测试的临时失败 | 一次是测试期间 Vite 整页刷新清空内存；另一次是点击组后焦点留在过程内，既有焦点保护有意阻止自动折叠。固定测试环境、完成前移焦点到输入框后独立通过，未改变生产焦点行为 |
| 构建提示 | 既有 CSS `::highlight` 及大 chunk 提示仍在，构建成功；本轮不扩大到打包拆分 |
| 平台覆盖 | 本机 macOS 已验收；6 项平台条件跳过，未声称完成 Windows 真机验收 |
| 冻结 schema | 新契约用新建 Task；不迁移旧 Task，不增加兼容分支，不删除真实旧数据 |

已知 Hook 改写业务参数的实时/历史差异仍按原定边界另记；本轮只保证原始调用意图的一致性。没有新增未验证的生产路径。
