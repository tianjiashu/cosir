# 精简计划（关卡一）：系统配置页

- 标准版本：1.3
- 本轮基准：`755400b4`；对比基线：本轮范围 23 个文件均无未提交改动，以 HEAD 为基线（工作区其他文件有他人未提交改动，执行方式待人类选择）
- 本轮种子：`apps/desktop/components/system-configuration-page.tsx`（人类指定）
- 扩散范围：沿调用链到它挂载的全部面板（`settings/`、`configuration/`、`agent-team/configuration-panel.tsx`、`terminal-denylist-configuration-panel.tsx`、`environment-configuration-form.tsx`、`model/`）；沿同一事实源到 `workspace-configuration-page.tsx`（同一批面板的工作区作用域）、`lib/api/configuration.ts`、`lib/query/query-keys.ts`、`query-options.ts`。共 23 个文件、3950 行
- 读范围大于改范围：工作台草稿面（`workbench/agent-*-draft-surface.tsx`）、`assistant-ui/model-selector.tsx`、`lib/api/agent-teams.ts` 只读，作为 M1 的另一侧
- 基线检查：渲染进程与宿主类型检查、lint、单测（109 文件 / 694 用例）全部通过
- 检测器：`run-all` 在范围内只报 3 条 F1（均已人工确认，见下）；其余候选来自三个只读子 Agent 分方向阅读与业务不变量枚举，主 Agent 已抽查关键证据

## 本轮建议只做两条链路（种子太大，按 §6.2 只取知识重复最集中的两块）

其余链路列在文末「下一轮候选」，不在本轮执行。

---

## L1：直接执行

| # | 变换 | 位置 | 依据 |
|---|---|---|---|
| 1 | D10 | `system-configuration-page.tsx:190-193` | 注释说「保存 / 删除后强制刷新模型目录」，对应的 `onChanged` 回调早已删除，刷新来自共享查询缓存 |
| 2 | D1 | `system-configuration-page.tsx:156` 的 `as Record<…>` 与 `:9` 的 `EnvironmentFieldValue` 导入 | 推断类型本就可赋值，断言无效；删后导入无人使用 |
| 3 | D10 | `environment-configuration-form.tsx:29`、`:149-150` | 「未知控件不会降级为输入框」与 `:71-101` 不符；「调用方可复用实现不同保存策略」是预留扩展的说法，只有一个调用方 |
| 4 | D2 | `markdown-source-preview-editor.tsx:17` `export type MarkdownEditorMode` | 只在本文件使用，去掉 export |
| 5 | D3 | `markdown-source-preview-editor.tsx:36,39` 私有组件里的 `readOnly`、`ariaLabel` 默认值 | 外层总会传入，默认值不可达；文案字面量重复两次 |
| 6 | D6 | `model/model-selector.tsx:391` `value !== undefined` | `value: string` 必填，条件恒真（检测器已报） |

## L2：待确认（按链路分组，可整组通过或否决）

### 链路一：子 Agent 配置的作用域（M1，表达点 4 → 1）

| # | 变换 | 位置 | 等价论证要点 |
|---|---|---|---|
| 7 | M1 | `lib/api/configuration.ts:99-113` 与 `:150-164`（两组函数只差路径前缀）；`query-options.ts:24-26`；`system-configuration-page.tsx:42-47`；`workspace-configuration-page.tsx:68-73`；`workbench/agent-configuration-draft-surface.tsx:40-45` | 子 Agent 增删改查改为接收 `workspaceId: number \| null`，由一个路径函数生成前缀；删掉两个手工 adapter、查询选项里的分支与工作台里的分支，面板只收 `workspaceId`。请求 URL、方法、返回值与拒绝语义逐一不变；单测 `configuration-api.test.ts` 已断言系统级路径，补工作区级断言。Team 侧已用同样方式收敛（`agent-teams.ts:90-99`），不照搬它 `:95` 的运行期 throw |

### 链路二：单文本配置文档（Markdown 与 .fileignore）

| # | 变换 | 位置 | 等价论证要点 |
|---|---|---|---|
| 8 | M1 + D3 | `use-markdown-configuration-document.ts:17,52` 与 `markdown-configuration-panel.tsx:52` | 「空内容禁止保存」两处表达且对缺省值的理解相反（hook 缺省禁空，面板缺省允许）。`allowEmpty` 改必填，hook 算一次谓词交给面板。三个 adapter 都已显式传值，行为不变。先补特征测试 |
| 9 | M3 | `workspace-configuration-page.tsx:32-62`（.fileignore 面板）对 `use-markdown-configuration-document.ts:41-96` | 草稿跟随服务端、保存期间新编辑保留、dirty 公式、写回缓存、编辑时清错，逐项相同。hook 的文档约束放宽为 `{content}`（它只读 `content`），.fileignore 改用 hook。日志事件名 `workspace_fileignore_save_failed` 属受保护可观测性，经参数保留。.fileignore 无任何测试，先补特征测试。连带消除 #10 的守卫 |
| 10 | F1 | `workspace-configuration-page.tsx:42` `savedContent !== undefined` | 草稿只能在 `savedContent` 已定义后经输入框写入；随 #9 一并消失 |
| 11 | D6 | `workspace-configuration-page.tsx:97,98` 内层 `key={workspaceId}` | 路由已给整页加 `key={workspaceId}`（`router.tsx:68`），内层 key 恒定；注释 `:65` 改为说明保证在路由 |
| 12 | D3 + D6 + D10 | `markdown-source-preview-editor.tsx` 的 `readOnly`（导出组件）；`markdown-editor-shortcuts.ts:237` 的 `view.state.readOnly` 与 `:233` 注释 | 两个调用方都不传 `readOnly`，全仓无其他使用；只读场景由 `readonly-text-viewer` 承担。CodeMirror 默认即可编辑，effect 仍只在挂载时执行 |
| 13 | D3 | `markdown-source-preview-editor.tsx` 的 `minHeight` 默认值、`onChange?` 可选 | 两个调用方都显式传入 |
| 14 | 类型 | `markdown-configuration-panel.tsx`、`use-markdown-configuration-document.ts` 的泛型 `TDocument` | 两处只读 `content` 与 `max_tokens`，泛型不带来任何约束；只改类型 |
| 15 | M3 | `markdown-editor-shortcuts.ts:9,111-121` | 三种块前缀的正则在 `hasBlockPrefix`、`removeBlockPrefix`、`LIST_LINE_PATTERN` 各写一遍，收成一张表；`listContinuationPrefix` 对引用要求 `>\s+`，语义不同，不并入。单测已覆盖 |
| 16 | M4 / D6 / F1 | `markdown-editor-shortcuts.ts:11-15,37-102,105,134,219` | 私有转调内联、调用点已判空后的重复判空、`/^\s*/` 必定匹配后的 `?? ""`、选区至少一行时的长度判断。单测已覆盖 |

## L3：只报不做（需人类决定是否另行立项）

| # | 位置 | 说明 |
|---|---|---|
| L3-1 | 界面文案 `system-configuration-page.tsx:216-217`「保存后实时生效」 对 后端响应 `effective_on="next_run"`（`GlobalInstructionResponse.py:34` 等） | 两边说法矛盾，前端也不读这个字段。需要你确认哪边是事实，再统一文案或删字段 |
| L3-2 | `model-config-manager.tsx:271` 状态图标；`ModelConfig.enabled`、`api_key_configured` | 后端列表只返回启用配置、且不允许空 key，图标恒为绿色；`enabled` 前端从不写入，是一条死链（D8）。删字段还是补启停能力，由你定 |
| L3-3 | 后端响应里前端无人读取的字段 | `token_length`、环境变量的 `default/disk_value/configured/source/process_override`（E2E 还断言这些文案不显示）、`.fileignore` 的 `exists/rule_count/max_rules`、工作区指令的 `relative_path/exists`、子 Agent 的 `file_name`（D8，跨进程） |
| L3-4 | `agent-configuration-form.tsx:28` | 硬编码后端工具组的中文名来决定哪些组不能授予子 Agent；后端也有一份（跨进程 M1） |

## 发现的缺陷（只记录，不在本轮修）

1. **MCP 草稿测试结果挂到已保存服务的卡片上**：`mcp-configuration-panel.tsx:130-136` 的 hook 级 `onSuccess` 对编辑框里的草稿测试也生效，以草稿名写入 `lastTests`，放弃修改后卡片仍显示「本次测试成功」。
2. **快捷键绕过超预算拦截**：超预算时只禁用了按钮（`markdown-configuration-panel.tsx:75`），Cmd+S 走 hook 的 `save`（`use-markdown-configuration-document.ts:80-83`），照样发请求，由后端拒绝。
3. **文案错误**：工作区作用域加载子 Agent 时也显示「正在读取系统配置…」（`agent-configuration-panel.tsx:141`）。
4. **环境变量未知字段守卫无效**：`system-configuration-page.tsx:128-130` 对已下线字段仍带上字段名，后端 `environment_configuration_service.py:175-177` 遇到未知字段会让整个请求失败。只在开发期字段目录变化时触发。
5. **布尔初值可能判错（存疑）**：`system-configuration-page.tsx:72` 用 `Boolean(field.value)`，对字符串 `"false"` 为真；与另外三处 `v === true || v === "true"` 规则不同。目前没有布尔字段。
6. **日志策略不一致**：Markdown 的校验错误按 ERROR 记录，子 Agent 的同类错误跳过。

## 下一轮候选种子（按知识重复与死代码密度排序）

1. **`model/model-selector.tsx`**：从 assistant-ui 复制来的泛型组件带大量无人使用的选项（`icon`、`disabled`、`efforts: true`、无搜索模式、cva 的 `muted`/`lg` 变体等）；两个私有薄包装；推理强度标签放错位置；默认推理强度 `"high"` 在 `assistant-ui/model-selector.tsx:66,121` 写了两遍（M1）；docstring 多为英文。
2. **子 Agent / Team 保存流程**：面板与工作台各写一份保存流程（M3），Team 的 persist 与 profiles 推导两份（M1），`unsaved-guard.tsx` 的死参数与离开确认重复。
3. **环境变量**：布尔真值判定 3 处（M1）；编辑器从页面文件拆出、归位到配置目录。
4. **目录归位**：配置中心同时用 `settings/` 与 `configuration/` 两个目录名，另有 4 个文件散在 `components/` 根目录（S1/S8）。
5. **MCP 面板**：两处死写 / 死字段（`jsonDraft` 初始写入、`ServerDraft.headers`）、三处相同的解析兜底。
6. **`query-keys.ts:15` 的 `fileResource`**：全仓无人使用（D2）。

## 标准与 skill 修订建议

- SKILL 写的是 `run-all --json`，本项目的 `run-all.mjs` 用 `--out` 输出候选文件，两边接口不一致。建议 SKILL 改为「`run-all` 输出机读候选清单（参数以项目索引为准）」。
