# Workspace 级 Skill / MCP 改造验收方案

**状态**：已实现，独立复核通过（P1 已修复）
**日期**：2026-11-08

> 本文是完成门禁，不是实现建议。没有证据的项目不得标记为「通过」；只靠单元测试替身得出、未经真机运行的项目，不得标记真机项通过。

## 1. 验收对象与范围

验收对象是 Skill 与 MCP 从「只有系统级」改造为「系统级 + workspace 级」的全部五个阶段：

1. 后端作用域化：路径、两层合并视图、MCP 测试记录迁出、连接池按作用域键控、workspace stdio 服务 cwd。
2. 目录变更通知统一：`CatalogChange` 取代 `AgentCatalogChange`，覆盖子 Agent / Team / Skill / MCP。
3. Workspace 配置 API 与前端：workspace 路由、两个面板、输入框选择器、统一图标。
4. Skill 提及结构化：`[[cosir-skill:name]]` 由后端校验并在入队时固化正文与目录；`<user_input>` 正文转义。
5. 文档对齐：`docs/mcp-skill-tools.md`、`AGENTS.md`、`tool_ui_display_contract.md`、相关 docstring。

不验收：MCP 返回图片、MCP 卡片展示参数与结果、凭据加密、MCP resources / prompts / OAuth、子 Agent 同名规则（见方案【刻意不做】）。

## 2. 通过规则

| 等级 | 含义 |
|---|---|
| P0 | 硬门槛，任一失败即整体不通过 |
| P1 | 重要质量项，失败必须修复或由用户明确接受 |

整体通过必须同时满足：全部 P0 为 PASS；P1 无未解释失败；后端 `pytest`、前端 `vitest`、`tsc`（`npm run typecheck` 或等价）、`eslint` 全绿或失败已证明与本次无关；至少一名独立子 Agent 按本文逐条复核代码事实与测试证据。

## 3. 目录与路径变更（P0）

| ID | 验收项 | 方法 |
|---|---|---|
| D1 | workspace 布局与系统级一一对应：`<ws>/.cosir/skills/<name>/SKILL.md`、`<ws>/.cosir/config/skills.json`、`<ws>/.cosir/config/mcp_servers.json`；名字常量只在 `app/utils/path/workspace_cosir.py` 定义一次，`system_cosir.py` 复用 | 代码审查 + 单测 |
| D2 | MCP 测试记录只在系统级 `<DATA>/.cosir/config/mcp_tested_servers.json`；`mcp_servers.json`（两个作用域）不再含 `tested_servers` 字段，含该字段的旧文件按格式无效处理（绿地，不做兼容） | 代码审查 + 单测 + 真机 |
| D3 | 新建 workspace 时预建 `<ws>/.cosir/skills/` | 单测或真机 |
| D4 | Skill 导入的暂存目录不在被扫描的 skills 根目录内，导入期间列表不出现「元数据无效」告警 | 代码审查 + 单测 |
| D5 | workspace stdio 服务的 stderr 日志文件名带作用域区分，两个 workspace 的同名服务不写同一个文件 | 代码审查 + 真机 |
| D6 | 文件改名 / 新增与方案一致：`service/configuration/mcp_tested_servers.py`（新）、`runtime_notices/catalog_change.py`（取代 `agent_catalog_change.py`，旧文件删除）、`lib/api/extensions.ts`（取代 `system-extensions.ts`，旧文件删除），无残留引用 | `git diff --stat` + grep |

## 4. 代码逻辑行为

### 4.1 阶段 1：作用域化（P0）

| ID | 验收项 | 方法 |
|---|---|---|
| B1 | Skill 合并视图：系统与 workspace 同名时 workspace 覆盖，列表只出现一次并标 `scope=workspace`、`overrides=true`；不同名两者都出现 | 单测 + 真机 |
| B2 | 每个作用域的启用状态只由本作用域的 `skills.json` 决定；文件缺失时本作用域全部启用 | 单测 |
| B3 | `skill load` 加载 workspace Skill 正文与目录内资源；资源路径越界仍被拒绝；返回内容带 Skill 根目录绝对路径 | 单测 + 真机 |
| B4 | MCP 合并视图：同名 workspace 覆盖系统；只有「enabled 且本机测试记录指纹匹配」的服务对 Agent 可用 | 单测 + 真机 |
| B5 | workspace `mcp_servers.json` 里伪造任何测试相关字段都不能让服务可用（格式拒绝或忽略），必须经本机测试 | 单测 + 真机 |
| B6 | 测试草稿成功即记录指纹；之后保存同一配置立即可用，无需二次测试；保存不同配置则不可用；对已保存配置重测失败撤销可用 | 单测 + 真机 |
| B7 | 连接池按「作用域 + 服务名」键控：两个 workspace 的同名不同配置服务各自持有连接，互不触发 `config_changed` 重建 | 单测 + 真机 |
| B8 | workspace stdio 服务的子进程 cwd 是工作区根目录；系统级服务行为不变 | 单测 + 真机 |
| B9 | `mcp` / `skill` 工具参数 schema 不变（Task 固化 schema 与缓存不受影响）；`execution_context` 为必填 | 代码审查 + 现有 schema 测试 |
| B10 | 系统提示词的 MCP / Skill 目录按 Task 工作区投影两层合并结果 | 单测 |
| B11 | MCP 配置与测试记录的「读-改-写」在 `ConfigurationFileStore.locked` 内完成 | 代码审查 |

### 4.2 阶段 2：目录变更通知（P0）

| ID | 验收项 | 方法 |
|---|---|---|
| N1 | 一个 `CatalogChange` 类型覆盖 agents / agent_teams / skills / mcp_servers 四种目录；`AgentCatalogChange` 及其文件删除，子 Agent 通知内容语义不变（增 / 删 / 描述变化） | 代码审查 + 单测 |
| N2 | 命中规则：Task 已在本进程装配、非隐藏 Agent、固化工具含对应工具（delegate_task / agent_team / skill / mcp）、作用域匹配（系统级变更命中全部 workspace；workspace 变更只命中该 workspace） | 单测 |
| N3 | 被覆盖时不误报：系统级同名项变更时，若该 workspace 有同名项覆盖（Team / Skill / MCP），该 workspace 的 Task 不收到通知 | 单测 |
| N4 | 发布点：Team 增删改、Skill 导入 / 删除 / 启停、MCP 保存 / 测试导致可用集合变化时发布；可用集合未变不发布；通知失败只记日志不让配置请求失败 | 单测 + 真机（看 `task_messages` 或上下文观测） |

### 4.3 阶段 3：Workspace API 与前端（P0 / P1）

| ID | 等级 | 验收项 | 方法 |
|---|---|---|---|
| F1 | P0 | workspace 路由 `/workspaces/{id}/configuration/skills…` 与 `/workspaces/{id}/configuration/mcp-servers…` 可用，workspace 不存在 404，根目录失效 400 | 单测 + 真机 |
| F2 | P0 | workspace 列表返回可见合并结果，每项带 `scope` 与 `overrides`；写操作（启停、导入、删除、MCP 整表保存）只作用于本作用域，不能改动系统级文件 | 单测 + 真机 |
| F3 | P0 | Skill 启停为单项接口；旧的整表 `PUT /configuration/skills/enabled` 删除 | 代码审查 + 单测 |
| F4 | P0 | 工作区设置出现「MCP 服务」「Skills」两个标签；继承自系统的条目只读并标注；面板文案不再写死「系统」 | 真机（浏览器面板截图） |
| F5 | P0 | 输入框 Skill 选择器按当前工作区列出合并结果；系统级变更后各工作区缓存都会失效 | 单测 + 真机 |
| F6 | P1 | Skill 在设置页、面板、输入框、胶囊、调用行使用同一个图标；调用行与列表显示作用域标记 | 代码审查 + 真机 |

### 4.4 阶段 4：Skill 提及（P0）

| ID | 验收项 | 方法 |
|---|---|---|
| S1 | 发送时正文保留 `[[cosir-skill:name]]`，不再物化为 `【Skill: name】`；相关物化 / 还原函数与正则删除 | 代码审查 + 单测 |
| S2 | 后端以单趟扫描识别 file / image / skill 三类 token；Skill 不存在或已停用时以 400 `SKILL_UNAVAILABLE` 同步拒绝 | 单测 + 真机 |
| S3 | 入队时把 Skill 正文与根目录固化进模型正文 `<user_input>`（同一 Skill 去重；正文按工具输出同一预算截断并说明）；`display_text` 保留 token | 单测 + 真机（上下文观测） |
| S4 | `<user_input>` 内用户原文与 Skill 正文都经 `escape_tag`，输入 `</user_input>` 不能破坏边界 | 单测 |
| S5 | 已发送气泡、排队区、冷重建、撤回放回输入框、fork 后胶囊都能正确显示 / 还原；手打 `【Skill: x】` 只是普通文字 | 单测 + 真机 |
| S6 | 侧栏兜底标题去掉 Skill token | 单测 |

## 5. 真机测试环境与场景（P0）

环境：真实后端进程（`uvicorn`，`CODING_AGENT_DATA_DIR` 指向隔离临时目录，不碰用户 `~/.cosir`）；两个临时 workspace；真实 stdio MCP 服务（基于 `mcp` SDK 的脚本，工具返回自身 cwd 与一个配置标记）；本地 OpenAI 兼容假模型服务（按脚本返回文本或工具调用）；前端用 Vite dev 指向该后端，在内置浏览器面板操作（不弹 Electron 窗口）。

| ID | 场景 | 期望 |
|---|---|---|
| R1 | 系统级与 workspace A 各放一个同名 Skill，另放一个仅系统级 Skill | A 的列表 2 项，同名项为 workspace 版本并标覆盖；workspace B 列表为系统版本 |
| R2 | workspace A 的 `mcp_servers.json` 手写一个 stdio 服务 | 未经测试时 Agent `mcp list` 看不到；在 A 的设置页测试后可见 |
| R3 | A、B 各配同名不同参数的 stdio 服务并测试通过，Agent 分别调用 | 返回各自的配置标记与各自工作区 cwd；后端日志无 `config_changed` 互相重建；stderr 日志分文件 |
| R4 | 新建服务「测试 → 保存」 | 保存后立即可用，无需再测 |
| R5 | 在 A 中发送带 Skill 胶囊的消息（假模型直接回复文本） | 上下文观测里 `<user_input>` 含 Skill 正文与目录；气泡显示胶囊；撤回后胶囊回到输入框 |
| R6 | 发送引用已停用 Skill 的消息 | 400 `SKILL_UNAVAILABLE`，草稿恢复 |
| R7 | 某 Task 已运行过一次后，在系统级导入新 Skill / 在 A 测试通过新 MCP | 该 Task 外部队列出现 `catalog_change` 通知；B 的 Task 只收到系统级那条 |
| R8 | 工作区设置页的两个新标签 | 渲染正常，继承项只读，截图存证 |

## 6. 证据记录（实现方自测，待独立复核）

自动化测试（2026-11-09）：

- 后端 `uv run pytest`：2804 passed，5 failed。这 5 个失败在改动前就存在：把改动 stash 掉后，同样的 5 个照样失败，与本次无关。它们是：
  - `test_live_cold_parity::test_a_failure_before_the_response_is_committed_settles_its_calls`
  - `test_model_request_cancellation::…[timeout-reading|timeout-closing]`
  - `test_tool_call_chain_audit::test_t2_disabled_*`（2 个）
- 前端 `vitest run tests/unit`：930 passed。`tsc --noEmit` 通过。
- `eslint` 只有 1 个旧错误，在未改动的 `tests/unit/terminal-query-replies.test.ts`。

真机测试环境：真实后端进程，数据目录隔离；真实的 stdio MCP 探针服务；本地假模型，会把收到的每个请求原样记录下来；Vite dev 直连这个后端，在内置浏览器里操作。

| 场景 | 结果 | 证据 |
|---|---|---|
| R1 | PASS | ws-a 列表：`shared` 为工作区版、`overrides=true`；ws-b 列表：`shared` 为系统版。在 ws-a 启停继承项返回 400，未登记的工作区返回 404。导入后 skills 目录里没有残留暂存目录 |
| R2 | PASS | 手写配置里夹带 `tested_servers` 时，接口返回 400「格式无效」。合法但未测试时，Agent 调 `mcp list` 返回 `[]` |
| R3 | PASS | 任务 1（ws-a）返回 marker A，cwd 为 ws-a；任务 2（ws-b）返回 marker B，cwd 为 ws-b。任务 1 再次调用复用连接：日志里只有 2 次 `mcp_connection_opened`，没有 `config_changed`。stderr 日志是两个文件：`probe.<hash>.stderr.log` |
| R4 | PASS | 通过 API 与界面各走一遍：测试草稿时 `agent_available=false`，保存后 `tested=true`，不用再测。测试记录只出现在 `<DATA>/.cosir/config/mcp_tested_servers.json`，按作用域键记录 |
| R5 | PASS | 假模型收到的最后一条 user 消息里，有 `<skill name="shared">`、ws-a 版正文和绝对目录，`</user_input>` 已转义。快照里保留的是 token。气泡显示胶囊，撤回后胶囊回到输入框。重启后端后，冷重建的气泡仍显示胶囊 |
| R6 | PASS | 引用已停用的 Skill 返回 400 `SKILL_UNAVAILABLE` |
| R7 | PASS | ws-a 的 probe 测试通过后，只有任务 1 收到 `catalog_change(mcp_servers, added, probe)`，并在下一次推进时并入上下文。导入系统级 Skill，任务 1、2 都收到通知；停用系统级 `shared`，只有任务 2 收到，ws-a 有同名覆盖 |
| R8 | PASS | 工作区设置出现「MCP 服务」「Skills」标签。继承项标「系统继承」，界面上没有启停、删除、编辑入口；覆盖项标「覆盖系统同名」 |
| 补充 | PASS | 重启后测试记录仍然生效，ws-b 调用成功。新建 Task 的系统提示词目录：MCP 只有 `["probe"]`（未测试的 `sys-probe` 和被覆盖的系统级 `probe` 都不在），Skill 为合并结果 |

## 7. 独立复核结果与处理（2026-11-09）

独立子 Agent 逐条复核了 D、B、N、F、S 五组共 33 个 ID：P0 全部 PASS，F5、F6 部分通过。它另外起了一套后端和 Vite（端口不同），在浏览器面板里复跑了 F1、F2、F4、F5。

复核发现的问题与处理如下。

| # | 问题 | 级别 | 处理 |
|---|---|---|---|
| 1 | 测试一份未保存的草稿，会顶掉已保存配置的「测试通过」，并误发一条 removed 通知。这是本次引入的回归 | P1 | 已修复：测试记录改为「服务名 → 测试通过的指纹集合」，测试或撤销草稿都不影响已保存配置。回归测试：`test_mcp_test_record_keeps_saved_config_when_a_draft_is_tested`、`test_testing_an_edited_draft_does_not_revoke_the_saved_server`。真机复核：测草稿后，已保存服务仍是 `tested=true` |
| 2 | 测试记录永不清理 | 建议 | 已修复：保存时裁到「仍存在的服务的当前配置」（`keep_only`），删除工作区时 `drop_scope`。回归测试：`test_mcp_test_record_is_pruned_on_save_and_dropped_with_the_workspace`、`test_deleted_server_must_be_retested_when_added_back`。真机复核：删除后加回，`tested=false` |
| 3 | workspace 配置文件损坏时，该工作区的 MCP 整体不可用 | 建议 | 保持失败即关闭，已写进 `docs/mcp-skill-tools.md` |
| 4 | 测试成功、但已保存的文件已损坏时，接口报 500 | 建议 | 已修复：`_is_saved` 就地按「未保存」回答。测试：`test_successful_test_with_a_broken_saved_file_is_not_a_server_error` |
| 5 | 通知的「变更前」快照与写入不在同一把锁里 | 建议 | 接受：只影响通知旁路，属于低概率并发 |
| 6 | 调用行没有作用域标记（F6） | P1 | 已修复：load 与 call 的 `display_data` 带 `scope`，调用行标「工作区」，契约文档已同步 |
| 7 | `docs/api/openapi.json` 未重新生成 | 建议 | 已重新生成。这份文件原本就落后于代码，所以 diff 里也包含了与本次无关的历史差异 |
| 8 | 旧代码里还有 3 处直接拼 `".cosir"` | 建议 | 已收敛（后续追加）：`workspace_fileignore_configuration_service.py`、`workspace_files/browser_visibility.py`、`workspace_files/file_watch.py` 改走 `workspace_cosir_dir`；`file-browser.json` 也收进 `workspace_file_browser_settings_file`。现在 `app/` 下只有 `workspace_cosir.py` 拼 `.cosir`。相关 246 个用例通过 |
| 9 | 前端有两处重复定义 | 建议 | 已合并：`replaceSkillMentionsWithNames`、`mcpServerConfiguration` 各只保留一份 |
| 10 | 通知里的描述可以写出 `</system_notice>` | 建议 | 已修复：描述先 JSON 编码，再转义两层外框标签。测试：`test_descriptions_cannot_forge_notice_boundaries` |
| F5 | 缺少缓存失效范围的单测 | — | 已补：`tests/unit/extension-query-keys.test.ts` |

修复后的测试结果：

- 后端：2809 passed。仍有 5 个失败，就是改动前已有的那 5 个。
- 前端：`vitest` 931 passed，`tsc` 通过，本次改动文件的 eslint 无错误。
