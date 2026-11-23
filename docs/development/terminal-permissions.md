# 终端权限：实现与验收

本次实现只覆盖 Agent 的 `execute_terminal`，平台为 macOS 与 Linux。用户交互终端、文件工具、内置浏览器和 MCP 继续使用原有边界。它不是整个 Agent 或整个桌面应用的统一安全模式。

## 两档行为

| 模式 | 默认文件范围 | 默认网络 | 越界处理 |
| --- | --- | --- | --- |
| 请求批准 `request_approval` | Session 可写根和 own scratch 可写；其他可读路径只读；系统 `.cosir` 禁读，skills/scratch 保留读取例外 | 关闭 IP 网络 | 调用设置 `escalate_sandbox` 并在 `intent` 里说明用途，用户批准这条原命令后执行 |
| 完全访问权限 `full_access` | 当前 OS 用户权限 | 当前 OS 用户权限 | 直接执行，不进入终端沙箱，不要求单次批准；不自动 sudo |

终端权限是会话的事实（`sessions.terminal_permission_mode`），前端只从 snapshot 读、经命令切换，不在本地保存。新会话默认请求批准；本机确定不支持沙箱时默认完全访问权限，且不能选请求批准。运行中随时可切：Agent 每条命令启动时现读「本会话与所有权祖先中最严的那个」，降权立即对子 Agent 生效，已启动的命令沿用启动时的策略。`session_send` 不能发给另一棵所有权树里权限更宽的会话。

升级只对这条命令及其子进程生效，不记住永久授权；一次给足 IP 网络与工作区外写入（写域为 `/`），模型不必猜命令会写哪些目录。批准的是整个 shell 命令，不对管道内部逐步放行。网络包含互联网、局域网、localhost 与监听端口；不按域名过滤。控制目录、后端/宿主/前端程序和 Worker 保持只读，系统数据保持禁读，升级也不能解除；own scratch 是独立的显式写入例外。

路径在批准前解析，Rust 启动前重新验证实际路径。工作目录可以是可读的外部目录，工作目录不是权限边界。终端对文件系统不透明：不解析命令的读写目标，也不建立已读资格，写入范围只由 OS 沙箱限制；改到读过的文件由读后变更通知告知模型。

## 事实与进程边界

```text
Composer 模式 → UserInput/AgentMessage RunLaunch → RunLedger → RunRecord/SQLite
                                                    ↓
ExecuteTerminalTool：构建本条有效策略，等待单次决定，准备文件证据
                                                    ↓
LocalExecutionBackend → Worker start_exec(policy) → Rust 平台沙箱 → shell/子进程
                                                    ↓
终端输出读取沿用启动时的策略；批准投影进 Session snapshot
```

- `app/tools/terminal_runtime/permissions/policy.py` 构建有效文件与网络策略；Python 侧的终端观察消费它，不能在沙箱外替命令打开禁读文件。
- `core/tool/terminal_approval/registry.py` 保存进程内待批准请求，`yielding.py` 裁决它与新用户输入的先后；用户停止只撤本会话自己发起的批准。原工具线程等待，不使用 `ask_user`，不把命令重发给模型。错误显示会话、重复决定和过期决定拒绝。取消与批准竞争以真实裁决结果为准，批准先赢时继续走执行取消链。
- 后端重启、Session 删除、运行取消或调用取消使相关待批准请求失效。未批准命令不会执行；执行结果未知或存在部分副作用时不自动重放。
- 权限通知在每次模型请求前推导（`notices.request_notices`），有效权限或可写根变了才发。前置通知、空 assistant 草稿不会另占一个可见回合；写入与冷重建共用 `core/context/turn_visibility.py`。
- 日志事件 `terminal_policy_resolved` 和 `terminal_approval_changed` 关联 session/run/tool call；终端、批准、策略失败均使用现有落盘日志链路。

## macOS

Rust 直接构造 Seatbelt profile，经系统 `/usr/bin/sandbox-exec` 执行；默认拒绝，子进程继承。每个允许写域扣除控制区，scratch 单独恢复；保护路径的祖先禁止改名，特殊 fcntl 写入被拒绝。默认网络关闭，获准后只开放 IP 和 DNS/TLS 所需的精确系统服务，包括 `/private/var/run/mDNSResponder` 的出站解析通道。其他 Unix socket、任意 Mach 服务和桌面自动化通道仍受限制。

正常命令、超时、取消和进程组清理由现有 Worker 生命周期负责。macOS 下主动 `setsid`、daemonize 或其他脱离进程组的程序，不能保证全部随停止回收；它们仍继承沙箱限制。此实现不把命令停止等同于文件副作用回滚。

## Linux

Worker 同目录自带非 setuid 的 `cosir-bwrap`；不依赖系统 PATH 中的 bwrap，不启动 Node 服务，不让 Worker 自重入。独立 user/PID/IPC namespace，禁止再次创建嵌套 user namespace（同时覆盖 raw clone/clone3 取回 namespace capabilities 的路径）；默认另有 network namespace。根文件系统只读，再绑定本条写域；保护路径祖先固定为挂载点，防止改名后重建旧控制程序路径。先恢复只读保护，再遮蔽系统数据，恢复 skills/scratch 子挂载；最后把被遮蔽数据根设回只读。`/proc`、`/dev` 的固定视图晚于广泛目录授权，批准 `/` 也不能恢复宿主 proc。

seccomp 通过 FD 传给 bwrap，始终限制 ptrace、跨进程内存、mount、namespace 变更、io_uring 等旁路。网络授权只允许 AF_INET/AF_INET6，不开放宿主 Unix socket；匿名 AF_UNIX stream pair 用于子进程管理，DGRAM/RAW pair 均拒绝。x86_64 的 x32 编号空间拒绝。bwrap 的 PID 1 负责收割子进程，退出时销毁 PID namespace。限制模式停止时以整 namespace 无残留收尾为准，不保证内部 shell 的 HUP/TERM trap 得到宽限执行；完全访问路径沿用原停止策略。

构建依赖 C 编译器、pkg-config 与 libcap 开发包（Ubuntu/Debian 为 `libcap-dev`）；运行要求宿主允许非特权 user namespace、对应 namespace/mount 操作和 seccomp。能力探测真实启动一次最小沙箱，失败则拒绝请求批准模式，绝不自动裸跑。容器限制、AppArmor 或禁用 user namespace 的机器可能不可用，需要调整宿主环境或由用户显式选择完全访问权限。

第三方来源固定于 `apps/terminal-worker/vendor/`；bubblewrap 0.11.2 为 LGPL-2.0-or-later，Codex 复用的 profile/build/wrapper 规则为 Apache-2.0，保留源码来源、LICENSE、NOTICE。stage 产物携带第三方许可和 vendored C 源码；Linux 附带 helper，其他平台不附带它。

## 本机控制 API 身份

获准联网的命令能访问 localhost，因此后端不再仅依赖 CORS。Electron main 为每代后端生成随机 capability，仅在可信主窗口顶层 frame 的直连请求中由网络钩子注入。调用方自行提供的同名头先删除；外部请求重定向进后端不能取得身份，离开后端时不携带身份。

HTTP 与 WebSocket 在同一个 ASGI 边界验证；`/health` 公开，`/host/link` 保留独立的首帧 token，OPTIONS 只用于预检。capability 不进 renderer API、snapshot、工具参数或用户命令环境。开发工具若绕过 Electron 直接调用后端，须自行设置 `CODING_AGENT_API_TOKEN` 并携带对应 `X-Cosir-Capability`；没有无身份的开发旁路。

## 交付与验证记录（2026-11-22）

以下区分已执行与待执行，CI 配置本身不算通过证据：

- macOS Apple Silicon：真实 Rust Worker 全套 **51 项通过**，含保护根内普通授权和祖先改名回归；独立审查 36/36 原生/环境验证通过，包含大小写、firmlink、软链、hardlink 创建、IPv4/IPv6、Unix socket、Mach、fcntl 80/110 和真实系统 curl HTTPS。
- Electron：真实宿主、后端、SQLite、批准 UI 和 Rust 命令；仅模型 HTTP 响应替换为本地固定流。7 项桌面 E2E 通过：批准前不执行、允许一次、拒绝不执行、重复批准 409、完全访问下一 Run 生效；另验证 HTTP/SSE/终端 WebSocket 输入、iframe 拒绝、无身份 WS 拒绝、重定向无提权/泄漏、关闭与退出。
- 前端/宿主：1,226 项单测通过；前端与 Electron TypeScript 构建通过；release stage 的 sandbox probe 与 PTY 冒烟通过。
- 后端全量 **3,023 passed、23 skipped、1 xfailed**；独立审查发现的取消竞争、旧 full 子 Agent 后台唤醒、同事务读取和首轮通知/草稿回合问题均有对应回归。精确计数以本次交付结果为准。
- Linux Ubuntu 22.04 x86_64（kernel `5.15.0-124-generic`，Rust/Cargo 1.99.0）：bundled C helper 与 Worker 原生构建、**54 项 native 测试**、Clippy、release staging 和真实 sandbox probe 均通过。独立 release 验收：root（UID 0）与 nobody（UID 65534）各 **11/11 通过、0 失败、0 跳过**；覆盖写域、保护区、额外/父目录/根授权、原始 user namespace 创建、网络与 Unix socket、真实 HTTPS，以及正常结束/shutdown/后端 EOF/Worker SIGKILL 后的 setsid 子进程收尾。获准网络和完全访问的 HTTPS 均返回 HTTP/2 200，关闭网络不能解析/连接。源码、公开依赖、工具链与假数据只在专属临时目录验证，不修改系统工具链或服务；证据取回后，远端源码、工具链、依赖及两套测试假数据目录已清理。
  Release Worker SHA256：`e5b07223d57daaad186d80950fe5302879db86d8248f940deff9cebb30d3c23c`；bundled helper SHA256：`4b101e6cf816342cb4c3ac654aea7859a26dbd3ebc6c3644b9f606111009ce49`。原始 clone 的宿主/full-access 阳性对照成功，限制模式网络开/关均被内核配额拒绝，避免把宿主不支持误判为隔离有效。本次 Linux 实机证据限于该 x86_64 环境，aarch64 与其他发行版未实测。
- Windows：本次不实现限制模式，探测明确不可用；已有用户终端与完全访问路径保留，Rust Windows 目标交叉检查与 Clippy 已通过。

本次数据库增加 Run 权限字段，按绿地项目约定不做旧 schema 迁移。验收始终使用隔离的新数据库，未删除用户原数据。现有旧开发数据库需在停止应用后另行重建，或通过 `CODING_AGENT_DATA_DIR` 使用新数据根。

验证入口：

```sh
cargo test --package cosir-terminal-worker
cargo clippy --package cosir-terminal-worker --all-targets -- -D warnings
cd apps/backend
.venv/bin/python -m pytest -q
cd ../desktop
npm run build
npm run test:unit
npm exec playwright -- test -c playwright.desktop.config.ts desktop-smoke terminal-permissions local-control-identity
npm run stage:terminal-worker
npm run test:terminal-worker-bundle
```

macOS 原生 sandbox 测试必须在允许 sandbox-exec 的宿主执行，外部再套一层受限执行沙箱会使探测失败。测试不读取真实凭据，Electron 的 HOME 和数据根均隔离到临时目录；代码保持未提交。

相对开工前快照，本次约新增 10,600 行、删除/替换 660 行，其中约 6,550 行为 vendored 源码和许可；其余主要是权限执行、跨进程身份、批准展示及现有消费方/测试契约同步。原先 42 个已修改或未跟踪路径均保留，未重置或暂存用户改动；HEAD 未变化。精确文件数会随最终文档整理略有变化。


## GitHub Actions Linux 实测（2026-11-23）

当前工作区快照在 Ubuntu 22.04.5 LTS x86_64 上通过终端权限与沙箱核心验收。三次运行均先核对同一份 1,903 个源码文件的 SHA-256 清单。内核为 `6.8.0-1070-azure`，测试以普通用户 `runner`（UID 1001）运行。

| 范围 | 实测结果 |
| --- | --- |
| Rust Worker 与沙箱 | 56 项通过，含 11 项原生沙箱权限测试。Clippy、debug 和 release 沙箱探测、release 包 PTY 冒烟均通过。[主运行](https://github.com/tianjiashu/cosir/actions/runs/45109452457) |
| `execute_terminal` → release Worker | 默认受限、单次批准和完全访问分别在 IPv4、IPv6 回环服务上实测，6/6 通过。用实际文件和服务端请求计数核实：外部工作目录不会扩大写域；批准前命令未启动，批准后恰好执行一次；批准后控制目录仍只读，受保护数据仍禁读。[主运行](https://github.com/tianjiashu/cosir/actions/runs/45109452457) |
| release 网络与进程清理 | 8/8 通过，零跳过。默认受限时无法连接 IPv4、IPv6 回环服务和公网 HTTPS，联网授权及完全访问时可连接。命令自然退出、显式停止、后端 stdin EOF、Worker 被强杀后，宿主均确认测试子进程已退出。缺少随包 helper 时，受限命令启动失败，磁盘上没有执行标记。[release 补测](https://github.com/tianjiashu/cosir/actions/runs/45109713556) |
| Linux 桌面 | 终端前端单测 77 项通过，桌面构建成功。真实 Electron E2E 6/6 通过，覆盖允许一次、拒绝、新输入接管本人和子 Agent 的审批、终端 WebSocket 输入，以及后端重启后的控制身份换新。[主运行](https://github.com/tianjiashu/cosir/actions/runs/45109452457) |

### zsh 首轮失败与后端复测

首轮后端终端相关用例为 **427 通过、29 跳过、9 个夹具初始化错误**，主运行因此标红。9 个错误都发生在等待 zsh 集成进入 `ready` 时，用例尚未进入行为断言。历史上出过问题的超时温和停止用例在同一 runner 单独连续运行 3 次，全部通过。[首轮 Actions 日志](https://github.com/tianjiashu/cosir/actions/runs/45109452457)

首轮工作流漏了仓库 Linux CI 已有的 `compaudit` 准备步骤。对照运行捕获到真实 PTY 画面：`/usr/share/zsh/vendor-completions` 的写权限触发 zsh 的 `compinit` 交互确认，shell 停在提示处，集成状态保持 `starting`。按仓库配置移除该目录的组和其他用户写权限后，`compaudit` 无告警，集成进入 `ready`。同一源码、同一批后端用例复测为 **435 通过、30 跳过、0 错误**。首轮的 9 个错误中，8 项通过，1 项进入用例后按 Windows 专属条件跳过。[zsh 启动画面对照与复测](https://github.com/tianjiashu/cosir/actions/runs/45110157015)

最终 30 个跳过项中，9 项指定了 runner 上没有的 Homebrew bash 路径，21 项依赖 Windows、PowerShell 或 ConPTY 等平台条件。Linux 的 `/bin/bash` 和 zsh 均已实测。

## 新用户输入接管审批（2026-11-22）

单次批准绑定原工具调用；一轮内的不同命令不能共用批准。用户在审批展示会话成功发送新消息时，后端按 immediate 接纳，在同一 Run 内处理新回合。没有终端审批的普通发送仍排队；显式“立即发送”也遵循下面的批次接管规则。

- Actor 在共享 Session 操作闸门内协调新输入、批准和迟到的审批登记。先提交消息，再按既有队列顺序投递 mailbox，最后撤销审批、解除原工具等待。
- HTTP 快速路径与 Actor loop 走同一个首次投递边界；发送请求在提交后断开，也由 loop 接管。批准入口先投递已提交输入，不能越过这条输入批准旧命令。mailbox 按消息身份去重，旧快照重复投递不会撤销新回合的合法审批。
- 原待批命令返回 cancelled，确定没有启动；同批已完成结果保留，尚未开始的调用逐个补齐 cancelled 结果，再进入 observe / prepare_input，不留下未配对的 tool call，不重放旧命令。
- 子 Agent 的请求展示到最近的用户会话。新输入撤销该页面已有的全部旧审批，并按记录的 run_id 停止对应子驱动；独立分支、无待批审批的其他工作和已经启动的后台命令保留。迟到取消不能误停后来 Run。
- 输入准备、校验或入库失败不撤销旧审批；批准和输入接管按共享操作边界先后裁决。已经获准并启动的命令不承诺回滚。合法撤回新输入后，也不恢复旧审批和已跳过调用。
- “立即”在工具安全边界生效：等待审批会马上解除；其他已经开始的普通工具先按既有契约收尾。后台事件和 Agent 消息不等于新的用户指令，不触发此批次接管。

审批登记表迁入 core/tool，由工具执行控制层拥有；平台路径策略、沙箱和 Worker 继续归 tools / Rust。删除旧的 tools/terminal_runtime/permissions/approval_registry.py，不留转发或第二份登记。Run 模式、委派权限上限与命令 policy 未改变，没有增加持久审批、回合权限表或通用 epoch。

真实 Electron 新增 direct、send-now、child 三种场景：模型 HTTP 使用本地固定流，Electron、后端、SQLite、SSE、审批 UI 与 Rust Worker 均为真实实现。验证旧审批消失、迟到批准 409、旧命令及同批余项没有写文件、新输入能启动真实新命令、Run startedAt 与权限模式不变。既有允许一次、拒绝、完全访问模式回归也继续验证。

### 本轮验证结果与未决项

- 审批、调度、立即发送、工具批次、跨会话调用身份和依赖方向共 **122 passed**（最终同序回归）；覆盖发送失败、批准竞争、迟到登记、提交后 HTTP 消失、重复投递、子 Run 精确取消与独立分支保留。
- 真实 macOS Electron **4 passed**：三个新增输入接管入口，加原有允许一次、拒绝与完全访问模式场景。前端/Electron TypeScript 与生产构建通过，相关 Python Ruff 和迁移后的审批模块 strict mypy 通过。
- 两路独立审查完成。本次发现并修复提交后 HTTP 断开导致的审批接管缺口；新增测试假驱动漏清委派取消信号的问题修在夹具，原顺序独立对照由 49 passed / 1 failed 收敛为 50 passed。
- 最后一次全量后端为 **3,042 passed、1 failed、23 skipped、1 xfailed**，不能算全量通过。剩余失败是 `test_local_backend_process_cleanup.py::test_timeout_stops_the_command_gently_first` 的 HUP 标记未写出。本轮未修改该测试、LocalExecutionBackend、exec.rs 或 containment.rs。

**刻意不做：Worker 温和收尾契约。** 该用例以 full_access 直接调用 Worker，不经过本次输入/审批逻辑。独立原生实验确认，既有 Worker 发 HUP 后约 20ms 即升级 TERM，1 秒是 KILL 前总宽限；HUP handler 先等待 0.15 秒时连续 3/3 被 TERM 打断，忽略 TERM 后连续 3/3 完成收尾。它证明旧机制存在风险；原全量现场没有信号轨迹，不能认定这是该次缺标记的唯一原因。本方案明确不修改 Rust，保留此问题，后续需单独确定 HUP/TERM/KILL 的收尾契约。

另一轮全量曾出现原有 MCP OAuth 短 TTL 测试的刷新计数差异。独立基线不注入延迟也复现：1 秒令牌在首次长连接调用前已经过期，SDK 正常额外刷新，使精确计数断言从 1 变为 2；MCP 实现与本次基线一致。本次不修改 MCP；最后一次全量该项通过。

验证证据（本机临时目录，测试数据均隔离）：

- 最终相关回归：`/private/tmp/cosir-approval-targeted-final.log`
- 最终全量：`/private/tmp/cosir-approval-backend-final.log`
- 修正夹具后的独立同序对照：`/private/tmp/cosir-identity-fixed-sequence-review.log`
- 原生温和收尾实验：`/private/tmp/cosir-gentle-stop-probe.log`
- MCP 基线对照：`/private/tmp/cosir-mcp-refresh-review-Rhw4pb/`
- Electron direct / send-now / child 的截图与 JSON：分别位于临时 `cosir-desktop-e2e-IsKmNc`、`cosir-desktop-e2e-XyET5m`、`cosir-desktop-e2e-qofAFA` 目录。

改动账本：审批登记表整体迁移并删除旧路径；生产逻辑净增约 135 行，测试净增约 545 行，另同步架构约定与本文。测试增量主要来自输入提交/竞争/子驱动边界和三个完整 Electron 场景。本次审批改动保留在工作区，未执行提交；工作区同时存在的其他修改与并行身份诊断提交均予保留。
