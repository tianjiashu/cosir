---
target: Workbench 前端交互与样式
total_score: 24
max_score: 40
na_heuristics:
p0_count: 0
p1_count: 2
target_identity: "file:/Users/tianjiashu/Documents/tianjiashu/cosir/apps/desktop/components/workbench.tsx"
target_fingerprint: "sha256:0c9c585aca9be0e6b6f6f8cdc3b6cc50dc7e19395d8768ca4185bb97803423c1"
target_path: /Users/tianjiashu/Documents/tianjiashu/cosir/apps/desktop/components/workbench.tsx
timestamp: 2026-10-17T07-17-49Z
slug: apps-desktop-components-workbench-tsx
---
Method: dual-agent (A: /root/design_review · B: /root/detector_review)

# Workbench 交互与样式评审

2026-10-17。只读评审，未修改产品代码。范围：Workbench 容器、文件阅读、主布局，以及子 Agent / 配置草稿的源码路径。

## 判断

Workbench 值得优先优化。已有能力和视觉基调适合本地编程工具；主要问题集中在空间分配、草稿生命周期和阅读区信息层级。建议局部结构化收敛，保留现有中性配色、字体、成熟组件和后端事实边界。

## 证据与限制

两名独立评审分别进行设计评审和静态检测/浏览器观察。实际查看 1280×720 浅色浏览器，文件目录、Markdown/代码阅读，以及 480、700、900px 工作台。900px 面板下主区域约剩92px并明显裁切。子 Agent 与配置草稿为源码评审，未动态复现；未覆盖真实 Tauri、深色和960/1440px视口。

CLI 对 workbench.tsx 与 workbench-surface.tsx、workbench-agent-run-surface.tsx、workspace-files 扩展扫描均返回 []，exit 0。静态规则零命中不代表交互与布局无问题。

## 设计健康度

每项0—4，4最好；这是启发式评价，不是可用性测试结果。

| 项目 | 分数 | 主要判断 |
|---|---:|---|
| 状态可见 | 2 | 文件同步清楚，标签缺运行/未保存状态 |
| 符合用户语言 | 3 | 文件动作直观，部分配置字段偏技术 |
| 用户控制 | 2 | 面板极限尺寸破坏主区可用性 |
| 一致性 | 3 | 基础组件统一，收起与丢弃语义冲突 |
| 错误预防 | 2 | 关闭有确认，跨工作区清草稿缺保护 |
| 识别优于记忆 | 2 | 多层工具栏和标签状态增加记忆负担 |
| 操作效率 | 3 | 有键盘基础，缺工作台高频快捷入口 |
| 简洁与审美 | 2 | 色彩克制，正文前工具条过多 |
| 错误恢复 | 3 | 读取和重同步都有恢复入口 |
| 帮助说明 | 2 | 有tooltip，布局与草稿保留规则不明确 |
| 总分 | 24/40 | 空间与状态语义优先 |

## 做得好的地方

- 文件树、标签和阅读器复用成熟方案，已有键盘导航、焦点恢复、文件内查找和键盘调宽。
- 路径、引用到对话、实时同步与只读提示衔接了检查产物和继续任务。
- 文件读取/子 Agent 同步提供恢复入口；关闭单个未保存草稿已有确认。

## 优先问题

### P1：工作台扩宽会挤坏主对话（浏览器复现）

1280px窗口侧栏展开，分隔条按End将工作台扩到900px，主区域约剩92px，输入区与按钮被裁切。workbench.tsx:128使用固定宽度和shrink-0，:133固定上限900；布局没有为主区预留可用宽度。

建议：AppLayout统一协调侧栏、主区、工作台空间预算，动态限制停靠宽度；确实需要更宽时提供明确专注阅读模式和返回对话入口。仅把900改小无法覆盖不同窗口和侧栏状态。建议命令：impeccable adapt。

### P1：跨工作区会清空未保存草稿，缺少对应保护（源码确认）

从工作区A的草稿切到B的任务，workbench/context.tsx:21随workspaceId调用ensureWorkspace；store.ts:40直接清空tabs。app-layout.tsx的路由保护范围只有settings/dialog，selectTask也没有工作台草稿离开保护。

建议：在统一工作区切换意图边界处理真实会话销毁。两种可选产品策略是切换前确认放弃，或按工作区保留内存会话。默认建议先补完整的销毁确认契约；是否跨工作区保留应作为独立产品选择。建议命令：impeccable harden。

### P2：收起却提示放弃修改，实际内容仍保留（源码确认）

app-layout.tsx:173调用confirmDiscardUnsaved('workbench')，unsaved-changes.ts提示离开会丢失，随后仅setPanelOpen(false)。表单修改已同步store，Team也由session持有。

建议：收起只改变可见性并保留草稿；关闭/销毁才确认放弃。与上一项一起梳理生命周期，避免只改一句文案而保留冲突行为。建议命令：impeccable harden、clarify。

### P2：正文之前工具栏层数过多（浏览器观察）

480px阅读面板包含工作台标签、工作区工具条、文件标签、路径、引用/文件信息、阅读操作、引用行范围等多层横条。720px高窗口正文约从y=264—280开始。workspace-files-surface.tsx:123/:348、file-reading-tabs.tsx:58、file-preview.tsx:69、text-preview.tsx:238分别拥有这些层。

建议：合并为导航和当前文件操作两个层级；高频保留返回目录、可辨识路径、查找、引用，文件元信息和阅读设置渐进展开，行范围在发起引用时展示。保留长路径价值，短路径减少重复。不建议靠全面缩小字体节省空间。建议命令：impeccable distill、layout。

### P2：标签显示身份，却不足以支持监控（源码确认）

workbench.tsx:139起仅渲染类型图标、标题和关闭按钮。多个子Agent/草稿并存时，不能扫一眼看出运行、失败或未保存；截断标题缺完整标题提示。

建议：增加未保存标记、完整标题tooltip与标签溢出入口，运行状态直接投影后端canonical snapshot；不得另建前端状态机。清楚区分当前标签、非当前标签和待处理状态。建议命令：impeccable polish。

## 认知负荷与使用者风险

阅读工具条重复分组、同一目录切换存在两个入口、标签缺状态，形成中等认知负荷；不能将“超过4个按钮”机械视为错误。核心情绪低谷是读文件加宽后无法继续主任务，以及只想收起却看到放弃提示。

高频用户需要更直接的工作台打开/聚焦、快速找文件和切标签命令；应接入已有APP_SHORTCUTS和cmdk。键盘用户已有可用基础，但End键能合法触发坏布局。初次使用者需要分辨“文件”和“工作台”入口、两层标签、收起和关闭的关系。

## 改造路线比较

补丁路线：缩小最大宽度、删减几个间距、逐按钮补确认。实现快，但空间规则和草稿规则继续散落。

推荐局部结构化路线：AppLayout负责空间预算；Workbench会话边界负责隐藏/激活/关闭/工作区切换；文件surface统一导航与操作栏；共享标签呈现负责状态与溢出。影响范围限定前端布局、Workbench store/上下文及相关surface，无需改变后端Run生命周期、HTTP/SSE或数据库。

建议顺序：先两项P1与收起语义，再整理阅读工具栏和标签，最后统一focus、选中态、字体与间距。

## 次要观察

- Agent配置草稿使用sm:grid-cols-2，按窗口而非面板宽度切列；窄面板应按容器布局。保存按钮位于长表单末尾，可考虑固定操作区。
- 外层已有PanelResizeHandle，文件目录分隔条另有一套实现，可统一键盘与焦点行为。
- workbench-agent-run-surface.tsx:84仍为英文docstring；unsaved-changes.ts关于卸载与放弃的描述对store/session持有草稿不完全适用。本次仅报告，未修改。

Questions skipped: 用户明确要求先只交付报告，不进入修改决策。
