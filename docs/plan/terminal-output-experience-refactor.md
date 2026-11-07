# 命令输出阅读体验改造

## 目标与事实归属

用户确认实施；不提交 Git。执行结果属于后端 Terminal Worker / ExecutionResult；运行中增量仅用于预览，终态完整 output 覆盖预览。状态与已收集输出独立：非零退出、超时、取消均可阅读已有输出，不表示成功。父 Task 停止后只结算原占位，不投递新消息唤醒 Agent；编辑撤回的卡片不复活。

结构问题位于展示职责和结果契约：tool-catalog 与详情重复展示 command；复制借用并破坏 xterm 选区；卡片和对话争夺滚轮；终态投影错误覆盖完整结果；取消结果未携带输出。采用局部结构改造，复用 xterm、Dialog、disclosure store、canonical metadata，不新增终端服务或 ANSI 解析器。

## UI 契约

- 标题只显示描述（缺省“执行命令”）、状态、展开入口。命令只在详情展示一次，默认两行，可显式展开完整命令。
- “复制命令”复制原始参数，不含 UI 的 `$`；“复制输出”读取 xterm 公共 buffer，保留用户选区。按钮常驻可键盘操作，成功就地反馈，失败可见并记结构化日志。
- 行内输出最多 8 行，没有内部纵向滚动；滚轮交给对话。查看输出打开已有 Dialog，独立阅读区是唯一输出滚动面。
- 阅读器在底部时跟随输出；上翻后保持阅读位置，有新内容时提供“有新输出 · 回到最新”。关闭再开保留阅读位置，终态不强制关闭。开合与阅读状态属于 Task UI，不写后端。
- xterm 回滚区有界；达到容量时明确“复制保留内容”，可下载后端完整原始输出（含终端控制字符），不把屏幕缓冲称作完整日志。
- 启动目录用 basename 和完整路径提示，语义不是命令执行 `cd` 后的当前目录。
- 取消/失败仍保留短状态，结构化结果可展开；无结构化结果仍为简短状态行。取消按钮和后台调度语义保留。

## 范围与刻意不做

覆盖普通调用行、后台看板、前台/后台终端结束、快照重建。交互终端会话不变；不解析 command 中的 cd，不添加日志检索服务，不保留旧复制实现或双套终端渲染。

## 验证与复核要求

1. 后端真实命令：成功、非零、无输出、ANSI/回车/CJK、超时、取消、长输出；终态尾部优先于滞后增量；父停止保留输出但不投递消息。
2. 前端单测：快照追加/替换、8 行布局、无选区副作用复制、失败/取消可读。
3. Chromium UI：长命令单份展示、明确复制、剪贴板失败、预览滚轮、阅读器滚动与新输出、关闭/重开、深浅色与窄窗口。
4. gpt-6-sol 子 Agent 按本文和第零铁律只读复核；记录实际结果与限制，不把 mock transport 测试冒充端到端模型执行。

## 验证记录

实施完成，结果见下文。

## 实施与事实链路

- `execute_terminal._result_observation` 为正常、取消、超时统一从 ExecutionResult 构造展示；`tool_call_event_projection` 仅在缺失最终结构化 output 时沿用预览，明确存在的空字符串也具有权威性。
- `BackgroundRegistry` 通过登记时显式传入的 `collect_cancelled` 获取执行器唯一结论；先撤销全部作业、再在锁外等待。等待上限归执行器，BackgroundJob 不另设短时限。`TaskCancellation` 将结果映射交原占位结算，不创建 Agent 消息。
- 单项取消仍只回报原活动 Run；父 Task 停止不唤醒；编辑重跑移出的历史卡不复活。失败/取消没有结构化数据时仍保持短状态行。
- `TerminalOutputView` 统一承载 xterm 写入、有限缓冲、布局与读取公共 buffer 的复制；`TerminalViewport` 管 React 生命周期；`TerminalOutputReader` 只管 Dialog、下载和 Task UI 阅读位置。`terminal-result-view` 组装命令、预览和目录入口。
- `CopyIconButton` 的真实两类调用方分别使用图标模式（消息操作栏）与文字模式（终端命令/输出），共用复制反馈与错误处理。
- xterm 当前版本的滚轮先由内部 SmoothScrollableElement 处理，单纯隐藏滚动条或 attachCustomWheelEventHandler 不足以禁用预览滚动。预览容器在 capture 阶段 stopPropagation，不 preventDefault，使外层对话仍可默认滚动；阅读器使用 xterm 自身滚动。
- 用户进入/聚焦过程详情后保留过程及调用区展开，避免终态隐藏正在阅读的内容。阅读器关闭后不再接受动画触发的滚动位置写回。异步 write 回调在销毁后不再访问 xterm。

## 复核结论

两位 `gpt-6-sol` 子 Agent 分别按本文只读复核 UI 和后端取消契约。发现并修复：

1. 固定 10 秒的中间层等待可能短于执行器实际收尾，已删除重复时限。
2. queued write 在恢复阅读位置前继续下一次写入，已改为恢复后再释放队列。
3. 复制以光标行为终点遗漏 ANSI 光标上移后的可见行，改为读取公共 buffer 保留行。
4. 以命令字符数估计裁切遗漏窄窗口，改为 ResizeObserver 检测实际裁切。

最终只读复核未发现剩余代码阻断。真实 UI 还检出并修复视口销毁后回调、预览内滚动和关闭动画覆盖阅读位置。没有 Git 提交。

## 验证边界

- 后端相关测试：95 项通过，包含真实 Terminal Worker 的前台/后台、输出预算、取消和超时。真实停止集成验证从命令收集到 TaskCancellation、SQLite 重新读取、冷重建共用投影器，且 Agent 消息队列为空。
- 前端相关单元测试：52 项通过，覆盖队列写入、销毁、复制不碰选区、错误/取消仍可展开、过程区与后台任务。
- Chromium：使用 `tests/fixtures/terminal_output_samples.py` 通过实际 ExecuteTerminalTool/Terminal Worker 采集 ANSI、中文、回车进度、6000 行、非零、无输出、超时样本；通过测试 Transport 注入 UI。浏览器操作真实剪贴板、下载、滚轮、焦点、主题与窄布局。流式追加/终态切换由测试 Transport 驱动，并非在线模型端到端执行。
- 构建（含 TypeScript/Electron/Vite）通过；前端相关 ESLint 通过。构建仍提示既有 highlight CSS 与大 bundle 警告。
- 本次新增/修改行无新增 Ruff 问题；涉及文件全量 Ruff 仍有 16 处既有 E501 长行，逐行与 HEAD 内容核对，不借本次改造扩大清理范围。
- 本轮在 macOS / Chromium 验证；未声称完成 Windows 或在线模型验证。

最终 Chromium 回归 30 项全部通过（9 项本轮终端输出 + 21 项既有对话/后台任务回归）。运行日志：`/tmp/cosir-terminal-ui-final.log`；截图：`apps/desktop/test-results/slot-32/terminal-preview-light.png`、`terminal-preview-dark.png`、`terminal-reader-light.png`、`terminal-reader-dark.png`。
