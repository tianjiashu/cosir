---
name: dsh-delegate
description: 把本机 DeepSeek Harness 当成阻塞式子 Agent 调用。用于查询工作区（含会话数）、创建工作区、在工作区里创建会话、给会话指定权限并发送输入、等待这一轮的最终答复；也用于继续已有会话或用 last 兜底读取已落盘的答复。当用户说“让 DSH 去做/另开一个会话跑/交给子 Agent/问一下本机 Harness”，或需要把一段独立调查、实现、评审外包出去再拿回结论时使用。不用于普通文件读写或纯本地脚本执行。
---

# DSH 子 Agent（delegate bridge）

你通过本目录的 `scripts/dsh_delegate.py` 调用本机正在运行的 DeepSeek Harness 桌面版。**不要手写 curl、不要猜端点**：脚本负责发现端点（`$DSH_HOME/delegate.json`）、令牌、超时、断点续等，以及把最终答复交回给你。

## 何时使用

- 把一段可以独立完成的任务交给本机 Harness Agent（它在真实工作区里读写文件、跑命令）。
- 查询/整理本机的工作区与会话，或继续一个之前的外部会话。
- 需要“另一个 Agent 的结论”而不是在当前上下文里展开过程。

不要用于：需要你全程盯着的逐步确认；只读一个本地文件（直接读）；把 DSH 当成第二个 shell。

## 工作流（两种，按你的宿主能力选）

### A. 宿主能后台运行命令（推荐，Claude Code `run_in_background` / Codex 轮询）

一条命令提交并阻塞到终态，退出即唤醒你：

```bash
python3 scripts/dsh_delegate.py prompt \
  --session <sessionId> --permission workspace-write --expire 8h \
  --until-done --out /tmp/dsh-answer.txt - <<'PROMPT'
把 X 改成 Y，跑测试，提交，回报结论与验证证据。
PROMPT
```

- 退出 0：stdout 是答复（截断版），完整答复在 `--out` 指定的文件；*先读文件*再决定下一步。
- 退出 7：回合在等人工处理（审批/回答）。plug-in 的 watcher 约 1–2 秒内就能发现；stdout 第一行是 `input-required: <种类>`，随后列出待处理项。你可以**自己批**（推荐，见下）或交给用户。处理完用同一条后台命令继续同一个 job：
  `python3 scripts/dsh_delegate.py wait --job <jobId> --until-done --out /tmp/dsh-answer.txt`
- 退出 5：失败/取消/丢失/过期（`failed` / `cancelled` / `lost` / `expired`），stdout 第一行给原因；`lost`/`expired` 还会打印 `extensions:` 与 `last observed:`。
- 退出 4：Harness 没开或插件没装；退出 3：令牌失效（重启过宿主，重试即可）。

后台命令被宿主打断也没关系：job 与结果都落盘，之后用 `wait --job <jobId>` 继续，或 `last --session <id>` 兜底。

### B. 宿主只能前台阻塞（单次调用有上限）

```bash
printf '%s' '任务正文' | python3 scripts/dsh_delegate.py prompt \
  --session <sessionId> --permission workspace-write --wait 90 -
```

- 退出 0/5/7 同上；退出 6 = 还在跑，stdout 给 `job:`，继续 `wait --job <jobId> --wait 90`，最多 6 次；仍未结束就把 jobId 报给用户，不要重复提交同一提示。

## 选工作区与会话

1. `python3 scripts/dsh_delegate.py health`
2. `python3 scripts/dsh_delegate.py workspaces`（每行：`id 会话数 标题 路径`）
3. 复用会话：`python3 scripts/dsh_delegate.py sessions --workspace <id>`；
   新建会话：`python3 scripts/dsh_delegate.py session-create --workspace <id> --title "claude: 任务名"`
4. 新建工作区：`python3 scripts/dsh_delegate.py workspace-create /绝对路径 [--title 标题]`

会话是持久的：把 `sessionId` 写进你的结论，用户下次可以让你继续同一个会话。

## 审批与问答（你可以自己作答）

`pending --session <id>` 列出待处理项；`--json` 有全量字段（请求 id、工具、调用 id、提升到的沙箱模式、理由、工具参数、question 的问题与选项）。

```bash
S=scripts/dsh_delegate.py
python3 $S pending --session <sessionId> --json
python3 $S approve --session <sessionId> --request <requestId> --reason "在其工作区内写文件，任务直接相关"
python3 $S deny    --session <sessionId> --request <requestId> --reason "超出任务范围"
python3 $S answer  --session <sessionId> --request <requestId> --option "Yes"
```

纪律（务必遵守）：

- 只批准**与委派任务直接相关、范围可解释**的请求（例如在它自己的工作区/worktree 里写文件、跑本仓库测试）。
- 推送、部署、删除工作区外数据、读取或写入凭据、修改全局配置——一律 `deny` 或交给用户。
- `--reason` 只进 bridge 审计日志（`events.jsonl`），**不会**传给 Agent；Agent 收到 DSH 标准拒绝文案。
- 作答成功 exit 0；请求已被 GUI 抢先处理/已取消 → exit 8（`request-not-pending`），不要重试，用 `pending` 刷新再看。
- 你没有"总是允许"：每次都要按请求 id 单独作答。

## 权限

- `--permission` 取 `presets` 命令列出的值：`read-only`、`workspace-write`、`danger-full-access`（启用 auto review 时还有 `auto`）。
- 默认用 `workspace-write`；只有用户明确要求“不限权限”时才用 `danger-full-access`，并在汇报里写明。
- 权限对该会话后续轮次持续生效；`workspace-write` 下越界操作会挂起成 `input-required`，等人批准。

## 兜底与排障

- `last --session <id> [--turns N] [--out FILE]`：直接读会话里最近 N 轮最终 assistant 文本，不依赖 job 内存。
- 遇到 `lost`：job 的回合已无法观察（会话被删/空闲无 turn end/状态连续读不到）。先用 `last` 取已有输出；确认会话还在的话，可以新建会话重投，或让用户看 GUI。
- 遇到 `expired`：任务超出了观察窗口 + 2 小时硬上限，可能仍在跑。先用 `last` 取已有输出并汇报 `extensions`/`last observed`，再去 GUI 看该回合；不要继续等同一个 job。
- 同一会话同时只能有一个 job；再提交得到 `session_busy`（409），应改为 `wait --job`。
- stdout 只放答复；默认截断 8000 字符（`--max-chars` 调整，`--out` 保存全文）。
- `$DSH_HOME/delegate-jobs/<jobId>.json` 是终态结果落盘位置（至少保留 24 小时）；不要修改 `$DSH_HOME/delegate.json`，不要绕过脚本直连端口。

## 参考

- `references/protocol.md`：HTTP 端点、job 状态机（含 `lost`）、过期与恢复语义、退出码。
