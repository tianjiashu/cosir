# dsh-delegate

把本机 **DeepSeek Harness 桌面版** 当成外部 Agent（Claude Code / Codex）的**阻塞式子 Agent** 调用。

```
Claude Code / Codex
  └─ SKILL  dsh-delegate
       └─ scripts/dsh_delegate.py  ──HTTP──▶  DSH 桌面版
                                              └─ dsh-delegate 插件
                                                   ├─ workspaceRegistry   列/建工作区
                                                   ├─ sessionController   建会话 / prompt / 取消 / 历史
                                                   ├─ permissionPresets   指定权限
                                                   ├─ session/event       等 turn/end 拿最终答复
                                                   └─ $DSH_HOME/delegate-jobs/   job 落盘与恢复
```

## 组成

| 路径 | 作用 |
|---|---|
| `plugin/` | DSH 宿主插件（Cordis bundle），注册 `/delegate/v1` 本机 HTTP 路由 |
| `skill/` | 给 Claude Code / Codex 的 SKILL：`SKILL.md` + `scripts/dsh_delegate.py` + `references/protocol.md` |
| `plugin/test/`、`skill/test/` | 插件纯逻辑与客户端契约测试 |
| `install.sh` | 把插件复制到稳定目录、把 skill 软链到两个 Agent 的 skills 目录 |

## 安装 / 升级

```bash
# 1) 插件复制到版本化目录（link 依赖，装完后不要移动/删除）
./install.sh                       # -> ~/dsh-plugins/dsh-delegate-<version>

# 2) 装进 desktop profile（首次安装；二选一）
#    a. 在 DSH 里对 Agent 说：用 plugin_manager install_bundle 安装 ~/dsh-plugins/dsh-delegate-<version>
#    b. 或用 CLI：dsh plugin --profile desktop add ~/dsh-plugins/dsh-delegate-<version>

# 3) 验证
python3 ~/.agents/skills/dsh-delegate/scripts/dsh_delegate.py --json health
```

**升级插件代码**（两种都行）：

```bash
./install.sh   # 复制成新的版本目录
# A. 不重启宿主（live reload，约 1 秒）：
#    plugin_manager remove_bundle  target="dsh-delegate"
#    plugin_manager install_bundle target="$HOME/dsh-plugins/dsh-delegate-<new-version>"
# B. 省事：把版本目录指回同一路径 / 或直接装新目录后重启桌面版
```

运行中的宿主按文件 URL 缓存 JS 模块，所以**同一路径覆盖文件不会热加载**；换新版本目录再 remove+install 才会加载新的模块代。重启宿主后无论路径是否变化都会加载当前版本。验证用 `health` 的 `version` 字段。

## 使用（在 Claude Code / Codex 里）

宿主能后台跑命令时（推荐）：

```bash
python3 ~/.agents/skills/dsh-delegate/scripts/dsh_delegate.py prompt \
  --session <sessionId> --permission workspace-write --expire 8h \
  --until-done --out /tmp/dsh-answer.txt - <<'PROMPT'
把测试跑一遍，把失败原因与修复建议写清楚。
PROMPT
```

- 退出 0：答复在 stdout（截断）与 `--out` 文件（全文）。
- 退出 7：需要人在 DSH GUI 处理；记下 `jobId`，处理完用 `wait --job <jobId> --until-done` 继续。
- 退出 5：失败/取消/丢失；退出 4：DSH 没开或插件没装。

手工查询：

```bash
S=~/.agents/skills/dsh-delegate/scripts/dsh_delegate.py
python3 $S workspaces
python3 $S session-create --workspace <id> --title "claude: 跑测试"
python3 $S last --session <sessionId> --out /tmp/last.txt
```

## 设计要点

- **工作区/会话是 GUI 同一份状态**：插件跑在桌面版宿主进程内，`sessionController.create({workspaceId})` 会把会话 attach 到工作区，GUI 侧栏可见、可接管。
- **长任务不会被 job 自己掐断，但也不会无限挂起**：观察窗口默认 12 小时（`--expire` 可调，最大 24 小时）；到点时按会话状态延长（running / 等人工 / 挂起），会话确定消失则立刻 `lost`，暂时读不到状态连续 3 次或 30 分钟也 `lost`；从创建起超过「窗口 + 2 小时硬上限」无条件 `expired`（带最后观察到的会话状态与延长次数）。
- **结果是事件驱动的**：监听 `session/event` 的 `turn/end` 与 `agent/status`，不用 `session/wait` 的 idle 猜本轮是否结束（Run 挂起时 idle 会误判）。
- **审批等待也能被唤醒**：DSH 没有可订阅的审批待决事件，插件给每个活跃 job 跑一个 watcher，复用 `sessionController.wait`（其内部每 1 秒采样被动状态），`pendingInput` 出现即置 `input-required` 并让调用方 exit 7；批准后回到 `running` 并记 `delegate.job.resumed`。
- **调用方可以自己批**：GUI 的审批/问答是转发到浏览器的 waterfall + `$events/result`；插件以独立 owner answering Peer 接同一入口，`pending` 能列出完整请求，`approve`/`deny`/`answer` 走同一条路——GUI 卡片会随网关的 `cancel` 自动收起，回合继续。每次作答写 `delegate.job.approval-answered` 审计（调用方标识、决定、理由、请求摘要），job 视图保留最近一次作答。
- **job 落盘**：每轮结果与 `extensions`/`lastObserved` 写 `$DSH_HOME/delegate-jobs/<jobId>.json`（0600，保留 24 小时）；插件重载/桌面重启后，终态结果仍可取回，未终态 job 会重新挂监听或落 `lost` 并说明原因。
- **可排查**：每次延长与每次落终态写一行 JSONL 到 `$DSH_HOME/delegate-jobs/events.jsonl`（`delegate.job.extend` / `delegate.job.settle`），带 jobId、sessionId、原因、延长次数、累计时长、最后观察状态；同时尝试镜像到宿主 logger。
- **权限走 DSH preset**：`permissionPresets.set` 写 durable 的 preset/sandbox/approval 事件，第一次工具调用前生效；越界操作挂起等 GUI 人工批准。
- **令牌鉴权**：路由不经过 GUI 的 cookie 认证，插件启动生成随机令牌写 `$DSH_HOME/delegate.json`（0600）；带 `Origin` 的浏览器请求一律拒绝。

## 测试

```bash
cd plugin && node --test test/                       # 过期/结算/持久化纯逻辑
python3 -m unittest discover -s skill/test -p 'test_*.py'   # 客户端退出码、--until-done、--out、--expire
```

另有一次性 sidecar e2e 方法：用临时 `DSH_HOME` 起一个 `dsh web` 实例装本插件，预置合成 job 文件，验证「终态 job 重启后仍可取回」「未终态 job 恢复为 lost」。

## 风险与边界

- 插件调用的是 DSH 0.2.2-rc.1 的内部服务契约（`workspaceRegistry` / `sessionController` / `permissionPresets`），**升级 DSH 可能失效**；失效时错误码会明确给出。
- 依赖桌面版常开；插件以 `link:` 依赖装进 `~/.dsh/profiles/desktop`，**包目录不能移动/删除**。
- `input-required` 需要人；无人值守请让任务避开需要审批的越界操作，或用用户明确授权的权限预设。

## 卸载

```bash
# 在 DSH 的 Plugins 页删除 Delegate bridge，或：
dsh plugin --profile desktop remove dsh-delegate
rm -rf ~/dsh-plugins/dsh-delegate
rm -f ~/.agents/skills/dsh-delegate ~/.claude/skills/dsh-delegate ~/.codex/skills/dsh-delegate
rm -rf "$DSH_HOME/delegate.json" "$DSH_HOME/delegate-jobs"
```
