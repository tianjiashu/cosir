# 阶段二执行计划：交付记录与卡片

> 状态：开工前定稿（2026-11-16），分支 `feat/workspace-files-cache-and-watch`。设计依据是 `design.md` §3、§4.1、§4.3、§4.6、§4.8、§11（已按第四轮核对修正，见 §12）；本文件只写怎么做、怎么验，不重复设计。
> 前置已完成：图片按版本长期缓存、监听按工作区注册、路径身份（`file_identity`）。

## 1. 改造方案（第零铁律模板）

```text
【任务类型】新增能力 + 功能改造
  新增：交付记录、回合聚合卡、snapshot 产出清单、工作区级文件监听。
  改造：present_files 改名重写为 present_deliverables，卡片从读 display_data 改为读 part 上的记录。

〔新增能力〕
  完成标准：Agent 交付文件、目录或网址后，回复下方出现一组胶囊（工作区内图片是方块），点开就到对的地方；
            文件改了或删了，卡片跟着变；同一回合交付两次同一文件只出现一次。
  最不想看到：卡片出现又消失、点开是空白、整组卡片因为一项失败而不出现、实时和刷新后显示不一样。
  归属：
    - 交付记录是工具结果行 metadata 上的强类型事实（ToolMessageMetadata.deliverables），后端拥有；
    - part 上的 deliverables 与 snapshot 顶层 deliverables 都是它的投影；
    - 排版、聚合、去重只在前端 deliverable-layout 一处；
    - 「文件变了」的信号归工作区级监听 store，面板与会话订阅界面只是登记方。
  容纳性：
    - 记录沿用 background 已有的透传路径（观察 → 事件 → 结算 → part），不新增通道；
    - 聚合卡沿用改动卡的身份复用写法；
    - 打开沿用 use-open-link，只加一个「从记录得出目标」的入口；
    - 现读沿用 fileInfoQueryOptions 与资源地址。
  新契约：
    - ToolMessageMetadata.deliverables / ToolObservation.deliverables / ToolCallStatusChangedEvent.deliverables；
    - 工具 part 新字段 deliverables（驼峰 wire 形状）；snapshot 顶层 deliverables；
    - 资源接口 edge 参数（铺满 240×192）；StripImage.previewSrc、ImageTile / StripImage.onError；
    - 工具名 present_deliverables，参数 items[].target / caption。

【链路复用】
  直接复用：
    - BackgroundWork 的透传路径（tool_observation_projection.py:49-57、tool_result.py:40-42）；
    - 改动卡的身份复用（turn-layout.ts:160-165，抽成共用小函数）；
    - use-open-link 与 resolve-link；fileInfoQueryOptions；useFileResourceSrc / useImageLoadFailure；
    - RootDirectory / canonical_path（交付时观察）；inspect_file（文件类型）；resolve_read_scope（目录）；
    - 夹具的 deliver_change（合成事件）；视觉回归设施与 WorkspaceFilesBackend。
  改造后复用：
    - useFileWatch 从面板自持改为由 AppLayout 按工作区挂载，面板与会话只登记路径；对面板用户行为不变。
    - image_preview 增加「铺满框」缩放；不带 edge 的请求行为不变。

【问题清单】
  - 实时终态早于结果行落库：驱动失败补写的取消被已完成守卫丢弃，实时快照仍显示卡片、冷重建才消失。
    所有工具共有（display_data 也一样），本次不处理，见刻意不做。置信度高。
  - 交付工作区外受保护目录（桌面、文稿）里的文件时，macOS 授权框在工具调用那一刻弹出，
    工具时限 10 秒，用户来不及作答时这一项会以失败列出。未实测，置信度中；人工验收 M3 确认。
  - 产物区估高给每张卡加 16，实际间距 8（artifact-zone.tsx:47），多张卡时估高偏大。现有问题，
    本次聚合后交付物只占一张卡，顺手统一为同一常量。

【选定路线】
  记录作为持久化事实 + 「有才写」+ 不另存 key；三步提交，每步结束都能用。
  否决：
    - 清除语义（事件不带记录就清掉）：快照增量没有删除，要么每个 part 都写 null、要么新增删除增量，
      为一个所有工具共有的时序不一致付出全局改动，不值；
    - 把 system_preview、media 提前到阶段二：后缀表与消费方都在阶段三、四，提前就是预留；
    - 提示词工具名守卫：改为提示词里一律用 TOOL_* 常量拼接，改名不可能漏，不需要守卫。

【改动账本】（按步，交付后以 git diff --stat 对账）
  二a：
    精简：display/deliverables_display.py（−13）；present_files 工具与参数（−108，由新实现取代）；
          reply-presentation-display.ts 的 readDeliverables 与 DeliverableFile（−14）；deliverables-card.tsx（−70，重写）；
          turn-layout / artifact-zone / message-height-estimate 里按 display_data.kind 的交付物分支（约 −20）。约 −225。
    新增：后端值对象与 FileKind（+60）、透传各跳（+20）、工具与参数（+160）、part wire 形状与校验（+30）、
          提示词常量（+5）；前端契约与校验（+30）、deliverable-layout（+90）、聚合卡与胶囊组件（+200）、
          打开器入口（+30）、估高（+15）。约 +640。测试约 +350，夹具约 +15。
  二b：
    新增：image_preview 铺满框（+25）、资源接口 edge（+10）；StripImage / ImageTile 的 previewSrc 与 onError（+20）；
          方块与失败状态（+60）。约 +115。测试约 +80。
  二c：
    精简：resource-watch.tsx（−36）；面板自持的 useFileWatch 拼装（约 −10）。约 −46。
    新增：snapshot 顶层清单投影与校验（后端 +70、前端 +30）；file-watch-store 与 AppLayout 接入（+80）；
          卡片现读状态（+50）；read-problem 的「无法访问」类别（+10）；打开器隐藏目录提示（+20）。约 +260。
          测试约 +200，夹具约 +25（9 个内联 snapshot 的 spec 与 3 个夹具文件）。
  合计：应用约 +1015 / −270，测试与夹具约 +670。
  目录 or 文件命名变更：
    - 新增 apps/backend/app/models/file_kind.py：类型词表放在 models 层，FileInfo 与 Deliverable 共用，
      避免 models 依赖 service。正向：按「文件类型」能落到 models；反向：读得出是持久化契约里的文件类型。
    - 改名 tools/tool_handler/present_files.py → present_deliverables.py、tool_models/present_files_args.py →
      present_deliverables_args.py：与工具名一致。
    - 新增 assistant_transport/state/conversation_state_deliverable.py：照 conversation_state_background_task.py。
    - 新增 lib/assistant/turn/deliverable-layout.ts：聚合、去重、排版、行数，回合布局与估高共用。
    - 新增 components/conversation/tools/reply-presentation/deliverable-capsule.tsx、deliverable-tile-row.tsx，
      重写 deliverables-card.tsx 为聚合卡；新增 components/ui/middle-truncate.tsx（仓里没有中间截断组件）。
    - 新增 lib/workspace-files/file-watch-store.ts；删除 lib/workspace-files/resource-watch.tsx。
  同步：契约文档（工具表与 kind 表）、AGENTS（kind 清单、snapshot 顶层字段、文件监听）、测试计划对应编号。

【刻意不做】
  - 实时终态早于结果行落库的不一致：根因在批次执行器先发事件、observe 后写行，影响全部工具，单独改造。
  - 工具描述之间互相引用工具名的字面量（如 offer_next_step 描述里的 ask_user）：同类问题，改提示词常量时不扩到描述。
  - 媒体网址交系统浏览器、音频条、系统缩略图：依赖阶段三、四的后缀表与宿主方法。
  - 侧栏「产出」卡：阶段五。
```

## 2. 测试计划

每一步先写会失败的测试（在上一步提交的代码上跑应失败），再实现。E2E 只跑无头，槽位用非 0（如 `COSIR_E2E_SLOT=7`）。

### 二a 交付记录与胶囊卡

| 编号 | 验证什么 | 层级与文件 |
|---|---|---|
| A1 | 工具三种条目：工作区内文件（`workspace_path`、规范 `absolute_path`、`kind`、`size`）、工作区内目录、网址；工作区外文件以真实路径所在目录为根观察；交付的是链接时记录目标 | PY `test_present_deliverables.py`（由 `test_reply_presentation_tools.py` 改写） |
| A2 | 部分失败：不存在、受保护路径、`/proc` 类伪文件树（目录走 `resolve_read_scope`）、观察时 `PermissionError` 与 `UnsupportedFileSystemError` 各自进失败名单，其余照常；全部失败时短提示是中性说法 | 同上 |
| A3 | `key`：同一文件的大小写变体与链接写法得到同一 `key`；网址 `http://h:80` 与 `http://h/`、去片段、协议主机小写等价；硬链接两个名字 `key` 不同 | 同上 + `test_file_identity.py` 不动 |
| A4 | 透传：观察 → 事件 → 结算 → metadata → part；`update_tool_result_metadata` 等逐字段构造处不丢字段；fork 深拷贝后记录仍在 | PY `test_present_deliverables.py`、`test_runtime_message_context_manager*` 补一条 |
| A5 | 实时与冷重建一致：一次交付后实时快照与冷重建的 part `deliverables` 相同 | PY `test_live_cold_parity.py` 加用例，`live_cold_support.py` 的归一化加入 `deliverables` |
| A6 | part 字段写入侧校验：坏条目使物化失败 | PY `test_state_snapshot_write_side_validation.py` |
| A7 | 改名：工具注册、Agent 定义、Session 工具范围、契约文档守卫、前端目录守卫全部指向新名；提示词里工具名来自常量（断言渲染结果含新名、不含旧名） | PY `test_session_tool_scope.py`、`test_tool_registry_display_guard.py`、`test_system_prompt_builder.py` |
| A8 | 前端校验：part 允许 `deliverables` 并逐项校验，拒绝未知字段与错类型 | VT `snapshot-validation.test.ts` |
| A9 | 排版与聚合：同一回合多次调用合成一张卡、位置在第一次带记录的调用、同一 `key` 留最后一次、工作区内图片是方块、工作区外图片与其余是胶囊、失败调用（无记录）不占位 | VT 新 `deliverable-layout.test.ts`、改写 `turn-layout.test.ts` 的回复呈现组 |
| A10 | 估高：按方块行与胶囊行计算；状态变化（交付时大小 → 现读）不改变估高 | VT `message-height.test.ts` |
| A11 | 打开器：从记录得出的目标——工作区内文件进面板、目录进文件树、工作区外 `.html` 进内置浏览器、工作区外其他条目「在文件夹中显示」、公网网址确认后交系统浏览器 | VT `open-link.test.tsx` |
| A12 | E2E：点胶囊进面板、进浏览器、进文件树；次按钮调用 `files.reveal`；悬停提示条含说明与路径；无宿主时没有次按钮 | PW `reply-presentation.spec.ts` 整体重写 |
| A13 | 回归：内联 snapshot 不受影响（二a 只加 part 字段，不加顶层字段），全部 E2E 绿 | PW 全量 |

### 二b 图片方块

| 编号 | 验证什么 | 层级与文件 |
|---|---|---|
| B1 | `edge` 铺满框：1000×4000 的长图缩到宽 240（铺满）、300×200 不放大、GIF 与动态 WebP 在预算内原样返回；带 `edge` 的响应同样长期缓存 | PY `test_workspace_files.py` |
| B2 | `previewSrc`：方块用小图、灯箱用原图；不传时三处现有调用方行为不变 | VT `image-strip` 相关单测、`image-read-view.test.ts` |
| B3 | 失败：`onError` 触发重读文件信息，版本变了换新地址；版本没变显示失败图标，方块尺寸不变 | VT + PW 集成（夹具 `image-update`） |
| B4 | 宽高为空的图不请求资源，直接显示无法预览的方块 | PW 集成（夹具加一张损坏 PNG） |

### 二c 现读与监听

| 编号 | 验证什么 | 层级与文件 |
|---|---|---|
| C1 | 清单投影：按 `key` 留最后一次、新的在前；继承行计入；撤回后消失；事件带记录时重算；历史窗口只带最近一段时清单仍含窗口外条目 | PY 新 `test_deliverable_projection.py`、`test_snapshot_window.py` |
| C2 | 顶层字段写入侧校验、HTTP 契约、前端严格校验 | PY `test_state_snapshot_write_side_validation.py`、`test_snapshot_http_contract.py`；VT `snapshot-validation.test.ts` |
| C3 | 夹具一次改完：后端 7 处整 dict 断言，前端 9 个内联 snapshot 的 spec、两份 `transport-fixtures.ts`、`test-server.mjs`、`history-window.spec.ts` 的键列表 | PY / PW 全量 |
| C4 | store：按工作区引用计数；登记清零即关闭订阅；面板与会话登记同一工作区时合并成一条连接 | VT 新 `file-watch-store.test.ts` |
| C5 | E2E 集成：有工作区内交付物时订阅存在（面板关着也在）；离开会话订阅归零；合成事件后卡片大小更新；文件删除后胶囊变「已不存在」 | PW `workspace-files-integration.spec.ts` |
| C6 | 现读状态：403 →「无法访问」（read-problem 新类别）、500 →「—」、工作区不存在 →「—」；目录大小一格恒为「文件夹」 | VT + PW（`page.route` 伪造） |
| C7 | 打开器隐藏目录：被浏览可见性隐藏的目录点击后提示并给「在文件夹中显示」；对话里的目录链接同样 | VT `open-link.test.tsx` |
| C8 | 非集成 E2E 补桩：快照里有工作区内交付物的用例都用共用的 `page.route` 桩应答 `/files/info` 与 `/files/watch`，不出现无限重连告警 | PW 共用 helper（`tests/e2e/workspace-files-stubs.ts`） |
| C9 | 现有用例「收起面板后订阅数为 0」不变 | PW 集成 |

### 每步提交前的固定检查

- 后端全量 pytest、前端全量 Vitest、Playwright 全量（无头）、视觉回归；
- 改动文件的 ruff / ruff format / mypy、tsc / eslint，只修本次引入的问题；
- 本步新增的回归用例在上一步提交的代码上跑一遍，确认会失败。

## 3. 视觉验收计划

### 3.1 自动截图基线

新建 `apps/desktop/tests/e2e/deliverables.visual.spec.ts`，沿用 `WorkspaceFilesBackend` 与 `seedState` 注入快照，基线只在 macOS 录，规则见 `playwright.visual.config.ts`。每项录深色与浅色。夹具工作区补：长文件名的 csv、一张 svg、一个目录、一个指向工作区外的链接（用来得到 403）、一张损坏的 png。

| 编号 | 界面 × 状态 | 步 |
|---|---|---|
| V1 | 常见组合：md、长名 csv（中间截断，扩展名可见）、目录、本机网址 | 二a |
| V2 | 悬停一枚胶囊：底色加深、次按钮叠在大小一格上、宽度不变、提示条两行（与 V3 合并为一张图，减少不稳定） | 二a |
| V3 | 键盘焦点在主按钮、在次按钮 | 二a |
| V4 | 工作区外的条目：无次按钮、大小是交付时的值 | 二a |
| V5 | 回合顺序：正文 → 交付物 → 下一步建议 | 二a |
| V6 | 无宿主的开发模式：没有次按钮 | 二a |
| V7 | 窄列宽（约 640px）换行 | 二a |
| V8 | 图片方块：已加载、SVG、加载中占位（路由延迟）、加载失败图标 | 二b |
| V9 | 方块与胶囊同组：方块一行在上、胶囊在下 | 二b |
| V10 | 现读状态：已不存在、无法访问、「—」 | 二c |

合计约 10 个用例、20 张图。

### 3.2 人工验收（打包或开发期真实应用）

| 编号 | 看什么 | 通过标准 |
|---|---|---|
| M1 | 用真实模型完成一个产出文件的任务（例如「整理成一份 Markdown 报告」） | 模型调用 `present_deliverables`；不交付草稿、日志等过程文件；回复不重复文件内容 |
| M2 | 让模型交付一个本机预览地址与一个目录 | 网址胶囊进内置浏览器；目录胶囊在文件树里展开 |
| M3 | 交付桌面或文稿目录里的工作区外文件 | 记录授权框出现的时机；10 秒内不作答时该项以失败列出，其余项照常出现 |
| M4 | 交付后在编辑器里改文件、删除文件，文件面板关着 | 卡片上的大小与「已不存在」在 15 秒内跟上（原生事件到达时更快） |
| M5 | 同一回合让模型交付同一文件两次、不同写法（大小写变体） | 只出现一枚胶囊 |
| M6 | 对照 `ui-mockup.html` 与 §11 逐项看深浅色、悬停、焦点、截断 | 与规格一致；不一致处记下是规格问题还是实现问题 |
| M7 | 长对话里上下滚动经过交付物卡片 | 没有明显跳动；图片方块滚回来不重新加载（打包应用里看网络面板） |

M1、M2 是这次要补上的「真实模型行为」证据，此前工具只有单测与无头 E2E。
