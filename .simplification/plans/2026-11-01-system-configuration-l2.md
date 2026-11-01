# L2 精简方案（扩大审查范围后）：系统配置页

接 `2026-11-01-system-configuration.md`。原计划的两条 L2 链路只看了前端；本次沿调用链读到后端路由、service、注册表、系统提示词装配与工作台草稿，三个只读子 Agent 分方向调查，主 Agent 已抽查全部关键论断。

**结论先行**：前端看到的重复大多是症状。根因分三层，按第零铁律在根因处解决：
1. **前端编辑机制的契约太窄**：单文本编辑 hook 被声明成「Markdown 专用」，校验规则写进了草稿状态机，别的文本配置用不上，只好各写一份。
2. **「按作用域保存配置」没有归属模块**：路由与失效散在宿主组件里；子 Agent 的共用编辑器抽取停在表单层。
3. **后端契约有多事实源与过时字段**：Team 的作用域有多个来源；可见性规则在前后端各一份且说法相反；`effective_on` 过时且取值错误。

方案按「行为不变的精简」与「有意改变行为的结构性改造」分开：前者属于精简标准的 L2，后者按标准 §6.3 交给第零铁律流程，需要你逐项拍板。

---

## 一、调查结论与问题清单

| # | 现象 | 证据 | 根因层级 | 置信度 |
|---|---|---|---|---|
| P1 | 单文本配置的草稿规则（草稿跟随服务端、保存期间新编辑保留、dirty、写回缓存）写了 3 份 | hook `use-markdown-configuration-document.ts:45,55-58,70,90`；`.fileignore` `workspace-configuration-page.tsx:39-53`；终端规则 `terminal-denylist-configuration-panel.tsx:60-70` | 前端机制层：hook 只读 `content`，却把类型约束绑在 Markdown 的 token 字段上（`lib/markdown-document.ts:7-12`），日志事件名与文案写死 | 高 |
| P2 | 「空内容禁止保存」两处缺省语义相反；快捷键保存绕过超预算禁用 | hook `:52` 与面板 `markdown-configuration-panel.tsx:52`；快捷键 `:37-42` → hook `save :80-83` | 同上：校验规则混进状态机，按钮与快捷键各判各的 | 高 |
| P3 | 子 Agent「作用域 → 端点」写在 3 个宿主里；Team 已在 API 层一处收敛 | `system-configuration-page.tsx:42-47`、`workspace-configuration-page.tsx:68-73`、`workbench/agent-configuration-draft-surface.tsx:40-45`；对照 `lib/api/agent-teams.ts:90-99` | 前端 API 层：用例没有归属 | 高 |
| P4 | 「保存后失效哪个列表」写了 4 处；系统级 Team 变更只失效系统列表，工作区列表（含系统项）最长 30 秒显示旧数据 | 子 Agent 面板 `:75`、草稿 `:50-51`；Team 面板 `:117,149,160`、草稿 `:61`；`agent_teams_api.py:140-151`、`query-client.ts:23` | 前端 query 层：失效规则无归属 | 高 |
| P5 | 作用域有两种前端编码（`workspaceId \| null` 与 `scope + workspaceId`），换算写了 5 处；同一字面量联合写 4 次 | `agent-configuration-panel.tsx:62`、草稿 `:51`、Team 面板 `:114`、`agent-team-editor/session.ts:66-71`；`agent-teams.ts:4,34,90,124` | 缺一个作用域值类型（只限子 Agent 与 Team 两种两级配置） | 高 |
| P6 | 子 Agent 保存主干在面板与工作台各一份；Team 两入口共用编辑器 | `agent-configuration-panel.tsx:68-97` 对 `agent-configuration-draft-surface.tsx:46-61`；Team `configuration-editor.tsx`；git：Team 编辑器 92003f4f 起即共用，子 Agent 抽取 a6ce4be7 停在表单层 | 演化遗留，不是有意设计；`agent-editor/session.ts:57` 称「与 TeamEditorSession 同形」与事实不符 | 中高 |
| P7 | Team 作用域有多个事实源：目录、文件里的 `scope` 字段（缺省 `workspace`）、路由、请求体、`configuration.scope` | `agent_team_configuration.py:87-90`；`agent_teams_api.py:53,111,161,196`；service `:86,115` | 后端契约 M7 | 高（触发概率低） |
| P8 | 工作区可见性规则前端写了一份，说法与后端相反 | 前端 `agent-team-editor/agent-profiles.ts:7-10`「工作区覆盖系统」；后端 `agent_profile_registry.py:178-190` 系统优先、冲突拒绝 | 后端列表契约不对称：工作区 Agent 列表不合并系统项（`list_workspace_sub_agents`），Team 列表合并 | 高（目前无可观察影响） |
| P9 | 生效时机没有归属：后端 `effective_on="next_run"`，前端文案「实时生效」，两者都不准 | 系统提示词每个 Task 首建后固定（`runtime_message_context_manager.py:77-92`）；变更经广播追加通知，只投给正在执行的 Task（`system_prompt_update_broadcaster.py:99-101`，弱引用）；`effective_on` 在 87159f0d 加入，早于广播 9cb7c0bc | 后端契约过时，前端文案无事实源 | 高 |
| P10 | 「读旧内容 → 写入 → 生成 delta → 广播」写了 3 份，广播失败时文件已写但用户收到错误 | `global_instructions.py:36-47`、`main_agent_prompt.py:36-48`、`workspace.py:137-149`；`runtime_notice_broadcaster.py:41-43` 要求调用方降级 | 后端 API 层 M1 | 中 |

**同类出现点与不动的理由**：
- 系统级与工作区 AGENTS.md：文件定位、字段、广播来源都不同，属于 M2，不并进「带作用域的配置」。
- 只有一级的配置（MCP、Skills、环境变量、主 Agent 提示词）：不引入作用域抽象。
- 后端五个文本配置 service：校验各不相同（token 预算、gitignore 语法、正则），属于 M2；已共用 `ConfigurationFileStore` 与 `raise_configuration_error`，不加基类。
- 子 Agent「系统优先」与 Team「工作区覆盖」是不同的业务规则，不合并成通用可见性逻辑。

---

## 二、路线与阶段

每个阶段都能单独验证、单独合入。

### 阶段 A：行为不变的精简（精简标准 L2，可在下一轮执行）

| 步 | 内容 | 解决 | 等价论证要点 | 需先补的测试 |
|---|---|---|---|---|
| A1 | 前端作用域值类型 `ConfigurationScope`（判别联合，只用于子 Agent 与 Team）；子 Agent 与 Team 的 API 各一个按作用域拼路径的函数；查询键与列表查询选项按作用域取；删掉 `AgentConfigurationApi`、两个页面的 adapter、草稿面板里的分支、`agent-teams.ts:95` 的运行期 throw 与 Team 面板 `:145` 的多余 scope 覆盖 | P3、P5 | 请求 URL、方法、请求体逐一不变；查询键取值不变（`workspaceId ?? "system"`）；throw 不可达改由类型保证 | `configuration-api.test.ts` 补工作区级路径断言 |
| A2 | 文本配置编辑 hook 改为通用「单文档编辑」：类型约束放宽为 `{content}`；adapter 提供校验函数、标签、成功文案与日志事件名（三个现有事件名原样保留）；`.fileignore` 改用它；删掉 `allowEmpty` 的两处表达，按钮与快捷键共用 hook 算出的「能否保存」 | P1、P2（缺省语义相反） | 草稿、dirty、写回缓存逐项同一套规则；日志事件名、提示文案不变 | `.fileignore` 面板特征测试；hook 的空内容与预算单测 |
| A3 | Markdown 编辑器与快捷键模块的死参数和私有冗余（原计划 #12–#16） | — | 见原计划 | 已有单测覆盖 |

A 阶段**有一处会顺带改变行为，需你确认**：A2 让快捷键与按钮共用同一个「能否保存」，超预算时按快捷键将不再发请求（原来发出后被后端 400 拒绝）。我建议接受，它消除的是一个缺陷。若不接受，A2 保留快捷键现有行为。

终端规则面板**不在 A2 里合并**：它保存期间禁用输入框、内容是数组与文本互转，合并会改变「保存期间能否继续编辑」，放到 B。

### 阶段 B：有意改变行为的结构性改造（按第零铁律，逐项拍板）

| 步 | 内容 | 解决 | 改变的行为 | 需要你决定 |
|---|---|---|---|---|
| B1 | 「保存到某作用域并失效」收进 lib 层一处（子 Agent 与 Team 各一个）；系统级变更按前缀失效所有工作区列表 | P4 | 系统级 Team 改动后工作区列表立即刷新，不再最长 30 秒旧数据 | — |
| B2 | 子 Agent 补共用编辑器，对齐 Team：编辑器管保存、提示与日志，宿主只注入持久化与保存后行为 | P6 | 两入口的成功提示与日志事件名统一 | 统一成哪一套文案与事件名（受 E2E 保护的「配置更改成功」要不要改） |
| B3 | 后端 Team 作用域只认路由：请求体与配置文件去掉 `scope`，响应里的 `scope` 由所在目录投影；API 边界只解析一次作用域，顺带修掉 `agent_teams_api.py:145` 绕过工作区上下文的旁路 | P7 | Team 文件格式与请求体变化（绿地项目，旧文件可删） | — |
| B4 | 工作区子 Agent 列表与 Team 对齐：返回合并后的可见项并带拥有者作用域；前端删掉 `visibleWorkspaceProfiles` | P8 | 工作区子 Agent 列表开始显示继承来的系统项，需像 Team 面板那样区分本地与继承 | 界面是否接受显示继承项 |
| B5 | 删除 `effective_on`（前后端）；生效文案按事实改写，并给工作区 AGENTS.md 补上 | P9 | E2E 断言的「保存后实时生效」文案变化 | 文案措辞（建议：「正在运行的任务在下一次请求前收到变更，新任务直接使用，不影响前缀缓存」）；另一个产品问题见下 |
| B6 | 后端「保存 → 广播」收成一处，广播失败时记日志、请求仍成功；终端规则面板并入通用编辑 hook | P10、P1 剩余部分 | 广播失败不再报错；终端规则保存期间可继续编辑 | — |

---

## 三、影响范围

- **前端**：`lib/api/configuration.ts`、`lib/api/agent-teams.ts`、`lib/query/query-keys.ts`、`lib/query/query-options.ts`、`lib/agent-editor/session.ts`、`lib/agent-team-editor/`（session、agent-profiles、layout-storage）、`components/configuration/`（agent 面板、markdown 面板与 hook、编辑器、快捷键）、`components/agent-team/configuration-panel.tsx`、`components/workbench/` 两个草稿面板、两个配置页、`terminal-denylist-configuration-panel.tsx`。
- **后端（只在 B3–B6）**：`api/configuration/` 三个路由、`api/agent_teams_api.py`、Team 配置模型与 registry、两个配置 service、`workspace_configuration_context.py`、响应 schema、广播入口。
- **测试**：前端单测 `configuration-api.test.ts` 与新增的 hook、`.fileignore` 测试；E2E `system-configuration.spec.ts`（文案）、`agent-team-session.spec.ts`（请求体）；后端配置 service 与 Team 定义的测试，工作区路由补 HTTP 层测试。
- **文档**：AGENTS.md 前端架构里关于配置的描述、工具 UI 契约无影响。

## 四、刻意不做

- 不给全部配置做作用域框架或注册表；作用域值类型只用于子 Agent 与 Team。
- 不合并两个编辑会话（子 Agent 平铺表单与 Team 图、撤销、布局差异大），只对齐保存契约。
- 不改 `/workspaces/{id}/...` 这种 URL 嵌套。
- 不加后端文本配置的 service 基类。

## 五、记入问题清单、不在本方案处理

- **已有但当时空闲的 Task 收不到提示词变更**：广播只投给正在执行的 Task，空闲、挂起、未物化的 Task 再运行时用的仍是旧提示词。要改需从数据库按作用域找 Task 写持久化通知，跨层；需要你决定是接受并写进文案，还是另行立项。
- 主 Agent 提示词：读侧把空内容当合法的「未配置」，写侧禁止空内容，用户无法在界面上恢复成未配置。
- `.fileignore` 规则数上限：保存时数有效规则行，运行时数原始行（`ignore_rules.py:251`），保存能过的文件运行时可能被截断。
- 终端规则的条数与长度在请求 schema 与编译时各查一次，用户只看到笼统提示。
- `/workspaces/*/configuration` 的请求校验错误返回 422，系统级返回 400 定制形状。
- 子 Agent 首次保存进行中仍可改 `agent_id`；子 Agent 面板保存后无条件关闭，会丢掉保存期间的新输入。
- 原计划「发现的缺陷」中的 MCP 草稿测试结果、工作区加载文案、环境变量未知字段。
