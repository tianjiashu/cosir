# 模型节点流式 Chunk 结构实测分析

> 本文档记录对 `app/core/workflows/nodes/model_node.py` 中
> `async for chunk in model.astream(messages):` 产出的真实 `AIMessageChunk`
> 结构的实测结果，以及据此得出的代码优化结论。
>
> 数据来源（两份调试文件，均由 `_dump_raw_chunk_debug` / `_dump_merged_chunk_debug`
> 绕过常规日志 `MAX_LOG_TEXT_LENGTH` 截断落盘生成）：
> - `logs/debug_raw_chunks.jsonl`：astream 循环**逐 chunk** 落盘（含 `index` 序号）。
> - `logs/debug_merged_chunks.jsonl`：每步**合并累积后**的 `AIMessageChunk.model_dump()`
>   （即 `model.astream` 结束后 `reduce` 合并的结果，对应代码中 `_collect_chunk_to_ai_message` 的产物）。
>
> 调试手段：在 `model_node.py` 新增 `_dump_raw_chunk_debug(chunk, index)`
> （astream 循环内、取消检查之前调用，带序号）+ 既有 `_dump_merged_chunk_debug`，
> 二者互补观察「流式过程」与「合并结果」。
>
> **数据快照元信息**（结论可复现性）：RAW 2366 chunk / MERGED 32 step，均生成于
> 2026-08-10T06:32 前后的本地调试会话。MERGED 内含 **4 个独立 run**（按 `merged.id`
> 的 run 前缀区分）；RAW 是其中第 4 个 run（`019fea5f`，MERGED idx 26-31）的逐 chunk
> 记录，二者可**精确对齐**验证，并非不同 run。

## 一、样本概况

| 维度 | RAW（逐 chunk） | MERGED（合并后） |
|------|----------------|------------------|
| 记录数 | **2366** chunk（6 step 累计，每 step `index` 从 0 重启，故末 chunk `index`=855 ≠ 行数） | **32** step |
| 所属 run | 单个 run `019fea5f`（idx 26-31，6 step） | 4 个 run：019fea09(idx 0-7) + 019fea0a(idx 8-11) + 019fea58(idx 12-25) + 019fea5f(idx 26-31) |
| 与对方关系 | 即 MERGED idx 26-31 的逐 chunk 视角 | idx 26-31 与 RAW 同属一个 run |
| `finish_reason` 分布 | RAW 末 chunk：`tool_calls`×5 + `stop`×1（对应 MERGED idx 26-31） | `tool_calls`×30 + `stop`×2（跨 4 run 汇总） |
| 并行工具调用上限 | MERGED 内单步最多 **9**（run 019fea09 idx=5，对应 jsonl 物理第 6 行） | 同上 |

> 注：全文行号统一用 **idx（0-based）** 口径；jsonl 物理行号 = idx + 1。
> RAW 与 MERGED **idx 26-31 属同一 run（`019fea5f`）**，可逐 step 对齐验证
> 「流式分片 → 合并聚合」的全链路，是本文最强证据。其余 3 个 MERGED run 无对应 RAW。

## 二、实测 Chunk 结构（独立 chunk 均为 `AIMessageChunk`）

```json
{
  "content": "",                          // 思考/工具流期间恒空；仅最终回复与"边说明"非空
  "additional_kwargs": {"reasoning_content": "..."},  // 思考流逐 token 累加
  "response_metadata": {"model_provider": "openai"},  // 多数 chunk 仅此字段；末 chunk 补 finish_reason 等；收尾空壳为 {}
  "type": "AIMessageChunk",
  "name": null,
  "id": "lc_run--<uuid>",                // 同一步内稳定不变
  "tool_calls": [],                      // 仅每步末 chunk 聚合出完整调用（合并后才稳定）
  "invalid_tool_calls": [],              // 流式中间分片误报（合并后清空）
  "usage_metadata": null,                // 仅每步倒数第二 chunk 携带
  "tool_call_chunks": []                 // 流式工具调用分片（name 先定 → args 流式拼 JSON）
}
```

**合并后（MERGED）的 `response_metadata` 字段完整**：每一行均是
`{"model_provider":"openai", "finish_reason":"tool_calls"|"stop",
"model_name":"deepseek-v4-flash", "system_fingerprint":"fp_..."}`。
LangChain 合并累积时把末 chunk 的 `finish_reason`/`model_name`/`system_fingerprint`
左对齐补全到整步，故合并后的 `AIMessageChunk` **确实携带这些字段**（见第五节修正）。

## 三、流式形态规律（RAW 视角，基于 run `019fea5f`）

| 阶段 | chunk 特征 |
|------|-----------|
| 思考流 | `additional_kwargs.reasoning_content` 逐 token 累加，`content` 恒空 |
| 工具流 | `tool_call_chunks` 逐片到达：`name` 先定 → `args` 从 `""`→`"{"`→`"path"`→`"{...}"` 流式拼 JSON |
| 倒数第二 chunk | `usage_metadata` 出现（`input`/`cache_read`/`output`/`reasoning`）；`response_metadata` 在此补全 `finish_reason` 等 |
| 末 chunk（空壳） | `chunk_position:"last"`，但 `response_metadata` 为 `{}`、`usage_metadata` 为 `null`——纯收尾标志，无业务字段 |
| 合法聚合 | 每步末尾才出现 1 个完整 `tool_calls`（合并累积后稳定） |

> 实测末两 chunk：idx=854 携带 `finish_reason`+`usage_metadata`（倒数第二）；
> idx=855 `chunk_position:"last"`、`response_metadata={}`、`usage_metadata=null`（收尾空壳）。
> `usage_metadata` 与 `finish_reason` 在**倒数第二 chunk**，`chunk_position:"last"` 在**末空壳**，
> 二者分属不同 chunk，不可混述。

### 关键规律
1. **`reasoning` 与 `content` 永远不在同一 chunk**（同 chunk 共存 = 0 次）：
   DeepSeek 严格分段——先吐完所有 `reasoning_content`，再开始吐 `content`。
   与 `_extract_reasoning_content` / `_extract_text` 分字段提取逻辑完全契合。
2. **工具步内 `content` 是"边说明边调工具"的说明文本**（如 `好的，我来逐一测试...`），
   且 `content` 与 `tool_call_chunks` **从不同时出现在同一 chunk**（同 chunk = 0）。
   合并时 content 拼接 + tool_calls 聚合彼此独立，无冲突。
3. **`content` 无 list 形态**（实测 0 次）：未触发 DeepSeek 把 tool_use block 塞进
   `content` 的边界情况；`_extract_text` 的 list 防御仍是预防性正确。
4. **RAW 中 `response_metadata` 绝大多数仅 `model_provider`（2360/2366），仅末 chunk
   补 `finish_reason`/`model_name`/`system_fingerprint`（各 6 次），收尾空壳为 `{}`**——
   独立 chunk 与合并后 AIMessageChunk 的字段丰富度不同，读字段需分清对象。

## 四、合并后结构（MERGED 视角，4 个 run 共 32 step）

### 按 run 分组
| run 前缀 | step 数 | `finish_reason` | content 非空 | 最大并行工具调用 |
|---------|--------|----------------|-------------|----------------|
| 019fea09 | 8 | `tool_calls`×8 | 8/8 | 9 |
| 019fea0a | 4 | `tool_calls`×3 + `stop`×1 | 4/4 | 6 |
| 019fea58 | 14 | `tool_calls`×14 | **0/14** | 8 |
| 019fea5f | 6 | `tool_calls`×5 + `stop`×1 | 6/6 | 5 |
| **汇总** | **32** | **`tool_calls`×30 + `stop`×2** | **18/32** | **9** |

### 字段分布（每 step 非空率，汇总）
| 字段 | 非空 step 数 | 说明 |
|------|-------------|------|
| `additional_kwargs.reasoning_content` | 32/32 | 思考流（每步都有） |
| `content` | 18/32 | 工具步说明文本 + `stop` 步最终回复；run 019fea58（英文 reasoning）全程为空 |
| `tool_calls` | 30/32 | 2 个 `stop` 步为 0 |
| `tool_call_chunks` | 30/32 | 与 `tool_calls` 同步 |
| `usage_metadata` | 32/32 | 每步都有 |
| `invalid_tool_calls` | **0/32** | 合并后流式误报已消失 |
| `response_metadata` | 32/32 | 含 `finish_reason`/`model_name`/`system_fingerprint` |

### 关键结论
1. **`finish_reason` 是分支判据**：`tool_calls`×30 → 工具分支；`stop`×2 → 最终回复分支。
   与代码 `_handle_model_node` 的分支判断（`finish_reason`/`tool_calls` 判空）完全吻合。
2. **`stop` 步 = FINAL_RESPONSE**：两个 `stop` step（content `len`=2682 / 1440 字符）均
   `content` 非空、`tool_calls=0`、`reasoning_content` 仍存在（合并后保留思考痕迹）。
3. **`invalid_tool_calls` 合并后 0/32 全空**：确认 RAW 中的 250 次 `name=null/error=null`
   （当前文件可复现；早期 v1 dump 曾达 797 次，文件已删不可复现）纯属流式中间分片误报，
   合并累积后由 LangChain 正确聚合为 `tool_calls`，不残留。
   → 当前"仅合并后检查 `invalid_tool_calls`"的实现**正确**，切勿改为逐 chunk 检测。
4. **并行工具调用确凿**：单 step `tool_calls` 数量分布跨 4 run 汇总，
   上限 9（run 019fea09 idx=5，即 jsonl 物理第 6 行，`call_00`~`call_08`）。代码 `_collect_chunk_to_ai_message`
   处理多 `tool_calls` 聚合正确。
5. **Prompt cache 命中 32/32（100%）**：`usage_metadata.input_token_details.cache_read`
   每次 >0（典型值 7936，为跨 run 复用的系统提示词缓存）。说明 DeepSeek prompt cache
   稳定生效；注意 7936 是**系统提示词缓存**，跨 run 复用，不是单 run 内逐 step 递增命中。
6. **`content` 非空率 18/32 的规律**：run 019fea58（英文 reasoning 的代码审查任务）
   **14 步全程 `content` 为空**——该 run 模型只用 `reasoning_content` 思考、不输出边说明文本；
   其余 3 个 run 的 step content 均非空。这是模型行为差异，非代码问题。

## 五、对代码的优化启发（结论）

### 1. `invalid_tool_calls` 是流式中间分片误报，非真实非法调用 ⚠️ 重点
- 当前 RAW 实测 `invalid_tool_calls` **250 次**（全部 `name=null, error=null`）；
  早期 v1 dump 曾 797 次（文件已删，不可复现）。
- 根因：DeepSeek 流式发出工具调用中间分片（`tool_call_chunks.args` 还是片段时），
  LangChain 在逐 chunk 解析时暂存为 invalid，待末 chunk 才聚合为合法 `tool_calls`；
  **合并后 MERGED 实测 0/32 残留**，印证聚合正确。
- 当前 `model_node.py` 仅在**合并后的 `ai_message`** 上检查 `invalid_tool_calls`
  （约 480-505 行），聚合后已无残留，**功能正确、无 bug**。
- 隐患：若将来改成在 `astream` 循环内对每个 chunk 实时告警，会疯狂误报。
  **保持现状，不要改成逐 chunk 检测。**

### 2. `FINAL_RESPONSE` 分支已确认存在且正确
- MERGED 中 `finish_reason='stop'` 的 2 个 step 均 `content` 非空（`len`=2682/1440 字符）、
  `tool_calls=0`。
- RAW 中 `stop` 末 chunk（idx=854）为 usage-only 且携带 `finish_reason`，
  紧随其后的空壳 chunk（idx=855）`chunk_position:"last"`、`content` 在前序 chunk 已累积完。
- 代码分支判断（`stop` → 最终回复）与实测一致，**无需改**。

### 3. `response_metadata` 合并后完整携带 `finish_reason`/`model_name`/`system_fingerprint` —— 原假设错误，已修正
- ⚠️ 文档早期版本曾误判"`response_metadata` 仅含 `model_provider`，docstring 不实字段待删"。
  实测纠正：独立 chunk 仅末 chunk 携带 `finish_reason` 等（其余仅 `model_provider`，收尾空壳为 `{}`），
  但**合并后的 `AIMessageChunk` 32/32 全部携带完整 `response_metadata`**
  （LangChain 合并时左对齐补全）。
- 因此 `model_node.py` 中 `_collect_chunk_to_ai_message` 的 docstring 对
  `finish_reason`/`model_name`/`system_fingerprint` 的描述**真实准确，不应删除**。
- 代码若需读取 `finish_reason`，应在**合并后的 `ai_message`** 上读取，而非独立 chunk。

### 4. 调试落盘需加开关，避免常驻写盘压力
- `_dump_raw_chunk_debug` 单次 run 可产生 2366 行（≈ MB 级）。
- 建议给落盘加开关（如 `Settings.DEBUG_DUMP_CHUNKS`），默认关闭，
  避免生产环境每 chunk 写盘的 I/O 压力；或限制单文件行数/大小防无限增长。

### 5. 已验证正确的部分（无需改）
- `usage_metadata` 末 chunk 累加：`_extract_usage_from_chunk` 逐 chunk 取、倒数第二 chunk 才非空 → 正确。
- `reasoning_content` 剥离：合并后 `pop("reasoning_content")`，不回灌模型 → 正确。
- 工具调用聚合：末 chunk 聚合出完整 `tool_calls`（含并行多调用）→ 正确。
- `content`/`reasoning` 分字段提取、`content` 判空防御 → 正确。

## 六、待办（ACTION）

1. ~~修订 docstring 删除 `finish_reason`/`model_name`/`system_fingerprint`~~ →
   **已撤销**：实测合并后 `response_metadata` 完整携带这些字段，docstring 描述准确，不删。
2. 给 chunk 调试落盘加 `Settings.DEBUG_DUMP_CHUNKS` 开关，默认关闭，避免常驻写盘压力。
   （符合规范第六章「调试日志不得污染生产默认输出」，建议优先落实。）
3. ~~补一次纯问答任务验证 FINAL_RESPONSE 分支~~ → **已通过 MERGED `stop` 步验证**：
   分支存在且正常，`content` 非空、`tool_calls=0`，与代码契约一致。

---

*注：调试文件 `logs/debug_raw_chunks.jsonl` / `logs/debug_merged_chunks.jsonl`
由调试落盘生成，排查完建议删除，避免堆积。*

## 七、附录：可复现统计方法

为使第四节「按 run 分组」表从断言升级为可验证结论，下列统计口径与输出均来自本仓库
本地数据，任何人可重跑核对。

### 7.1 MERGED 按 run 前缀分组（逐行实测映射）

`merged.id` 形如 `lc_run--019fea09-b812-...`，取 `lc_run--` 之后第一段 uuid 前缀区分 run。
32 行完整映射（idx 均为 0-based；jsonl 物理行号 = idx + 1）：

| idx | run | finish_reason | content 非空 | len(content) |
|----:|-----|---------------|:------------:|-------------:|
| 0 | 019fea09 | tool_calls | True | 27 |
| 1 | 019fea09 | tool_calls | True | 32 |
| 2 | 019fea09 | tool_calls | True | 17 |
| 3 | 019fea09 | tool_calls | True | 33 |
| 4 | 019fea09 | tool_calls | True | 17 |
| 5 | 019fea09 | tool_calls | True | 23 |
| 6 | 019fea09 | tool_calls | True | 21 |
| 7 | 019fea09 | tool_calls | True | 27 |
| 8 | 019fea0a | tool_calls | True | 36 |
| 9 | 019fea0a | tool_calls | True | 17 |
| 10 | 019fea0a | tool_calls | True | 28 |
| 11 | 019fea0a | stop | True | 2682 |
| 12 | 019fea58 | tool_calls | False | 0 |
| 13 | 019fea58 | tool_calls | False | 0 |
| 14 | 019fea58 | tool_calls | False | 0 |
| 15 | 019fea58 | tool_calls | False | 0 |
| 16 | 019fea58 | tool_calls | False | 0 |
| 17 | 019fea58 | tool_calls | False | 0 |
| 18 | 019fea58 | tool_calls | False | 0 |
| 19 | 019fea58 | tool_calls | False | 0 |
| 20 | 019fea58 | tool_calls | False | 0 |
| 21 | 019fea58 | tool_calls | False | 0 |
| 22 | 019fea58 | tool_calls | False | 0 |
| 23 | 019fea58 | tool_calls | False | 0 |
| 24 | 019fea58 | tool_calls | False | 0 |
| 25 | 019fea58 | tool_calls | False | 0 |
| 26 | 019fea5f | tool_calls | True | 32 |
| 27 | 019fea5f | tool_calls | True | 92 |
| 28 | 019fea5f | tool_calls | True | 52 |
| 29 | 019fea5f | tool_calls | True | 62 |
| 30 | 019fea5f | tool_calls | True | 58 |
| 31 | 019fea5f | stop | True | 1440 |

汇总（与第四节表一致）：`019fea09`(8, tc=8, content 8/8, max 9) ·
`019fea0a`(4, tc=3+stop=1, content 4/4, max 6) ·
`019fea58`(14, tc=14, content 0/14, max 8) ·
`019fea5f`(6, tc=5+stop=1, content 6/6, max 5) ⇒
**32 step · tool_calls=30 · stop=2 · content 非空=18/32 · max 并行=9**。
（并行上限 9 出现在 idx=5，对应 jsonl 物理第 6 行，含 `call_00`~`call_08` 共 9 个 tool_calls。）

### 7.2 RAW 与 MERGED 末 6 行对齐证据

RAW 全部 2366 chunk 的 `chunk.id` 前缀均为 `019fea5f`，末 chunk `id` 末段
`019fea5f-72b0-...` 与 MERGED idx=31（`stop`）完全一致 ⇒
RAW 是 MERGED idx 26-31（run `019fea5f`，对应 jsonl 物理第 27-32 行）的逐 chunk 记录，二者可精确对齐验证。

### 7.3 复现命令（本地）

仓库提供可复现统计脚本 `scripts/verify_chunk_stats.py`，输出本文档引用的全部关键数字
（MERGED 按 run 分组、finish_reason 分布、content 非空率、最大并行工具调用、RAW 的
invalid_tool_calls 次数）。

- **Windows cmd**：
  ```bat
  uv run python scripts/verify_chunk_stats.py
  ```
- **Git Bash / WSL**：
  ```bash
  python scripts/verify_chunk_stats.py
  ```

（该脚本从仓库根读取 `logs/debug_merged_chunks.jsonl` 与 `logs/debug_raw_chunks.jsonl`，
路径相对于脚本所在 `scripts/` 的上级目录，无需手工 cd。）

