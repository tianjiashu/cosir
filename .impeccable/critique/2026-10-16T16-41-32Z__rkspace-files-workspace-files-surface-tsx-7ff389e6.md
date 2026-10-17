---
target: Workbench 文件浏览器
total_score: 24
max_score: 40
na_heuristics:
p0_count: 0
p1_count: 2
target_identity: "file:/Users/tianjiashu/Documents/tianjiashu/cosir/apps/desktop/components/workspace-files/workspace-files-surface.tsx"
target_fingerprint: "sha256:de139c154ab1ec647b38e8039ad4e913dedc1d40a847bb292f3df282049f0308"
target_path: /Users/tianjiashu/Documents/tianjiashu/cosir/apps/desktop/components/workspace-files/workspace-files-surface.tsx
timestamp: 2026-10-16T16-41-32Z
slug: rkspace-files-workspace-files-surface-tsx-7ff389e6
closed: true
---
Method: dual-agent (A: /root/critique_design · B: /root/critique_evidence)

# Workbench 文件浏览器设计评审

**结论：功能基础扎实，默认阅读体验还不够成熟。24/40，属于“可用，但需要明显改善”。** 优先修复深色代码配色与正文空间，再处理搜索、阅读状态和键盘交互。此次只评审，未修改产品代码。

## 设计针对性
工作区路径、文件类型图标、实时同步、只读预览与任务对话并置，符合 Cosir 的本地编程工作场景。熟悉的 IDE 文件浏览范式值得保留；最需要强化的产品特点是“打开即可检查成果，回来仍保留阅读位置和方式”。

自动扫描覆盖 workspace-files 下 10 个 TSX 文件，退出码 0、发现 0 条，因而没有规则名、命中位置或假阳性。它未发现本轮实际观察到的阅读和交互问题，零告警不代表体验达标。浏览器不支持可变脚本注入，未生成检测覆盖层；使用真实页面截图、AX、只读 DOM 和源码交叉取证。

## 设计健康评分

| 原则 | 分数 | 主要依据 |
|---|---:|---|
| 系统状态可见 | 2/4 | 完成搜索后仍可“停止”，状态会变成“已停止” |
| 符合用户认知 | 3/4 | 文件树和路径熟悉，部分搜索/分段术语需要解释 |
| 用户控制与自由 | 3/4 | 可关闭、折叠、调整大小，阅读模式不能随标签恢复 |
| 一致性与标准 | 2/4 | 深色语法配色、标签键盘行为不一致 |
| 错误预防 | 3/4 | 明确只读，低风险浏览不设置多余确认 |
| 识别而非回忆 | 2/4 | 路径可见，图标与临时预览行为说明不足 |
| 灵活高效 | 2/4 | 成熟树和编辑器基础良好，跨文件操作仍有摩擦 |
| 美学与简约 | 2/4 | 视觉克制，但目录及工具栏压过正文 |
| 错误识别与恢复 | 3/4 | 可重试，扫描不完整有提示但缺少原因详情 |
| 帮助与说明 | 2/4 | 有快捷键提示，缺少情境化说明 |
| **总计** | **24/40** | **可用，需要明显改善** |

这是本轮设计判断，不是功能测试通过率。

## 做得好的地方

- 文件路径、类型图标、定位当前文件和复制路径，帮助确认读的是哪份文件。
- 复用 React Arborist、CodeMirror 和 PDF.js，复杂能力具有合理的维护基础。
- 只读与同步状态持续可见；Markdown 渲染后的正文结构清楚。

## 优先问题

### 1. [P1] 深色代码高亮难读

真实页面的函数名为纯蓝色 rgb(0,0,255)，关键字为深紫色 rgb(119,0,136)，字符串为暗红色 rgb(170,17,17)，与黑色背景不匹配。核查代码是主任务，这直接增加阅读负担。

证据：apps/desktop/components/workspace-files/code-preview.tsx:39；apps/desktop/components/code-editor-theme.ts:4。共享主题只设置正文、背景和行号，没有统一明暗语法高亮。

修复：在共享 CodeMirror 主题边界接入成熟的明暗高亮方案，验证主要 token 的对比度，避免逐个文件追加颜色补丁。建议命令：impeccable colorize / audit。

### 2. [P1] 默认布局给正文的空间太少

在 1280×720、默认 480px 工作台中，目录固定占约 40% 高度，下面又叠加文件标签、路径、属性、阅读工具和查找栏，实际代码仅能看到约 7 行。加宽至 672px 后切换左右布局，正文高度恢复，但对话空间随之减少。

证据：apps/desktop/lib/workbench/store.ts:39；workspace-files-surface.tsx:122；text-preview.tsx:164、254。

修复：调整目录与阅读区域的组合方式。窄面板打开文件后优先给正文空间，目录可收起或作为抽屉，增加明显的展开阅读入口；宽面板保留左右分栏。文件属性和低频工具渐进展示。结构调整优于只修改 40% 比例，影响范围应聚焦文件 surface 与必要的 Workbench 容器。建议命令：impeccable layout / distill。

### 3. [P2] 搜索完成态与动作不一致，低频控件常驻

搜索已完成仍能点击“停止”，并把文案改成“已停止”；仅有一个结果时，重搜和分页仍占空间。文件内查找尚未启动也显示“停止”。

证据：apps/desktop/components/workspace-files/file-search.tsx:79；text-preview.tsx:254；workspace-files-surface.tsx:140。

修复：按生命周期呈现取消、重试和分页；把排除规则放进清晰的筛选入口；整合文件内查找入口，保留大文件全文查找能力并说明范围。无结果时提供清空条件、返回目录操作。建议命令：impeccable clarify / distill。

### 4. [P2] 固定标签没有保留阅读方式

实测 PRODUCT.md 切到预览并固定，打开代码文件后再回来，Markdown 重置为源码。跨文件核对需要重复设置。

证据：workspace-files-surface.tsx:276；text-preview.tsx:40；apps/desktop/lib/workspace-files/store.ts。

修复：在文件会话中保存 Markdown 模式和换行等有价值的阅读偏好，不必保存所有瞬时搜索状态。建议命令：impeccable harden。

### 5. [P2] 标签键盘交互与点击目标不够成熟

两个文件标签之间按左右箭头不会切换；tab 缺少完整的焦点管理和 panel 关联。固定与关闭按钮实际命中区约 12×12px。

证据：workspace-files-surface.tsx:212–251；apps/desktop/components/workbench.tsx:108。

修复：复用项目成熟 Tabs 能力，落实方向键、Home/End、焦点管理和语义关联；扩大固定/关闭命中区域而保留紧凑外观，为图标补 Tooltip。这里不套用移动端按钮标准。建议命令：impeccable harden / audit。

## 认知负荷与情绪旅程

八项认知负荷检查有三项明显失败：主次层级、渐进披露、跨文件阅读连续性，属于中等负荷。文件列表超过四项本身不是问题；真正负担来自搜索、排除、停止、重搜、分页、编码和阅读模式同时占据小面板。

进入文件浏览器和获得搜索结果带来确定感；打开代码时，低对比度和狭小正文造成低谷；Markdown 预览能改善阅读感受，但切换文件后丢失模式又产生挫折。最应改善的结束体验是“返回文件时仍保持刚才的阅读方式”。

## 用户视角

- 熟练开发者：文件找得快，但正文过小、标签不能用方向键切换，容易回到外部编辑器核查。
- 键盘或低视力用户：代码低对比度、标签键盘契约缺失、微小点击目标构成实际障碍。
- 初次使用者：不容易理解斜体临时标签为何被替换；图标缺少可见说明；“条目未扫描”缺少进一步原因。

## 次要观察

- 实际搜索 README.md 返回首批 1000 个结果，很快进入 .venv。可提供明确的搜索范围选项，同时保留用户要求的全工作区搜索，不应静默排除依赖目录。
- Markdown 渲染视图仍显示编辑器专用快捷键提示，适用范围不清楚。
- 复制路径缺少成功反馈；建议按钮短暂显示“已复制”。
- 文件行的展开按钮可能被读作“展开目录 文件名”，应按条目类型校准语义。

## 进一步设计问题

阅读是否应在窄面板中优先占据空间？只读 Markdown 是否应默认渲染并保持上次模式？能否将当前两种文本查找组织为一个入口，并明确当前段与全文件范围？

## 评审范围

主评审实测深色 1280×720、480px 与672px工作台，覆盖目录、文件名搜索、代码、Markdown和标签操作。未在本轮重新验收图片/PDF、实时文件变更、浅色、200%缩放及真实Tauri/Windows。已有实现验收与本轮设计评审是不同证据。未发现需要单独报告的明确 docstring 漂移。
