# Agent 沙箱与智能审查

> 状态：设计稿，未实施（2026-11-14）。本稿只记需求、设计与差距，实施计划另起。
> 前置：[`terminal-ownership-redesign.md`](terminal-ownership-redesign.md) **已实施**——Agent 没有常驻 shell，Agent 的命令只经 `execute_terminal` 起进程。
> 参考：**Claude Code**（对标对象）、Codex、sandbox-runtime（Claude Code 沙箱的开源实现）、DSH（deepseek-harness，自动审查），见附录。

## 1. 要什么

**完成标准**：Agent 在工作区内照常读写、跑命令，不打扰用户；越界时（写到可写区外、命令申请沙箱外能力）交给模型审查，放行就执行；审查拒绝时，模型确实需要就用提问工具 `ask_user` 向用户说明风险、征得许可，用户同意后重试并放行。子 Agent 越界被拒就是被拒。

**最不想看到**：

- 沙箱或审查让 Agent 频繁失败、频繁打扰用户。
- 看似隔离、实际没隔离：某条通道绕过策略；沙箱不可用时悄悄裸跑；一次提权就交出模型 Key；命令调后端 API 替自己作答。
- 策略、审查、受保护路径各有一套事实，改一处漏一处。
- 用户自己的操作被沙箱拦住。

**本期防什么**：Agent 失误造成的破坏——写到工作区外、写进「下次自动执行」的文件、读到 cosir 自己的机密。**不防注入后外传**：浏览器、`web_extract` 本身能把数据带出去，单封终端网络换不来安全；审查器看到的命令也是模型写的。这一点写进提示词与说明，不让人误以为已隔离。

## 2. 对标 Claude Code

本期形态等同 Claude Code 的 **auto 模式 + 沙箱开启**：沙箱内的命令直接执行，越界交给分类器（本稿叫审查器），被拦后模型换路或向用户提问，不弹审批卡片。Claude Code 有、本期不做的列为待做（§11）。

| Claude Code | 本稿 | 关系 |
|---|---|---|
| auto 模式：分类器审查需要权限的动作，不弹窗 | 越界交给审查器（§6.2） | 一致 |
| 沙箱开启 + `autoAllowBashIfSandboxed`：沙箱内命令直接执行 | `execute_terminal` 恒在沙箱里，`default` 不审（§5） | 一致；沙箱不可关 |
| 沙箱写区：工作目录 + `--add-dir` + `$TMPDIR`；读全盘除禁读项 | 工作区范围 + 临时目录 + 包管理器缓存；读全盘除受保护（§3） | 偏离：多放开缓存，否则首次装依赖必失败；`--add-dir` 待做 |
| 沙箱网络：代理 + 域名白名单，新域名要批准 | 网络开，只屏蔽后端端口（§8） | 偏离：白名单须与浏览器一起做才有意义（§11） |
| `dangerouslyDisableSandbox` 重试 → 分类器 → 完全无沙箱 | `sandbox: "escalated"` → 审查器 → 仍禁读写 cosir 信任根、屏蔽后端端口（§6.1） | 偏离：一次放行不交出模型 Key |
| 沙箱不可用时照常运行、走权限流程 | 每条命令先审查再无沙箱执行（§8） | 一致（auto 下即每条过分类器） |
| 分类器只看用户消息、工具调用与 CLAUDE.md，剥掉工具结果 | 用户输入、提问回答、AGENTS.md、派生方任务说明、工具调用参数（§6.2） | 一致；子 Agent 另带根会话用户原话（照 Codex） |
| 连续 3 次 / 累计 20 次拦截 → 暂停 auto、回落逐次确认 | 同阈值 → 中断回合、告诉用户（§6.2） | 偏离：没有审批卡片可回落（§11） |
| 受保护路径（`.git`、`.vscode`、`.idea`、`.husky`、`.claude`、shell rc 等）；沙箱禁写配置与 `.mcp.json` | 信任根 + 「下次自动执行」两类（§7） | 一致；`.git` 本身可写，只锁 hooks 与 config |
| `AskUserQuestion`：1–4 题、选项（可带预览）、多选、「其他」与批注；问题一直等；子 Agent 没有 | `ask_user`：描述、参数、卡片、回执照搬（§6.3） | 一致；用户不理会提问直接发新消息时旧提问作废（§6.4） |
| 安全规则（指令来源边界、动作三级、隐私、版权），写在系统提示词里 | 同一份文本进系统提示词，并作审查器的判定标准（§6.5） | 一致；审查器复用是本稿加的 |
| 子 Agent 继承父级模式与规则；后台子 Agent 的弹窗冒泡到主会话 | 同一策略、工作区范围只能收窄；被拒即被拒（§4、§6.3） | 偏离：无弹窗可冒泡；范围收窄是本稿加的 |

## 3. 策略：一份固定策略

没有档位、没有选择器、没有配置文件：所有 Agent 都按同一份策略执行。

**文件系统规则**只有一种形态：一组「路径 × 访问」，访问取读 / 写 / 禁止；路径越具体越优先，同样具体时禁止 > 写 > 读（照 Codex）。可写区、受保护路径、例外（如系统 `.cosir` 禁读而其中的 `skills/` 可读）都是这组规则里的条目，文件工具与命令沙箱消费同一份。

| 读 | 写 | 命令网络 |
|---|---|---|
| 全盘（受保护除外，§7） | 工作区范围（§4）+ 临时目录 + 包管理器缓存（受保护除外） | 开（后端端口除外，§8） |

**不变式**

1. 策略只约束 **Agent 发起的动作**。人的终端永远不进沙箱，包括点回复里命令块的「运行」、或在 Agent 填入后按回车执行的命令。
2. Agent 发起的进程只有一个出口 `execute_terminal`，一律按策略起进程；没有绕过策略的工具、参数或终端。
3. 沙箱不可用时**不裸跑**：每条命令都先过审查（§8）。

## 4. 子 Agent：工作区范围收窄

委派（`delegate_task`）与 Agent fork（`fork_session`）加一个可选参数 `workspace_scope`，**只能收窄、缺省继承**：取值是工作区内已存在的目录，必须在发起方的范围之内（含相等），不能落在受保护路径里；越界时返回错误并写明上限。

- **工作区范围是 Session 级事实**，创建时定、之后不变；侧栏会话恒为整个工作区。上级的范围也不可变，所以「在上级之内」只在创建时校验一次；要换范围就新派生一个。
- **范围决定**：沙箱的可写区、写类文件工具的可写判定、`execute_terminal` 的缺省工作目录。
- **不变的**：相对路径仍相对工作区根解析，工作区 `.cosir`（附件、下载、日志）仍在工作区根——Session 树与界面（链接、文件面板）共用一套坐标，范围只收窄可写区与缺省目录，不是另一个工作区。读不受范围限制。
- 子 Agent 的系统提示词写明自身的可写范围；委派与分叉的调用行展示范围。

## 5. 执行点与判定顺序

同一份策略，按动作的性质落到两处：

| 通道 | 执行点 | 规则 |
|---|---|---|
| 读类文件工具（`read_file` / `search_content` / `find_files` / `list_directory` / `read_image`） | 工具自身 | 受保护读路径一律拒绝；从上层目录遍历时跳过受保护子树 |
| 写类文件工具（`write_file` / `patch_write` / `apply_patch` / `delete_file` / `move_file`） | **准入门禁** | 按下面的判定顺序 |
| `execute_terminal` | **OS 沙箱** | 按策略起进程；申请 `escalated` 时先审查 |
| MCP 工具、浏览器、`web_search` / `web_extract` | — | 本期不受策略约束，说明里写明；MCP 服务进程本身不进沙箱 |
| 读用户终端、`terminal_write` 填入、回复里的命令块 | — | 都不执行任何东西（执行由人触发） |

**文件工具走门禁、命令走沙箱**：文件工具在后端进程内执行，OS 沙箱管不到，但目标路径在执行前就能从参数算出来；命令的影响无法从参数推出，只能由 OS 沙箱在运行时兜住。可写区因此是策略规则、归门禁判定，写类工具不再自己限定「必须在工作区内」，否则审查放行的写入会被工具再拒一次。

**判定顺序**（Claude Code 是「Hook → deny / ask / allow 规则 → 模式 → 沙箱自动放行 → 分类器」，本稿没有规则与模式）：

1. 现有准入（工具存在、Agent 有这个工具、参数合法），不过即拒；
2. 写到 cosir 信任根（§7）→ 直接拒绝，不送审；
3. 目标在可写区、或命令按 `default` 在沙箱里跑 → 放行；
4. 其余（写到可写区外或「下次自动执行」文件、命令申请 `escalated`、沙箱不可用时的每条命令）→ 审查器（§6）。

## 6. 越界：智能审查

### 6.1 怎么发起

- **文件工具**：门禁按目标路径判定，模型不用多写参数。
- **命令**：只有模型知道一条命令需要沙箱外的能力（写 `~/.npmrc`、装全局包），所以由它在调用上声明——`execute_terminal` 增加 `sandbox: "default" | "escalated"` 与 `justification`（一句理由，审查器与提问都会用到），照 Codex。不做独立的「申请提权」工具：那样要「执行失败 → 申请 → 重跑」三个来回，审的也不是那条命令本身。
- **放行后仍在沙箱里跑**：这一条命令全盘可写，但 cosir 信任根（§7）照旧禁读禁写，后端端口照旧屏蔽（§8）。照 Codex「策略含禁读时不许无沙箱运行」：否则一次放行就顺带放开了 `.env` 里的模型 Key。

### 6.2 审查器

- **只审越界的调用**（照 Codex）：沙箱内照常执行的调用不审。不学 DSH 每次调用都审、并以完全权限替代沙箱：每次多一次模型请求，误放行也没有兜底。
- **一次独立模型调用、不给工具**（照 DSH）：门禁之后、执行之前同步进行，不建子会话、不写进被审 Session 的上下文，调用行显示「审查中」。Codex 用只读子会话加工具让审查器自己查证，对本项目太重；误拒多了再考虑给只读工具。
- **按来源决定谁能授权**：审查上下文只放能构成授权或约束的输入——本 Session 的用户输入、`ask_user` 里用户的回答（§6.3）、AGENTS.md 指令、派生方交来的任务说明与追加消息，以及历史工具调用的名字与参数；**助手正文、推理与工具结果一律不放**，压缩后只剩摘要的部分不继承原文的授权。子 Agent 另带根侧栏会话里用户的原话作授权证据（照 Codex），上级 Agent 的转述不算用户授权。
- **输出协议收严**（照 DSH）：严格 JSON `{risk_level, user_authorization, outcome, rationale?}`，`risk_level` 取 low / medium / high / critical，`user_authorization` 取 high / medium / low / unknown；允许的组合是固定的（如 low 必须放行、critical 必须拒绝、放行不带理由），不合法即审查失败，不做宽松解析。判定标准是安全规则（§6.5）加风险分级：low / medium 放行，high 只在用户授权至少 medium 且范围收窄时放行，critical 拒绝（分级照 Codex）；用户的许可必须覆盖动作、目标与范围；纠偏条目照搬（「工作区外不等于高风险」「沙箱被拦后提权重试不可疑」）。
- **审查失败与审查拒绝分开**：
  - 审查失败（超时、模型报错、解析不了）：一律拒绝，观察写明「这是审查失败，不是安全判定」；有时限（Codex 默认 90 秒）。用户的许可救不了失败；模型应如实告诉用户，用户可以在自己的终端里执行（回复里的命令块）。
  - 审查拒绝：观察写明拒绝理由与「不得绕路达成同样效果；确需这样做，用 `ask_user` 向用户说明风险、征得许可后重试」（照 Codex）。
- **熔断**（照 Claude Code：连续 3 次、或同一 Session 累计 20 次拒绝）：触发即中断这个回合并告诉用户哪些动作被拒；放行清零连续计数，熔断后两项都清零。审查失败不计入、也不清零（Codex 失败会清零连续计数，持续故障的审查器因此永不熔断）。
- **用户发了新消息，进行中的结论作废**（照 Codex）：审查进行中开启了新回合，这次结论不采用、按新上下文重审。
- **不缓存结论**：同一调用再次出现重新审。
- **留痕**：每次结论（含放行）、熔断、失败都落结构化日志，调用行可回看。
- **审查模型**：先用本次 Run 的模型，审查消息排布成只追加、吃前缀缓存（照 DSH）；是否换成固定的审查模型（Claude Code 固定用 Sonnet，Codex 用专用模型、低推理强度）以后再定。

### 6.3 拒绝之后：通用提问工具 `ask_user`

审查拒绝不弹审批卡片，由模型自己决定要不要问用户。问用户用**通用提问工具** `ask_user`：澄清需求、在几个方案里选一个、确认取舍，征得越界许可只是用途之一。工具描述、参数、卡片与回执都照 Claude Code 的 `AskUserQuestion`（根目录 [`claude-code-system-prompt.md`](../../claude-code-system-prompt.md) 的 AskUserQuestion 一节，含参数说明与回执）。

**工具描述**（给模型，英文原文照录；删去 Plan mode 一段，cosir 没有 plan 模式；`codebase` 改 `workspace`，`dialog` 改 `card`，参数名按 cosir 蛇形命名）：

```text
Use this tool only when you are blocked on a decision that is genuinely the user's to make: one you cannot resolve from the request, the workspace, or sensible defaults.

Usage notes:
- Users will always be able to select "Other" to provide custom text input
- Use multi_select: true to allow multiple answers to be selected for a question
- If you recommend a specific option, make that the first option in the list and add "(Recommended)" at the end of the label

Reserve this for decisions where the user's answer changes what you do next — not for choices with a conventional default or facts you can verify in the workspace yourself. In those cases pick the obvious option, mention it in your response, and proceed.

The user may see only the question card: tool results and text you wrote earlier in this turn may not be on their screen while it is open. Make each question stand on its own: if it depends on something you found, say so briefly, and name the items you are asking about in the question itself.
```

**参数**（各字段说明照录 Claude Code 的参数说明）：

| 字段 | 约束 |
|---|---|
| `questions` | 1–4 个；`question` 在本次调用内不重复（回答按问题原文作键） |
| `questions[].question` | 完整问句，以问号结尾 |
| `questions[].header` | 不超过 12 字，卡片上的页签 |
| `questions[].options` | 2–4 个，同一题内 `label` 不重复；不写「其他」（卡片自动提供） |
| `options[].label` / `description` | 必填；`label` 1–5 个词 |
| `options[].preview` | 可选；选项聚焦时显示的预览（界面草图、代码片段、对比） |
| `questions[].multi_select` | 缺省 `false` |

Claude Code 的 `answers`、`annotations` 由对话框写回调用输入，`metadata` 是它的统计字段；cosir 的回答走用户决定（见下），三者都不进模型能填的参数。

**卡片**（展示数据就是参数原样）：每题一页，`header` 作页签；选项显示 `label` 与 `description`，单选或多选；带 `preview` 的选项聚焦时在旁边按等宽文本显示预览；每题固定多一个「其他」自由输入，并可附一段批注；全部作答后提交，关掉即不答。

**回答怎么回到模型**：走现有「回复类」用户决定——首次调用返回等待占位、Run 挂起，用户的回答作为决定消息交给模型。现在的决定只有「批准 / 驳回」加一句附言，要扩展成能携带结构化回答。

- **回答**照 Claude Code：`answers` 以问题原文为键，值为所选 `label`（多选为列表）或「其他」里填的文字；`annotations` 以问题原文为键，带批注与所选项的预览。
- **校验**：每题都答了；单选只能一个值；所选 `label` 属于该题选项（「其他」的文字不校验）。
- **交给模型的文字**照 Claude Code 较新的那版回执，批注附在对应回答后：

  ```text
  The user answered: "<question>"="<answer>", "<question>"="<answer>". Read the answers carefully — they may request clarification, changes, or that you not proceed — and follow what they actually say.
  ```

- **关掉不答**记为驳回，告诉模型「The user declined to answer.」；按 Claude Code 的读法，被拒就调整做法，不原样再问。

**其余规则**：

- **征得越界许可时**：模型写明要做的动作、为什么、审查器给的风险，选项如「允许」「不允许」。用户的回答连同提问内容一起进审查上下文；模型重试时，审查器据此把授权提到 high 并判断是否覆盖本次动作。critical 即使用户允许也拒。没有授权缓存，下一次越界照常审。
- **只给侧栏会话**：子 Agent 的工具列表里没有它（Claude Code 同样不给子 Agent）。
- **子 Agent 被拒即被拒**：拒绝观察对子 Agent 写明「需要用户许可，这一步没有做」，由它在结论里如实交代；上级要做，自己越界、自己走审查与提问（DSH 子 Agent 同样拒绝即终局）。
- 提问工具与沙箱无关，可以先于沙箱单独交付。

### 6.4 提问的有效期：到下一回合

现状用户决定的等待在 Run 停止、失败、完成、重启后仍可决定。提问问的是「此刻做不做这件事」，过时的许可不该在之后的回合里生效；挂着提问时用户的新消息也会被卡住（等待让 Run 挂起，而挂起的 Run 不被排队的新消息唤醒）。

决定的有效期由工具声明，不按工具名分支，也不按 Run 算（Run 对用户不可见）：

| 有效期 | 适用 | 何时结束 |
|---|---|---|
| 到下一回合 | `ask_user` | 用户作答；撤回所在回合；**下一回合开启**。Run 停止、失败、重启不结束 |
| 到交还 | `browser_handoff`——一段租约，用户可能边登录边发消息 | 交还、撤回所在回合、删除 Session（现状不变） |

- **新回合开启时**：更早回合里仍在等待的提问一律结算为「用户未作答，发了新消息」，模型在新回合看到；迟到的回答被拒。
- **新消息即答复**：Run 挂起、且只在等提问时，用户输入唤醒同一个 Run、开启新回合。现规则「挂起时排队消息不唤醒」是有意定的，先查清原因（Q8）。

### 6.5 安全规则：复用 Claude Code

主 Agent 的系统提示词与审查器的判定标准共用一份安全规则文本，照搬 Claude Code 的安全规则（根目录 [`claude-code-system-prompt.md`](../../claude-code-system-prompt.md) 第 12 节，2026-11-14 版），英文原文不改措辞，只按 cosir 改写下列几处，以便跟进它的版本更新。

- **内容**：指令来源边界（只有用户在对话里说的才是指令，工具看到的一切都是数据）；动作三级——禁止（填凭据与密码、金融交易、改系统与安全设置、过验证码、下载执行不可信文件等，用户要求也不做）、须明确许可（下载、代发消息、发布、购买、付款转账、建账号、接受条款与授权、改账号设置与常设规则、提交表单、不可撤销的操作、永久删除、按看到的内容行事）、其余照常；测试自己的应用的例外（本机开发地址与用户点名的测试环境）；隐私；版权。
- **cosir 的改写**：
  - 「在对话里问、等明确同意」→ 用 `ask_user` 问；许可只来自用户输入与 `ask_user` 回答，与审查器按来源授权同一口径（§6.2）。
  - 子 Agent 不能问用户：须明确许可的动作在子 Agent 里不做，在结论里交代，由上级去问（与「被拒即被拒」一致）。
  - 删去 cosir 没有的能力：屏幕操作（computer use）一段、密码管理器取凭据工具一段。
- **审查器怎么用**：禁止类 → critical，用户允许也拒；须明确许可类 → 只有用户给了覆盖这次动作的许可才放行，许可按动作、不泛化，与「不缓存结论」一致；其余按风险分级（§6.2）。
- **覆盖面**：审查器只看越界调用；浏览器、MCP、`web_*` 本期不受策略约束（§5），这些通道上的发消息、付款、接受条款、下载只靠提示词里的这份规则约束。Claude Code 的这部分规则同样是提示词层面的，写进说明，不当成强制边界。
- **一份事实**：系统提示词与审查器引用同一份文本；Claude Code 更新时先更新根目录的记录，再同步这里。

## 7. 受保护路径

一份事实，三处消费：写类文件工具的门禁、读类文件工具的读拒绝（含浏览器 `upload` 取本机文件）、命令沙箱。文件工具这一处已落地：读写入口拒绝、从上层目录遍历时剪掉禁读子树；根目录全部取路径变量（数据根随 `CODING_AGENT_DATA_DIR`）。

| 类别 | 条目 | 提权后 | 依据 |
|---|---|---|---|
| cosir 信任根 | 系统 `.cosir` 禁读禁写，其中 `skills/` 可读；工作区 `.cosir` 只读 | 仍生效 | 系统 `.cosir` 有 `.env`（模型 Key）、业务库、日志；工作区 `.cosir` 有 workspace 级 MCP 配置，而 MCP stdio 服务不进沙箱，能写它就能在沙箱外执行任意代码（Claude Code 禁写 `.mcp.json` 同一理由） |
| 下次自动执行 | `.git/hooks`、`.git/config`（`.git` 为指针文件时保护其实际目录）；`.gitconfig`、`~/.config/git`、`.gitmodules`；shell rc（`.bashrc` `.bash_profile` `.zshrc` `.zprofile` `.profile`）；`.envrc`；`.husky`；`.vscode`、`.idea`——均只读 | 解除 | 用户无沙箱的 git、shell、编辑器会执行它们；照 Claude Code 受保护路径与 sandbox-runtime 强制清单，加 `.envrc`（direnv） |

- **`.git` 本身可写**（提交、切分支照常），只锁 hooks 与 config。代价：`git push -u`、`git remote add` 写 config，要提权过审；不放开，因为 config 能配 `core.hooksPath`、`core.fsmonitor`。
- **包管理器缓存默认可写**（`~/Library/Caches`、`~/.cache`、`~/.npm`）：不放开时 npm / pip / uv / cargo 首次拉依赖即失败。代价是缓存可被投毒，写进说明（Q6）。
- **默认不禁读 `~/.ssh`、`~/.aws`**：会让 `git push`、云命令在沙箱里失败；用户追加受保护路径列为待做（§11）。
- 受保护条目**不要求路径已存在**：首次创建也拦（如 `mkdir .cosir`）。

## 8. 平台

- **命令进沙箱**：起命令时带上展开后的规则，由 Terminal Worker 包进系统沙箱；人的终端起进程时没有这一项，「人的终端不进沙箱」由协议本身保证。
- **后端端口一律屏蔽**：后端 API 无认证（AGENTS 待办「本机 API」），不屏蔽时命令能替用户回答提问；Codex 为同类原因屏蔽自己的服务 socket。
- **macOS**：Seatbelt（`/usr/bin/sandbox-exec`，固定路径防 PATH 劫持），子进程继承同一沙箱，基础策略以 Codex 的为起点。必须堵住的绕过（照 Codex）：
  - 受保护路径还不存在时也要生效；
  - 改名受保护条目的上级目录再改内容（`mv .git x`）——上级目录也不许改名或删除；
  - 经只读文件描述符改写文件；
  - 符号链接：可写根先取真实路径（`/tmp` → `/private/tmp`），否则规则匹配不上。
- **Windows**：Codex 的非提权受限令牌**做不到禁读**，守不住 `.env`，断网只靠环境变量（不是安全边界）；专用用户与容器方案要管理员 setup。选定前按「沙箱不可用」处理。
- **Linux**：不是桌面目标，不做。
- **沙箱不可用**（Windows 未就绪、`sandbox-exec` 缺失、包装失败）：每条 `execute_terminal` 都先过审查，审查请求标明「这条命令不在沙箱里运行」，审查器按无沙箱收严判定；放行后无沙箱执行。不裸跑，也不让终端整体不可用。

## 9. 被拦之后

- **判断**：命令非零退出（2 / 126 / 127 除外，照 Codex 的快速排除）且输出含 `Operation not permitted` / `Read-only file system` → 观察里附一句「可能被沙箱拦截；确需沙箱外能力，用 `sandbox: "escalated"` 重新调用并说明理由」；调用行短提示「沙箱拦截」。
- **只提示、不自动重跑**：「被拦 → 去沙箱外重跑」只有模型显式 `escalated` 一条路（Codex 也已取消 `on-failure` 的独立语义）。
- 不做 macOS 违规日志监听与按命令归因：要常驻监听并解析系统日志，sandbox-runtime 也默认关闭。启发式误判的代价只是一句多余提示。

## 10. 提示词

系统提示词现在写的是「Shell commands are not sandboxed」，改为：

- 安全规则（§6.5）；
- 能力说明：可写范围、受保护路径、越界怎么申请（`sandbox: "escalated"` + 理由）、越界会经智能审查、被拒后可用 `ask_user` 向用户征得许可且不得绕路；
- `ask_user` 也用于澄清需求与选方案；加入 Claude Code 的「信息够了就动手，权衡时给建议而不是罗列」一句，免得提问被滥用；
- 沙箱不可用的平台如实说明「命令不在沙箱里运行，每条都先审查」。

策略固定、平台在进程启动时定，写进系统提示词不会过时，也不破坏提示词缓存；子 Agent 的可写范围随它的 Session 固定（§4）。

## 11. 待做：与 Claude Code 的差距

本期只做 auto 模式这一种形态。下面是 Claude Code 有、本期不做的，出现真实需求再做；做时照它的形态，不另起一套。

- **其他权限模式**：Manual（越界逐次弹窗确认）、plan（只读）、acceptEdits（工作区内改文件免确认）、dontAsk（越界一律拒绝，适合无人值守）、bypassPermissions（完全放开）。前三种需要审批卡片。
- **allow / ask / deny 规则与配置分层**：按工具 + 模式写规则（如 `Bash(npm run test *)`、`Edit(./src/**)`），deny > ask > allow；系统级与 workspace 级两层，deny 跨层生效，workspace 级随仓库共享只能收紧；「不再询问」写入本地规则；坏配置不回退默认（照 sandbox-runtime），设置页显示原因。规则不是安全边界，安全靠沙箱（Claude Code 原话同义）。
- **网络代理与域名白名单**：起始为空，新域名先问（auto 下问审查器）。要和浏览器权限一起做，否则防不住外传；做时还要处理本机 API 无认证。
- **熔断后回落人工确认**：依赖 Manual 模式的审批卡片。
- **Hook**：执行前 Hook 先于规则拦截，审批请求 Hook 可替用户放行或拒绝。后端目前没有 Hook。
- **额外可写目录**（`additionalDirectories`）与 **沙箱外命令清单**（`excludedCommands`，如 `docker`、会自己调 `sandbox-exec` 的构建，Q7）。
- **用户追加的受保护路径**（如 `~/.ssh`、`~/.aws`），随规则一起做。

## 12. 刻意不做

- **MITM / TLS 终结**：流量内容检查。
- **浏览器、MCP 工具与 MCP 服务进程的约束**：本期不在策略内。
- **网络开时的提权通道**：docker socket 等本机服务等同 root，网络开时不拦；本期只防失误，写进说明。
- **安全命令白名单与危险命令黑名单**：Codex 自己已删掉安全命令白名单。
- **审批卡片与授权缓存**（「本会话内允许」）：许可由 `ask_user` 留在上下文里，由审查器读取。
- **Linux**。

## 13. 问题清单

| # | 问题 | 层级 | 置信度 / 待验证 |
|---|---|---|---|
| Q1 | 读类工具与浏览器 upload 能读系统 `.cosir/.env`（读路径只拦设备文件） | 契约缺口 | 已解决（2026-11-14）：文件工具的读写入口与搜索遍历都按受保护路径拦截 |
| Q2 | 写类工具自己写死「必须在工作区内」，与「策略决定可写区」冲突 | 契约 | 高 |
| Q3 | 命令能调用无认证的后端 API 替用户回答提问 | 设计缺口 | 高；屏蔽后端端口只封终端通道，浏览器通道随浏览器权限另做 |
| Q4 | `sandbox-exec` 被 Apple 标为弃用 | 外部风险 | 中；Codex 与 sandbox-runtime 仍在用，持续观察 |
| Q5 | Windows 受限令牌做不到禁读 | 平台 | 高；选定前按不可用处理 |
| Q6 | 包管理器缓存清单是否够用（npm / pip / uv / cargo / pnpm 位置各异） | 取舍 | 中；用真实项目校准 |
| Q7 | 命令自己调 `sandbox-exec` 时嵌套失败（如 `swift build` 的 manifest 沙箱） | 平台 | 中；提权过审，或提示词引导 `--disable-sandbox` |
| Q8 | 挂起中的 Run 不被用户排队的新消息唤醒，提问挂着时新消息干等 | 设计缺口 | 高（现象）；当初「挂起时不唤醒」的理由待查，等后台结果的挂起是否一并放开待定（§6.4） |
| Q9 | 工作区范围之外的上级目录里有子 Agent 需要写的构建产物（如 monorepo 根的 `node_modules`、lockfile） | 取舍 | 中；子 Agent 越界过审（被拒即被拒），或派生时范围给到仓库根 |
| Q10 | 审查是同步模型调用，越界调用会多等一次模型往返 | 取舍 | 中；只审越界，量可控；观测耗时再决定是否换小模型 |

## 14. 选定路线与被否方案

**选定**：对标 Claude Code auto 模式 + 沙箱；一份固定策略、两个执行点（门禁管进程内工具，OS 沙箱管命令进程）、越界交给智能审查（单次模型调用、严格输出、按来源授权）、被拒后由模型用 `ask_user` 征得用户许可、提权保留 cosir 信任根；子 Agent 被拒即被拒；人的终端永远无沙箱。

| 被否方案 | 否决理由 |
|---|---|
| Claude Code 的 Manual 模式（越界逐次确认）作为本期模式 | 频繁打扰正是「最不想看到」；Claude Code 也已把 auto 作为起始模式 |
| 越界弹审批卡片 + 「本会话内允许」 | 许可由 `ask_user` 留在上下文里由审查器读取，不需要另一种等待事项与授权登记 |
| 专门的权限提问工具 | 提问是通用能力，征得许可只是用途之一 |
| 子 Agent 越界冒泡给用户 | 要为子 Agent 开放用户界面，且没有审批卡片可冒泡 |
| 审查器做成只读子会话 + 工具（Codex） | 子会话、主线池、增量游标太重；先单次调用 |
| 每次调用都审、用审查替代沙箱（DSH） | 每次多一次模型请求，误放行无兜底 |
| 提权即完全不进沙箱（Claude Code 的 `dangerouslyDisableSandbox`） | 一次放行就交出 `.env` |
| 沙箱不可用时让终端不可用 | 功能消失；每条先审更顺 |

## 附录：参考来源

**Claude Code**：官方文档 `code.claude.com/docs/en/` 的 `permissions`、`permission-modes`、`sandboxing`、`settings`、`hooks`、`sub-agents`、`tools-reference`；系统提示词、安全规则与 `AskUserQuestion` 原文见根目录 [`claude-code-system-prompt.md`](../../claude-code-system-prompt.md)。

**Codex**（`codex-rs`，HEAD `5045589550`）

- 规则模型与优先级：`protocol/src/permissions.rs:108-136`；受保护元数据与 gitdir：`:2376-2419`。
- 越权参数与提权保留禁读：`core/src/tools/handlers/shell_spec.rs:230-264`、`core/src/tools/sandboxing.rs:290-302`。
- Seatbelt 与绕过点：`sandboxing/src/seatbelt.rs`（`:400-470` 符号链接、`:1004-1029` 屏蔽服务 socket / 上级目录 unlink / fcntl）、`seatbelt_base_policy.sbpl`；被拦判断：`sandboxing/src/denial.rs:13-72`。
- Windows 禁读局限：`windows-sandbox-rs/src/lib.rs:745-753`。
- Guardian 审查：路由 `ext/guardian-reviewer/src/routing.rs:59-67`；子会话与工具 `settings.rs:44-56`；判定标准 `prompts/templates/guardian/policy_template.md:66-78`；失败、时限、熔断 `completion.rs:20-24`、`lib.rs:87-89`、`circuit_breaker.rs:3-7`；结论作废 `core/src/guardian/review_request.rs:45-95`；拒绝文案 `prompts/src/model_messages/guardian.rs:10-16`；子 Agent 授权证据 `core/src/agent/control/user_authorization.rs:257-329`；提问工具 `core/src/tools/handlers/request_user_input.rs`。
- 不搬：execpolicy、危险命令黑名单、网络代理、整个 `codex-sandboxing` crate。

**sandbox-runtime**（commit `98a4016`）

- 强制禁写清单：`src/sandbox/sandbox-utils.ts:21-50`；`.git/hooks` 与 `.git/config`：`src/sandbox/macos-sandbox-utils.ts:103-135`；坏配置不回退：`README.md:177-185`；网络域名判定（待做）：`src/sandbox/sandbox-manager.ts:732-830`。
- 不搬：Node 常驻进程模型、TLS 终结与凭据掩码、Windows 独立账户方案。

**DSH**（`packages/experimental/auto-review/src/index.ts`）

- 每次调用都审、以完全权限替代沙箱（`:741-781`，不照搬）；单次调用不给工具（`:669-693`）；按来源筛上下文（`:228-298`）；只追加吃缓存（`:577-589`）；严格输出组合（`:57-64`、`:592-640`）；子 Agent 拒绝即终局（`packages/subagent/subagent/src/child-agent.ts`）。
- 缺陷（不照搬）：没有时限与熔断，放行不留审查记录，审查模型不可配置。
