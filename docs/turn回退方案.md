# Turn 级回退（含文件产物还原）技术方案

> 状态：计划阶段（待评审 + 落地）
> 范围：已结束 turn 的原地回退，文件写/改/删产物还原，对话 state 重置
> **范围约束（D6）**：第一版仅支持回退「序列中最新一个已结束的 turn」。中间历史 turn 的独立回退因会造成对话上下文悬空、同文件后续改动被覆盖等撕裂态，列为「以后做」，且需配套冲突检测后再开放（见 §十.11）。
> 关联：`AGENTS.md`「checkpoint / 工具系统 / 单一职责」决议；`rules/Agent代码开发规范.md`

---

## 一、背景与目标

当前系统已落地：

- 文件写/改/删经 `ToolScheduler` → `FileToolStateCoordinator` → `ToolExecutor` 三段式编排；
- LangGraph `AsyncSqliteSaver` 以 `thread_id = turn_id` 持久化每个 turn 的对话 state；
- `file_io.atomic_write` 保留 BOM/CRLF，`patch`/`write_file` 成功观察自带 `display_data["changes"]`（含 before/after 全文），`delete` 观察只带 `path/type`。

但**没有任何 undo / rollback / revert 实现**。用户在完成一个 turn 后，无法把"这次 agent 循环造成的文件改动 + 对话上下文"整体抹掉重来。

本方案目标：**支持对最新一个已结束 turn 做原地回退**（D6）——把该 turn 触碰过的文件按"执行前状态"还原，并把该 turn 在 checkpoint / turn_messages 中的对话轨迹一并清空，使该 turn 在用户视角"从未发生"。因仅回退"最新且其后无后续 turn"的 turn，不存在后续 turn 引用被删上下文或同文件被后续改动覆盖的撕裂态，回退结果完全自洽。

---

## 二、已确认决策基线（与用户对齐）

| 编号 | 决策 | 说明 |
|---|---|---|
| D1 | 回退方式 = **原地回退** | 直接抹掉该 turn 的文件与对话副作用；不做 fork 新 turn 的 branch 式回退（branch 式列为「以后做」） |
| D2 | 快照存储 = **业务库 `app.sqlite3`（主 SQLite）** | 复用现有 `storage/` 引擎（`main_session_factory`），新增 `file_snapshots` 表，与 `turn_messages` / `turns` / `runtime_events` **同库**。详见 §十.8：checkpoint 库为独立文件（`langgraph_checkpoints.sqlite`，经 `AsyncSqliteSaver` aiosqlite 直连），**不与之合并**；"清快照 + 清对话轨迹 + 置状态 + 写审计"在业务库单事务内原子完成，`adelete_thread` 作幂等补偿步骤 |
| D3 | 快照内容 = **统一存 V4A 操作（序列化 `PatchOperation`）** | 不存 unified diff 文本；`write_file`/`patch`/`delete` 各自构造对应 V4A 操作入快照（详见 §四）。代价：`modified` 也存 hunks/全文，不再「只存增量」——换取最稳还原 + 能还原 `moved` + 零新增依赖 |
| D4 | `modified` 还原 = **反向构造 V4A 操作 + 复用 `apply_all_with_diff`** | 不手写 reverse 算法，复用项目已有 `patch_apply.apply_all_with_diff`（见 §六），无 CRLF/hunk 边界风险 |
| D5 | `execute_terminal` **不进快照库** | 其副作用（装包、改系统配置）无法靠文件快照还原；回退响应中返回 `non_revertible_actions` 提示 |
| D6 | 回退对象 = **序列中最新一个已结束 turn** | 第一版只允许回退「其后无后续 turn」的最新已结束 turn；中间历史 turn 独立回退会造成对话上下文悬空、同文件后续改动被覆盖等撕裂态，列为「以后做」并需冲突检测（见 §十.11）。API 入口处按 `turn_id` 校验其是否为该 task 的最新已结束 turn，否则返回 `409 {"error": "only the latest finished turn can be reverted"}` |

---

## 三、事实依据（基于源码，非推断）

1. **文件落盘点只有 3 个写类工具 + 1 个终端工具**：
   - `write_file`（`write_file.py:170` 原子写，写前读 `original`）
   - `patch`（replace / patch 两模式，`patch_tool.py:233`/`:301`/`:391` 均原子写）
   - `delete`（`delete.py` 直接 `unlink`/`rmdir`/`rmtree`，**删前未读内容**）
   - `execute_terminal`（只跑命令，不落文件）
2. **`write_file` / `patch` 成功观察已带 `display_data["changes"]`**（`file_change_display.py:57`）：每项含 `path / status / before / after / insertions / deletions`。`status` 取 `modified`/`added`/`deleted`/`moved`。
3. **`delete` 观察只带 `path/type`**（`delete.py:294`），**没有 before 内容** → deleted 文件回滚必须改 `delete.py`，在删前读取内容。
4. **`ToolExecutionContext` 无 `turn_id`**（`tool_execution_context.py:15` 仅 `task_id/workspace_id/workspace_root`）→ 快照按 turn 关联需在 dataclass 加 `turn_id`，并在 `from_workspace` 调用链补传。
5. **checkpoint 以 `thread_id = turn_id`**（`runner.py:497` 用 `aget_tuple({"configurable": {"thread_id": turn_id}})`）。LangGraph `AsyncSqliteSaver` 自带 `adelete_thread(thread_id)`（`checkpointer.py:13` 的 `AsyncSqliteSaver`），可整 thread 删除。
6. **turn 对话轨迹还落在 `turn_messages` 表**（`storage/crud/turn_message_crud.py`），回退时需一并清空，否则下一轮上下文仍带"已回退 turn"记忆。

---

## 四、存储设计（主 SQLite，`file_snapshots` 表）

```sql
CREATE TABLE file_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id       TEXT    NOT NULL,          -- 关联 turn（来自 ToolExecutionContext.turn_id）
    tool_call_id  TEXT    NOT NULL,          -- 关联模型工具调用（预留给前端高亮"哪次调用被撤销"；当前回退流程只按 turn_id+seq 检索，不按它查询）
    tool_name     TEXT    NOT NULL,          -- write_file / patch / delete
    path          TEXT    NOT NULL,          -- 文件相对 workspace_root 的路径（主操作文件）
    action        TEXT    NOT NULL,          -- added / modified / deleted / moved
    op_json       TEXT    NOT NULL,          -- 序列化的反向 V4A 操作（PatchOperation 投影）
    seq           INTEGER NOT NULL,          -- 同 turn 内执行序号，回退时逆序应用
    created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_file_snapshots_turn ON file_snapshots(turn_id, seq DESC);
```

**存储策略（对应 D3：统一 V4A）**

快照不存 unified diff 文本，而是存**该操作的反向 V4A 操作本身的序列化 JSON**（`op_json`）。回退时反序列化 `op_json` 为 `PatchOperation`（即 `PatchOperation(**json.loads(op_json))`），直接交给 `apply_all_with_diff` 应用，无需 reverse 算法。

`op_json` 存的是**反向操作**（`reverse of what was applied`），以便在 §六 的 `revert_turn` 里直接 apply 即还原：

| 正向 action（落盘时实际发生） | 存进 `op_json` 的**反向** V4A 操作 | 还原语义 |
|---|---|---|
| `added`（新建文件，content 全文） | `DELETE <path>` | 删掉该 turn 新建的文件 |
| `modified`（hunks 增量） | `UPDATE <path>`，其 hunk 的 `+`/`-` 行对调 | 写回 before |
| `deleted`（删前读 before 全文） | `ADD <path>`，content = before 全文 | 找回被删文件 |
| `moved`（src → dst） | `MOVE <dst> → <src>` | 移回原路径 |

> 说明：反向操作在**采集时（scheduler 落库前）就构造好**并序列化进 `op_json`，回退路径因此极简——读 JSON → 构造 `PatchOperation` → `apply_all_with_diff`。这复用了 `patch_apply.py:233` 的现有应用逻辑（含 `atomic_write_text` 的 BOM/CRLF 保留、路径 containment 校验），**不重复造轮子、无 hunk/CRLF 边界风险**。
> `PatchOperation` 序列化形态：`{operation, file_path, new_path?, hunks:[{context_hint?, lines:[{prefix, content}]}], content?}`，与 `patch_parser.py:43` 的 dataclass 字段一一对应，落库前 `dataclasses.asdict` 即可。

---

## 五、采集切点（最小侵入）

### A. 不动 handler 的写盘逻辑，只在 `ToolScheduler.execute` 成功返回后补落库

切点：`tool_scheduler.py:260-271`，`filesystem` 分支 `self._executor.execute(...)` 成功、`self._state_coordinator.complete(...)` 之后。

逻辑（新增私有方法 `_record_file_snapshot`，借助新增 `tools/file_snapshot/v4a_reverse.py` 的 `build_reverse_operation`）：

```text
observation 成功 且 tool.resource_keys 含 "filesystem" 且 tool.name in {write_file, patch, delete}:
    turn_id = execution_context.turn_id           # 来自 §五 C 注入的 ToolExecutionContext.turn_id 字段
    # 从 handler 产出的事实构造「正向 V4A 操作」：
    #   - write_file / delete：由 change{path,status,before,after} 构造 ADD/DELETE/UPDATE
    #   - patch：patch 模式已有 operations；replace 模式由 change 构造 UPDATE
    forward_ops = build_forward_operations(tool.name, observation.display_data.get("changes", []))
    for op in forward_ops:
        reverse_op = reverse_v4a_operation(op)     # 反向：ADD<->DELETE / UPDATE 对调 hunks / MOVE 对调路径
        op_json = json.dumps(dataclasses.asdict(reverse_op), ensure_ascii=False)
        file_snapshot_store.save(turn_id, tool_call_id, tool.name,
                                 op.file_path, op.operation.value, op_json, seq)
        seq += 1
```

> 注意 `patch` 工具当前**只在内部**持有 `PatchOperation`，并未随 `observation` 返回（`patch_tool.py:391` `apply_all_with_diff` 返回 `FileDiffResult` 而非 operations）。因此采集时**不直接复用 patch 的 operations**，而是统一由 `build_forward_operations` 从 `display_data["changes"]` 的 `before/after` 重新构造 V4A 操作——`write_file`/`replace` 构造 `UPDATE`（hunks 由 before→after 逐行 diff 生成），`added` 构造 `ADD`，`deleted` 构造 `DELETE`（before 全文作 ADD 的 content）。这保证三个工具走同一构造入口，不依赖各自内部是否暴露 operations。
> `format_unified_diff` 仍用于 UI 展示（回退前预览），但**不再用于落库**。

### B. 仍需改的 handler：`delete.py` 删前读 before 内容

`delete` 观察当前只有 `path/type`（无 before，见 §三.3），而 `deleted` 的反向 V4A 操作是 `ADD`（content = before 全文），**必须拿到删前内容**。故在 `delete.py` 各删除成功路径（文件 `unlink`、目录 `rmdir`/`rmtree`、链接 `unlink`/`rmdir`）**执行删除前**读取目标当前内容，并**复用 `build_file_change_display_data`** 构造 `display_data["changes"]`——与 `write_file` / `patch` 保持同一产出结构，确保采集入口 `build_forward_operations`（§五 A）只解析一种 `changes` 形态，不会因 `delete` 手搓字典而解析失败。

- **文件**：删前 `Path(resolved).read_text(encoding="utf-8", errors="replace")` 作为 before，`build_file_change_display_data([FileDiffResult(path=resolved, status="deleted", before=<内容>, after="")])` 注入 `display_data`。
- **目录**：before 存目录树结构摘要（路径列表），回退时重建目录骨架；但目录重建细节列为「建议做」，第一版目录 deleted 仅记录、回退时打提示「无法自动还原目录内容」。
- 链接：`delete` 当前返回普通 `ToolObservation`（非 `tool_success`），需统一改为经 `build_file_change_display_data` 构造带 `display_data["changes"]` 的观察，与其它文件工具对齐。
- **读 before 失败的降级**：读失败不应阻断删除，仅记空 before 使该文件无法回滚，并打 warning 日志（回退时该文件在 `non_revertible_actions` 中提示）。

### C. `ToolExecutionContext` 注入 `turn_id`

`tool_execution_context.py:15` 增加字段 `turn_id: str`；`from_workspace` 增加 `turn_id` 参数（调用链：`runtime/runs` → `ToolScheduler.execute` 的 `execution_context` 构造处需补传 `turn_id`）。需逐一核对 `from_workspace` 调用点（见 §七 影响面）。

---

## 六、原地回退流程（TurnRevertService.revert_turn）

```
revert_turn(turn_id):
  0. 并发/重入防护：
       - 按 turn_id 分桶的进程内 asyncio.Lock，防同一 turn 被并发回退；
       - 若 turn.status == reverted：幂等直接返回成功（避免重复回退）。
  1. 校验 turn 已结束（status not in running/cancelling）；否则返回 409 拒绝（先 cancel）
     校验 turn 是否为该 task 序列中**最新一个已结束 turn**（D6）；若其后还存在其它已结束 turn，
     返回 409 `{"error": "only the latest finished turn can be reverted"}`（对应 §十.11：中间历史
     turn 回退会造成上下文撕裂，第一版不开放）。校验依据：按 task_id 查询该 task 全部 turn，
     取 created_at / 序号最大的已结束 turn 与入参 turn_id 比对。
  2. 查 file_snapshots WHERE turn_id ORDER BY seq DESC   # 逆序：后做的先撤
  3. 逐文件 restore（reverse order）：
       读 op_json → 构造 PatchOperation（reverse 已存好）→
       apply_all_with_diff([op], resolver)   # 复用 patch_apply.py:233，内部 atomic_write 保留 BOM/CRLF、路径 containment 校验
  4. 若任一文件还原失败：保留当前已还原/未还原的磁盘状态并明确报错，返回失败 + 已还原清单；
       因文件还原是**可重入**的（同 op_json 重复 apply 幂等），出错后用户/前端重试同一
       revert_turn 即可继续，不需手动回滚半残态。
  5. 全文件成功 → 业务库单事务（同一 main_session_factory 连接）原子完成：
       - turn_message_crud.clear_by_turn(turn_id)        # 清对话轨迹
       - turn.status = reverted
       - 写 runtime_event(turn_reverted) 审计日志
       - file_snapshot_crud.clear_by_turn(turn_id)       # 清快照（与上面同事务）
     提交后，再执行**补偿式步骤**：
       - 经 build_checkpointer() 独立拿 AsyncSqliteSaver 实例：
         async with build_checkpointer() as saver: await saver.adelete_thread(turn_id)  # 清 checkpoint
       该步骤幂等；若失败，已提交的业务库状态不受影响，重入 revert_turn 重试即可。
  6. 返回 {reverted_files, non_revertible_actions}
```

> 两段式依据见 §十.8：业务库事务与 `adelete_thread` 分属两个 SQLite 连接，无法跨连接原子；故把"能原子化的 4 项"收进业务库单事务，`adelete_thread` 作幂等补偿。这与 §二 D2 一致。

**文件还原顺序要点**：`op_json` 已存**反向**操作，逆序 apply 即按"后做的先撤"还原；同 path 出现多次（先 modified 再 deleted）按 seq 逆序自然正确。

### 反向 V4A 操作构造（D4，复用 apply_all_with_diff）

新增 `tools/file_snapshot/v4a_reverse.py`，提供 `reverse_v4a_operation(op: PatchOperation) -> PatchOperation`：

| 正向 `op.operation` | 反向操作 | 构造方式 |
|---|---|---|
| `ADD` | `DELETE <path>` | 直接改 operation 类型；DELETE 无 hunks/content |
| `DELETE` | `ADD <path>`，content = 原 DELETE 隐含的 before 全文 | 见 §五 B：deleted 反向 ADD 的 content 来自采集时记录的 before |
| `UPDATE` | `UPDATE <path>`，其每个 hunk 的 `+`/`-` 行对调 | 遍历 `hunk.lines`，`+`→`-`、`-`→`+`、` ` 不变（见 `patch_parser.py:185` 行前缀约定） |
| `MOVE` | `MOVE <new_path> → <file_path>` | 交换 `file_path` 与 `new_path` |

> **UPDATE 反向语义澄清（避免实现误解）**：反向 `UPDATE` 的 hunks 必须基于**采集时由 `before→after` 生成的 diff**（`build_forward_operations` 产物，见 §五 A）再做 `+`/`-` 对调；**不是**对调用户原始 `patch` 工具传入的 hunks。`replace` 模式 hunks 来自 `fuzzy_find_and_replace` 的 `before/after`，`patch` 模式 hunks 来自用户 patch 文本——两者经统一入口 `build_forward_operations` 后都已归一为"采集 diff"，反向入口只需对这一层产物对调，不对原始来源做区分。`v4a_reverse.py` 注释需固化此约束。

> 关键收益：回退路径**完全复用** `patch_apply.apply_all_with_diff`（`patch_apply.py:233`），它内部已处理 BOM/CRLF 保留（`atomic_write_text`）、路径 containment（`ProjectPathResolver`）、MOVE 的 `os.replace`——不重写任何落盘逻辑，不引入 hunk/CRLF 边界风险，零新增依赖。
> `ADD`/`UPDATE` 的 content/hunks 来自采集时由 `before/after` 构造的 V4A 操作（§五 A），反向后其内容即"还原目标状态"，apply 即还原。

> 风险：`unified_diff` 在 CRLF 文件上 reverse 可能出现 `\ No newline` 边界错位。缓解：reverse 前把 current_text 与 diff 统一归一化到 LF 计算，写回时再恢复原行尾（见 `atomic_write` 现有 BOM/CRLF 保留逻辑复用）。这部分须在单测覆盖。

---

## 七、目录与文件清单（新增 / 修改）

### 新增（叶子模块，零上层依赖）

| 路径 | 职责 |
|---|---|
| `storage/model/file_snapshot_model.py` | `file_snapshots` 表 ORM 模型（继承 `StorageBase`，与 `turn_model.py` / `turn_message_model.py` 同级）；仅承载表结构 |
| `storage/crud/file_snapshot_crud.py` | `file_snapshots` 表**唯一 CRUD 收口**：`save()` / `list_by_turn()` / `clear_by_turn()`（import 上面的 model）；纯数据层，不依赖 service |
| `tools/tool_handler/patch/v4a_reverse.py` | `build_forward_operations()`（由 change 构造正向 V4A 操作）+ `reverse_v4a_operation()`（ADD↔DELETE / UPDATE 对调 hunks / MOVE 对调路径）；纯转换逻辑，不落盘，与 `patch` 子系统同域（操作 `PatchOperation`、复用 `apply_all_with_diff`） |
| `service/turn_revert_service.py` | 编排回退：读快照 → 反序列化 op_json → `apply_all_with_diff` 逐文件 restore → 清 checkpoint/turn_messages → 置 status；返回 `non_revertible_actions` |
| `models/file_snapshot_record.py` | `FileSnapshotRecord` 值对象（`from_row` 工厂） |
| `api/schemas/request/RevertTurnRequest.py`（可选） | 回退请求体（第一版可无 body） |
| `api/schemas/response/RevertTurnResponse.py` | `{reverted_files, non_revertible_actions, status}` |
| `tests/test_file_snapshot_*.py` / `tests/test_file_snapshot_*.py` / `tests/test_turn_revert_*.py` | 单测 |

### 修改（最小改动）

| 路径 | 改动 |
|---|---|
| `tools/schemas/tool_execution_context.py` | 加 `turn_id` 字段；`from_workspace` 加 `turn_id` 参数 |
| `tools/tool_execute/tool_scheduler.py` | `filesystem` 成功分支后调 `_record_file_snapshot`；导入 `storage.crud.file_snapshot_crud`（落库）与 `tools.tool_handler.patch.v4a_reverse`（构造反向操作） |
| `tools/tool_handler/delete.py` | 删前读 before，注入 `display_data["changes"]`（deleted） |
| `storage/init_schema.py` | 新增 `file_snapshots` 建表（含列迁移兼容） |
| `api/turns_api.py` | 新增 `POST /turns/{turn_id}/rollback` |
| `core/runtime/runs/*` 中 `from_workspace` 调用点 | 补传 `turn_id`。runtime 层在构造 `execution_context` 时已持有 `turn_id`（checkpoint 以 `thread_id=turn_id`，见 §三.5），落地前用 `search_content` 全量定位 `from_workspace(` 调用点逐一补传，避免缺参运行错误（对应 §十.7） |
| `service/turn_revert_service.py` 内部 | `revert_turn` 自行 `async with build_checkpointer() as saver: await saver.adelete_thread(turn_id)` 拿 `AsyncSqliteSaver` 实例（不能复用 graph 编译期实例）；属 §六 第 5 步的补偿式步骤 |

> 依赖方向遵守 `AGENTS.md`：`api → service`；`service → storage/tools/models`；`tools` 不依赖 service。`file_snapshots` 表的数据层归属严格收口在 `storage/`（`storage/model/file_snapshot_model.py` 定义结构 + `storage/crud/file_snapshot_crud.py` 唯一 CRUD）；`ToolScheduler`（`tools/`）采集时直接调 `storage.crud.file_snapshot_crud.save()`，属 tools 向 storage 单向依赖（合规，与 `tasks_api` 等既有调用一致），不在 `tools/` 内另建伪 storage 层。`v4a_reverse.py` 因只操作 `PatchOperation`、与 `patch` 子系统同域，置于 `tools/tool_handler/patch/` 而非新建 `tools/file_snapshot/` 子包，减少跨子包耦合。

---

## 八、API

```
POST /turns/{turn_id}/rollback
```

- 鉴权/权限：复用现有 turn 归属校验。
- 前置：turn 必须已结束；运行中返回 `409 {"error": "turn not finished; cancel first"}`。
- 前置（D6）：turn 必须是该 task 序列中最新一个已结束 turn；若其后还有其它已结束 turn，返回 `409 {"error": "only the latest finished turn can be reverted"}`。
- 成功 `200`：
  ```json
  {
    "turn_id": "...",
    "status": "reverted",
    "reverted_files": ["src/a.py", "src/b.py"],
    "non_revertible_actions": [
      {"tool": "execute_terminal", "tool_call_id": "...", "note": "终端副作用无法回退，请手动核对"}
    ]
  }
  ```
- 失败 `207 Partial` / `500`：返回已还原文件清单 + 失败原因，便于用户手动收尾。

---

## 九、测试清单（pytest，关键路径必须有覆盖）

> **测试范围说明（受 D6 约束）**：第一版仅回退「最新已结束 turn」，其后无后续 turn，故
> **不存在跨 turn 冲突、上下文悬空、后续改动被覆盖**等场景，以下测试均围绕「单 turn 自洽回退」
> 设计，**不含**「中间历史 turn 回退」「同文件多 turn 冲突」用例（那些属于 §十.11 的「以后做」）。
> 测试分层：纯函数（T1–T3，无 IO）→ 文件还原（T4，tmp_path）→ 编排集成（T5–T8）。

- **T1 `v4a_reverse` 纯函数**：`reverse_v4a_operation` 四类映射正确（ADD↔DELETE / UPDATE hunks 对调 / MOVE 路径对调 / DELETE→ADD 的 content=before）；断言产物 `operation`/`file_path`/`new_path`/`hunks` 行前缀。
- **T2 `file_snapshot_crud` 往返**（内存 SQLite）：`save()`→`list_by_turn()` 顺序正确（seq DESC）；`op_json` 经 `json.loads` 后能 `PatchOperation(**...)` 还原；`clear_by_turn()` 清干净；含一例中文路径 `ensure_ascii=False` 往返。
- **build_forward_operations 构造**（从 `changes` 构造，不碰真实文件）：输入 `display_data["changes"]` 三种 status（`added`/`modified`/`deleted`），断言产出 `PatchOperation` 形态（§五 A 统一入口隔离）。
- **T4 文件还原端到端**（tmp_path，复用 `apply_all_with_diff`）：新增文件被删除、删除文件写回 before 全文、modified 还原 == before；CRLF/BOM 还原后行尾/编码不变；同 path 多次操作（先 write 再 patch 再 delete）逆序回退后磁盘 == 初始。
- **T5 `revert_turn` 编排集成**（内存库 + tmp_path + 真实临时 checkpoint）：文件还原 + `adelete_thread` 删 thread + `turn_messages` 清空 + `status=reverted` + `runtime_event(turn_reverted)` 落库。
- **T6 前置守卫**：运行中 turn 调 rollback 返回 409（`turn not finished; cancel first`）；**D6 守卫**——task 存在「已结束 turn #N+1 在 #N 之后」时对 #N 调 rollback 返回 409（`only the latest finished turn can be reverted`），仅对最新已结束 turn 进入回退。
- **T7 并发/重入**：`turn.status==reverted` 时重复调用幂等返回成功；文件还原中途注入异常→报错，修复后重入 `revert_turn` 可继续完成（验证 op_json 重复 apply 幂等，对应 §六 第 0/4 步）；同 turn 两协程经 `asyncio.Lock` 分桶仅一个真正执行。
- **T8 `delete` before 读取 + `non_revertible_actions`**：`delete` 工具跑后观察 `display_data["changes"]` 带 before 全文（§五 B）；读 before 失败降级（mock 读盘异常→删除仍成功、before 为空、warning 日志）；turn 含 `execute_terminal`（不进快照）时响应含 `non_revertible_actions` 提示项。
- **T9 `from_workspace` 调用点回归**：落地后全量 `from_workspace(` 调用点均补 `turn_id`（smoke test 兜住 §十.7 缺参运行错误）。
- **T10 API 层**（可选但建议）：`POST /turns/{id}/rollback` 的 200 / 409（运行中）/ 409（D6 非最新）/ 207（文件还原部分失败）覆盖。

---

## 十、明确不涵盖 / 风险

1. **`execute_terminal` 副作用不可回退**（装包、改系统配置、数据库变更等）；UI 必须标注「此 turn 含不可回退的终端操作」。
2. **`moved`（patch move 操作）现已可回退**：统一 V4A 方案下 `MOVE` 的反向操作是 `MOVE dst→src`（`os.replace` 移回），第一版即支持（见 §六 反向表）。
3. **目录 deleted 重建细节**：第一版目录 deleted 仅记录、回退时打提示，不自动重建目录树（列为「建议做」）。
4. **回退只对已结束 turn 有效**；运行中 turn 须先 cancel。且第一版仅允许回退该 task 序列中最新一个已结束 turn（D6），见第 11 条。
5. **无 workspace 的纯对话 turn**：只回退 checkpoint / turn_messages，无文件快照。
6. **`modified` 不再「只存增量」**：统一 V4A 后 `modified` 的 `UPDATE` 操作携带 hunks（逐行 diff 上下文），体积比 unified diff 略大但远小于存全文；`deleted`/`added` 仍按最小必要信息存储。若未来介意体积，可改为 hunks 仅在采集时生成、落库只存 before/after 全文由 `apply_all_with_diff` 内部重算——但当前选 V4A 是为还原可靠性优先。
7. **`from_workspace` 调用点扩散**：加 `turn_id` 参数会波及 runtime 调用链，须在落地前用 `search_content` 全量定位并逐一补传，避免 `from_workspace` 缺参导致运行错误。
8. **两库不合并、跨连接非原子（已知局限）**：业务库 `app.sqlite3`（SQLAlchemy 引擎 `main_session_factory`）与 checkpoint 库 `langgraph_checkpoints.sqlite`（`AsyncSqliteSaver.from_conn_string` aiosqlite 直连，`store_engines.py` 明确"不经过本模块引擎"）是**两个独立 SQLite 文件**，`file_snapshots` 落在业务库。SQLite 事务为**连接级**，即便合并两库文件，`adelete_thread` 仍走 LangGraph 自有连接、业务库走 `main_session_factory`，**两者无法跨连接原子提交**。故回退采用**两段式**：① 业务库单事务（清 `file_snapshots` + 清 `turn_messages` + 置 `turn.status=reverted` + 写 `runtime_event` 审计）原子完成；② `adelete_thread` 作为**补偿式幂等步骤**在事务提交后执行，失败可重入 `revert_turn` 重试，不破坏已提交的业务库状态。合并两库仅能"同文件"而非"同事务"，且会污染 LangGraph 受版本控制的内部 schema、破坏存储收口约定，故**不合并**。
9. **目录 deleted 混合场景局限（已知局限）**：若某 turn 删了一个目录、又在同一 turn 新建了同路径下的文件，回退时目录仅提示（见第 3 条「建议做」）、文件被 ADD 反向 `DELETE` 删掉，结果是"目录没了、文件也没了"；但若该目录里**还有其它未被本 turn 触碰的文件**，它们会残留磁盘、不被清理。第一版明确此局限，回退响应中对该目录标注「可能残留未追踪文件，请手动核对」。
10. **审计事件 `turn_reverted` 命名一致性（落地核对项）**：`turn_reverted` 符合「稳定英文 snake_case、禁 f-string」日志规范；落地时需确认不与现有 `runtime_event` 的 event 命名空间冲突（若项目有统一 event 枚举则登记，否则保持字符串字面量一致即可）。
11. **仅支持回退最新已结束 turn（D6，已知范围限制）**：第一版不允许回退中间历史 turn。原因——每个 turn 结束时把 checkpoint 的 `messages` 落库到 `turn_messages` 供后续 turn 拼回上下文（`runner.py:480-484`），若回退中间 turn #N，其后续 #N+1、#N+2 落库的 `turn_messages` 仍引用 #N 内容，造成"上下文悬空"；且 #N 碰过的文件若被后续 turn 改过，只回退 #N 会覆盖/删除后续改动，与后续 turn 的 completed 状态矛盾。故第一版约束为"仅最新已结束 turn 可回退"，此时其后无后续 turn，三类不一致全部消失，回退完全自洽。中间历史 turn 的独立回退列为「以后做」，开放前必须补：① 同文件多 turn 冲突检测（`conflicting_subsequent_turns`）；② 级联 or 覆盖式二次确认；③ 后续 turn 上下文悬空的修复策略。

---

## 十一、实施阶段（必须 / 建议 / 以后）

- **必须做**：§七 新增+修改清单；§九 测试（含 D6 守卫 409 用例）；`turn_id` 注入；`delete` before 读取；`v4a_reverse` 四类反向操作；`revert_turn` 复用 `apply_all_with_diff`；`POST /rollback`；**D6 守卫**（仅最新已结束 turn 可回退，否则 409）。
- **建议做**：目录 deleted 自动重建；回退前 UI 预览 diff（`format_patch_diff` 已有，复用）；回退审计事件进前端 timeline。
- **以后做**：git worktree 集成，使 `execute_terminal` 副作用也可随 worktree `reset` 回退；branch 式 fork 回退；**中间历史 turn 的独立回退**（需先补 §十.11 的冲突检测与上下文悬空修复）。
