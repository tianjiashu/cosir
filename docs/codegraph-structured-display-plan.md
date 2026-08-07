# CodeGraph 工具客户端结构化展示改造方案

> 状态：v3（已根据独立审查 Agent 复审意见修订：缩小「打开文件」承诺、澄清包化落位，重新提交审查）
> 关联样例数据：`docs/codegraph-tool-outputs.md`（本项目真实 Kernel 输出，作为结构化解析的验收基准）
> 遵循：rules/Agent代码开发规范.md（第零铁律优先；五条铁律；分层架构；单一职责；可排查日志）
> 修订记录：v1 被独立审查 Agent 判「不通过」（可点击跳转建立在未实现能力上 / __init__ re-export 风险 / 第零铁律该大不大该小不小）。v2 已逐条修正。

---

## 一、目标

把 CodeGraph 6 个工具当前「折叠行写 verb + 展开态一大坨纯文本」的展示，升级为：

- **4 个格式规整的工具（search / node / callers / callees）**：解析为结构化列表，复用前端现有 `list` 布局（`ToolListEntry`），每项带 `filePath` / `lineNumber` / `kind` / `edge`，**本期支持点击「打开文件」**（调用现有 `onOpenFile(filePath)` 机制，复用单 path 打开能力，立即可用、闭环）。
- **impact**：解析为「受影响符号」扁平列表（每项带 `filePath` / `lineNumber`），先扁平，分组增强后续再做。
- **explore（本期不做结构化）**：维持全文文本展示；本期仅把后端「索引降级 / 陈旧」banner 从结果体剥离为独立提示，正文原样透传，不做分节/关系图解析。

**不改动 vendor**（`third_party/codegraph`）：vendor 设计契约第一版即「透传 MCP 文本输出，不重新定义结构化响应」（`tool-service.ts:8`、`protocol.ts:101`）。结构化解析放在后端 Python 侧，符合「不重复造轮子 + 第零铁律（敢改、敢扩改动面，但不碰无关契约）」。

---

## 二、可行性证据（已实测 + 审查复核）

### 2.1 透传链路已验证（技术可行）

后端 → 前端 `data` 字段端到端透传路径（已逐层读码 + 独立审查 Agent 复核确认）：

```
codegraph_query.py: tool_success(content=..., data={...})
   → ToolSuccess.observation.data = merged_display_data        (tool_success.py:63-77)
   → ToolExecutionService 构造 ToolCallFinishedPayload(data=observation.data or {})  (tool_execution_service.py:189)
   → SSE runtime_event 透传到前端
   → 前端 projectToolResult(toolName, payload.data)            (projector.ts:projectToolResult)
```

- `ToolObservation.data` 是「机读字典」，前端渲染按 key 取（见 `app/tools/schemas/tool_observation.py`）。
- `ToolExecutionService.clear_display_data()`（转模型消息前清空）**只影响模型消息，不影响 SSE 事件的 `data`**（`tool_observation.py:101-107` 在事件 payload 已复制 data 值后才清空）——展示链路不受影响。

**结论：`data` 透传链路成立，无需改协议层。**

### 2.2 前端 list 布局契约（需小幅扩展以支持跳转）

`apps/shared/ts/toolDisplayRules.ts`：
- `ToolListEntry` 现有字段：`name` / `path` / `type?` / `filePath?` / `lineNumber?` / `content?` / `contentTruncated?`。
  - **本期扩展**：新增 `kind?`（符号类型，如 function/class）与 `edge?`（调用边标签，如 via import）两个**可选字段**，向后兼容（不影响现有 `web_*` / `search_files` 投影）。这样 codegraph 不再用字符串拼接把 kind/edge 塞进 name/path（避免打补丁式将就，符合第零铁律）。
- 现有 `RESULT_RULES` 按 `toolName` 路由，如 `web_search: (data) => projectWebResult(data, "web")`，从 `data["web"]` 取列表。**新增 codegraph 只需在 `RESULT_RULES` 加 5 条规则**，不散落到组件。
- `projectToolResult` 是纯函数、不抛异常、字段缺失安全降级——新增规则零破坏现有展示。

**「点击打开文件」本期必做，但「定位到指定行」本期不做（关键修订）**：独立审查 Agent 复核指出——当前 `apps/desktop/src/components/chat/ToolCallCard.tsx` 的 `ListView`/`renderListEntry` 在 `filePath` 分支（第 467-477 行）仅渲染**纯文本** `{filePath}:{lineNumber}`，无 `onClick`；`onOpenFile`（打开文件到 IDE）只在 diff/write 布局头部使用，**list 布局未透传**。因此方案 v1 声称的「可点击跳转」建立在未实现能力上，是可行性硬伤。

进一步核查链路发现：现有打开机制 `TurnTimeline.tsx:127 → openFileInEditor(path) → Rust open_file_in_editor(path)`（见 `apps/desktop/src-tauri/src/commands/fs.rs:22-43`）**只接受单 path、用系统默认应用打开，无法定位到具体行**。因此「复用现有机制 + 跳转到指定行」自相矛盾——这是 v1 同源硬伤在新位置的复发。

**修正决策（第零铁律：改动聚焦 + 敢扩该扩的）**：
- 本期交付**可闭环**的能力：`ListView`/`renderListEntry` 增加 `onOpenFile(filePath)` 透传参数，`filePath` 分支渲染为可点击按钮（参考 `DiffFileHeaderContent` 的 ExternalLink 打开按钮），`ToolCallCard` 把现有 `onOpenFile`（单 path）传入 list 布局。点击 = 用现有机制打开文件，立即可用、不引入未实现承诺。
- **「定位到指定行」列为后续增强**（不在本期）：需扩展 `onOpenFile(path, line?)` / `openFileInEditor` / Rust `open_file_in_editor` 支持 `line` 参数、并确保能落到带行定位的编辑器（`--goto path:line` 集成），改动面不小且牵连现有打开机制，不应塞进本期以免误伤其他文件打开场景。本期列表项仍如实展示 `lineNumber`（如 `filePath:line` 文本），点击行为仅打开文件。

### 2.3 真实输出结构（来自 `docs/codegraph-tool-outputs.md`，审查复核一致）

| Tool | 文本形态 | 解析可行性 |
|---|---|---|
| search | `name (kind)` / `path:line` / `` `signature` `` 每组 3 行 | 易（正则分组） |
| node | `name (kind)` → `Location:` / `Signature:` / `Calls →` / `Called by ←` 固定前缀字段 | 易 |
| callers | `**Callers of X (N found)**` + `- name (kind) - path:line [— via edge]` | 极易（单行 `- `，edge 可选） |
| callees | 同 callers | 极易 |
| impact | `**Impact: ...**` 按 `**file:**` 分节 + `sym:line, sym:line` | 易（分节 + 冒号分隔） |
| explore | 自然语言 + blast radius + relationships + 源码块（半结构化） | **本期不做**（维持全文） |

---

## 三、架构决策（对应规范条款）

### 3.1 单一职责
- 新增 `apps/backend/app/tools/tool_handler/codegraph_query/result_parser.py`：**只做**「codegraph 原始文本 → 结构化 list」的解析，不混入 `codegraph_query.py` 的执行/分发逻辑。
- 解析逻辑按 tool 名分派（`parse_search` / `parse_node` / `parse_callers` / `parse_callees` / `parse_impact`），每个函数单一职责、带完整 docstring。
- `codegraph_query/__init__.py`：薄壳 re-export 全部 6 个 `build_*_definition`（及 `CodegraphQueryTool`），**不得留空**——`tool_system.py:9-16` 用包式导入依赖此 re-export，留空会破坏后端启动。

### 3.2 分层架构（不跳层）
- 解析在**工具 handler 层**（执行后、产出 `data` 前），属「工具执行层」合法职责（类比 `web_search` 把 `display_data.web` 结构化后透传）。
- 前端 `toolDisplayRules.ts` 是「展示投影层」，只消费 `data` 不感知解析细节。两层通过 `data` 字典解耦，无反向依赖。

### 3.3 第零铁律校准
- 引入新文件 `result_parser.py`、前端加 `RESULT_RULES` 分支 + 扩 `ListView` 跳转——属于「为长期可维护性做聚焦的结构性改动」，改动面小、职责清晰。
- **不碰 vendor**：vendor 明确第一版透传文本，强行改 vendor 协议会扩大改动面、违反其设计契约，且不利后续 vendor 升级——遵循「不重复造轮子 / 不越界」底线。
- **扩 `ToolListEntry` 加 `kind?`/`edge?` 而非字符串拼接**（修正 v1 的「该小不小」）：第零铁律允许/鼓励为长期可维护性扩契约；字符串拼接是典型打补丁将就，后续 ListView 扩跳转时还需二次迁移，反而增债。字段可选，向后兼容。
- **本期扩 `ListView` 支持跳转**（修正 v1 的「该大不大」）：核心收益（跳转定位）不应为 scope 收敛而推迟，属于值得做的结构性改动。

### 3.4 不重复造轮子
- 复用现有 `ToolListEntry` / `ListView` / `RESULT_RULES` 投影机制，不自研前端列表组件。
- 解析仅用标准库 `re` / `dataclasses`，不引入新依赖。

### 3.5 可排查日志
- `result_parser.py` 解析失败时（正则无命中）**不抛异常、不静默丢弃**：降级为「全文透传 + 一条 `log.warn`」，保证 agent 仍能拿到文本结果，且问题可定位（日志含 tool 名 + 原始文本前 N 字符；`trace_id` 由日志子系统 filter 自动回填，不在此手动拼）。
- 前端 `projectCodegraphResult` 纯函数、字段缺失安全降级，不抛异常。

---

## 四、改动清单

### 后端
1. **包化落位（消除「文件与目录同名」歧义）**：当前 `codegraph_query` 是单模块文件 `apps/backend/app/tools/tool_handler/codegraph_query.py`。本期把其**内容迁入包内** `apps/backend/app/tools/tool_handler/codegraph_query/tool.py`（承载 `CodegraphQueryTool` 与 6 个 `build_*_definition`），并新建 `apps/backend/app/tools/tool_handler/codegraph_query/__init__.py` 作**强制薄壳 re-export**（re-export 全部 `build_*_definition` ×6 与 `CodegraphQueryTool`）。不得留空，且原 `codegraph_query.py` 需删除，避免同名文件/目录冲突。
2. **新增** `apps/backend/app/tools/tool_handler/codegraph_query/result_parser.py`：
   - `dataclass CodegraphListEntry`（name, kind, filePath, line, edge?, signature?）对应 `ToolListEntry` 的扩展字段。
   - `parse_tool_result(tool: str, text: str) -> dict`：分发到各 parser，返回 `{"codegraph": {"items": [...]}}` 或 `{"codegraph": {"raw": text}}`（解析失败时降级）。
   - 各 `parse_*` 纯函数，正则解析，字段缺失安全；`callers`/`callees` 正则须同时覆盖**带 edge 与不带 edge** 两态。
3. **修改** `apps/backend/app/tools/tool_handler/codegraph_query/tool.py`（即原 `codegraph_query.py` 迁入包内的实现体）：
   - 执行成功后，用 `result.content[0].text`（explore 维持全文）调 `parse_tool_result` 得结构化 dict。
   - `tool_success(content=..., data={"codegraph": structured})`——**替换**当前的 `data={"tool": self.name, "query_params": kwargs}`（旧 key 前端未消费，属无用透传）。
   - 剥离「索引降级 / 陈旧」banner：用正则识别 vendor banner 前缀，降级提示单独放 `data["codegraph"]["notice"]`，正文不混 banner。

### 前端（shared）
4. **修改** `apps/shared/ts/toolDisplayRules.ts`：
   - `ToolListEntry` 扩展 `kind?: string` / `edge?: string`（可选，向后兼容）。
   - 新增 `projectCodegraphResult(data: ToolDataRecord, key: string): ToolResultProjection`，从 `data[key].items` 映射成 `ToolListEntry[]`（`filePath`/`lineNumber`/`kind`/`edge` 透传）。
   - `RESULT_RULES` 加 5 条：`codegraph_search` / `codegraph_node` / `codegraph_callers` / `codegraph_callees` / `codegraph_impact` 均 `(data) => projectCodegraphResult(data, "codegraph")`。
   - `codegraph_explore` 不加规则（维持 `EMPTY_PROJECTION`，由 `result` 全文渲染）。

### 前端（desktop）
5. **修改** `apps/desktop/src/components/chat/ToolCallCard.tsx`：
   - `ListView` / `renderListEntry` 增加 `onOpenFile` 参数；`filePath` 分支渲染为可点击按钮（参考 `DiffFileHeaderContent` 的 ExternalLink 打开按钮），点击调用 `onOpenFile(filePath)`（**复用现有单 path 打开机制**，只打开文件、不定位行）。
   - `ToolCallCard` 把已有的 `onOpenFile` prop 透传给 `ListView`（当前仅在 diff/write 布局使用，本期扩展至 list 布局）。
   - 不修改 `onOpenFile` / `openFileInEditor` / Rust 命令签名（本期保持单 path，避免牵连其他文件打开场景）；「定位到指定行」见后续增强。

### 测试
6. **新增** `apps/backend/tests/test_codegraph_result_parser.py`：覆盖 5 个 parser 的正常解析、字段缺失、异常文本降级为 raw；`callers`/`callees` 显式覆盖**带 edge 与不带 edge** 两态（用 `docs/codegraph-tool-outputs.md` 真实片段作 fixture）。
7. **新增** `apps/desktop/src/tests/toolDisplayRules.test.ts`（或并入现有）：`projectCodegraphResult` 从 `data.codegraph.items` 映射 `ToolListEntry` 的字段正确性、缺 items 时安全降级。
8. **新增** `apps/backend/tests/test_codegraph_import.py`（冒烟）：确认 `from app.tools.tool_handler.codegraph_query import (build_explore_definition, ...)` 全部 6 个在包式改造后仍可导入，防止 `__init__.py` 漏 re-export 导致后端启动失败。

### 文档
9. `docs/codegraph-tool-outputs.md`：保留为验收基准（真实输出样例）。
10. `apps/backend/temp/probe_codegraph_tools.py` 及 `codegraph_tool_outputs.md`：验证用，用完清理（已在 .gitignore）。

---

## 五、折叠态行为（补充，避免展示缺口）

- 前端 `toolDisplayRules.ts` 的 `REQUEST_SUMMARY_RULES` 当前无 codegraph 条目，折叠行会降级为通用请求摘要（verb + 参数）。本期**不新增 codegraph 折叠态规则**，接受该降级行为（折叠行展示「代码关系查询 query=...」即可，展开态才是结构化重点）。若后续要更精致折叠摘要，再补 `REQUEST_SUMMARY_RULES`。

---

## 六、分阶段实施与验收

- **阶段 1（本期）**：search / callers / callees / impact 结构化 + explore 全文维持 + banner 剥离 + **ListView 点击跳转** + `ToolListEntry.kind/edge` 扩展。
- **阶段 2（后续，不在本期）**：explore 分节折叠 / 关系图；impact 按文件分组；node 的 calls/called-by 双向分栏；**「点击定位到文件指定行」**（需扩展 `onOpenFile(path, line?)` / `openFileInEditor` / Rust `open_file_in_editor` 支持 `line` 参数 + 接真实编辑器 `--goto path:line`，改动面不小，不塞入本期）。
- **验收**：
  - 后端：`result_parser.py` 单测覆盖 5 个 parser（正常 + 字段缺失 + 异常文本降级 + 带/不带 edge）。
  - 前端：5 个工具展开态 `listEntries` 非空、展示 `kind`/`edge`、`filePath` 项可点击调用 `onOpenFile(filePath)` **打开文件**（本期不定位到行）；explore 展开态显示全文。
  - 回归：现有 `web_*` / `search_files` 等 `RESULT_RULES` 不受影响（`projectToolResult` 纯函数、安全降级）；`ListView` 现有 web/file 渲染路径不受影响（onOpenFile 仅 filePath 分支新增点击，其余分支不变）。
  - 冒烟：`test_codegraph_import.py` 通过，后端可正常导入 codegraph 包。

---

## 七、风险与缓解

| 风险 | 缓解 |
|---|---|
| vendor 文本格式随版本漂移，正则失效 | parser 解析失败降级为全文透传 + warn 日志；单测锁定当前格式；vendor 升级时同步更新样例 |
| `ListView` 点击跳转实现踩坑（onOpenFile 未透传/打开机制不符） | 实施时复用现有 `DiffFileHeaderContent` 打开按钮机制；前端单测验证 `renderListEntry` filePath 分支渲染可点击且回调 `onOpenFile` |
| 包式改造破坏导入 | `__init__.py` 强制 re-export + 冒烟测试 `test_codegraph_import.py` |
| `ToolListEntry` 扩展字段未被 `ListView` 渲染 | `renderListEntry` filePath 分支同步展示 `kind`/`edge`（如 `kind · edge` 后缀），非破坏现有分支 |
| 改动误伤其他工具展示 | parser 仅作用于 codegraph 工具；`RESULT_RULES` 只增不改；`projectToolResult` 安全降级 |

---

## 八、与第零铁律的对应小结

- 改动聚焦、职责清晰（新增单一职责 parser 文件、前端只增规则、扩 ListView 跳转），不堆积技术债。
- 不碰 vendor 契约、复用前端 list 机制——不重复造轮子、不越界。
- **扩 `ToolListEntry` 加 kind/edge 而非拼接**（修正 v1 打补丁）——敢扩契约、不将就。
- **本期扩 ListView 支持「点击打开文件」**（修正 v1 保守推迟）——核心收益（跳转打开文件）落地、该大则大；同时**不把「定位到指定行」硬塞进本期**（现有打开机制不支持，强塞会牵连其他文件打开场景、违背改动聚焦），列为后续增强——该小则小。
- 解析失败安全降级 + warn 日志（带 trace_id 回填）——可排查、不丢结果。
- 分阶段（explore 后续）避免一次性大改动引入不可控风险，同时本期即交付可感知的结构化 + 跳转收益。
