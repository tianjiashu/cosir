# 客户端日志显示方案

> 本文档定义桌面客户端「日志页」的展示与交互方案，配套 `rules/Agent日志开发规范.md`。
> 后端落盘的 9 字段（`ts` / `level` / `trace_id` / `logger` / `caller` / `event` / `msg` / `data` / `error`）如何在客户端呈现，以此为准。
>
> **核心目标**：让用户/开发者「扫得快、定位准、串得起」——一眼看清发生了什么（`msg`），需要时能定位是哪段代码（`caller`），点一下能把整条链路（`trace_id`）串起来。

---

## 一、设计原则

1. **人读优先**：默认视图以 `msg`（中文）为主体，机器键（`event` / `caller` / `trace_id`）弱化但可点。
2. **分层展开**：头部信息永远显示；`data` / `error` 详情默认折叠，按需展开，避免信息过载。
3. **链路可串**：`trace_id` 是唯一关联键，点击即筛选全链路——这是排查的核心动作。
4. **级别驱动视觉**：`level` 用颜色区分，ERROR 自动突出，无需用户主动找。

---

## 二、单条日志的视觉结构（三行）

```
┌──────────────────────────────────────────────────────────────┐
│ 18:04:08.518  [INFO]  task_created            ← 第1行：头部     │
│ 新任务已创建，等待运行时调度                     ← 第2行：msg     │
│ ▸ runner:AgentRuntime.run_task:587  ·  trace 2a543a9d ← 第3行：来源│
└──────────────────────────────────────────────────────────────┘
```

### 第 1 行：头部（永远显示）
| 元素 | 来源 | 渲染 |
|------|------|------|
| 时间 | `ts` | 只显示 `时:分:秒.毫秒`，日期折叠（同一天查询） |
| 级别 | `level` | **彩色徽章**：INFO 蓝灰 / WARNING 黄 / ERROR 红 / DEBUG 淡灰 |
| 事件 | `event` | 等宽字体加粗，机器键一眼定位事件类型 |

### 第 2 行：msg（人读主体）
- 中文描述，正常字号——用户扫日志时**最先看的**。
- 出错时若 `error.message` 存在，可在 msg 后追加简短提示。

### 第 3 行：来源（弱化显示，灰字）
| 元素 | 来源 | 渲染与交互 |
|------|------|-----------|
| 方法路径 | `caller` | 缩短显示（去掉包前缀），如 `runner:...run_task:587`；点击（future）跳转源码 |
| 链路键 | `trace_id` | 截断前 8 位，点击**筛选该 trace 全链路** |
| 来源名 | `logger` | 一般省略，太长；放 tooltip |

---

## 三、可展开的细节（点击整条展开）

### `data`（结构化业务字段）
JSON 格式化缩进展示，可折叠：
```
{
  "tool": "read_file",
  "duration_ms": 30500,
  "threshold_ms": 30000
}
```

### `error`（错误现场，仅出错非空）
红色块展示：
```
错误类型: ConnectionError
信息: upstream connect timeout
[展开堆栈 ▸]     ← stack 默认折叠
```
- **ERROR 级别**：默认展开 `error.message`，折叠 `error.stack`。
- 其余级别：整体折叠，点击才展开。

---

## 四、关键交互

| 交互 | 作用 |
|------|------|
| 点 `trace_id` | 立即筛选该 trace 的完整链路（**最核心的排查动作**） |
| 点 `level` 徽章 | 按级别快速过滤 |
| 点整条 | 展开 `data` / `error` 详情 |
| 点 `caller` | （future）跳转源码位置 |
| 点 `data` 里的实体 ID | （可选）从 `data` 抠出 `task_id` 等做快捷筛选（展示层便利，非日志关联键） |

---

## 五、展示样例

### 例 1：普通 INFO（默认折叠）
```
18:02:11.318  [INFO]  task_created
新任务已创建，等待运行时调度
▸ runner:AgentRuntime.run_task:587  ·  trace 2a543a9d
```
展开后追加 `data: {agent_id: developer, status: pending}`。

### 例 2：WARNING（data 带耗时）
```
18:02:45.902  [WARN]  tool_call_timeout
调用 read_file 工具超时，已触发熔断，tool_call_id=9c1a
▸ execution.service:ToolExecutor.execute:213  ·  trace 2a543a9d
```
展开 `data`：
```
{ "tool": "read_file", "duration_ms": 30500, "threshold_ms": 30000 }
```

### 例 3：ERROR（默认展开 message，stack 折叠）
```
18:03:02.117  [ERROR]  stream_interrupted
模型流式接口中断，任务执行失败，task_id=t-7781
▸ adapters.openai:OpenAIStream.parse:88  ·  trace 2a543a9d
─────────────────────────────────────
错误类型: ConnectionError
信息: upstream connect timeout
[展开堆栈 ▸]
```

### 例 4：点 trace 后的全链路筛选（核心排查视图）
```
筛选中: trace 2a543a9d (5 条)  [清除筛选]
18:02:11.318  [INFO]  task_created         ▸ runner:...:587
18:02:45.902  [WARN]  tool_call_timeout    ▸ execution:...:213
18:03:02.117  [ERROR] stream_interrupted   ▸ adapters:...:88
18:03:30.441  [INFO]  approval_resolved    ▸ approvals:...:142
18:03:31.009  [DEBUG] step_start           ▸ runner:...:455
```
一条 `trace_id` 把整次任务（创建 → 工具超时 → 模型中断 → 审批 → 重试步骤）按时间线串起，一目了然。

---

## 六、页面级布局

### 顶部工具栏
- **级别过滤**：DEBUG / INFO / WARNING / ERROR 多选。
- **关键字搜索**：匹配 `msg` / `event` / `caller`。
- **trace 筛选态**：激活时顶部显示「筛选中: trace xxx」+ 一键清除。
- **时间范围**：当天为主，可选历史文件。

### 主区
- **结构化列表**（默认）：按上述三行卡片渲染，虚拟滚动。
- **原始文本**（可选 tab）：显示原始 JSONL，供复制/导出贴给他人。
  - 建议主视图只留结构化展示，纯文本改为「复制/导出」按钮；如需保留原始视图，用 tab 切换，不与结构化区并列重复。

### 空态 / 异常态
- 无日志：提示「当前无日志或未开始运行」。
- 加载失败：提示读取失败并给重试。

---

## 七、颜色约定（配合 `docs/ui-guidelines.md`）

| level | 徽章色 | 语义 |
|-------|--------|------|
| DEBUG | 淡灰 | 调试细节，视觉最弱 |
| INFO | 蓝灰 | 正常流程 |
| WARNING | 黄 | 可预期异常/降级 |
| ERROR | 红 | 失败路径，视觉最强 |

> 具体色值以 `docs/ui-guidelines.md` 的设计令牌（shadcn/ui + Tailwind）为准，本文档只定语义。

---

## 八、待拍板 / 后续

- `caller` 点击跳源码：需桌面端与编辑器/文件定位能力打通，列为 future。
- `data` 实体 ID 快捷筛选：展示层便利功能，不改变「日志只认 trace_id」的后端约束。
- 原始文本视图去留：默认建议只留结构化 + 导出，最终以实际使用反馈调整。
