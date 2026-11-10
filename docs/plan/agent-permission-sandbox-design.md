# Agent 权限档位与沙箱

> 状态：2026-11-09 设计稿，未实施。取代同日 DeepSeek 起草的「终端沙箱」稿（原名 `terminal-sandbox-design.md`，未提交版本另存，不进仓库）与更早的 srt sidecar 稿（git 历史）。
> 前置：[`terminal-ownership-redesign.md`](terminal-ownership-redesign.md)——Agent 不再有常驻 shell，命令只经 `execute_terminal` 起进程；人的事在人的终端做。**本稿的 P2（命令进沙箱）必须在终端改造之后发布**，否则 Agent 的常驻 shell 是一条无沙箱旁路。
> 参考：Codex（`codex-rs`，commit `ac91c14`）、sandbox-runtime（commit `98a4016`），借鉴清单见附录。

## 1. 要什么

**完成标准**：用户在输入框旁选一个权限档位（仅可查看 / 工作区内修改 / 完全权限 / 自动审查 EXP / 自定义），Agent 在档位内照常干活、不打扰；越界时停下来问，用户批准后就执行这一次调用。

**最不想看到**：

- 沙箱让 Agent 频繁失败或频繁弹窗，用户最后只敢开「完全权限」。
- 看似隔离、实际没隔离（某条通道绕过档位；沙箱不可用时悄悄裸跑）。
- 档位、审批、受保护路径各有一套事实，改一处漏一处。
- 用户自己的操作被沙箱拦住。

**本期防什么**：Agent 失误造成的破坏——写到工作区外、写进「下次自动执行」的文件、读到 cosir 自己的机密。**不防注入后外传**：浏览器、`web_extract` 本身能把数据带出去，单封终端网络换不来安全（见 §13）。这一点写进档位说明与提示词，不让人误以为已隔离。

## 2. 模型：档位是四个维度的预设

照 Codex 的分解（`utils/approval-presets/src/lib.rs:28-61`）：**档位 = 文件系统规则 × 网络 × 越界时怎么办 × 谁来批**。内置档位只是预设组合，自定义档位是用户存的同结构预设，没有特例。

| 档位 | 文件读 | 文件写 | 命令网络 | 越界时 | 审批人 |
|---|---|---|---|---|---|
| 仅可查看 | 全盘（受保护除外） | 无 | 关 | 问 | 用户 |
| 工作区内修改（默认） | 全盘（受保护除外） | 工作区 + 临时目录（受保护除外） | 开 | 问 | 用户 |
| 完全权限 | 不限 | 不限 | 开 | —（不越界） | — |
| 自动审查（EXP，P5） | 同「工作区内修改」 | 同左 | 开 | 问 | 模型 |
| 自定义 | 声明 | 声明 | 声明 | 问 / 拒绝 | 用户 / 模型 |

- **越界时**：`问` 走审批（§5）；`拒绝` 直接返回错误，适合无人值守的自定义档。
- **审批人**只决定谁回答「问」，不改变进程能力。自动审查因此只是换一个回答者，不另开链路。
- **受保护路径**不属于任何档位，全局生效（§6）。
- **网络**本期只有开 / 关两态：macOS 上「关」就是 SBPL 不放行网络，几乎零成本；域名白名单要本机代理，放到后面（§13）。

**不变式**

1. 档位只约束 **Agent 发起的动作**。人的终端永远不进沙箱，包括你点回复里命令块的「运行」、或在 Agent 填入后按回车执行的命令（终端改造稿 §3.3、§3.4）。
2. Agent 发起的进程只有一个出口 `execute_terminal`，一律按档位起进程；没有绕过档位的工具、参数或终端。
3. 沙箱不可用时**不裸跑**：退化为「每条命令都要问」（§8）。

## 3. 生效粒度：随 Run 冻结

- `ConversationRunOptions` 增加 `permission_mode`（内置档 id 或自定义档 id），随 `RunLaunch` 冻结（`apps/backend/app/models/conversation_run_options.py`、`core/workflow/message_queue/message_bodies/user_input.py:27`），与 `reasoning_effort` 同构：活动 Run 期间前端锁定选择器，新 Run 用新档位。
- 驱动开始时把档位解析成**策略快照**（只读值对象）放进 `RuntimeContext`，经 `ToolExecutionContext` 交给门禁与工具；配置文件中途被改，下一次驱动才生效。
- 运行中想放宽不用改档位：越界时批准「本 Task 内允许」（§5）。
- 前端选择器的「上次选择」是 localStorage 偏好（同模型选择）；首次默认取配置 `default_mode`。

**Agent 消息开的 Run 取哪个档位**（委派、fork、Team 节点、`send_message`）：现在这些路径只透传模型与推理强度（`core/tool/multi_agent/delegate_task.py:189-205`、`agent_message.py:43`、`send_message.py:164`），要补档位：

- 新派生的 Task（委派、Agent fork、Team 节点）取发起方当前 Run 的档位。
- 已有 Task 被 `send_message` 唤起新 Run 时，取「接收方上一个 Run 的档位」与「发送方档位」中**更严**的一个——否则宽档 Agent 能借另一个 Task 提权，严档 Task 也会被别人放宽。严宽只在内置档间有序（仅可查看 < 工作区内修改 ≈ 自动审查 < 完全权限）；涉及自定义档不可比时取「仅可查看」。
- 批准范围（grant）不随派生继承。

## 4. 两个执行点

同一份策略快照，按动作的性质落到两处：

| 通道 | 执行点 | 本期规则 |
|---|---|---|
| 读类文件工具（`read_file` / `search_content` / `find_files` / `list_directory` / `read_image`） | 工具自身（`PathResolver`） | 受保护读路径一律拒绝，与档位无关；目录类工具从上层目录遍历时跳过受保护子树 |
| 写类文件工具（`write_file` / `patch_write` / `apply_patch` / `delete_file` / `move_file`） | **准入门禁** | 目标在可写区 → 放行；否则按「越界时」问或拒 |
| `execute_terminal` | **OS 沙箱**（worker 起进程时） | 按策略起进程；调用显式请求越权时先问（§5） |
| MCP 工具 | 准入门禁 | 「仅可查看」下问；其余档放行。MCP 服务进程本身不进沙箱 |
| 读用户终端、`terminal_write` 填入、回复里的命令块 | — | 都不执行任何东西（执行由人触发），不受档位约束 |
| 浏览器、`web_search` / `web_extract` | — | 本期不受档位约束（§13），档位说明里写明 |

**为什么文件工具走门禁而不是沙箱**：文件工具在后端进程内执行，OS 沙箱管不到；而它们的目标路径在执行前就能从参数算出来，门禁能在执行前判定、越界时当场问，比「执行失败再申请」少一个来回。命令的影响无法从参数推出，只能由 OS 沙箱在运行时兜住。

**门禁怎么知道一次调用要访问什么**：`ToolDefinition` 声明访问类别（读 / 写 / 执行 / 外部）；写类工具另提供「从已校验参数取目标路径」的纯函数（`apply_patch` 从补丁文本解析目标）。门禁在现有三段检查（`core/tool/execute/tool_access_gate.py:91`：注册表 → Agent 工具权限 → 图片能力 → 参数校验）之后加第四段「权限」，输出 放行 / 要决定 / 拒绝。

**契约随之变化**：写类工具今天在 `PathResolver.resolve_within_workspace` 里自己强制「必须在工作区内」（`write_file.py:117`、`apply_patch_tool.py:145` 等 7 处）。改造后「工作区内」是档位规则，归门禁；`PathResolver` 只守与档位无关的不变量（受保护路径、工作区 `.cosir` 保留区、设备路径），否则「批准写工作区外」会被工具自己再拒一次。

## 5. 越界与审批

### 5.1 怎么发起

- **文件工具**：门禁按目标路径判定，模型不用多写参数。
- **命令**：只有模型知道一条命令需要沙箱外的能力（写 `~/.npmrc`、装全局包），所以由它在调用上声明——`execute_terminal` 增加 `sandbox: "default" | "escalated"` 与 `justification`（一句给用户看的理由）。照 Codex（`core/src/tools/handlers/shell_spec.rs:230-264`）。批准后**这一条命令**不进沙箱执行。
- 不做独立的「申请提权」工具：那样要「执行失败 → 申请 → 重跑」三个来回，批准的也不是那条命令本身。

### 5.2 复用 HITL，改一处判定

现有 HITL：工具声明 `user_decision="execute"` → 首次调用返回等待占位 → 用户决定入队 → `prepare_input` 确认决定后绕过模型重放原调用（`core/workflow/nodes/prepare_input_node.py:107-113`、`service/conversation/tool_decision_service.py`）。权限审批是同一件事，区别只是「要不要决定」不再由工具静态声明，而由门禁逐次判定：

1. 门禁判定「要决定」→ 执行器直接返回等待占位（`tool_awaiting_decision`），占位 metadata 记 `approval: "permission"`，展示数据是通用审批卡（`kind: "permission-request"`：动作、目标、理由）。
2. 决定入口照旧按 `tool_call_id` 寻址；`DecisionBody` 带上 `approval` 来源，`prepare_input` 的「是否绕过模型重放」改为：工具声明 `execute` **或** 这是权限审批。
3. 重放时门禁看到本调用的批准决定 → 放行（命令按越权执行、写入放行该路径）；驳回 → 返回「用户拒绝」错误观察，模型换做法。
4. 「要决定的调用必须单独成批」（`core/workflow/node_helper/tool_batch_runner.py:54`）保留，判定函数 `_requires_user_decision` 改为调用门禁的同一纯函数——判定在执行前就能做出，规则不变。

### 5.3 批准范围

审批卡三个按钮：**批准本次** / **本 Task 内允许** / **拒绝**。

- 「本 Task 内允许」的键：命令按「规范化命令文本 + workdir」精确匹配（Codex `ApprovalStore`，`core/src/tools/sandboxing.rs:42-118`）；文件写按目标路径。
- grant 归进程内 `PermissionGrantRegistry`（按 `task_id`），不持久化、不跨重启，Task 删除时清理——与交接登记表同类。
- 不做「永久记住」：永久规则只能改配置（§7）。

### 5.4 没有用户界面的 Agent

子 Agent 与 Team 节点（`models/task_kind.py` 的 `TASK_KINDS` 规定它们不承接用户界面）遇到「要决定」：审批人是用户时直接拒绝，错误里写明「需要用户批准，请报回上级」；审批人是模型时交给自动审查（P5）。

## 6. 受保护路径：一份事实，三处消费

新模块 `app/core/permission/protected_paths.py` 是唯一事实源，消费方：门禁（写判定）、`PathResolver` 的读路径（读拒绝）、策略快照（交给 worker 编译进沙箱）。浏览器 `upload`（`tools/tool_handler/browser/act.py:89-107`）经 `resolve_for_read` 自然跟上。`search_content` / `find_files` 走的是 `resolve_without_boundary`（`search_content.py:76`、`find_files.py:66`），不经 `resolve_for_read`，而且会从上层目录递归进受保护目录——读拒绝要放在两条解析路径共用的地方，遍历时也要剪掉受保护子树。

| 类别 | 内置条目 | 依据 |
|---|---|---|
| 禁读（所有档，含文件工具） | 系统 `.cosir`，`skills/` 除外 | 有 `.env`（模型 Key）、业务库、日志、MCP 配置；`app/utils/path/system_cosir.py` 是布局事实源 |
| 禁写（所有沙箱档） | 系统 `.cosir`；工作区 `.cosir`；`.git/hooks`、`.git/config`；`.gitconfig`、`.gitmodules`；shell rc（`.bashrc` `.bash_profile` `.zshrc` `.zprofile` `.profile`）；`.envrc`；`.vscode`、`.idea` | 「下次自动执行」的文件；照 sandbox-runtime 强制清单（`src/sandbox/sandbox-utils.ts:21-50`），加 `.envrc`（direnv 在用户无沙箱终端里会执行它） |

- **`.git` 本身可写**（提交、切分支照常），只锁 hooks 与 config。Codex 把整个 `.git` 设为只读，提交都要越权；对桌面编码 Agent 太频繁，不采用。
- **默认不禁读 `~/.ssh`、`~/.aws`**：会让 `git push`、云命令在沙箱里失败；需要的用户在自定义里加。
- 完全权限档不进沙箱，禁写对命令不生效；文件工具的禁读照常（那是工具对 cosir 自身机密的规则）。

## 7. 配置

系统级 `<DATA_DIR>/.cosir/config/permissions.json`，pydantic strict + `extra="forbid"`，原子写（照 `models.json` / MCP 配置样板），由 `app/service/configuration/permission_configuration_service.py` 现读现写。

```json
{
  "default_mode": "workspace-write",
  "protected_paths": { "deny_read": ["~/.aws"], "deny_write": [] },
  "profiles": {
    "shared-lib": {
      "label": "工作区 + 共享库",
      "read": "all",
      "write": [":workspace", ":tmp", "/Users/me/shared-lib"],
      "network": true,
      "on_out_of_bounds": "ask",
      "reviewer": "user"
    }
  }
}
```

- 自定义档是**完整声明**，设置页「基于某档位新建」时复制一份再改。不用 Codex 的 `extends` 继承链：受保护路径是全局的，不需要靠继承跟上内置更新；完整声明所见即所得。
- 路径符号只有 `:workspace`、`:tmp` 两个，其余写绝对路径或 `~`。
- **坏配置不回退默认**：文件存在但解析或校验失败时，自定义档与用户追加的受保护路径不可用，设置页显示原因；内置档照常可用（定义在代码里）。选了不可用自定义档的 Run 启动即失败，错误写明原因。
- workspace 级配置本期不做（随仓库共享的配置只能收紧，否则是提权路径）。

## 8. 平台落地（Rust，terminal-worker 内）

**策略快照 → 启动帧**：`StartExec` 增加必填 `sandbox` 字段（`apps/terminal-worker/src/protocol.rs:18-34` 现无此字段）：`{ enforce, read_deny[], write_roots[], write_deny[], network }`。后端只给快照；平台规则在 Rust 侧生成。用户终端的 `StartSession` 不带沙箱。

- **macOS**：生成 SBPL，调用固定路径 `/usr/bin/sandbox-exec`（防 PATH 劫持）原地 exec。路径作为 `-D` 参数传入、不拼进策略字符串；`(deny default)` 起手，子进程继承同一沙箱。照搬 Codex `sandboxing/src/seatbelt.rs` 与 `seatbelt_base_policy.sbpl` 的结构，不引用其 crate（绑死 `codex-protocol`）。
- **Windows**（P4）：受限令牌 + capability SID + ACL（Codex `windows-sandbox-rs/src/{token,acl}.rs`），未实机验证。
- **Linux**：不是桌面目标，不做。

**沙箱不可用**（Windows 未就绪、`sandbox-exec` 缺失、包装失败）：沙箱档下每条 `execute_terminal` 都按「要决定」处理，审批卡写明「此平台沙箱不可用，这条命令将不受限制地运行」。不裸跑，也不让终端整体不可用。

## 9. 被拦之后

- **判断**：命令非零退出且输出含 `Operation not permitted` / `Read-only file system` / 网络关闭时的连接失败特征 → 观察里附一句「可能被沙箱拦截（当前档位：…）；确需沙箱外能力，用 `sandbox: "escalated"` 重新调用并说明理由」；卡片短提示 `status_hint`「沙箱拦截」。
- 不做 macOS `log stream` 违规监听与按命令归因：要常驻监听进程并解析系统日志，sandbox-runtime 也默认关闭（`enableLogMonitor`）。启发式误判的代价只是一句多余提示。

## 10. 提示词

`core/context/system_prompt_builder.py:168` 的「Shell commands are not sandboxed」改为按策略快照生成的能力说明：可写范围、网络开关、受保护路径、越界怎么申请；沙箱不可用时如实说明「命令每条都需要用户批准」。

## 11. 自动审查（P5，本期只留位置）

- 位置：审批人维度。门禁「要决定」后，审批人是模型时把请求交给审查器而不是挂等待占位。
- 审查器：只读、无工具的单次模型调用；输入是动作、目标、理由与最近对话摘要；输出严格 JSON `{risk_level, user_authorization, outcome: allow|deny, rationale}`（照 Codex `ext/guardian-reviewer/src/assessment.rs:13-24`）。
- **失败处理（fail-closed）**：超时、解析失败一律拒绝；有用户界面的 Task 可回落给用户（Codex 不回落，因其「替我批」档默认用户不在场）——P5 再定。
- 熔断：同一回合连续拒绝 3 次中断（Codex `circuit_breaker.rs:3-7`）。
- 结论与理由落日志、在审批卡位置回看。它看到的命令是模型写的——**只防失误，不防注入**，写进档位说明。

## 12. 日志

`permission_policy_resolved`（`task_id` `run_id` `mode` `source`）、`permission_decision_required` / `permission_decided`（`tool_call_id` `target` `scope` `outcome`）、`permission_denied`（无界面或「越界时拒绝」）、`sandbox_wrap_failed`（平台、原因）、`sandbox_denial_suspected`（`tool_call_id`、命中特征）。命令只记 `command_preview[:200]`。

## 13. 刻意不做

- **网络域名白名单 / 本机代理 / MITM**：要做就和浏览器权限一起做，否则防不住外传（浏览器与 `web_extract` 不经终端）。做时还要处理本机 API 无认证（AGENTS 待办「本机 API」）。
- **浏览器、MCP 服务进程的沙箱化**：档位只在门禁层粗管 MCP 调用；浏览器不在档位内。
- **永久规则与命令前缀规则**（Codex execpolicy / `prefix_rule`）：永久放行只能改配置。
- **按命令归因的违规监听**（§9）。
- **workspace 级权限配置**、**Linux**、**自动审查的「记住」**。
- **运行中改档位**：用「本 Task 内允许」覆盖运行中放宽的需要。

## 14. 问题清单

| # | 问题 | 证据 | 层级 | 置信度 / 待验证 |
|---|---|---|---|---|
| Q1 | 读类工具与浏览器 upload 能读系统 `.cosir/.env` | `PathResolver.resolve_for_read` 只拦设备路径（`tools/tool_handler/security/path_resolver.py:227-241`） | 契约缺口 | 高 |
| Q2 | 工作区 containment 写死在 `PathResolver`，与「档位决定可写区」冲突 | 7 处 `resolve_within_workspace` 调用（§4） | 契约 | 高；`session_service.py:295` 的终端 cwd 校验随终端改造重看 |
| Q3 | Agent 消息开 Run 不带档位，派生与 `send_message` 有借道提权 | §3 所列三处只透传模型与推理强度 | 设计缺口 | 高 |
| Q4 | 混批规则按工具静态声明判定 | `tool_batch_runner.py:54,109` | 结构 | 高；改为门禁纯函数 |
| Q5 | 交互 shell 下 seatbelt 的 PTY 规则 | 终端改造后 Agent 不再有交互 shell，**本稿不再需要** | — | 已消解 |
| Q6 | `sandbox-exec` 被 Apple 标为弃用 | Codex 与 srt 仍在用 | 外部风险 | 中；持续观察 |
| Q7 | Windows 受限令牌下命令行为 | 未实机 | 平台 | 低；P4 实测 |

## 15. 选定路线与被否方案

**选定**：一份策略、两个执行点（门禁管进程内工具，OS 沙箱管命令进程）、审批挂在单次调用上并复用 HITL 重放链路；Agent 没有常驻 shell（前置稿），人的终端永远无沙箱。

| 被否方案 | 否决理由 |
|---|---|
| 「终端沙箱」作为独立能力（DeepSeek 稿） | 档位管的是 Agent 全部动作；只做终端会让「仅可查看」名不副实 |
| 「仅可查看」用 `disabled_tool_groups` 隐藏写工具 | 混淆用户的工具选择与权限；只读的语义是「改要先问」 |
| 独立提权工具 `request_permission_escalation` + 只对新终端生效的 grant | 三个来回；批准的不是那条命令；时序难懂 |
| 用户新建终端也进沙箱 | 拦住用户自己的操作 |
| 命令级包装（在共享 shell 里把 Agent 的每条命令包进沙箱） | 在 ssh、REPL、已激活环境里失效，复杂度高；终端改造后不再需要 |
| 沙箱不可用时让终端不可用 | 功能消失；退化为逐条审批更顺 |
| 自定义档 `extends` 继承链 | 受保护路径全局化后继承无收益，完整声明更直观 |
| 结构化违规监听（`log stream`） | 常驻监听 + 日志解析，本期收益低 |

## 16. 分阶段

| 期 | 内容 | 验证 | 依赖 |
|---|---|---|---|
| P0 | 受保护路径事实源；读类工具（含搜索、查找、列目录的遍历）与 upload 拒读系统 `.cosir` | 单测：`read_file` 读 `.env` 被拒、读 `skills/` 放行；从用户主目录 `search_content` 不返回 `.cosir` 内的命中 | 无 |
| P1 | 档位模型与 Run 选项；策略快照；门禁第四段（写类工具、MCP）；权限审批占位、通用审批卡、批准范围与 grant 登记；Agent 消息档位传递；输入框选择器；提示词（如实写「命令未沙箱」） | 后端单测覆盖三档 × 写工作区内 / 外；E2E：审批卡批准与拒绝 | P0 |
| — | **终端改造**（前置稿 T1–T3） | 见前置稿 | — |
| P2 | macOS `execute_terminal` 进沙箱；`sandbox: escalated`；被拦提示；不可用退化为逐条审批；提示词改为如实描述沙箱 | 后端集成测试：工作区外写失败、`escalated` 批准后成功、网络关时连接失败 | P1 + 终端改造 |
| P3 | 自定义档位、`permissions.json`、设置页「权限」 | 配置单测（坏配置不回退）；设置页 E2E | P1 |
| P4 | Windows 受限令牌 | 实机验证 | P2 |
| P5 | 自动审查 | 审查器单测（fail-closed、熔断） | P1 |

## 17. 改动账本（粗估，实施前按阶段细化）

- **精简**：`PathResolver` 的工作区 containment 移交门禁（约 −40）；`system_prompt_builder` 的「not sandboxed」段落替换（约 −8）；`_requires_user_decision` 并入门禁判定（约 −10）。合计约 −60。
- **新增**：
  - 后端 `app/core/permission/`（档位与预设、策略快照、受保护路径、grant 登记，约 +450）；门禁第四段与工具访问声明（约 +200）；HITL 路由改造（约 +80）；配置服务（约 +200）；`execute_terminal` 参数与沙箱字段、被拦判断（约 +150）；Agent 消息档位传递（约 +60）。
  - Rust `apps/terminal-worker/src/sandbox/`（策略 → SBPL，约 +400；Windows 另计）。
  - 前端：输入框档位选择器、通用审批卡、设置页「权限」（约 +700）。
- **净变化**：约 +2200（不含 Windows 与自动审查）；实施时按阶段以 `git diff --stat` 对账。
- **命名**：
  - `app/core/permission/`（新目录）——正向：找「权限、档位、受保护路径」能落到；反向：core 层的权限规则。放 core 是因为门禁在 `core/tool/execute/`，而 `core` 不得导入 `app.tools`。
  - `app/service/configuration/permission_configuration_service.py`——与 MCP / 模型配置服务同处。
  - `apps/terminal-worker/src/sandbox/`——worker 内「怎么起受限进程」。
  - 本文由 `terminal-sandbox-design.md` 改名：内容已是 Agent 权限而非终端。
- **同步**：AGENTS.md「工具 UI 渲染边界」（HITL 不再只由工具静态声明）、「代码目录边界」（新目录）、待办「安全与权限」（完成项勾掉）；`tool_ui_display_contract.md` 加 `permission-request`；快照契约（`activity.launch` 带档位）。

## 附录：参考实现借鉴清单（已抽查）

**Codex**（`codex-rs/`）

- 档位预设：`utils/approval-presets/src/lib.rs:28-61`；四维度：`protocol/src/permissions.rs`、`protocol/src/protocol.rs:1163-1186`（审批策略）、`protocol/src/config_types.rs:183`（审批人）。
- 越权参数 `sandbox_permissions` + `justification`：`core/src/tools/handlers/shell_spec.rs:230-264`。
- 审批缓存键与范围：`core/src/tools/sandboxing.rs:42-118`、`approvals.rs:282-319`。
- Seatbelt 策略：`sandboxing/src/seatbelt.rs`（`-D` 参数传路径、网络三态）、`seatbelt_base_policy.sbpl`。
- 自动审查：`ext/guardian-reviewer/src/{assessment,completion,circuit_breaker}.rs`。
- 不搬：execpolicy（Starlark 规则）、exec-server / PathUri、网络代理、整个 `codex-sandboxing` crate。

**sandbox-runtime**

- 强制禁写清单：`src/sandbox/sandbox-utils.ts:21-50`；`.git/hooks` 与 `.git/config` 的处理：`src/sandbox/macos-sandbox-utils.ts:103-135`。
- 坏配置不回退默认：`README.md:177-185`。
- 网络（以后）：域名判定顺序 deny → allow → strict → ask（`src/sandbox/sandbox-manager.ts:732-830`），局限见 `README.md:1265-1284`。
- 不搬：Node 常驻进程模型、TLS 终结与凭据掩码、Windows 独立账户方案。
