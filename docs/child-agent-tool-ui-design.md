# 子 Agent 工具 UI 展示设计方案

状态：设计方案，尚未实施。

目标：让子 Agent 在消息流中成为可识别的**第二执行主体**，而不是"又一个工具调用"。

---

## 1. 现状与问题

### 1.1 渲染链路

```text
handler（child_task/*.py）
  └─ display/child_agent_display.py、delegation_display.py → display_data（带 kind）
     └─ ConversationTaskStateRebuilder → ToolCallPart（注入 child_task_id / agent_role）
        └─ Transport snapshot → 前端 lib/assistant/contract.ts
           └─ components/assistant-ui/tools/tool-part.tsx → routeToolPart（按 kind 路由）
```

### 1.2 三个具体问题

**P1 · 形态断层。** 只有 `delegation-result` 走专属 `DelegationToolRow`（深色卡 + 打开/停止）。
`child-agent-result`（send/status）与 `child-agent-wait-result`（wait）落进通用 `DetailsTool`，
与 `read_file`、`search_content` 使用同一套行式排版。用户在消息流中无法分辨"这里有另一个 Agent 在跑"。

**P2 · locator 只下发了一半。** `conversation_task_state_rebuilder.py` 注入
`child_task_id` / `child_run_id` / `agent_role` 的判定条件是
`display_data.get("kind") == "delegation-result"`。send / status / wait 三个工具的
`display_data` 里明明带 `child_task_id`，却拿不到"打开工作台"与"停止"能力。

**P3 · 视觉语汇与终端冲突。** `DelegationToolRow` 与 `TerminalTool` 都硬编码
`bg-zinc-900 text-zinc-100`。子 Agent 沿用深色块会被读成"又一个终端"；且
`globals.css` 存在 light/dark 双主题，硬编码 zinc 在浅色主题下是孤立深色块。

### 1.3 硬约束

契约禁止把 prompt / message 放入 `display_data`。因此卡片**不能展示任务描述原文**。
主标题只能来自 `agent_name`（send/status 用 `child_task.title`，即 delegate 时的 `agent_name`）与 `role`。

---

## 2. 设计语言：Agent Card

### 2.1 五个差异化手段

| 手段 | 作用 | 与其他工具的区分点 |
| --- | --- | --- |
| Avatar 徽标 | 26px 圆角方块 + 首字缩写，子 Agent 成为"有名字的实体" | 文件/搜索/终端均为线性图标，无身份 |
| 左侧血缘 rail | 2px 竖线，表达"从我这里分出去的分支" | 唯一带缩进语义的工具卡 |
| 紫罗兰专属色 | 紫 = 智能体 / 推理 | 终端占中性深色、diff 占绿/红、搜索占中性灰，互不冲突 |
| 双层结构 | 常驻 header（身份 + 状态）+ 折叠 body（本轮产出） | 通用行只有一层 |
| 进度条 + 计时 | 2px 进度条 + `12s` 计时，给长时运行以时间感 | 通用行只有一个 spinner 文案 |

**关键决策：rail 恒定紫色，状态只由 avatar 环 + 徽标表达。**
不让状态色污染身份标识——"这是同一个子 Agent 的第 2 轮"应当在视觉上一眼连贯，
`send` 卡不需要重新建立认知。

### 2.2 四种形态

统一外壳 `AgentCard`，内部由 `ToolDisplayHints.variant` 区分 4 种语义：

| 工具 | variant | kind | 语义 | header meta |
| --- | --- | --- | --- | --- |
| `delegate_task` | `child-agent-spawn` | `delegation-result` | 诞生：创建子 Agent | `角色 · task N · run N · 12s` |
| `child_agent_send` | `child-agent-relay` | `child-agent-result` | 续跑：追加一轮 | `第 N 轮追加输入 · task N · run N · 3s` |
| `child_agent_status` | `child-agent-probe` | `child-agent-result` | 探针：只读快照 | `角色 · task N · run N · 48s` |
| `child_agent_wait` | `child-agent-await` | `child-agent-wait-result` | 等待：阻塞等结论 | `等待 60s · task N · run N` |

### 2.3 并行泳道（第二阶段）

`delegate_task` 声明 `parallel_mode="parallel"`，模型常在同一回复中派多个子 Agent。
当同一条 assistant message 内出现 ≥2 个子 Agent 卡时，用 `AgentSwimlane` 容器收拢：

- 容器 header：`3 个子 Agent 并行` + `1 完成 · 2 运行中 · 18s`
- 内部共享一条 1px 子 rail，每行 20px avatar + 名称 + 状态 + 计时
- 容器级操作：`全部停止`、`展开全部`
- 复用 `tool-group.aui.tsx` 的 `GroupedParts` / `groupBy`，**按 `kind` 分组，不按工具名分组**

---

## 3. 视觉规格

### 3.1 尺寸

| 元素 | 规格 |
| --- | --- |
| 卡片圆角 | `rounded-xl`（12px） |
| 卡片边框 | `0.5px` + 左侧 `2px` rail |
| header 高度 | 44px（含 `padding: 10px 12px`） |
| header 间距 | `gap: 10px` |
| avatar | 26px，圆角 8px，11px 字号，weight 500 |
| body | `padding: 0 12px 11px 48px`，12px 字号，line-height 1.6 |
| 进度条 | 2px，卡片底边通栏 |
| 泳道内条目 | avatar 20px，行间距 7px，子 rail 1px + `padding-left: 11px` |

### 3.2 配色（语义 token，不用硬编码 zinc）

紫色 = 身份，恒定；状态色 = 生命周期，随状态切换。

| 角色 | Light | Dark |
| --- | --- | --- |
| rail（running） | `#534AB7` | `#AFA9EC` |
| rail（terminal） | `#AFA9EC` | `#7F77DD` |
| rail（未收敛：pending/timeout） | 紫 + dashed | 紫 + dashed |
| avatar 底 / 环 · running | `#EEEDFE` / `#534AB7` | `#3C3489` / `#AFA9EC` |
| avatar 底 / 环 · completed | `#EAF3DE` / `#639922` | `#27500A` / `#97C459` |
| avatar 底 / 环 · failed | `#FCEBEB` / `#E24B4A` | `#791F1F` / `#F09595` |
| avatar 底 / 环 · cancelled | `--muted` / `--border` | 同左 |
| avatar 底 / 环 · timeout | `#FAEEDA` / `#BA7517` | `#633806` / `#EF9F27` |
| 徽标文字 · running / completed / failed / timeout | `#3C3489` / `#27500A` / `#A32D2D` / `#854F0B` | `#CECBF6` / `#C0DD97` / `#F7C1C1` / `#FAC775` |

卡片背景走 `--color-background-primary`，泳道容器走 `--color-background-secondary`。
所有色值必须落在 light/dark 双主题上，禁止硬编码 `bg-zinc-900` 一类固定深色。

### 3.3 状态 → 视觉映射

| Transport 状态 | avatar 环 | rail | 徽标 | 额外元素 |
| --- | --- | --- | --- | --- |
| `pending` | 紫虚线环 | 紫 dashed | 准备执行 | — |
| `running` | 紫实线环 | 紫实线 | 运行中 + 6px 紫点 | 2px 进度条（indeterminate）+ 计时 + `停止` |
| `completed` | 绿环 | 紫 45% | 已完成 | body 展示 `final_output` |
| `failed` | 红环 | 红 | 失败 + `status_hint` | 不展示结果字段 |
| `cancelled` | 灰环 | 灰 dashed | 已取消 | — |

`wait` 的 `timed_out=true` 属成功态 + amber 徽标"等待超时"，**不是失败态**，
不得显示"失败"，且必须保留 `停止` 操作（子 Agent 未被取消）。

### 3.4 无障碍

- 对比度：徽标文字/底色均满足 4.5:1（正文）与 3:1（大字）。
- 键盘：`停止` / `打开` 为真实 `Button`，header 折叠沿用 `CollapsibleTrigger`。
- 屏幕阅读：`aria-label` 形如 `停止子 Agent：解析 parser 模块`；状态变化用 `aria-live="polite"`。
- 动效：进度条与脉冲一律包在 `@media (prefers-reduced-motion: no-preference)` 内。
- 触摸目标：`停止` / `打开` 高度 ≥ 28px（当前 `h-7`）。

---

## 4. 契约改动

### 4.1 后端

1. **`ToolDisplayHints.variant`** 扩展 4 个子 Agent 变体
   （`child-agent-spawn` / `-relay` / `-probe` / `-await`）。
   `variant` 是既有字段（terminal 已在用），不新增字段。
2. **`conversation_task_state_rebuilder.py`**：把 `child_task_id` / `child_run_id` /
   `agent_role` 的注入条件从"仅 `delegation-result`"扩展为"三种 child-agent `kind` 通用"，
   使 send / status / wait 也能跳转工作台与停止。
3. **`display/child_agent_display.py`** 新增可选 UI-only 字段（均非 prompt 原文）：
   - `role`：角色（send / status 目前缺失，只有 delegate 有）
   - `round`：第几轮（send 递增）
   - `elapsed_ms`：运行时长，供前端渲染计时
   - `task_brief`（**可选增强**）：由后端生成的 ≤60 字安全摘要，**不是 message 原文**
4. **`tool_ui_display_contract.md`**：错误短提示表补充 `child_agent_*`：
   `子任务不存在`、`子 Agent 忙碌`、`等待超时`、`委派失败`。

### 4.2 前端

1. `tool-part.tsx`：新增 `"child-agent"` 路由（按 `kind`，不按工具名），
   `KNOWN_DISPLAY_KINDS` 不变。
2. 新建 `components/assistant-ui/tools/child-agent/`：
   - `child-agent-card.tsx` — 外壳（rail + avatar + header + body）
   - `child-agent-avatar.tsx` — 徽标与状态环
   - `child-agent-body.tsx` — 4 种 variant 的正文
   - `agent-swimlane.tsx` — 并行容器（第二阶段）
3. `tool-icons.tsx` 增加 `agent` 图标（fallback 用，主视觉走 avatar）。
4. `delegation-tool-row.tsx` 并入新体系（variant = `spawn`），删除硬编码 zinc。
5. `details-tool.tsx` 移除 `ChildAgentResult` / `ChildAgentWaitResult` 两个分支。

### 4.3 不变的部分

- `display_data` 仍不携带 prompt、堆栈、完整模型正文。
- 失败态仍只返回 `{"status_hint": "..."}`，状态仍取自 `ToolObservation.status`。
- 未知 `kind` 仍走 `ToolFallback`。

---

## 5. 实施分期

| 阶段 | 内容 | 收益 |
| --- | --- | --- |
| P0 | 修 locator 注入 + 抽 `AgentCard` 外壳 + 四种 variant 落地 | 消除形态断层，send/status/wait 获得身份与操作 |
| P1 | 双主题 token 化，替换 `DelegationToolRow` 硬编码 zinc | 修复浅色主题下的孤立深色块 |
| P2 | `AgentSwimlane` 并行容器 | 多子 Agent 场景下的消息流密度 |

---

## 6. 验收清单

- [ ] 四个子 Agent 工具在消息流中均可一眼识别为"另一个 Agent"，而非普通工具行。
- [ ] send / status / wait 的卡片具备与 delegate 相同的"打开工作台"与"停止"能力。
- [ ] 浅色主题下不出现孤立深色块；全部色值走语义 token。
- [ ] `wait` 超时显示为 amber "等待超时"，不显示为失败，且保留停止操作。
- [ ] 键盘可完整操作；屏幕阅读器可播报子 Agent 名称与状态变化。
- [ ] 关闭动效偏好时，进度条与脉冲不播放。
- [ ] `routeToolPart` 中不出现按工具名的分支。
