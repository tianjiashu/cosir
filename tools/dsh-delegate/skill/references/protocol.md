# delegate bridge 协议（v0.2）

外部调用方（Claude Code / Codex 的 SKILL 脚本）通过本机 HTTP 访问 DSH 桌面版里由 `dsh-delegate` 插件发布的接口。端点来自 `$DSH_HOME/delegate.json`（插件每次启动重写，0600）：

```json
{
  "version": 1,
  "plugin": "dsh-delegate",
  "pluginVersion": "0.2.0",
  "origin": "http://127.0.0.1:51502",
  "routePrefix": "/delegate/v1",
  "token": "<32-byte hex>",
  "pid": 12345,
  "startedAt": "2026-11-10T11:00:00.000Z"
}
```

所有请求要求 `Authorization: Bearer <token>`；带浏览器 `Origin` 头的请求一律 403。响应都是 JSON；失败形如：

```json
{ "ok": false, "error": { "code": "session/not-found", "message": "..." } }
```

## 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/delegate/v1/health` | liveness；返回 pid、工作区数、内存中 job 数 |
| GET | `/delegate/v1/workspaces` | 工作区列表（id/title/path/status/未归档会话数） |
| POST | `/delegate/v1/workspaces` | `{path, title?}`；先 `mkdir -p` 再注册工作区 |
| GET | `/delegate/v1/sessions?workspaceId=&limit=&includeArchived=` | 按工作区筛会话 |
| POST | `/delegate/v1/sessions` | `{workspaceId \| cwd, agentPreset?, title?, sessionId?}` |
| GET | `/delegate/v1/sessions/{id}` | 被动状态（`active/running/queue/pendingInput`）+ cwd + 当前权限 |
| POST | `/delegate/v1/sessions/{id}/prompt` | `{text, permission?, mode?, waitMs?, expireMs?}` → job 视图（202） |
| POST | `/delegate/v1/sessions/{id}/cancel` | 取消当前回合（DSH 语义：保留 inbox） |
| GET | `/delegate/v1/sessions/{id}/last?turns=N` | 直接读会话最近 N 轮 assistant 文本（冷会话可用，job 丢失时兜底） |
| GET | `/delegate/v1/jobs/{jobId}?waitMs=` | 等待/读取 job；终态返回 `text` 与 `reason` |
| POST | `/delegate/v1/jobs/{jobId}/cancel` | 取消该 job 所属会话的当前回合 |
| GET | `/delegate/v1/presets` | 权限预设目录 + agent preset 列表 |
| GET | `/delegate/v1/sessions/{id}/pending` | 列出该会话当前待处理项（approval / question），含请求 id、种类、工具名、调用 id、理由、提升到的沙箱模式、工具调用参数 |
| POST | `/delegate/v1/sessions/{id}/pending/{requestId}/answer` | 作答：approval 用 `{decision:"allowed-once"\|"rejected", reason?}`；question 用 `{answer:{answers:[...]}}` |

## job 视图

```json
{
  "jobId": "uuid",
  "sessionId": "session-...",
  "state": "running",
  "text": "",
  "reason": null,
  "turn": null,
  "error": null,
  "pendingInput": [],
  "expireAt": 1730000000000,
  "createdAt": 1730000000000,
  "updatedAt": 1730000001000
}
```

`state` 取值：

| state | 含义 | 调用方动作 |
|---|---|---|
| `running` | 本轮还在跑 | 继续 `wait` |
| `suspended` | 回合挂起但没有待答事项（等后台工作） | 继续 `wait` |
| `input-required` | 回合在等 GUI 里的人工批准/回答；`pendingInput` 给种类。**由 per-job watcher 主动发现，约 1–2 秒内可见**，不需要等回合结束 | 通知用户处理；之后继续 `wait` 同一个 job |
| `done` | 正常结束，`text` 是本轮最后一条 assistant 文本 | 使用 `text` 继续 |
| `failed` | 回合 error/blocked，`error.message` 给原因 | 汇报失败，不要盲目重试 |
| `cancelled` | 被取消 | 汇报取消 |
| `lost` | 会话已确定消失（not-found/deleted），或空闲无 `turn/end`，或会话状态连续读不到 | 用 `last --session` 兜底；确认会话是否被删除 |
| `expired` | 超过 `expireMs + 2 小时硬上限` 仍未落终态（任务可能仍在跑） | 用 `last --session` 取已有输出；去 GUI 看该回合；不要再等同一个 job |
| `timeout` | 旧版兼容兜底，v0.3 不再产生 | — |

`reason` 是 DSH 的 `turn/end` kind：`completed` / `aborted` / `blocked` / `error` / `max-tokens` / `interrupted` / `forked`。

## 过期（observation window）语义

- `prompt` 的 `expireMs` 决定观察窗口，默认 **12 小时**、最大 **24 小时**；客户端写 `--expire 8h`。
- 窗口到点时**不直接判失败**，而是按当前会话状态决定延长或落终态：
  - 会话**确定消失**（`session/not-found` 等确定性错误）→ **立刻**落 `lost`（`error.code=session-gone`），不延长；
  - 会话状态**暂时读不到**（超时/内部错误）→ 允许延长，但连续读不到 **3 次**或累计 **30 分钟**即落 `lost`（`error.code=state-unreadable`，message 带最后一次错误）；
  - 仍在 running、有待办（`pendingInput` 非空）、或挂起（suspended）→ 延长一个 grace 步（≤30 分钟）继续观察；
  - 会话确实空闲、没有 `turn/end` 可观察 → 落 `lost`。
- **总时长硬上限**：从 job 创建起，超过 `expireMs` 后最多再延长 **2 小时**（`HARD_EXTENSION_MS`），到点无条件落 `expired`（message 带最后一次观察到的会话状态：running / input-required(...) / suspended），避免 `--until-done` 永远不退出。测试可用环境变量 `DSH_DELEGATE_HARD_EXTENSION_MS` 缩短这个值。
- job 视图新增 `extensions`（已延长次数）与 `lastObserved`（最后观察到的会话状态），落盘记录同时保存这两项。
- 每次延长与每次落终态都写一行结构化 JSONL 到 `$DSH_HOME/delegate-jobs/events.jsonl`（0600，超过 8 MiB 轮转为 `events.jsonl.1`），并尝试镜像到宿主 logger：
  `{time, event:"delegate.job.extend"|"delegate.job.settle", jobId, sessionId, reason/state, errorCode?, extensions, elapsedMs, lastObserved, unreadableStreak?}`。

## 持久化与恢复

- 每个 job 的视图（含终态 `text`）写在 `$DSH_HOME/delegate-jobs/<jobId>.json`（0600，原子写），终态记录至少保留 **24 小时**。
- 插件热重载或桌面版重启后：
  - 已终态的 job 直接可按 `jobId` 取回（`GET /jobs/{id}`）；
  - 未终态的 job 会尝试重新挂到同一会话的 `turn/end` 监听；如果会话已没有 live Agent，则落 `lost` 并写明原因（不会静默消失）；
  - 重启窗口里错过的 `turn/end` 会从会话历史里补读，补到就直接结算。
- 一个会话同一时刻只允许一个未结束 job：重复 `prompt` 返回 409 `session_busy`，并带上已有 `jobId`。

## input-required 的发现方式

DSH 没有可订阅的「审批待决」事件：`pendingInput` 是 `typertGateway.getPendingRemoteEvents()` 的采样结果，浏览器 UI 也是靠 `sessionController.wait` 内部每 1 秒采样一次被动状态。因此插件不自己造轮询，而是每个活跃 job 跑一个 watcher：

- 反复调用 `sessionController.wait({sessionId, timeoutMs: ≤5s})`（`DSH_DELEGATE_WATCH_MS` 可调窗口，默认 5000）；
- 每次返回的被动状态里 `pendingInput` 非空（优先于 `running`）→ job 置 `input-required`，`awaitJob` 立即返回（客户端 exit 7）；
- 审批通过后状态回到 `running` → job 回到 `running` 并记 `delegate.job.resumed`；
- 每次 `GET /jobs/{id}` 也会做一次一次性刷新；
- watcher 在 job 落终态时随 AbortSignal 停止；`session/not-found` 立刻落 `lost`（`session-gone`），其他读错误进入可读性预算（30 分钟）。

状态翻转写 `events.jsonl`：`delegate.job.input-required` / `delegate.job.resumed`，job 文件同步。

## 审批/问答的作答入口（v0.4）

GUI 的审批与问答本来就是**转发到浏览器的 waterfall**：Host 把 `approval/request` / `user-questions/request` 投影到 `$events` 流，浏览器通过 `$events/result` 回写决定；网关结算后给其他已投递的客户端发 `cancel`。桥用**同一个入口**：它以一个独立的 owner answering Peer 连接 `$events`，因此能看到与 GUI 完全相同的请求；谁先作答谁结算，另一侧的 GUI 卡片会因为 `cancel` 自动收起，回合照常继续。没有另造审批链，也没有插 waterfall。

- `approval` 的决定词只有 `allowed-once` / `rejected`（DSH 契约没有"总是允许"，也不允许附带文本）。`--reason` 只进 bridge 审计日志，**不会**交给 Agent——Agent 收到的是 DSH 标准拒绝文案。
- `question` 的 answer 形状是 `{answers:[{id, selected:[labels], custom?}]}`，与 GUI 提交的一致。
- 请求 id 就是 `$events` 的事件 id；被 GUI 抢先作答、被取消或已过期时，bridge 返回 409 `request-not-pending`。

## 权限

- 允许值来自 `GET /presets` 的 `permissions.options[].value`。
- 插件在 `prompt` 前通过 DSH 的 permission preset 写入路径切换（durable `permission/preset` + `sandbox/mode` + `approval/policy`），对该会话后续轮次持续生效。
- `workspace-write` 配 `ask`：越界操作挂起成 `input-required`，由 GUI 里的人工决定；`danger-full-access` 是 sandbox 全开 + 不询问，只在用户明确要求时使用。

## 退出码（SKILL 脚本）

| 退出码 | 含义 |
|---|---|
| 0 | 成功（`done`） |
| 2 | 参数错误（含非法 `--expire`） |
| 3 | 认证失败（令牌失效，重启宿主后重试即可） |
| 4 | 找不到/连不上 delegate bridge（Harness 没开，或插件没装） |
| 5 | 失败/取消/丢失（`failed`/`cancelled`/`lost`/`timeout`） |
| 6 | 还在运行（只在有界 `--wait` 且未加 `--until-done` 时出现） |
| 7 | 需要人工处理（`input-required`）；stdout 第一行是 `input-required: <种类>`，随后是待处理项摘要 |
| 8 | 作答冲突：请求已不在 pending（被 GUI 答过/取消/不存在）或 Host 拒绝作答 |

## 排障

- `health` 退出 4：确认桌面版在运行；确认 `$DSH_HOME/delegate.json` 的 `origin` 端口与当前 GUI 一致（端口每次启动随机）。
- 退出 3：重启过 Harness 后令牌会变，脚本每次调用都重读端点文件，重试即可。
- `prompt` 返回 409 `session_busy`：同一会话已有 job；用 `wait --job`。
- `input-required`：用户在 GUI 处理完审批/问答后，Run 继续，同一 job 的 `wait` 会拿到 `done`。
- job 显示 `lost`：用 `last --session <id>` 读会话里已落盘的最后一轮答复。
- 升级插件后 `health` 仍显示旧版本：重启桌面版（新的 JS 模块代需要重新加载）。
