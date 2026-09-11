# Tool UI 展示契约

状态：方案契约，适用于非 CodeGraph 工具。

本文定义工具执行结果如何被桌面端 UI 展示。CodeGraph 工具不适用本契约，保持独立。

## 1. 运行边界

本项目是单用户、本机运行的桌面 Agent。工具 UI 展示不新增进程或服务：

```text
Tauri Rust 主进程
└─ WebView2 / React 前端
   └─ localhost Assistant Transport
      └─ FastAPI 后端进程
         └─ 工具 handler + tools/display 展示数据构建
```

- 工具在 FastAPI 后端进程执行。
- `apps/backend/app/core/tools/display/` 内的纯函数负责把执行结果投影成 UI 展示数据。
- `ToolDisplayHints` 随 `ToolDefinition` 传给客户端，描述工具的静态展示方式。
- `ToolObservation.display_data` 承载一次执行完成后的 UI 结构化数据。
- Assistant Transport 可以把展示数据带入事件和 snapshot，以支持前端渲染与重连恢复；展示数据不是任务、Run、Agent context 或文件变更事实源。
- `ToolObservation.artifact_data` 只服务文件快照、ChangeSet、回退和审计等后端能力，不进入 UI Transport。
- 后端重启后由已有 snapshot 恢复 UI，不隐式重放旧工具执行。

## 2. 两层契约

### 2.1 `ToolDisplayHints`：静态声明

位置：`apps/backend/app/core/tools/schemas/tool_display.py`

`ToolDisplayHints` 只声明“这个工具通常如何展示”，不包含动态结果，也不包含渲染函数。

字段语义：

- `verb`：客户端标题动词，例如“读取文件”“执行命令”。
- `icon`：客户端使用的图标名。
- `surface`：`trace` 表示普通执行轨迹，`standalone` 表示需要独立强调的结果。
- `expandable`：是否允许展开。
- `expand_layout`：客户端使用的通用布局：`none`、`details`、`list`、`diff`、`write`、`terminal`。
- `default_open`：完成后的默认展开偏好，不代表执行状态。
- `show_result`：是否把模型结果正文暴露给客户端；不影响 `display_data`。

约束：

- 不按工具名在前端编写专用渲染分支。
- 不把 query、path、命令输出、diff 或摘要文本写入 `ToolDisplayHints`。
- `surface`、布局和默认展开只是静态 UI 意图，执行状态仍以 Transport tool-call status 为准。

### 2.2 `display_data`：动态展示数据

位置：`apps/backend/app/core/tools/schemas/tool_observation.py`

`display_data` 是一次工具执行产生的、面向客户端的结构化 JSON 数据。每个 payload 必须有稳定的 `kind` 字段：

```json
{
  "kind": "<stable-display-kind>",
  "...": "UI-safe structured fields"
}
```

约束：

- 只放客户端渲染所需的事实：目标、列表、统计、diff、分页、有限状态信息。
- 不放 `content`、`reason`、堆栈、原始异常、完整 prompt、原始 provider 响应或凭据。
- 不由客户端从 `result`、`args` 或模型正文推导展示结果。
- 不把 `display_data` 当作后端业务事实源。
- 字段投影和敏感字段过滤必须先于 `DisplayDataBudget`；大小预算不能替代安全 allowlist。
- 所有字符串、列表、diff 和终端输出都必须服从有界大小；被截断时应有明确的 `truncated` 标记。
- 外层工具状态使用 Transport 的 `pending`、`running`、`completed`、`failed`、`cancelled`，不在 payload 中重复建立第二套 Run 状态机。
- 失败时 UI 默认只展示由 `ToolObservation.status` 投影出的失败状态“失败”，以及 `tool_error` `display_data` 中由后端受控生成的短提示；不得把模型侧的完整错误原因直接展示给用户。

错误态不在 `display_data` 中携带目标、结果或其它业务字段。无法生成安全短提示时，错误 `display_data` 仍只返回通用短提示，不把未经筛选的原始参数传给 UI。

### 2.3 错误展示策略

错误信息分为三个通道，不能混用：

```text
ToolObservation.error / reason
  └─ 面向模型的诊断与修正建议，不自动展示给用户

ToolObservation.display_data.status_hint
  └─ tool_error 错误态唯一允许的 UI 数据，约 5 个字的受控短提示

Transport tool-call status
  └─ pending / running / completed / failed / cancelled 等生命周期状态
```

错误态以及部分成功、截断等非完整结果的工具行遵循以下规则：

- 必须从 `ToolObservation.status` 得到状态；`display_data` 不得定义或覆盖状态。
- `tool_error` 的 `display_data` 只能是 `{"status_hint": "..."}`，提示建议约 5 个字，最多 8 个字符。
- `status_hint` 必须来自后端明确的分类映射，不得直接复制 `error`、`reason`、异常字符串或 provider 原始响应。
- 没有安全且有价值的分类时使用通用提示“执行失败”；客户端仍只显示“失败 + 短提示”。
- 取消态显示“已取消”，不显示为失败；部分成功可以使用“部分成功”等受控提示。
- UI 不展示堆栈、完整网络错误、模型 prompt、凭据或大段模型正文。

示例：

```json
{
  "status_hint": "文件不存在"
}
```

客户端的紧凑展示类似：

```text
读取文件  · 失败 · 文件不存在
```

其中“失败”来自 `ToolObservation.status` 的 Transport 投影，“文件不存在”来自错误 `display_data.status_hint`；模型仍然可以从 `error` / `reason` 获得完整修正建议。

Transport 投影必须遵循以下固定映射：

```text
ToolObservation.status == "success"   → tool-call status "completed"，error 为空
ToolObservation.status == "error"     → tool-call status "failed"，error 为 status_hint
ToolObservation.status == "cancelled" → tool-call status "cancelled"，error 为“已取消”
```

`tool_observation_dispatcher` 不得把 `summary["error"]` 或 `summary["reason"]` 写入
`ToolCallStatusChangedEvent.error`。完整诊断只通过模型上下文写回；进程恢复时的
`ToolCallsSettledEvent` 也只能写入“执行异常”“已取消”等短提示。

错误态的 `display_data` 不得再包含对应工具成功态的 `kind`、path、URL、结果列表、diff 或终端输出。成功态仍按各工具章节定义完整的 UI 数据；例如 `read_file` 因文件过大截断时可以在成功数据中携带“文件过大，已截断”，这不属于 `tool_error`。

### 2.4 各工具错误短提示

以下提示是 UI 文案，不是内部错误码，也不是模型侧的 `error` / `reason`。同一工具应优先使用表中的具体提示，无法判断时使用“执行失败”。

| 工具 | 建议短提示 |
| --- | --- |
| `read_file` | `文件不存在`、`无法读取`、`非文本文件`、`路径无效` |
| `write_file` | `写入失败`、`路径无效`、`无法写入` |
| `replace` | `未找到文本`、`内容冲突`、`替换失败` |
| `apply_patch` | `补丁无效`、`目标已变更`、`应用失败` |
| `search_files` | `搜索失败`、`正则无效`、`路径不存在` |
| `list_directory` | `目录不存在`、`无法读取`、`路径无效` |
| `delete` | `目标不存在`、`权限不足`、`目录非空`、`删除失败` |
| `execute_terminal` | `命令超时`、`命令失败`、`目录无效`、`命令被拦截` |
| `web_search` | `服务未配置`、`网络失败`、`搜索失败` |
| `web_extract` | `服务未配置`、`网络失败`、`提取失败`、`URL 无效` |
| `delegate_task` | `委派失败`、`子任务失败`、`子 Agent 不存在` |

文件写入后的语法检查不属于 UI 感知范围：即使检查失败，也不生成“语法检查失败”短提示，不进入错误 `display_data`，并按文件写入本身的 UI 结果处理。

## 3. 非 CodeGraph 工具契约

当前非 CodeGraph 工具共 11 个，包括 `delegate_task`。

| 工具 | 静态展示声明 | `kind` | 动态展示字段 |
| --- | --- | --- | --- |
| `read_file` | `trace`、不可展开、`none`、`eye` | `read-file-meta` | `path`、实际展示的 `line_range`、`file_size`；文件过大截断时附带固定 `status_hint` |
| `write_file` | `standalone`、可展开、`diff`、`git-compare` | `file-changes` | `changes`、`diff_stats` |
| `patch`（replace） | `standalone`、可展开、`diff`、`git-compare` | `file-changes` | 与 `write_file` 相同 |
| `apply_patch` | `standalone`、可展开、`diff`、`git-compare` | `file-changes` | 多文件 `changes`、`diff_stats` |
| `search_files` | `trace`、可展开、`list`、`search` | `file-list` | `pattern`、`target`、`path`、`files`、`page`、`match_count` |
| `list_directory` | `trace`、可展开、`list`、`eye` | `directory-list` | `path`、`entries`、`page`、`total_entries` |
| `delete` | `standalone`、不可展开、`none`、`trash-2` | `delete-result` | `path`、`target_type`、`recursive` |
| `execute_terminal` | `standalone`、可展开、`terminal`、`terminal` | `terminal-result` | 脱敏后的 `command`、`workdir`、`output`、`exit_code`、`timed_out`、`truncated` |
| `web_search` | `standalone`、可展开、`list`、`globe` | `web-search-results` | `query`、`results`；结果只含 `title`、`url` |
| `web_extract` | `trace`、低噪声列表、`list`、`globe` | `web-extract-urls` | `urls`；每项只含 `url` |
| `delegate_task` | `trace`、可展开、`details`、`users` | `delegation-result` | `title`、`child_agent_id`、`delegation_id`、`child_task_id`、`child_run_id`、状态 |

交互式 terminal handler 当前处于隐藏实现阶段，尚未计入上述 11 个工具，也不会出现在
Agent tool schema。实现完成后继续复用现有 renderer 路由，静态布局为 `terminal`，动态
`kind` 为 `terminal-session`，只传 session identity、状态和有限 cursor 元数据；完整 PTY
输出只走独立的只读 `terminal-preview-v1` WebSocket，不进入 Assistant Transport 的工具
展示 payload。handler 的 docstring 明确记录“未注册、Agent 不可见”，注册必须在真实
worker 和跨平台集成验收后作为独立变更完成。

### 3.1 文件读取

`read_file` 的 UI 只展示读取目标、实际展示的起止行号和文件大小；不展示正文、总行数或下一页 offset。读到文件行尾时，行号范围固定渲染为 `L<start>-END`，例如 `L1-END`；尚未读到行尾时渲染为 `L<start>-L<end>`。只有因文件过大导致输出被字符预算截断时，额外固定展示“文件过大，已截断”。普通分页到达 `limit` 不属于该提示。正文和继续读取所需的 `next_offset` 继续通过 `content` 进入模型通道。

文件工具因重复调用而提前返回的成功观察也必须使用稳定 `kind`（`repeated-call`），但只展示受控的重复/未变化提示，不从模型结果反推文件内容。

```json
{
  "kind": "read-file-meta",
  "path": "apps/backend/app/core/tools/tool_handler/read_file.py",
  "line_range": {
    "start": 1,
    "end": 200
  },
  "file_size": 18320
}
```

读到文件行尾时：

```json
{
  "kind": "read-file-meta",
  "path": "small.py",
  "line_range": {
    "start": 1,
    "end": "END"
  },
  "file_size": 920
}
```

客户端应将上述范围显示为 `L1-END`。

发生截断时：

```json
{
  "kind": "read-file-meta",
  "path": "large.log",
  "line_range": {
    "start": 1,
    "end": 500
  },
  "file_size": 10485760,
  "truncated": true,
  "status_hint": "文件过大，已截断"
}
```

`line_range` 表示实际返回给模型的正文所覆盖的范围，而不是请求的 `offset` / `limit`；`end` 为数字时显示为 `L<start>-L<end>`，读到文件行尾时直接使用字符串 `"END"` 并显示为 `L<start>-END`。空文件或没有返回行时，`end` 可以为 `null`。`truncated` 和固定 `status_hint` 仅在确实因为文件过大、字符预算不足而截断时出现；如果只是因为分页 `limit` 到达页末，不能设置 `truncated`，也不能显示“文件过大，已截断”。

### 3.2 文件修改

`write_file`、`replace`、`apply_patch` 共用 `file-changes`。`display_data.changes` 只携带可由客户端直接解析的 Git 风格 `patch`、`status`、路径和增删行数，不携带完整文件正文；patch 超过展示预算时应返回 `patch: null` 和 `truncated: true`。完整回退与审计事实（包括 `before`/`after`）仍只放在 `artifact_data`。

```json
{
  "kind": "file-changes",
  "changes": [
    {
      "path": "src/example.py",
      "new_path": null,
      "status": "modified",
      "patch": "diff --git a/src/example.py b/src/example.py\n--- a/src/example.py\n+++ b/src/example.py\n@@ -1,2 +1,3 @@\n ...",
      "insertions": 2,
      "deletions": 1
    }
  ],
  "diff_stats": {
    "total_files": 1,
    "total_insertions": 2,
    "total_deletions": 1
  }
}
```

`patch` 必须以 `diff --git` 开头，并包含标准 `---`、`+++` 和 hunk 头；客户端不得根据 `before`/`after` 重新拼接 patch。`artifact_data` 与 `display_data` 是两个不同投影：展示预算只能影响 `display_data`，不能用被截断的展示 patch 执行回退。

文件写入后的语法检查属于后端和模型通道，不属于 UI 展示契约。无论语法检查是否通过，`display_data` 都只描述实际发生的文件变更，不得加入 `verification`、`syntax_errors` 或诊断正文。

如果文件写入本身已经成功，即使后置语法检查发现问题，UI 仍按正常文件变更展示；工具 UI 状态不能因此显示“写入失败”。语法检查诊断可以继续通过模型侧的 `content` 供 Agent 自修复，但不得投影到 UI Transport 的错误展示字段或 `display_data`。

### 3.3 搜索和目录

`search_files` 和 `list_directory` 都使用列表布局，但必须保持不同的 `kind`。搜索结果当前以文件路径为主；如果将来展示命中行或摘要，应新增有界的结构化字段，不解析模型正文。

```json
{
  "kind": "file-list",
  "pattern": "ToolObservation",
  "target": "content",
  "path": ".",
  "files": [{"path": "apps/backend/app/core/tools/schemas/tool_observation.py"}],
  "page": {
    "offset": 0,
    "limit": 50,
    "has_more": false,
    "next_offset": null
  },
  "match_count": 1
}
```

目录条目至少包含 `name`、`type`、`path`。`type` 使用 `file`、`dir`、`link` 等稳定值。

### 3.4 删除

删除 UI 只显示目标和删除模式，不显示被删除文件的全文。删除前内容只保存在 `artifact_data`，用于后端回退。

```json
{
  "kind": "delete-result",
  "path": "tmp/output.txt",
  "target_type": "file",
  "recursive": false
}
```

### 3.5 终端

终端 UI 展示必要的命令、工作目录和有限输出。命令和输出都必须经过自由文本脱敏；不能因为命令来自模型就认为其中没有凭据。

```json
{
  "kind": "terminal-result",
  "command": "npm test",
  "workdir": "H:/coding-agent",
  "output": "...",
  "exit_code": 0,
  "timed_out": false,
  "truncated": false
}
```

超时属于 `tool_error`：UI 只展示失败状态和短提示“命令超时”，不展示终端输出或其它结果字段。
执行器可以继续把已收集的部分输出放在面向模型的诊断通道中，但不得进入错误 `display_data`；
退出码也不能被 UI 当作正常完成的可靠依据。

### 3.6 网页搜索

`web_search` 使用独立的结果契约，不复用文件列表的语义。UI 不展示 provider、摘要或排名；每个结果只保留标题和可跳转 URL：

```json
{
  "kind": "web-search-results",
  "query": "local-first desktop agent",
  "results": [
    {
      "title": "Example",
      "url": "https://example.com/page"
    }
  ]
}
```

客户端可以打开已经返回的 URL，但不得因此重新调用 provider 或绕过后端网络边界。

### 3.7 网页正文提取

`web_extract` 的 UI 只展示 URL，不展示 provider、正文、metadata、原始响应或逐站点错误详情。前端根据 URL 解析网站名称和 favicon，并把 URL 作为可点击链接；不需要后端提供网站名称或 provider 字段。

```json
{
  "kind": "web-extract-urls",
  "urls": [
    {
      "url": "https://example.com/page"
    }
  ]
}
```

多 URL 调用保持后端返回的 URL 顺序。工具整体的成功、失败、取消或部分成功由 Transport tool-call status 和受控短提示表达，不在 URL 项中加入 provider 或错误正文。若将来需要正文预览，应新增明确的、限长的 preview 契约，不能把现有模型正文隐式暴露给 UI。

### 3.8 委派

`delegate_task` 的父工具 UI 展示委派生命周期，不展示完整 prompt，也不把 child 工具的 diff、终端输出或正文混入父工具 payload。

```json
{
  "kind": "delegation-result",
  "title": "Review the parser",
  "child_agent_id": "reviewer",
  "delegation_id": 12,
  "child_task_id": 34,
  "child_run_id": 56,
  "status": "completed"
}
```

如果客户端支持跳转 child task，只使用明确的 `child_task_id`，不从文本中解析链接。

## 4. 展示数据构建位置

展示构建方法放在 `apps/backend/app/core/tools/display/`，按展示语义拆分，避免每个 handler 内联大段字典：

```text
display/
├─ common.py
├─ file_change_display.py
├─ filesystem_display.py
├─ terminal_display.py
├─ web_display.py
└─ delegation_display.py
```

- `file_change_display.py`：文件修改工具共用的 diff 投影。
- `filesystem_display.py`：读取、搜索、目录、删除的展示投影。
- `terminal_display.py`：终端结果和命令脱敏投影。
- `web_display.py`：搜索结果与正文提取状态投影。
- `delegation_display.py`：委派结果的最小状态投影。
- `common.py`：路径、分页、字符串和列表的有界化工具。

builder 应接收已校验的参数和领域结果，返回新的普通字典；不得执行额外工具、网络请求、数据库写入或前端渲染。

## 5. 迁移和验收要求

### 后端

- 所有非 CodeGraph 工具的 `ToolDefinition` 都声明 `ToolDisplayHints`。
- 每个成功、失败、取消路径都能在安全范围内构建对应的 `display_data`。
- 失败路径的 `display_data` 只提供 `status_hint`，状态取自 `ToolObservation.status`，不把 `error` / `reason` 原文投影到 UI。
- `display_data` 在 allowlist 投影后再进入统一大小预算。
- `web_search` 的 UI 数据不得出现 provider、description 或 position；结果项只允许 `title` 和 `url`。
- `web_extract` 的 UI 数据不得出现 provider、content、metadata、逐站点状态或原始 provider payload；只允许 URL 列表。
- 文件已修改但语法检查失败时，UI 数据只保留实际变更；语法检查诊断不得进入 UI。
- `artifact_data` 不进入 Assistant Transport。
- `ToolObservation.content` 不被当作通用 UI 展示数据的替代来源。

### 前端

- 主要依据 `data.kind` 和 `ToolDisplayHints.expand_layout` 路由布局。
- 不从 `args` 或 `result` 补推执行结果。
- `web_extract` renderer 永远不渲染正文，即使收到意外字段也必须忽略。
- 未知 `kind` 使用安全 fallback，不导致消息流崩溃。
- `trace` 工具保持低噪声；diff、终端和网页搜索等结果工具可以使用独立展示区域。

### 当前实现迁移注意事项

以下问题在实现本契约时需要一并处理：

1. `execute_terminal.py` 的成功分支应使用 `display_data=`，当前源码使用了 `data=`。
2. `delegation_executor.py` 的成功委派结果同样应使用 `display_data=`，并补充父级委派展示数据。
3. `web_search`、`web_extract` 应补充稳定的 `kind`，不能只依赖通用 `entries`。
4. 错误分支不得补充目标、query、URL 或结果数据；只允许受控 `status_hint`。
5. 终端 `command` 也必须经过脱敏，不能只脱敏输出。
6. 通用 `DisplayDataBudget` 不能承担字段安全过滤；尤其网页提取必须先做字段级 allowlist。
