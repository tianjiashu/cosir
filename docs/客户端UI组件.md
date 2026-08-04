做 **coding-agent 桌面客户端**，我更推荐这套组合：

> **React + shadcn/ui + Monaco Editor + xterm.js + AI Elements**

不建议仅使用 Ant Design、Element Plus 这类传统后台组件库。Coding Agent 更接近 IDE，需要高密度布局、流式内容、代码 Diff、终端、文件树和工具执行状态，普通管理后台组件的抽象并不完全匹配。

## 推荐组件栈

| 模块       | 推荐组件                                     | 用途                                                  |
| -------- | ---------------------------------------- | --------------------------------------------------- |
| 基础 UI    | **shadcn/ui**                            | Button、Dialog、Popover、Tooltip、Tabs、Dropdown、Sidebar |
| Agent 对话 | **AI Elements + shadcn Chat Components** | 流式消息、推理过程、工具调用、附件、代码块                               |
| 代码编辑     | **Monaco Editor**                        | 编辑代码、语法高亮、补全、诊断                                     |
| 修改审查     | **Monaco Diff Editor**                   | 查看 Agent 修改前后差异                                     |
| 终端       | **xterm.js**                             | Shell、PTY、命令执行输出                                    |
| 多面板布局    | **react-resizable-panels**               | 文件树、编辑器、Agent 面板、Terminal 拖动调整                      |
| 文件树      | **React Aria Tree** 或自研                  | 文件浏览、键盘导航、拖拽                                        |
| 长列表      | **TanStack Virtual**                     | 大型文件树、日志、执行记录虚拟化                                    |
| 命令面板     | **shadcn Command**                       | `⌘K`、文件搜索、Agent 命令                                  |
| 图标       | **Lucide React**                         | 风格统一、适合开发者工具                                        |
| 通知       | **Sonner**                               | 保存成功、执行失败、权限提醒                                      |
| 状态管理     | **Zustand**                              | 工作区、面板、会话和任务状态                                      |

shadcn/ui 的优势是组件源代码直接进入项目，容易针对 IDE 场景修改，而不是不断覆盖第三方组件样式。它目前也提供 Message、Message Scroller、Bubble、Attachment 等聊天组件。([Shadcn][1])

## 1. 基础组件首选 shadcn/ui

Coding Agent 客户端通常需要大量定制：

* 更紧凑的组件间距
* 类似 VS Code 的暗色主题
* 自定义右键菜单
* Tool Call、权限确认等非标准组件
* 大量键盘交互
* 多层嵌套的 Popover、Context Menu、Dialog

shadcn/ui 比 Ant Design 更适合这种产品，因为你直接拥有组件代码，可以把 Button 高度统一成 28px、修改菜单焦点行为、加入快捷键提示，而不需要套很多 wrapper。([Shadcn][1])

初始化时我会先选择 **Radix base**。虽然目前 shadcn/ui 也支持 Base UI 和 React Aria，但使用 AI Elements 时选择 Radix 通常能减少组件兼容问题。shadcn 当前允许在 Radix、Base UI 和 React Aria 之间选择。([Shadcn][2])

## 2. Agent 对话不要从零写

推荐组合：

* shadcn `MessageScroller`：处理流式输出时的滚动锚定
* `Message` / `Bubble`：基础消息结构
* AI Elements：Tool Call、Reasoning、Code Block、流式 Markdown
* 自己设计 `AgentEventCard`

AI Elements 针对 AI 界面提供消息、工具调用、推理过程、代码块和流式 Markdown 等组件，适合直接改造成 Coding Agent 的事件流。

建议不要把所有东西都做成聊天气泡。Coding Agent 的内容最好拆成不同视觉类型：

```text
用户消息
Agent 分析
├── 搜索代码
├── 读取文件
├── 修改文件
├── 执行命令
├── 测试结果
└── 最终总结
```

对应组件可以设计成：

```tsx
<AgentMessage />
<ReasoningBlock />
<ToolCallCard />
<FileReadCard />
<PatchCard />
<CommandExecutionCard />
<TestResultCard />
<PermissionRequestCard />
```

其中 `ToolCallCard` 至少应该支持：

* running / success / error / cancelled 状态
* 折叠参数
* 折叠输出
* 执行耗时
* 重试
* 复制输出
* 定位到相关文件
* 中止执行

## 3. 编辑器使用 Monaco

Monaco 是 VS Code 使用的 Web 编辑器，支持语法高亮、补全、错误诊断和自定义 completion provider。它同时提供原生 Diff Editor，适合展示 Agent 对代码的修改。([Microsoft GitHub][3])

建议至少封装三个组件：

```tsx
<CodeEditor />
<DiffEditor />
<ReadOnlyCodeViewer />
```

不要用 Monaco 渲染聊天中的小代码块。聊天代码块使用轻量级语法高亮即可；只有真正需要编辑、定位行号或展示完整 Diff 时才加载 Monaco，否则内存占用会比较明显。

Agent 修改代码时，推荐展示：

```text
Modified  src/services/agent.ts

+32  -8

[Review changes] [Accept] [Reject] [Open file]
```

点击 `Review changes` 再打开 Monaco Diff Editor。

## 4. 终端使用 xterm.js

xterm.js 是这类客户端的标准选择。它可以连接 Electron/Tauri 后端创建的 PTY，并通过 addon 扩展自适应尺寸等能力。官方的 FitAddon 可以让终端根据容器尺寸重新计算行列。([Xterm.js][4])

建议封装：

```tsx
<TerminalPanel
  sessionId="..."
  cwd="..."
  status="running"
/>
```

功能包括：

* 多 Terminal Tab
* PTY resize
* 命令搜索
* 复制与粘贴
* 链接识别
* 退出码显示
* Agent 命令高亮
* 用户输入和 Agent 输入区分
* “允许 Agent 在终端运行”权限控制

终端输出不要同时完整复制到聊天窗口。聊天中只显示命令摘要，完整输出保留在 Terminal 或可展开区域。

## 5. 面板布局使用 react-resizable-panels

典型布局：

```text
┌────┬────────────┬──────────────────────┬─────────────────┐
│活动│ 文件树     │ 编辑器 / Diff        │ Agent           │
│栏  │            │                      │                 │
│    │            ├──────────────────────┤                 │
│    │            │ Terminal / Problems  │                 │
└────┴────────────┴──────────────────────┴─────────────────┘
```

`react-resizable-panels` 支持横向和纵向 Panel Group、折叠、尺寸限制以及布局持久化，适合构建 IDE 式布局。([GitHub][5])

布局状态要保存：

```ts
type WorkspaceLayout = {
  sidebarWidth: number
  agentPanelWidth: number
  bottomPanelHeight: number
  sidebarCollapsed: boolean
  agentPanelCollapsed: boolean
  bottomPanelCollapsed: boolean
}
```

不要只依赖 CSS Grid。拖拽、最小宽度、折叠和恢复布局会逐渐变得复杂。

## 6. 文件树和日志要虚拟化

项目可能有数万文件，Agent 执行记录也可能非常长。建议：

* 文件树交互：React Aria Tree
* 数据规模较大时：TanStack Virtual
* 文件搜索结果：虚拟化 Flat List
* Terminal 之外的日志列表：虚拟化

React Aria 提供无样式、可组合并内置无障碍和键盘行为的组件；TanStack Virtual 提供 React、Vue、Svelte 等框架适配器。([React Spectrum][6])

文件树节点建议包含：

```ts
type FileTreeNode = {
  path: string
  name: string
  kind: "file" | "directory"
  gitStatus?: "modified" | "added" | "deleted" | "untracked"
  agentStatus?: "reading" | "editing"
  diagnostics?: number
}
```

这样能显示：

```text
src/
  components/
    AgentPanel.tsx     M  ✦
    Terminal.tsx       M
  api/
    chat.ts            2 errors
```

## 我的最终选择

如果你使用 React，我会直接采用：

```text
UI Foundation       shadcn/ui（Radix base）
Agent UI            AI Elements + 自定义 AgentEventCard
Editor              Monaco Editor
Diff                Monaco Diff Editor
Terminal            xterm.js
Layout              react-resizable-panels
Tree                 React Aria Tree
Virtualization      TanStack Virtual
Icons                Lucide React
State                Zustand
Desktop shell        Tauri 或 Electron
```

其中最值得自己投入设计的不是 Button 或 Dialog，而是这五个核心组件：

1. `AgentEventTimeline`
2. `ToolCallCard`
3. `PatchReview`
4. `PermissionRequest`
5. `ExecutionStatusBar`

基础 UI 可以复用，但这五个组件会直接决定你的 Coding Agent 是否像一个真正的开发工具，而不是“带代码块的聊天软件”。 🧩

[1]: https://ui.shadcn.com/docs?utm_source=chatgpt.com "Introduction - shadcn/ui"
[2]: https://ui.shadcn.com/docs/changelog/2026-01-base-ui?utm_source=chatgpt.com "January 2026 - Base UI Documentation - shadcn/ui"
[3]: https://microsoft.github.io/monaco-editor/?utm_source=chatgpt.com "Monaco Editor"
[4]: https://xtermjs.org/docs/guides/using-addons/?utm_source=chatgpt.com "Using addons"
[5]: https://github.com/bvaughn/react-resizable-panels?utm_source=chatgpt.com "GitHub - bvaughn/react-resizable-panels · GitHub"
[6]: https://react-spectrum.adobe.com/react-aria/.../getting-started.html?utm_source=chatgpt.com "Getting Started – React Aria"
