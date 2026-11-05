# 终端沙箱：接入 sandbox-runtime

> 状态：2026-11-03 调研草案，暂不实施。安全与权限后续整体设计（目标：配置化的白名单 + 黑名单 + 越界审批，覆盖终端、读文件、浏览器、本机 API），见 AGENTS.md 待办「安全与权限」；本稿是那时的终端部分输入。
> 前置：[`terminal-process-unification-design.md`](terminal-process-unification-design.md)（所有用户命令只从 worker 启动），本设计在它的第 2 阶段之后实施。
> 范围：Agent 发起的终端命令（`execute_terminal` 与 Agent 建的交互终端）放进沙箱；越界时的用户审批；配置与平台分级；终端沙箱与读文件工具、浏览器上传共用的受保护路径（§4.4）。不含 MCP 服务、内置浏览器的权限模型、后端自身网络访问（§1.1、§8）。
> 待定：§4.1 的承载方式（后端拉起 Node sidecar / 宿主用 `utilityProcess` 托管 srt / 把 Codex 沙箱代码并入 worker）在重新评估中，其余各节不受影响。

## 1. 要解决什么

现在终端命令没有任何命令级防线：deny-list 已删，系统提示词直说「Shell commands are not sandboxed」（`apps/backend/app/core/context/system_prompt_builder.py:158`）。Agent 写错命令或被网页 / 文件里的注入文本带偏时，影响范围是整台机器。

沙箱要限住的是三件事：

- **写**：只能写工作区（和临时目录），写不到工作区外、写不到 `.cosir/`、写不到 git hooks / shell rc 这类「下次自动执行」的文件。
- **读机密**：读不到系统级 `.cosir` 里的机密（`.env` 里有模型 API Key，另有业务库、日志与 MCP 配置；技能目录除外）。
- **网络外传**：只能连放行的域名。

不防：用户自己在终端里做的事、内核级逃逸、恶意用户。

目标体验对齐 Claude Code 的做法：**沙箱内的命令照常自动执行、不弹审批；越界才问用户。**沙箱是让 Agent 少打扰用户的手段，不是多一道关卡。

### 1.1 跨通道的威胁模型

终端只是 Agent 接触外部的通道之一。整体防护取决于最弱的那条通道：只给终端设限，Agent 换一条路照样做得到。

| 通道 | 由什么限制 | 读受保护路径 | 外传数据 |
|---|---|---|---|
| 终端命令 | 系统级沙箱（本设计） | 禁止 | 只能连白名单域名 |
| `read_file` 等读文件工具 | 工具代码（`resolve_for_read`） | **现在能读**（S10），本设计改为禁止 | 不直接外传 |
| 浏览器 `upload` | 同上，与读文件共用规则 | **现在能读还能交给网页**（S10），本设计改为禁止 | 交给网页 |
| 浏览器导航与页面脚本 | Chromium 自带的进程沙箱；Agent 行为的权限模型待做 | 页面读不到本机文件 | **能**：打开任意网址即可带出数据 |
| 写文件工具 | 工具代码，只能写工作区 | — | — |
| MCP stdio 服务 | 不限制（用户自己配置） | 能 | 能 |

由此定两条：

- **受保护路径只有一份事实，所有能读本机文件的通道都消费它**（§4.4）。否则终端的禁读形同虚设。
- **终端的网络白名单只防命令自己往外发数据**（安装脚本、被注入的命令），不防 Agent 借浏览器主动外传。后者靠浏览器的权限模型（按站点授权、高风险动作确认），留在 AGENTS.md 待办「安全与权限」。浏览器不接系统级沙箱：网页内容已经在 Chromium 的进程沙箱里，缺的是对 Agent 行为的约束，而不是对进程的约束。

## 2. 已定的取舍

- **直接用 sandbox-runtime（srt），不移植**（统一设计 §2 已定）。
- **接入点只有一个**：worker 的启动帧。统一设计落地后，两种终端的进程都从 worker 启动，后端在拼启动帧时决定包不包沙箱；worker 只认「argv + 环境变量」，不知道沙箱存在。

## 3. 调查结论（已核实）

| # | 结论 | 证据 |
|---|---|---|
| S1 | srt 库接口：传「命令字符串 + 解释器」进去，拿回包装后的 argv 和 env，由调用方自己 spawn。`SandboxManager` 是进程级单例，配置一份；单条命令只能覆盖文件系统规则与 `allowPty`（`customConfig`），网络放行要另按 commandId 登记。wrap 必须 await，并且全局串行 | `sandbox-runtime/src/sandbox/sandbox-manager.ts:2319-2335,2398-2410,2502-2546,2630,2679,571` |
| S2 | 网络询问回调只拿到 `{host, port}`，不带 commandId：多个 Task 并发时，无法把「要不要放行 example.com」路由到发起它的那个 Task | `sandbox-schemas.ts:158-160` |
| S3 | srt 的 CLI 是一次性的（每条命令起一个代理），没有常驻服务模式；要复用代理与配置，只能自己写一个常驻的薄 Node 进程（下称 sidecar）调用 srt 库 | srt `src/cli.ts`（上次抽查） |
| S4 | macOS：底层是 sandbox-exec，原地 exec，进程组与控制终端不变；开 `allowPty` 后交互 shell 可以整体放进沙箱 | `sandbox-config.ts:1236-1239` |
| S5 | Windows（alpha）：要一次性 UAC 安装；命令以独立账户 `srt-sandbox` 运行，拿到的是全新的用户 profile，环境只有 PATH/PATHEXT 加代理变量，用户目录下的 npm / git 凭据、ssh key 都不在；Schannel 系工具（curl.exe、Invoke-WebRequest、winget）做不了 HTTPS，git 被强制改用 OpenSSL；broker 写完配置就关掉 stdin，所以**没有 stdin、没有 ConPTY，交互终端放不进去** | `windows-sandbox-utils.ts:4162,4225-4234,4240-4294`；`vendor/srt-win-src/src/logon.rs` |
| S6 | `allowLocalBinding` 在 macOS 上同时放行「监听本机端口」和「连本机任意端口」。开了它，沙箱里的命令就能访问 cosir 后端自己的 localhost API，而这个 API 没有认证 | `sandbox-config.ts:726-740`；AGENTS.md 待办「安全与权限」 |
| S7 | HITL 是按工具静态声明的：声明了 `user_decision` 的工具只给侧栏 Task（`requires_user_surface`），并且必须单独成批调用。所以不能给 `execute_terminal` 加一个「出沙箱」参数：一加，子 Agent 与 Team 节点就拿不到它，并行命令也会整批被拒 | `app/core/tool/schemas/tool_definition.py:55,82-85`；`app/core/workflow/node_helper/tool_batch_runner.py:12-14,56-57` |
| S8 | 项目里没有以 Node 模式起子进程的先例；宿主把 worker 路径用环境变量交给后端，开发期由启动脚本注入，打包后取资源目录。sidecar 可以照这个样板做 | `electron/main/backend/runtime.ts:29-43`；`process.ts:40`；`scripts/stage-terminal-worker.mjs` |
| S9 | 现在的等待卡只有「交还 / 打开面板」和 Team 审批表单，没有通用的「批准 / 驳回」 | `components/assistant-ui/turn/attention-card.tsx:36-89` |
| S10 | 读文件的统一入口 `resolve_for_read` 只拦设备路径，不拦系统 `.cosir`；浏览器上传也走它。Agent 现在就能用 `read_file` 读出 `.env`，或用 `browser_act` 上传到网页 | `app/tools/tool_handler/security/path_resolver.py:227-241`；`app/tools/tool_handler/browser/act.py:104-122` |
| S11 | 系统 `.cosir` 里只有 `skills/` 是 Agent 该读的（技能脚本会经终端执行）；`.env`、`storage/`、`logs/`、`runtime/`、`config/`（MCP 配置可能带令牌）不该读。路径布局的唯一事实源是 `system_cosir.py` | `app/utils/path/system_cosir.py:7-16` |

业界做法（详见调研记录，括号内为置信度）：

| | Claude Code | Codex | 本设计 |
|---|---|---|---|
| 默认写 | 当前目录 + 临时目录（高） | 工作区 + tmp，`.git` 只读（高） | 工作区 + 临时目录 |
| 默认读 | 几乎全放（中高） | 不限（中） | 全放，受保护路径除外（§4.4） |
| 网络 | 白名单 + 首次访问时询问（高） | 开关或静态规则（高） | 白名单（内置常用开发域名），不做首次询问（S2） |
| 越界 | 模型带标记重试，用户逐条审批（高） | 模型申请升级，用户审批（高） | 单独的出沙箱工具，用户逐条审批 |
| 交互 / PTY | 未说明 | `unified_exec` + `write_stdin` | macOS 整个 shell 进沙箱 |
| Windows | 不进沙箱（中高） | 有三种实现（高） | 实验性，默认关 |

## 4. 终态结构

```text
后端
├─ SandboxService（进程级，类比 MCPConnectionPool）
│   ├─ 读系统级 .cosir/config/sandbox.json（每次现读，指纹变了才推给 sidecar）
│   └─ 持有 sidecar 子进程：首次用到时拉起，崩溃后重拉，lifespan 关闭时关
├─ worker_client 拼启动帧（唯一接入点）
│   └─ 需要沙箱 → SandboxService.wrap(...) → 启动帧带包装后的 argv + env 叠加
└─ Terminal Worker：照常按 argv + env 起进程，不知道沙箱

srt sidecar（Node，调用 srt 库）
├─ 网络代理（沙箱里的命令经它出网）
└─ 违规记录（按 commandId 取）
```

### 4.1 sidecar 归后端

- **为什么归后端、不归宿主**：沙箱策略、审批、命令的启动与归属都是后端的事实；每条命令都要 wrap，走的是热路径；宿主工具链路只能由后端发起，而且后端单独跑时不可用（AGENTS.md「进程间接口边界」）。按「能力归它离不开的运行时依赖与事实所有者」，sidecar 归后端。
- **运行时**：打包后用 Electron 自带的 Node（`ELECTRON_RUN_AS_NODE=1` 运行宿主可执行文件）；开发期用系统 Node（≥ 22.12）。宿主照 worker 的样板注入两个变量：`CODING_AGENT_SANDBOX_NODE`（可执行文件）与 `CODING_AGENT_SANDBOX_SIDECAR`（脚本路径）。srt 与 sidecar 脚本由新的 stage 脚本放到 `target/resources/sandbox`，打包时走 `extraResources`。
- **协议**：stdin/stdout 上每行一个 JSON 请求 / 响应，只有四种：`configure(config)`、`wrap({commandId, command, shell, cwd, writeRoots, allowPty})` → `{argv, env}`、`violations(commandId)`、`release(commandId)`。commandId 用 `tool_call_id`（会话用会话 id）。
- **存活**：沿用后端自己的父管道契约。sidecar 的 stdin 由后端持有，读到 EOF 就退出；后端被强杀时，它随进程组一起被杀。
- **代价**：网络代理在 sidecar 里。sidecar 一重启，正在跑的沙箱命令就断网，这一点接受并记日志。wrap 全局串行，单次是毫秒级，不另做缓存。

### 4.2 接入点

- 后端 `worker_client` 拼 `start_exec` / `start_session` 帧这一处判断要不要包；要包就把包装后的 argv 与 env 叠加放进帧里。worker 的启动帧因此多一个 `env` 字段（只放叠加项，基础环境仍是 `user_command_environment()`）。
- `execute_terminal` 的解释器规则（`/bin/sh -c`、`cmd /c`、PowerShell）交给 srt 的 `binShell` 处理。Windows 的引号规则由 srt 统一处理（`parseWindowsBinShell`），后端不再另写一套。
- 进程归属不受影响：macOS 上 sandbox-exec 是原地 exec。Windows 上沙箱进程由 srt-win 经另一账户创建，worker 的 Job Object 可能罩不住它；srt-win 自带「父进程退出就杀整树」，所以 worker 杀掉 srt-win 时，整棵树一起走（列为待验证，见 §9）。

### 4.3 什么进沙箱

| 进程 | 进不进 | 理由 |
|---|---|---|
| `execute_terminal`（所有 Agent） | 进 | 主路径 |
| Agent 新建的交互终端 | macOS 进（整个 shell，`allowPty`）；Windows 进不了，见 §4.5 | S4、S5 |
| 用户新建的交互终端 | 不进 | 是用户自己的终端，对齐 Claude Code 的 `!` 命令与 VS Code |
| 用户在 Agent 的沙箱终端里打字 | 跟着这个终端，在沙箱里 | 沙箱随进程启动时定下，没法中途换；终端上有标记 |
| 出沙箱审批通过的命令 / 终端 | 不进 | §4.5 |

会话事实里新增 `sandboxed`，由 worker 启动时决定、投影进 snapshot `terminal_sessions`。**Agent 只能往沙箱内的会话输入**：往用户终端（不在沙箱里）打字等于绕过沙箱，所以这类输入一律拒绝，并告诉 Agent 改为自己新建终端。这是有意改变的行为（§6）。

### 4.4 默认策略与配置

配置文件放在系统级 `.cosir/config/sandbox.json`，契约用 pydantic（strict、`extra="forbid"`），写法照抄 MCP 配置：

- `enabled`：macOS 默认开，Windows 默认关。
- **写**：允许当前 Task 的工作区根目录（每条命令用 `customConfig` 传入，所以不同工作区的 Task 互不越界）和系统临时目录；禁止写工作区的 `.cosir/`。srt 内置的保护项照用（`.git/hooks`、`.git/config`、`.gitmodules`、shell rc、`.vscode`、`.idea`，见 `sandbox-utils.ts:21-47`）。
- **读**：全部允许，只禁读受保护路径（见下）。
- **网络**：
  - 白名单默认带常用开发域名：npm、PyPI、crates、Go proxy、GitHub 及其 raw / codeload / objects 域名。用户可增删。
  - 白名单之外一律拦截，**不做首次访问时询问**（S2：询问到不了发起它的 Task）。
  - `allowLocalBinding` 默认开：本地起开发服务器、内置浏览器打开 localhost 是核心用法。它的代价是沙箱里的命令能访问后端的 localhost API（S6）。这个问题并入已有的安全待办，本次不做。
- **配置变化**：后端每次现读配置；指纹一变，就 `configure` 推给 sidecar。网络白名单可以热更新；文件系统规则对之后启动的命令生效。

**受保护路径（一份事实，三处消费）**：

- 内置部分是系统 `.cosir` 里除 `skills/` 以外的几项（S11），由路径布局的事实源 `system_cosir.py` 给出：新增的系统目录在这里登记时一并决定要不要保护，不另立清单。
- 用户追加的部分（如 `~/.ssh`）写在 `sandbox.json` 里，由配置契约合并成完整列表。
- 消费方有三个：沙箱策略的禁读项、`resolve_for_read`（覆盖读文件工具与浏览器上传）、以及之后接入的任何读本机文件的通道。路径判定跟随符号链接，与 `resolve_for_read` 现有的解析一致。
- 这条规则**不随沙箱开关**：沙箱关闭或不可用时，读文件工具与上传照样禁读受保护路径。它是工具自己的规则，和系统级沙箱无关，所以可以先于沙箱单独落地。

设置页新增一个「沙箱」标签，界面保持克制，只放四样：开关、当前状态（可用 / 需安装 / 不可用及原因）、网络白名单（列表编辑）、额外受保护路径。

系统提示词里那句「not sandboxed」改为按实际状态生成：写清哪些地方可写、网络受白名单限制、被拦时该怎么做。

### 4.5 越界与审批

- **被拦**：命令照常结束（报权限错误或连接失败）。后端按 commandId 向 sidecar 取违规记录，在工具结果末尾附一段固定格式的摘要（被拒的写入路径、被拦的域名），卡片短提示显示「沙箱拦截」。模型据此自己判断：换一种做法，或者申请出沙箱。
- **一次性命令出沙箱**：新增工具 `execute_unsandboxed`。参数与 `execute_terminal` 相同，另加一个 `reason`；声明 `user_decision="execute"`。之所以单独成工具，是因为 S7：这样它天然只给侧栏 Task，必须单独调用；审批卡展示原始命令、工作目录和理由；批准后走同一条 worker 执行路径，只是不包沙箱。子 Agent 与 Team 节点拿不到这个工具，被拦时只能把情况报回上级，由侧栏 Agent 来申请，这和 Task 类型规则一致。
- **交互终端出沙箱**：`terminal_start` 增加 `unsandboxed` 与 `reason` 两个参数，并声明 `user_decision="execute"`（它本来就只给侧栏，代价只是要单独成批）。只有两种情况走审批：显式要求出沙箱，或者当前平台放不进沙箱（Windows）。其余情况下不等待，直接启动。出沙箱的 Agent 终端在标签上带一个警示标记。
- **审批粒度**：v1 只有「批准本次」和「驳回」，不做「本会话内都批准」。
- **通用审批卡**：等待卡新增一种通用的 execute 审批，展示原始参数并提供「批准 / 驳回」两个按钮（S9）。以后浏览器的高风险动作也能复用它。

### 4.6 不可用时

沙箱已开启、但当前不可用（sidecar 起不来、Windows 未安装、依赖缺失）时，**Agent 的终端命令直接失败**，错误里写明原因和处理办法（去设置页安装或关闭沙箱）；设置页显示同样的状态。不悄悄降级成不带沙箱运行。Claude Code 默认是降级继续跑；这里不这样做，因为那正是「禁止静默失效」要防的情形。

### 4.7 平台分级

- **macOS**：一次性命令和交互终端都进沙箱，默认开启。
- **Windows**：实验性，默认关闭。
  - 开启前，先在设置页点「安装」，会触发一次 UAC（由 sidecar 调 srt 的 `windows-install`）。
  - 只有一次性命令进沙箱；Agent 新建交互终端一律要审批。
  - S5 列出的环境差异要在设置页上写明。
  - 实机验证通过之前不建议用户开启。
- **Linux**：不是桌面目标平台，不支持。

### 4.8 日志

- sidecar 的 stderr 写到 `.cosir/logs/sandbox/sidecar.stderr.log`，照 MCP 的做法轮转。
- 后端结构化日志事件：`sandbox_configured`、`sandbox_wrap_failed`、`sandbox_violation`（带 `task_id`、`tool_call_id`、被拒的路径或域名）、`sandbox_sidecar_restarted`。

## 5. 分阶段（统一设计第 2 阶段之后；每步可单独验证）

0. **受保护路径**（不依赖统一设计）：`system_cosir.py` 给出内置列表，`resolve_for_read` 消费它；读文件工具与浏览器上传被拒时说明原因。
1. **sidecar 与 SandboxService**：Node 脚本、协议、生命周期、stage 打包、设置页状态显示。这一步不改任何命令行为。
2. **macOS 一次性命令进沙箱**：默认策略、违规摘要、系统提示词、设置页的白名单编辑。
3. **出沙箱审批**：`execute_unsandboxed` 工具、通用审批卡、工具目录与展示契约。
4. **macOS 交互终端进沙箱**：会话的 `sandboxed` 事实、Agent 输入规则、`terminal_start` 审批、标签标记。
5. **Windows 实验支持**：安装流程、一次性命令进沙箱、实机验证。

## 6. 行为变化

- Agent 的命令不能再写工作区外的路径、不能读受保护路径，白名单以外的网络连不上。
- `read_file` 等读文件工具与浏览器上传不能再读受保护路径，不论沙箱开没开。
- 子 Agent 与 Team 节点的命令被拦后不能自己出沙箱，只能报回上级。
- Agent 不能再往用户新建的终端里输入。
- 新增一个工具 `execute_unsandboxed`；`terminal_start` 多两个参数，并且要单独调用。工具 schema 在 Task 创建时固化，已有的 Task 不受影响（绿地项目，不做兼容）。
- 沙箱开启但不可用时，Agent 的终端命令直接失败。

## 7. 问题清单

- **注释漂移**（只报告，不在本次改）：`execute_terminal.py:3`、`execute_terminal.py:68`、`local_backend.py:78` 还在提已删除的危险命令策略。统一设计第 2 阶段会重写这几处，届时一并修正。
- **localhost 后端 API 无认证**（S6）：沙箱放行 localhost 时，等于放行后端 API。并入 AGENTS.md 待办「安全与权限」，不在本次处理。

## 8. 刻意不做

- **网络首次访问时询问**（S2）：用静态白名单加「出沙箱」审批代替。
- 工作区级的策略覆盖、「本会话内都批准」、按命令前缀自动放行。
- MCP stdio 服务进沙箱：服务由用户自己配置，Claude Code 也不把它放进沙箱，只在待办里记一笔。
- 内置浏览器的权限模型（按站点授权、高风险动作确认、经浏览器外传）：留在 AGENTS.md 待办「安全与权限」，理由见 §1.1；上传禁读受保护路径除外，它随 §4.4 一起做。
- 后端自身的网络访问。
- Linux。
- srt 的凭据遮罩、TLS 中间人等高级特性。

## 9. 风险与待验证

- **Electron RunAsNode fuse**：以后签名打包时如果关掉这个 fuse，sidecar 就得换成自带 Node 或 `utilityProcess`。打包阶段要确认。
- **Windows 进程归属**：srt-win 经另一账户创建的进程是否会漏出 worker 的 Job Object；worker 杀掉 srt-win 时，整棵树是否真的一起结束。
- **macOS 兼容性**：
  - 不认代理环境变量的工具连不上网，比如部分数据库客户端；
  - docker 不兼容；
  - 走 SSH 的 git 可能失败；
  - 违规监控底层是 `log stream`，开销要实测。

  这些情况的出路是「出沙箱」。
- **srt 上游更新频繁**：锁定版本，升级时重跑沙箱的集成测试。
- **sidecar 重启**：正在跑的沙箱命令会断网（§4.1）。

## 10. 参考：Codex 的权限模型（2026-11-03 读源码，提交 `ac91c144`）

整体设计「配置化白名单 + 黑名单 + 越界审批」时的主要参照。Codex 分三层，审批结果会写回前两层；srt 只相当于第一层的执行部分，规则与审批要由嵌入方自己做。

| 层 | Codex 的做法 | 证据 | 对应到本项目 |
|---|---|---|---|
| 权限配置 | `config.toml` 的权限 profile：内置 `:read-only` / `:workspace` / `:danger-full-access`，自定义 `[permissions.<名字>]` 可 `extends`。文件按路径给 `read` / `write` / `deny`，有特殊路径（`:project_roots`、`:tmpdir` 等）与通配（`"**/*.env" = "none"`）；工作区的 `.git`、`.agents`、`.codex` 在可写根下也自动只读。网络按域名给 `allow` / `deny`（支持 `*.x`、`**.x`，拒绝全局 `*`），另有只读网络模式、本机端口开关、请求改写（去掉认证头、代注密钥）。配置分层，项目级 `.codex/` 可覆盖用户级 | [permissions_toml.rs:113-347](https://github.com/openai/codex/blob/ac91c14443c11c4279a9305099354e443c6fa7d4/codex-rs/config/src/permissions_toml.rs#L113)、[permissions.rs:43-45,832-834](https://github.com/openai/codex/blob/ac91c14443c11c4279a9305099354e443c6fa7d4/codex-rs/protocol/src/permissions.rs#L43)、[models.rs:414-420](https://github.com/openai/codex/blob/ac91c14443c11c4279a9305099354e443c6fa7d4/codex-rs/protocol/src/models.rs#L414)、[config_layer_source.rs](https://github.com/openai/codex/blob/ac91c14443c11c4279a9305099354e443c6fa7d4/codex-rs/config/src/config_layer_source.rs) | 文件与网络的白名单 / 黑名单；受保护路径（§4.4）就是 `deny` 项 |
| 命令规则 | Starlark 规则文件 `~/.codex/rules/*.rules`：`prefix_rule(pattern, decision=allow\|prompt\|forbidden, justification, match, not_match)`、`network_rule(host, protocol, decision)`；`match` / `not_match` 是加载时校验的例子。未命中规则的命令交给内置的安全 / 危险命令判断 | [execpolicy/README.md](https://github.com/openai/codex/blob/ac91c14443c11c4279a9305099354e443c6fa7d4/codex-rs/execpolicy/README.md)、[parser.rs:349,410](https://github.com/openai/codex/blob/ac91c14443c11c4279a9305099354e443c6fa7d4/codex-rs/execpolicy/src/parser.rs#L349)、[exec_policy.rs:56-58](https://github.com/openai/codex/blob/ac91c14443c11c4279a9305099354e443c6fa7d4/codex-rs/core/src/exec_policy.rs#L56) | 命令级白名单（allow）、审批（prompt）、黑名单（forbidden），取代已删的 deny-list |
| 审批 | `approval_policy`：`untrusted` / `on-request`（默认）/ `granular`（按类别开关）/ `never`。模型执行时带 `sandbox_permissions`：`use_default` / `require_escalated`（出沙箱）/ `with_additional_permissions`（留在沙箱里只给这一条命令加权限）。审批选项：批准本次、本会话都批准、批准并写入命令规则、批准并写入网络规则、驳回（附理由）、中止。代理拦下未知域名时实时询问，因为代理以库的形式跑在 Codex 进程里、知道请求属于哪次调用 | [protocol.rs:1163-1203](https://github.com/openai/codex/blob/ac91c14443c11c4279a9305099354e443c6fa7d4/codex-rs/protocol/src/protocol.rs#L1163)、[models.rs:66-75](https://github.com/openai/codex/blob/ac91c14443c11c4279a9305099354e443c6fa7d4/codex-rs/protocol/src/models.rs#L66)、[protocol.rs:4454-4488](https://github.com/openai/codex/blob/ac91c14443c11c4279a9305099354e443c6fa7d4/codex-rs/protocol/src/protocol.rs#L4454)、[network_approval.rs](https://github.com/openai/codex/blob/ac91c14443c11c4279a9305099354e443c6fa7d4/codex-rs/core/src/tools/network_approval.rs) | 复用 `user_decision`；「批准并写入规则」让名单随使用长出来 |

执行链路：配置解析成权限 profile → `SandboxManager` 把命令改写成带沙箱的 argv、环境变量与网络代理（[manager.rs:102-117](https://github.com/openai/codex/blob/ac91c14443c11c4279a9305099354e443c6fa7d4/codex-rs/sandboxing/src/manager.rs#L102)）→ macOS 固定调用 `/usr/bin/sandbox-exec -p <生成的规则>`，基础规则默认全禁、参照 Chrome（[seatbelt.rs:61](https://github.com/openai/codex/blob/ac91c14443c11c4279a9305099354e443c6fa7d4/codex-rs/sandboxing/src/seatbelt.rs#L61)、[seatbelt_base_policy.sbpl](https://github.com/openai/codex/blob/ac91c14443c11c4279a9305099354e443c6fa7d4/codex-rs/sandboxing/src/seatbelt_base_policy.sbpl)）；Linux 是 bwrap + seccomp + landlock；Windows 是受限 token 或 Win11 原生容器 MXC（[sandbox.rs:29-35](https://github.com/openai/codex/blob/ac91c14443c11c4279a9305099354e443c6fa7d4/codex-rs/protocol/src/sandbox.rs#L29)），都支持 ConPTY。网络只能经 `codex-network-proxy`（HTTP + SOCKS5）出去，直连由系统沙箱堵上。

对本项目的结论（整体设计时再定，不在本稿实施）：

- **规则与审批的模型照 Codex 做**，与执行层选 srt 还是 Codex 无关。
- **执行层若改用 Codex 的代码**，可以直接并进 Rust worker：代理随命令一起启动，Windows 交互终端也能进沙箱，「首次访问域名时询问」也能定位到 Task（解决 S2）。代价是这些 crate 没有发布、依赖 Codex 内部的 `codex-protocol` 类型，只能复制进仓库自己维护，更新频繁（8 月以来 `sandboxing` 66 次、`windows-sandbox-rs` 85 次提交）。采用前先做可行性验证：拆分后的代码量与依赖、macOS 两种终端、Windows 实机 ConPTY。
