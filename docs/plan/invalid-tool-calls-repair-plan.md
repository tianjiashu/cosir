# `invalid_tool_calls` 自愈处理改造计划

> 状态：待实施（需独立审查 Agent 通过后方可落地）
> 日期：2026-08-14
> 关联文件：`apps/backend/app/core/workflows/nodes/model_node.py`、`model_tool_helper.py`、`common.py`、`tools_node.py`、`react/state.py`、`react/workflow.py`

---

## 0. 背景与代码事实（调研结论）

`invalid_tool_calls` 是 LangChain 在**模型输出字符非法（非 JSON / 缺字段）导致工具调用解析失败**时产出的产物，本质是"模型想调工具、但输出不合法"。虽然已在 `tool_definition.py` + `workflow.py` 落地 **strict 化治本**（`strict:true` 强制全 required + `additionalProperties:false`），但 strict 仅降低概率、不消除（DeepSeek beta 偶发 malformed JSON 已知）。因此需要把"自愈"落地为真正可工作的路径，而非当前带 bug 的死代码。

### 0.1 当前真实代码事实（已 CodeGraph + 读源码确认）

1. **采集**（`model_node.py:578`）：`invalid_tool_calls = getattr(ai_message, "invalid_tool_calls", None) or []`。
2. **决策**（`model_tool_helper.py:117` `decide_invalid_tool_handling`）：遍历 `invalid_tool_calls`，用 `invalid_tool_call_mention_tool_name`（子串匹配 `tool_name in str(invalid_tool_call)`）判断是否命中已注册工具名，命中进 `REPAIR`、否则进 `IGNORE`，返回 `{IGNORE: [...], REPAIR: [...]}`。
3. **REPAIR 执行分支**（`model_node.py:612-637`）：构造 `repair_message`/`repair_data` 后 **`return terminal_state(step_count)`** —— 直接终态，graph 走到 END，`pending_tool_calls` 从未被设置，`tools_node` 永远收不到 `deferred_repair_message`。**自愈意图完全失效**。
4. **类型 bug**（`model_node.py:680`）：`deferred_repair_content = str(repair_message.content) if ...`，但 `build_invalid_tool_call_repair_message` 返回值是 **`str`**，不是带 `.content` 的对象 → `AttributeError` 或恒为空。
5. **IGNORE 分支**（`model_node.py:594-610`）：仅 `log.warning`，合法工具照常执行，不修复、不阻塞。✅ 这部分逻辑正确。
6. **情形 (b) 完全缺失**：当 `invalid_tool_calls` 命中工具名、但**本轮没有任何合法 `tool_calls`** 时，当前代码没有"无工具可走、但仍要把修复提示喂给模型重试"的路径 —— 修复提示无处可去（工具分支 `if requested_tool` 不成立，最终回复分支 `if output_text` 也不成立，直接落 `RUN_FAILED(invalid_model_output)`）。可疑调用意图被直接判为非法输出丢弃。
7. **终态事件缺口**：REPAIR 分支的 `terminal_state` 是"裸终态"——不发 `RUN_FAILED`/`RUN_FAILED(parse_invalid)`，前端收不到终态事件。
8. **死写字段（两个）**：`model_node.py:696` 写 `"model_repair_count": ...`、`model_node.py:699` 写 `"continuation_error_data": ...` 到 return patch，但 `ReactGraphState`（`state.py`）**既没有 `model_repair_count` 也没有 `continuation_error_data` 字段**（`workflow.py:191,195` 在 input_state 初始化过二者、`max_steps_node.py:44` 读取 `continuation_error_data`）。LangGraph 宽松 schema 下忽略未知 key，但属无效写、且 `continuation_error_data` 读取在 strict 校验下会失败。
9. **失效 docstring**：`decide_invalid_tool_handling` docstring 提到 `REPAIR_IMMEDIATE`（枚举里根本没有），`build_invalid_tool_call_repair_message` docstring 写"返回追加到 RuntimeContext 的系统消息"但实际返回 `str`。
10. **子串匹配脆弱**（`model_tool_helper.py:74`）：`search_files` 的子串 `search`/`read`/`file` 可能误中无关文本（用户 prompt "请读取并搜索文件"），产生虚假修复循环。
11. **回流条件未满足（情形 b 致命缺口）**：`_should_continue`（`edges.py:26-27`）回到 `model` 的唯一条件是 `state.repair_requested` 为真。`repair_requested` 在 `state.py:63` 声明为 **`str`**，但现状 `model_node.py:697` 写 `"repair_requested": False`（bool），类型不一致；情形 (b) 分支若不显式设 `repair_requested` 为非空 `str`，graph 直接 END，自愈失效。
12. **脱敏调用位置**：`redact_terminal_output` 仅在 `model_tool_helper.py:10` import；`model_node.py` 未 import，脱敏逻辑应限定在 helper 内。

### 0.2 现有可用、不需重建的管道

- `tools_node.py:311-320`：**已正确消费** `deferred_repair_message` —— 工具执行后把它作为 `SystemMessage` 写进 `RuntimeContext`（`_runtime_context().add_message(SystemMessage(...))`）。脚本提示与工具观察结果一起经 `observe` 节点回灌下一轮模型。✅ 情形 (a) 只需让 `model_node` 的 REPAIR 分支走到工具分支即可复用。
- `_runtime_context().add_message(...)` 在 `model_node`/`tools_node` 均已可用（`state.py:21` 明确"工具观察结果经 `add_message` 追加到上下文末端，不进 graph state"）。✅ 情形 (b) 可直接用。
- `build_run_failed_payload(step_id, error, usage=, langfuse_trace_id=, data=)`（`common.py:105`）统一构造携带 token 摘要的 `RUN_FAILED` payload，支持 `data` 附加分类明细。✅ 情形 (b) 终态复用。

---

## 1. 目标与决策（已与用户确认）

- **决策点 1（终态行为）**：情形 (b) 无合法 tool 但存在可疑 `invalid_tool_calls` 时，**不终态**，直接把修复提示写进 `RuntimeContext` 并**回到 model 节点重试**（返回 patch 必须显式设 `repair_requested="true"`，否则 `_should_continue` 路由到 END，见 §2 #3），靠 `max_steps` 无限重试兜底；重试耗尽（step 超 `max_steps`）后由现有 `max_steps_node` 终态路径以 `"max_steps_reached"` 分类发 `RUN_FAILED`（**不是** `parse_invalid`，全库无此分类）。
- **决策点 2（tool_name 提取）**：`invalid_tool_call_mention_tool_name` 改为 **"精确 `name` 优先 + 词边界正则兜底"**，替代脆弱的子串 `in` 匹配。

### 用户原始意图复述

1. 检测 `invalid_tool_calls`，逐条转 `str`（提取 name/args/error 原文，因为存在解析失败非 JSON 情况）。
2. 在 str 中看是否存在可用 `tool_name` → 视为真实调用意图。
3. **字符超限控制**：原始片段可能很长，必须截断 + 限条数，避免污染上下文。
4. 转为一条 `SystemMessage` 提示模型"上次有非法工具调用，请修正后重发"。
5. **注入时机**：等 tool 执行完，和 tool 观察结果**一起**注入下一轮模型。
6. **两种情形都要提示**：(a) 有合法 tool 执行 + 可疑 invalid → 合法照常执行、修复提示延后；(b) 无有效 tool 但可疑 invalid → 也必须提示模型重试。

---

## 2. 目标行为（改造后）

```mermaid
flowchart TD
    A[合并 ai_message 取 invalid_tool_calls] --> B{有 invalid?}
    B -- 否 --> Z[正常走 tool/final 分支]
    B -- 是 --> C[decide_invalid_tool_handling 返回双列表 IGNORE / REPAIR<br/>REPAIR 项逐条转 str + 截断500 + 限5条 + 提取可疑 tool_name 精确优先/词边界兜底]

    %% 双轨消费：IGNORE 列表无论情形(a)/(b) 一律只打 warning（对接现有 594-610）
    C --> I1{IGNORE 列表非空?}
    I1 -- 是 --> I2[log.warning model_node_invalid_tool_calls_ignored<br/>仅排查日志, 不修复不阻塞]
    I1 -- 否 --> N1[跳过]
    I2 --> R1{REPAIR 列表非空?}
    N1 --> R1

    %% REPAIR 列表按 requested_tool 二维分流（核心，杜绝滑落 invalid_output）
    R1 -- 否 --> Z
    R1 -- 是 --> R2{requested_tool 即 bool tool_calls?}
    R2 -- 是[情形 a 有合法 tool] --> R3[不 return, 让修复提示自然落到下方工具分支<br/>经 deferred_repair_message 由 tools_node 写 SystemMessage]
    R2 -- 否[情形 b 无合法 tool] --> R4[在 REPAIR 块内显式 return 非终态:<br/>_runtime_context.add_message SystemMessage<br/>repair_requested='true', terminal=False, pending_tool_calls=[]]

    R3 --> T[tools_node 执行 + 写回观察 + 注入修复提示<br/>下一轮 model 消费 SystemMessage 重试]
    R4 --> T
    T --> END1[回到 model 节点重试, 受 edges.py:24 max_steps 拦截保护]

    %% 情形 b 无可疑（REPAIR 为空）终态：IGNORE warning 已在上方打过, 此处发 invalid_model_output 终态
    R1 -- 否且 IGNORE 也无 或 仅 IGNORE --> F0{无合法 tool 且无 REPAIR?}
    F0 -- 是 --> H[IGNORE warning（若 IGNORE 非空, 已打）+ RUN_FAILED invalid_model_output 终态<br/>（warning 是排查日志 / 终态是终态事件, 二者并存不冲突）]
```

**关键修复清单（必须全部落地）：**

| # | 问题 | 修复动作 | 文件 |
|---|------|----------|------|
| 1 | REPAIR 分支 `return terminal_state` 堵死工具执行 + 情形 b 滑落风险 | **不能只删 return**。在 `model_node.py` 的 `if result[InvalidToolOutcome.REPAIR]:` 块内（当前 612-637），把 `requested_tool = bool(tool_calls)` 的计算**提前到该块之前**（现状在 648 行才算，块内不可用），并把原 634 行的 `return terminal_state` 改为**块内按 `requested_tool` 分流**：① `requested_tool=True`（情形 a）→ 不 return，让修复提示自然落到下方 `if requested_tool:` 工具分支（679-693 经 `deferred_repair_message` 注入）；② `requested_tool=False`（情形 b）→ **在块内显式 `return` 非终态回流**（见 #3）。**必须保证情形 b 的 return 位于 706 行 `if output_text` / 751 行 `invalid_output` 之前**，杜绝 REPAIR 列表滑落到 `invalid_output` 被误判为非法输出终态 | `model_node.py` |
| 2 | `repair_message.content` 类型 bug | `model_node.py:680` 改为 `str(repair_message)`（返回值本就是 `str`） | `model_node.py` |
| 3 | 情形 (b) 无合法 tool 但可疑 → 必须提示且**能回流** | 在 REPAIR 块内 `requested_tool=False` 分支显式 `return`，**不滑落**到下方 invalid_output：直接 `_runtime_context().add_message(SystemMessage(repair_message))` + `return {terminal=False, repair_requested="true", requested_tool=False, final_response=False, pending_tool_calls=[]}`。**关键**：`_should_continue`（`edges.py:26-27`）回到 `model` 的唯一条件是 `state.repair_requested` 为真；`repair_requested` 在 `state.py:63` 声明为 **`str`**（现状 `model_node.py:697` 写 `"repair_requested": False` 是 bool，类型不一致，需一并改为 `"true"`），否则 graph 直接 END、自愈失效。该分支**不发终态事件**，靠 `max_steps` 兜底（受 `edges.py:24` `repair_requested and step>=max_steps` 拦截保护，达上限转 `max_steps_node` 终态）。**IGNORE 列表项的 warning 已在块前 594-610 独立打过，与此 return 互不干扰** | `model_node.py` / `state.py` / `edges.py` |
| 4 | 情形 (b) 无可疑 → 终态分类 | **复用现有 `invalid_model_output`**（与 `model_node.py:772` 现有 invalid_output 分支一致），不引入虚构的 `parse_invalid`。即 `build_run_failed_payload(step_id, "invalid_model_output", usage=, langfuse_trace_id=, data=分类明细)` + `RUN_FAILED` 事件 + `terminal_state`。注意：`max_steps` 兜底的 error 是 `"max_steps_reached"`（`max_steps_node.py:61`），全库无 `parse_invalid`，不要编造。**IGNORE warning（594-610）与 invalid_model_output 终态在无可疑路径下并存属预期**：前者是跨情形(a)/(b) 恒打的排查日志（噪声记录），后者是终态事件（前端/前端状态机），职责正交、不得为"去重"删除 594-610 的 warning | `model_node.py` |
| 5 | 子串匹配脆弱 | `invalid_tool_call_mention_tool_name` 改"精确 `name` 优先（`invalid_tool_call.get("name")` 精确等于某工具名）+ 词边界正则 `\b{tool_name}\b` 兜底" | `model_tool_helper.py` |
| 6 | 字符超限 | 复用 `INVALID_TOOL_ARGS_PREVIEW_CHARS=500` 截断每条 str + `INVALID_TOOL_CALL_SUMMARY_LIMIT=5` 限条数。拼接**改用结构化文本模板**（非 `json.dumps`）：逐条格式化为 `tool_name: <name>\nerror: <error>\nraw: <redacted_args_preview>`，整体再限 2000 字符兜底，**确保截断后每条仍含可定位的 tool_name 与 error**（不丢关键字段）。**脱敏调用只在 `model_tool_helper.py` 内进行**（该处已 `import redact_terminal_output`，`model_node.py` 未 import，勿跨文件引用） | `model_tool_helper.py` |
| 7 | 死写字段（`model_repair_count` + `continuation_error_data`） | `state.py` **无** `model_repair_count` 与 `continuation_error_data` 字段，但 `workflow.py:191,195` 初始化、`model_node.py:696,699` 写入、`max_steps_node.py:44` 读取 `continuation_error_data`。处理：① 在 `ReactGraphState`（`state.py`）**补上 `continuation_error_data: ...` 字段定义**使 schema 完整（`max_steps_node.py:44` 读取合法）；② 删除 `model_repair_count` 死写（`model_node.py:696` 写入行 + `workflow.py:191` 初始化）；③ `continuation_error_data` 写入保留（因已补字段）。**先确认 LangGraph 对未声明字段行为**：现状能跑说明当前是宽松 schema，但补字段更稳，避免 strict 校验回归 | `state.py` / `model_node.py` / `workflow.py` |
| 8 | 失效 docstring | 同步更新 `decide_invalid_tool_handling`（删 `REPAIR_IMMEDIATE` 描述、对齐 `REPAIR`/`IGNORE` 实际枚举）、`build_invalid_tool_call_repair_message`（"返回 `str`"、说明结构化模板与脱敏）、`InvalidToolOutcome` 类 docstring、`_model_node` 节点 docstring（回流机制说明） | `model_tool_helper.py` / `model_node.py` |

---

## 3. 分层职责（保持现有"纯决策 / 薄执行"架构）

- **`model_tool_helper.py`（纯决策，无副作用）**：
  - `invalid_tool_call_mention_tool_name`：精确 name + 词边界兜底，返回命中的 `tool_name` 或 `None`。
  - `build_invalid_tool_call_repair_message`：接收 `repair_datas`（每条含 `tool_name` + `invalid_tool_call` str），截断 + 限条数后构造**单条英文 `str` 提示**。
  - `decide_invalid_tool_handling`：纯判断，返回 `{IGNORE: [...], REPAIR: [...]}`，不写日志 / 不发事件 / 不构造消息。新增"是否有可疑意图 = `bool(REPAIR)`"供情形 (b) 判定（调用方直接 `if result[InvalidToolOutcome.REPAIR]` 即可，无需改签名）。
- **`model_node.py`（薄执行）**：
  - 调用决策函数；**IGNORE 列表**恒打 warning（594-610，跨情形 a/b）；**REPAIR 列表**在块内按 `requested_tool` 分流——情形 (a) `requested_tool=True` 落工具分支（修 #1/#2，经 `deferred_repair_message` 注入）、情形 (b) `requested_tool=False` 直接 `add_message` + 非终态回流（#3，显式 `repair_requested="true"`、在 `invalid_output` 之前 return）；情形 (b) 无可疑（REPAIR 空）→ `RUN_FAILED(invalid_model_output)`（#4，复用现有分类，非 `parse_invalid`）。**IGNORE 与 REPAIR 双轨独立消费，互不干扰**。
- **`tools_node.py`（已正确，不改动）**：维持 `deferred_repair_message` 消费（#1 修复后该路径被情形 (a) 真正触发）。
- **`common.py`（已正确，不改动）**：`build_run_failed_payload` / `terminal_state` 复用。

---

## 4. 边界与约束（遵循开发规范）

- **单一职责**：`model_tool_helper` 保持纯决策；`model_node` 保持薄执行；`tools_node` 不新增职责。
- **防膨胀**：不引入新文件、新依赖；复用现有 helper 与 `INVALID_TOOL_*` 常量。
- **日志可排查**（规范 §六）：
  - 情形 (a) REPAIR_DEFERRED：保留 `model_node_invalid_tool_calls_deferred_repair_requested`（warning），data 含 `step_id` + `repair_data`（已脱敏）。
  - 情形 (b) 可疑：新增 warning `model_node_invalid_tool_calls_no_tool_deferred`（data 含 `step_id` + `repair_message_length`），与情形 (a) 日志对称、可排查"为什么重试"。
  - 情形 (b) 无可疑 → IGNORE：保留现有 `model_node_invalid_tool_calls_ignored`（warning）。
  - 所有日志 **绝不输出** `invalid_tool_call` 原文中的 secret / 用户 prompt 片段：`build_invalid_tool_call_repair_message` 内部（仅限 `model_tool_helper.py`，该处已 import `redact_terminal_output`）对每条 str 脱敏后再截断。
- **终态事件完整性**：任何终态路径都必须发 `RUN_FAILED` 或 `RUN_FINISHED`（消除现状 REPAIR 裸终态缺口）。情形 (b) 可疑路径**非终态**不重复发事件（下一轮若仍失败，由 `max_steps_node` 路径统一发 `"max_steps_reached"`；情形 b 无可疑路径发 `"invalid_model_output"`，均复用现有分类，不编造 `parse_invalid`）。
- **docstring 四段式**：所有改动函数同步更新 docstring（参数/返回/异常/副作用），签名不变则不增加参数描述噪音。
- **类型与工具链**：改动后 `ruff check` + `mypy` 对改动文件清零；不越界修历史错误；不引入 `import *`。

---

## 5. 实施步骤（craft 阶段按序）

1. `model_tool_helper.py`：
   - 改 `invalid_tool_call_mention_tool_name`：精确 `name` 优先 + `\b{tool_name}\b` 词边界兜底。
   - 改 `build_invalid_tool_call_repair_message`：每条 `invalid_tool_call` 先 `redact_terminal_output(str(...))` 脱敏 → 截断 500 → 限 5 条 → 整体限 2000 字符；返回 `str`。
   - 同步 docstring（#8）。
2. `model_node.py`：
   - **`requested_tool = bool(tool_calls)` 计算提前**到 `if result[InvalidToolOutcome.REPAIR]:` 块之前（现状在 648 行，块内 634 处不可用，需前置），供 REPAIR 块内分流判断。
   - REPAIR 块内改写（#1/#3）：原 634 `return terminal_state` 改为块内 `if requested_tool: pass`（情形 a，不 return，自然落下方工具分支）`else: _runtime_context.add_message(SystemMessage(repair_message)); return {terminal=False, repair_requested="true", requested_tool=False, final_response=False, pending_tool_calls=[]}`（情形 b，在 invalid_output 之前显式 return）。
   - `str(repair_message)`（#2，680 行）。
   - 情形 (b) 无可疑 → `RUN_FAILED(invalid_model_output)`（#4，复用 751-777 现有路径，IGNORE warning 已在 594-610 打过）。
   - 删 `model_repair_count` 死写（#7，696 行）；`continuation_error_data` 写入保留（因 state 补字段后合法）。
   - `repair_requested` 返回值统一为 `str`（#3 / #11）：显式列出需改处——`model_node.py:697`（工具分支，改 `"true"` 语境）、`:443`（请求前取消分支）、`:477`（请求事件中取消分支）三处 `"repair_requested": False` 一律改为 `"false"`，与 `state.py:63` 的 `str` 声明一致。
   - 同步 `_model_node` docstring（#8，含双轨消费 + 情形 b 回流机制说明）。
3. `state.py`：补 `continuation_error_data` 字段定义（使 schema 完整，`max_steps_node.py:44` 读取合法）（#7）。
4. `workflow.py`：删 `model_repair_count=0` input_state 初始化（#7）。
5. 跑 `ruff check` + `mypy` 改动文件清零。

---

## 6. 验收（独立审查 + 独立测试闭环）

### 6.1 审查 Agent 检查点（对照规范）
- [ ] 每个文件能否一句话描述职责（无"和"）？
- [ ] 是否复用现有 helper / 常量（无重复造轮子）？
- [ ] 是否新增不应有的文件/职责（防膨胀）？
- [ ] 跨层调用 / 业务逻辑泄漏？
- [ ] 函数 docstring 完整且同步？
- [ ] 错误处理完整（无空 catch）？边界（空 invalid / 无合法工具 / 全部非法）覆盖？
- [ ] 日志可定位（情形 a/b 对称 warning + invalid_model_output / max_steps_reached 终态事件）？无 secret 输出？
- [ ] 字符预算（500/5/2000）真正生效？

### 6.2 测试 Agent 检查点（需在 `tests/` 补单测）
- [ ] `decide_invalid_tool_handling`：精确 name 命中 / 词边界命中 / 子串误中（如 "search" 出现在无关文本）**不再**误判 / 全非法。
- [ ] `build_invalid_tool_call_repair_message`：超长 str 被截断、超 5 条被限、整体超 2000 被限、含 secret 被脱敏。
- [ ] `model_node` 情形 (a)：有合法 tool + 可疑 invalid → 走到工具分支、`deferred_repair_message` 被设置、`pending_tool_calls` 非空、`terminal=False`。
- [ ] `model_node` 情形 (b) 可疑：无合法 tool + 可疑 invalid → `add_message(SystemMessage)` 被调用、`terminal=False`、**`repair_requested="true"`（str）**、不写 `pending_tool_calls`、不发终态事件。**且 `_should_continue`（edges.py:26-27）实际路由到 `model` 而非 END**（锚定 #3 修复效果，审查 Agent 强制要求）。
- [ ] `model_node` 情形 (b) 无可疑：无合法 tool + 无命中 → `RUN_FAILED(invalid_model_output)` + `terminal=True`（复用现有分类，非 `parse_invalid`）。
- [ ] **混合列表（IGNORE + REPAIR 同轮、且无合法 tool）**：`invalid_tool_calls` 同时含命中工具名项（REPAIR）与未命中项（IGNORE）、`tool_calls` 为空 → 断言：① IGNORE 部分触发 `model_node_invalid_tool_calls_ignored` warning（594-610）；② REPAIR 部分触发情形 (b) 回流（`add_message` + `repair_requested="true"` + 非终态），**且二者独立发生、REPAIR 回流不受 IGNORE 存在影响、不滑落到 `invalid_output` 终态**；③ `_should_continue` 路由到 `model`。
- [ ] 边界：空 `invalid_tool_calls` → 不触发任何修复分支。
- [ ] 失败路径验证：情形 (b) 可疑重试耗尽后，由 `max_steps_node` 终态发 `"max_steps_reached"`（非 `parse_invalid`）。
- [ ] `state.py` 补 `continuation_error_data` 字段后，`max_steps_node.py:44` 读取 `state.continuation_error_data` 不报错（schema 完整）。
- [ ] 脱敏验证：`build_invalid_tool_call_repair_message` 内部对含 secret 的 raw 片段经 `redact_terminal_output` 脱敏（仅 helper 内调用，未跨文件引用 model_node 未 import 的符号）。
- [ ] 2000 总预算截断后，每条结构化文本仍含 `tool_name` 与 `error` 关键字段（非半截 JSON）。

> 测试 Agent 不得修改业务代码，只生成/运行测试；不通过则交回开发 Agent 修复后重跑，直到审查 + 测试双通过。
