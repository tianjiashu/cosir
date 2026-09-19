# Task 级文件 ChangeSet 与回退接线方案

> 状态：当前实现采用单表 snapshot-first 流程。结构化文件写入先持久化隐藏的预写快照，再按实际落盘状态收口；启动只对账并记录当前磁盘状态，不恢复文件。任意终端写入仍不在捕获范围内。本文描述当前契约及平台验证边界。

## 1. 目标与决定

将文件 ChangeSet 作为 **task 级领域能力**：聚合该 task 多次 Run 产生的文件变更，并允许用户审阅、保留或回退 Agent 修改。

UI 对每个文件组只展示“最近一次 Keep 确立的基线 → 当前文件状态”的最终净 Diff，不展示逐次操作 Diff。一次 `Revert` 将该组从当前状态整体恢复到该基线；成功回退后，这个基线仍是后续变更的起点。

回退遵循以下规则：

1. 回退一个文件时，撤销该文件自最近一次用户处理边界以来的全部 Agent 文件操作，而不只撤销最后一次工具操作。
2. `Keep` 将当前文件状态设为新的回退基线；回退不得跨过该基线。
3. 每个文件变更组独立预检与提交。批量回退时，冲突文件失败，其余可安全回退的文件继续完成。
4. 回退期间发现文件状态与 Agent 变更链不一致，按人工或外部修改处理；该文件组不写入任何部分结果，不提供强制覆盖选项。
5. ChangeSet、Run 与文件快照的业务事实由后端管理。React 只读取和呈现，所有回退写操作由后端执行。

“尽最大可能”指批量操作在文件组之间尽力完成；不代表对检测到冲突的单个文件继续部分覆盖。

## 2. 实施前源码现状

下表记录改造前的源码基线，用于说明本方案解决的问题，不代表当前实现状态。

| 边界 | 改造前实现 | 当时的缺口或风险 |
| --- | --- | --- |
| 采集 | `FileSnapshotHook` 仅在工具观察 `status="success"` 时读取 `artifact_data.changes`，保存反向 V4A 操作；记录关联 `task_id`、`run_id` 和 task 内递增的 `seq`。 | 部分写入的失败观察没有 `artifact_data`，不会被当前 Hook 记录。新建和移动操作也缺少足以验证 Agent 写后状态的完整信息。 |
| 持久化 | `file_snapshots` 位于主业务库 `storage/app.sqlite3`，保存路径、反向操作、序号和 `pending/kept/reverted` 状态。 | `op_json` 当前只保存反向 `PatchOperation`，没有版本化状态指纹；查询也不能生成基线到当前状态的最终净 Diff。 |
| 查询 | `query_change_set` 按 task 聚合，并按路径只保留最新快照。默认会包含 `stable=0` 的记录。 | 聚合结果缺少完整 pending 尾段校验和相对基线的最终净 Diff。 |
| 回退 | `revert_file` 读取某路径的最新快照，应用其反向操作并更新状态。 | 多次修改同一文件时，只回退最新一条。 |
| 批量 API | `/tasks/{task_id}/changes/revert` 依次执行路径回退，遇到首个异常即返回错误。 | 前序文件可能已经成功写入，但响应没有逐文件成功/失败结果。 |
| 冲突检测 | UPDATE 对比当前文本和预期 Agent 状态；反向 DELETE 对 Agent 新建文件仅检查文件存在；MOVE 主要检查源、目标存在状态。 | 对被人工修改过的新建文件或移动后文件，存在误删或覆盖风险。文本尾换行归一化也会接受少数纯尾空行修改。 |
| 桌面 UI | 工具调用已有只读 `DiffTool`。`TaskPage` 按 taskId 装配 Assistant。 | 未发现 ChangeSet API 客户端或 task 级变更面板。 |
| 快照收口 | `runner.py` 有将 Run 快照标为 stable 的辅助方法，但其成功路径调用当前被注释，且未发现其他调用点。查询默认包含 unstable 记录，所以现有查询仍可读到它们。 | 需要统一 Run 所有终态下的快照收口语义；回退资格不能仅依赖 `stable`。 |
| 终端写入 | `execute_terminal` 可运行通用 shell 命令；提示文本建议文件修改使用专用工具，但该提示不是文件系统写保护。 | shell 命令可能修改 workspace 文件却不产生 `artifact_data.changes`。在补齐捕获或施加可验证限制前，不能宣称 ChangeSet 覆盖全部 Agent 文件修改。 |

源码入口：`apps/backend/app/hook/builtins/file_snapshot_hook.py`、`apps/backend/app/service/task/change_set/`、`apps/backend/app/api/changes_api.py`、`apps/backend/app/storage/model/file_snapshot_model.py`、`apps/desktop/components/task-page.tsx`、`apps/desktop/components/assistant-ui/tools/diff-tool.tsx`。

## 3. 进程、存储与生命周期

```text
Tauri Rust 主进程
  ├─ 启动 / 停止 / 有限恢复 Python FastAPI 子进程
  └─ WebView2 承载 React 桌面 UI
       ├─ 通过本机 HTTP 查询 task ChangeSet
       └─ 通过本机 HTTP 提交 Keep / Revert 命令

Python FastAPI 后端
  ├─ FileMutationService 是结构化文件编辑的唯一采集与持久化入口；不通过 Hook 二次投影
  ├─ service 聚合、预检并执行回退
  ├─ SQLite storage/app.sqlite3 保存 task 文件快照与预写操作标记
  ├─ storage/change_set/blobs 保存待回退的 before-image，临时 staging 在提交后清理
  └─ workspace 目录保存真实文件
```

ChangeSet 只覆盖结构化文件编辑工具产生的写入；`execute_terminal` 的命令可能修改 workspace 文件，但不纳入文件变更采集或回退范围。

- 不增加后端服务、消息队列、远程存储或 Tauri IPC 命令。领域请求继续经 `apps/desktop/lib/http/client.ts`，由它处理 runtime 动态基地址、`no-store` 和 `X-Trace-Id`。
- 功能运行在 Python/FastAPI 后端：Tauri Rust 主进程按现有 bootstate 与动态 localhost 端口机制启动后端；React 在 backend boot gate 显示 ready 后，通过 task domain API 读取或操作 ChangeSet。退出桌面应用时由 Rust 清理后端进程树，不由 React 管理子进程。
- SQLite 中的快照与 workspace 文件属于两种事实：快照描述 Agent 变更及其回退能力，文件系统保留实际当前内容。`FileMutationService` 是结构化工具写入与可回退记录之间的持久化边界。
- React 不读写 SQLite、也不直接修改 workspace 文件。ChangeSet 不写入 `localStorage`，不进入 Assistant Transport；`artifact_data` 保持后端内部用途。
- Tauri 仍是后端进程生命周期唯一所有者。退出时清理后端进程树；崩溃后只做有限串行恢复，不重放旧 Run。已终态或恢复后已取消 Run 留下的成功文件操作仍可由 ChangeSet 审阅和回退。
- 后端 lifespan 在接受业务请求前对账 `file_snapshots` 中的预写行和中断的 Revert 标记。Agent 文件操作按当前磁盘状态收口快照；Revert 若已到达 before 状态则标记 reverted，否则把当前磁盘状态记为新的 after 并保留 pending。启动不会恢复或覆盖 workspace 文件；无法读取的操作继续隐藏并阻止路径重叠写入，供后续启动重试。桌面 supervisor 的有限重启不重放 Agent Run。
- `ConversationRunModel.status` 是 Run 状态唯一事实源。运行中禁用 Keep/Revert；后端也要以 task 运行时操作闸门和 canonical Run 状态拒绝并发写操作，不能只依赖前端按钮状态。
- 第一阶段保证结构化文件工具的 ChangeSet 可回退能力。任意 shell 写入暂不纳入该保证；task 面板始终显示“当前追踪文件工具修改；终端命令可能产生未记录的文件变更”。如产品要求覆盖所有 Agent 写入，必须先完成终端写入捕获或可靠限制，并通过验收；不能仅凭 system prompt 认定命令只读。

## 4. 领域语义

### 4.1 文件操作历史与处理边界

每条 `file_snapshots` 记录仍代表一次 Agent 文件操作。查询列表可以继续按文件组聚合，但回退服务必须读取该组完整的有序历史。

把 `kept` 和 `reverted` 作为已处理边界：

- `Keep`：将当前文件组自上一个处理边界以来的 pending 快照全部标记为 `kept`。该文件当前状态成为后续回退的基线。
- `Revert`：读取最新处理边界之后的 pending 快照，按 `seq` 逆序回退；成功后把参与的快照全部标记为 `reverted`。回退后的状态成为下一次修改的基线。
- 后续新 Run 再次修改同一文件时，新快照形成新的 pending 尾段。下一次回退只处理该尾段。
- 重复回退已完成的文件组返回 `already_reverted`，不重复写文件。
- 冲突是一次回退命令的结果，不是永久文件状态。冲突不改写快照状态；UI 展示“本次未回退”，下次请求仍重新检查磁盘。

示例：文件初始为 `V0`，Agent 改成 `V1` 后用户执行 Keep；之后 Agent 又改成 `V2`、`V3`。此时 `V0→V1` 的快照属于 `kept` 基线，只有 `V1→V2→V3` 是 pending。Revert 验证当前为 `V3` 且尾段连续后恢复 `V1`，不跨过 Keep 去恢复 `V0`。若在 `V2→V3` 之间出现无法与快照链衔接的人工编辑，则这次尾段整体冲突，文件仍保持当前状态。



### 4.2 路径、移动和文件组

普通创建、修改、删除按 workspace 相对路径形成文件组。移动操作涉及源、目标两个路径，不能只用 `file_snapshots.path` 聚合。

- 从版本化快照的路径身份及受影响路径解析文件组；不能只按当前路径把记录连通，否则 `a.txt → b.txt` 后在 `a.txt` 新建另一文件会错误合并两个文件身份。
- 每次创建分配文件身份，原地修改沿用身份，移动将身份转到目标路径，删除终止该身份；删除后在同一路径新建属于新身份。关联的 MOVE 源、目标与连续移动链（如 `a.txt → b.txt → c.txt`）属于同一回退组，并按原操作序列处理。
- API 面向 UI 返回 `change_id`、`paths`（受影响相对路径）和可读的移动摘要。普通文件组的 `change_id` 由 file identity、处理边界和有序快照 ID 集合确定。递归删除组的 `change_id` 由 `atomic_group_id`、处理边界、完整成员 identity 集合及每个成员的有序 pending 快照 ID 集合共同确定；API 将整棵树返回为一个文件组，Keep/Revert 不接受成员级 ID。任一成员尾段变化时组 ID 必须变化。服务从不可变成员快照 ID 查终态：整个组全部 kept/reverted 后，旧请求重试分别返回 `already_kept` / `already_reverted`；不匹配当前或已完成集合时返回 `stale_change_id`，不能被重新解释成新的组。
- 路径始终由后端 `PathResolver` 约束在所属 workspace 内。前端只传服务端返回的 `change_id`，不把任意绝对路径作为回退目标。

### 4.3 快照格式与严格状态校验

`op_json` 使用版本化 envelope。正向操作类型沿用当前文件工具语义：`ADD`、`UPDATE`、`DELETE`、`MOVE`。回退不依赖预先序列化好的文本反向 patch，而是根据每个受影响路径的精确 before/after 状态构造恢复计划：

```json
{
  "schema_version": 2,
  "operation": { "type": "UPDATE", "operation_id": "mut_...", "tool": "patch_write" },
  "path_states": [
    {
      "file_identity": "fi_...",
      "path": "src/app.ts",
      "before": { "exists": true, "sha256": "...", "restore_ref": "sha256:..." },
      "after": { "exists": true, "sha256": "..." }
    }
  ],
  "display_patch": { "format": "unified", "text": "...", "truncated": false }
}
```

- `operation.type` 记录**正向**文件操作，取值为 `ADD` / `UPDATE` / `DELETE` / `MOVE`；`operation_id` 将同一工具调用涉及的快照关联到一次文件操作。数据库行仍以单个文件身份的一次操作为快照单位；MOVE 的源和目标保存在同一行的 `path_states` 中。普通批量操作可写入多条快照并共享 `operation_id`，但按文件身份独立报告结果。递归目录删除额外分配 `atomic_group_id`，把目录树所有成员作为一个整体 ChangeSet 文件组预检、提交和回退。
- 每个 `path_states[]` 项保存一个路径在操作前后的状态。普通文件记录 `entry_type: "file"` 和原始字节 SHA-256；需要回退恢复的 before 文件还记录 `restore_ref`。目录记录 `entry_type: "directory"` 与存在性；递归删除时每个目录项及文件项都单独列出，空目录也不能省略。符号链接记录 `entry_type: "symlink"`、原始链接目标、`target_is_directory`；状态 hash 覆盖 entry type、目标类型和链接目标字符串，before 的 `restore_ref` 保存目标字符串本身。恢复时按目标类型重建链接但绝不跟随读取目标。不存在状态写作 `{ "exists": false }`，不带 hash 或恢复引用。路径使用 workspace 相对路径。
- task 内 `seq` 只属于快照行。文件操作开始前按 task 锁预写隐藏的 per-path snapshot 行以占用序号；收口时复用这些行的 ID 和序号。外部 SQLite Session 写入快照时必须传入已预留的 `seq_start`，不在持有外部事务时等待序号锁。
- 工具输入名不决定快照操作类型，而由真实 before/after 决定：`write_file`、`replace`、`apply_patch` 对原本不存在的路径形成 `ADD`，对原本存在的路径形成 `UPDATE`；文件 `delete` 形成 `DELETE`。当前没有独立的 `move` 工具；`MOVE` 仅表示 `patch_write` V4A patch 内部携带的移动操作。快照中的逐操作 `display_patch` 只保留为操作记录，不作为 task 面板最终净 Diff 的来源，也不作为恢复材料。

#### ADD：新建文件

Agent 新建 `src/new.ts`，回退时应删除它。before 不存在，因此不需要保存 before-image；after 保存文件 hash，用于确认文件仍是 Agent 创建的内容。

```json
{
  "schema_version": 2,
  "operation": { "type": "ADD", "operation_id": "mut_add_1", "tool": "patch_write" },
  "path_states": [{
    "file_identity": "fi_new_1",
    "path": "src/new.ts",
    "before": { "exists": false },
    "after": { "exists": true, "entry_type": "file", "sha256": "hash(V1)" }
  }],
  "display_patch": { "format": "unified", "text": "...", "truncated": false }
}
```

回退前要求当前文件仍存在且 hash 等于 `hash(V1)`；匹配后删除。文件已不存在时可视为已到目标 before-state；若文件存在但 hash 不同，说明内容被外部修改，返回冲突，不删除。

#### UPDATE：修改已有文件

Agent 将 `src/app.ts` 从 `V0` 改为 `V1`。SQLite 保存前后 hash 和 before-image 的引用；blob 保存 `V0` 原始字节。

```json
{
  "schema_version": 2,
  "operation": { "type": "UPDATE", "operation_id": "mut_update_1", "tool": "replace" },
  "path_states": [{
    "file_identity": "fi_app_1",
    "path": "src/app.ts",
    "before": { "exists": true, "entry_type": "file", "sha256": "hash(V0)", "restore_ref": "sha256:blob-V0" },
    "after": { "exists": true, "entry_type": "file", "sha256": "hash(V1)" }
  }],
  "display_patch": { "format": "unified", "text": "...", "truncated": false }
}
```

回退前要求当前 hash 等于 `hash(V1)`；匹配后从 blob 原样恢复 `V0`。只要当前内容或字节（包括 BOM、换行、尾换行）不同，就返回冲突。

#### DELETE：删除文件

Agent 删除 `src/config.json`。before-image 保存在 blob；after 记录路径不存在。

```json
{
  "schema_version": 2,
  "operation": { "type": "DELETE", "operation_id": "mut_delete_1", "tool": "delete" },
  "path_states": [{
    "file_identity": "fi_config_1",
    "path": "src/config.json",
    "before": { "exists": true, "entry_type": "file", "sha256": "hash(V0)", "restore_ref": "sha256:blob-V0" },
    "after": { "exists": false }
  }],
  "display_patch": { "format": "unified", "text": "...", "truncated": false }
}
```

回退前要求该路径仍不存在；匹配后从 blob 原样重建文件。若文件已被人重新创建，无论内容是否相同，都不覆盖，返回冲突。若 Agent 在基线后新建文件又删除，整段 pending 历史最终回退到“不存在”，不重建该文件。

#### MOVE：`patch_write` 内部移动操作（不是独立工具）

Agent 将 `src/old.ts` 移到原本不存在的 `src/new.ts`。一个文件身份、一个快照行，同时记录源路径和目标路径的状态；before-image 用于恢复原路径内容。

```json
{
  "schema_version": 2,
  "operation": {
    "type": "MOVE", "operation_id": "mut_move_1", "tool": "patch_write",
    "source_path": "src/old.ts", "destination_path": "src/new.ts"
  },
  "path_states": [
    {
      "file_identity": "fi_old_1",
      "path": "src/old.ts",
      "before": { "exists": true, "entry_type": "file", "sha256": "hash(V0)", "restore_ref": "sha256:blob-V0" },
      "after": { "exists": false }
    },
    {
      "file_identity": "fi_old_1",
      "path": "src/new.ts",
      "before": { "exists": false },
      "after": { "exists": true, "entry_type": "file", "sha256": "hash(V0)" }
    }
  ],
  "display_patch": { "format": "rename", "text": "src/old.ts → src/new.ts", "truncated": false }
}
```

回退前同时验证源仍不存在、目标仍存在且 hash 匹配。匹配后移除目标并从 before-image 恢复源路径；如果源被重新创建、目标内容被改写或目标消失但源状态也不符合 before-state，整组冲突，不执行半条 MOVE。当前 `patch_write` 要求 MOVE 目标原先不存在；若未来支持覆盖已有目标，目标旧文件必须作为另一文件身份和恢复状态一并纳入同一 ChangeSet 操作组。

#### 同一文件的多条操作：逆序验证整条链

同一 `file_identity` 在最近 Keep/Revert 边界之后的快照按 `seq` 递增组成一条链。回退从最新快照开始逆序模拟：当前磁盘状态必须匹配该步 `after`，该步的 `before` 必须与前一快照的 `after` 连续相接。整条链验证通过后才写文件；最终直接恢复到最早一条快照的 `before`，不逐条落盘中间版本。

例如已有文件连续被 Agent 修改两次：

| seq | 操作 | before | after | before-image |
| --- | --- | --- | --- | --- |
| 10 | UPDATE | `hash(H0)` | `hash(H1)` | blob 保存 `H0` |
| 11 | UPDATE | `hash(H1)` | `hash(H2)` | blob 保存 `H1` |

当前文件必须是 `H2`。服务逆序验证 `H2 → H1 → H0`，确认两条记录连接后，从 seq 10 的 before-image 一次性恢复 `H0`，并将两条快照一起标记 `reverted`。若当前 hash 不是 `H2`，或 seq 11 的 before 与 seq 10 的 after 不同，整个文件组返回冲突，文件不写、快照状态不变。

再如文件在基线时不存在，Agent 新增、修改后又删除：

| seq | 操作 | before | after | before-image |
| --- | --- | --- | --- | --- |
| 20 | ADD | 不存在 | `hash(V1)` | 无，before 是不存在 |
| 21 | UPDATE | `hash(V1)` | `hash(V2)` | blob 保存 `V1` |
| 22 | DELETE | `hash(V2)` | 不存在 | blob 保存 `V2` |

当前不存在时，逆序链为“不存在 → `V2` → `V1` → 不存在”。链完整后，最终状态仍是不创建该文件，并把三条快照一起标记 `reverted`。如果删除后有人重新创建了同路径文件，当前状态不再匹配 seq 22 的 after（不存在），返回冲突，不覆盖人工文件。

人工修改也可能发生在两次 Agent 操作之间。例如 seq 10 的 after 是 `H1`，用户随后改成 `HM`，Agent 再从 `HM` 改到 `H2`。seq 11 的 before 会记录 `HM`，与 seq 10 的 after 不一致；即使当前文件正好是 `H2`，回退预检仍会发现链断开并拒绝整组回退。

链连续性按 **file identity 的逐路径状态投影**检查，不要求相邻快照的 `path_states` 路径集合完全相同。每条快照只改变自己列出的路径；未列出的路径状态沿该 identity 的模拟状态延续。MOVE 把 identity 从源路径转移到目标路径，因此后续 UPDATE 只列目标路径时，UPDATE 的 before 必须匹配前一 MOVE 后目标路径的 after；源路径仍保持不存在。路径被另一个新 identity 复用时，不与旧 identity 的历史链相连。

- `delete` 工具也可删除目录。空目录删除的快照记录目录路径 before 存在、after 不存在；回退时只在该路径仍不存在时重建空目录。递归删除必须在 mutation 开始前枚举并持久化整棵树的成员清单，记录每个文件、目录和符号链接的路径状态；文件保存原始字节 blob，目录保存类型与存在状态，符号链接保存目标字符串 blob 和 `target_is_directory`。它们共享 `atomic_group_id`，作为一个 ChangeSet 文件组禁止部分回退。回退前先验证整棵树的 after-state；任何路径已被重建或新增内容导致目录重新出现时整组冲突。恢复时先创建父目录，再还原文件与符号链接。各路径文件系统写入不具备跨路径原子性，预写快照负责启动后按实际磁盘状态收口；junction 或其他不能无损读取、验证和重建的 reparse point 在删除前拒绝，不创建不可回退快照。

- 状态指纹必须来自文件变更执行边界捕获的精确 before/after 状态，不能从 UI Diff 或可能已规范化换行的字符串反推。`path_states` 对每个受影响路径保存 file identity 及 before/after 状态；MOVE 至少包含源、目标两项。按原始字节计算 SHA-256，并保留 BOM、换行和尾换行差异。反向恢复依据持久化的精确 before-image，不依赖会改变行尾或编码的文本重写。
- 新建文件所需的隐式父目录也属于文件系统副作用：Agent 写入的预写快照记录原本不存在的父目录，handler 在内存中校验目录状态及可用的目录 identity；崩溃后按实际状态收口，不自动移除目录。快照 envelope 用 `side_effect_states` 记录该文件组创建的父目录基线、最终状态和目录 identity，但它们不创建额外 file identity，也不增加 UI 文件行。Revert 将这些目录纳入基线校验，并在调用目录删除前再次核对 identity。最后一次校验发现目录已替换时回退冲突；目录仍被其他文件使用时，Revert 先回退当前文件并保留该目录，后续组回退时若目录已空且 identity 未变，再移除目录。非空目录中的外部内容始终保留。复验与按路径删除之间仍有操作系统接口无法消除的极窄竞态，见第 11 节。
- `restore_ref` 指向本机 `storage/change_set/blobs/` 中按 SHA-256 寻址的 before-image；pending 快照保留引用，成功 Keep/Revert 后清空已收口行不再需要的引用。GC 在 `BEGIN IMMEDIATE` 事务中读取 pending 快照及预写快照清单中的引用，再删除零引用对象，避免检查与删除之间插入新引用。对象先写入独立 `storage/change_set/staging/`；启动对账后只清理未被预写快照引用的 staging。最终净 Diff 由后端投影返回，不把完整文件副本写进 SQLite。
- 回退前对当前磁盘状态与最新 Agent after-state 做精确校验；已处于目标 before-state 时视为幂等完成；两者都不匹配则为冲突。
- 对同一文件多次修改，先从当前状态按快照逆序模拟整个 pending 尾段。每一步都必须匹配该步的 after-state；中间任一步不匹配，整个文件组在写入前失败。
- Task ChangeSet 面板只展示相对当前 pending 变更组基线的最终净 Diff，不展示每次操作的 `display_patch`。查询由后端验证整条 pending 链，并在文件路径状态锁内比较基线 before-image 与当前文件状态；只有当前 hash 匹配链尾 after-state 且整条链连续时才生成净 Diff。中间链断裂或链尾不匹配时不把当前磁盘内容伪装成 Agent Diff，返回冲突/不可验证状态并隐藏净 Diff。Diff 复用 `file_change_display`，设置大小预算与 `truncated` 标记；不在前端重建、不把完整文件副本存入 SQLite。
- 快照若缺少精确 before/after 状态指纹或字节级恢复材料，相关文件组返回 `snapshot_unverifiable`，不做猜测性覆盖。
- 最终净 Diff 的生成沿用现有 `file_change_display` 投影和 Diff 基础设施，不在前端重造 diff parser；回退依据仍是持久 before-image 和状态指纹，而不是 UI Diff。

## 5. 回退用例与部分成功模型

服务层以文件组为最小成功/失败单元；API 层负责收集结果，不承载文件状态算法。

```text
验证 task / workspace 与 change_id
        ↓
取得 task operation 闸门；确认无 active Run
        ↓
取得受影响 workspace 路径锁
        ↓
读取最近 Keep/Revert 边界之后的全部已收口快照
        ↓
读取磁盘状态，预检完整历史链
   ┌────┴──────────────────┐
不匹配 / 无法验证           全部匹配
   ↓                         ↓
返回 conflict              在 file_snapshots 标记 reverting
不改文件、不改状态            ↓
                         写入目标 before 状态
                         ┌────┴─────────┐
                      成功             中断/崩溃
                       ↓                  ↓
                标记 reverted      按当前磁盘状态对账
```

- 在同 task 内复用已有 `TaskRuntimeSpace` 操作闸门，避免与 Agent Run、编辑或 resume 并发。闸门繁忙时返回明确的 `operation_busy`，不等待到 UI 失去操作上下文。
- 文件工具与 ChangeSet Revert 共用 workspace/path 锁。递归删除在枚举前取得目录子树锁，并持有到快照收口；实际删除前重新枚举并比对预写清单，锁外新增或变化会中止删除。普通文件操作只锁自身路径及必要的目录项。
- 文件修改前，服务把完整 before-image 放入 staging，并在 `file_snapshots` 中写入隐藏的 `prepared` 行及操作清单；提交后发布 blob，再允许 handler 修改 workspace。handler 的窄 guard 校验已捕获路径及其运行时状态，不写第二份持久日志。
- handler 完成或返回失败后，服务读取实际文件状态，并用原预留的快照行记录真实 before/after。没有实际变化时删除预写行。服务异常时保留预写行供启动对账；查询只返回 `mutation_state=applied` 的收口行。
- Revert 开始前，在参与的快照行上持久化 `reverting` 标记。成功时同一 SQLite 事务将快照设为 `reverted` 并清除标记。若操作被中断，启动比较磁盘与每条快照的 baseline：已到 baseline 的行标记 `reverted`；未到 baseline 的行把实际磁盘状态记为新的 after 并保留 `pending`。任何启动对账都不写 workspace 文件。
- 文件系统和 SQLite 不共享事务，因此多路径 Revert 可能只完成一部分。该状态会按已落盘结果拆分为已回退和仍待处理项，剩余项可再次操作；冲突项不会被自动覆盖。
- 不同文件组独立处理。一个组失败不撤销其他已成功的组，也不阻止继续处理其他组。MOVE 和递归删除仍按 ChangeSet 文件组预检及显示，但文件操作本身不被描述为跨路径原子事务。
- 外部编辑器不遵守后端锁。服务在文件恢复提交前再次读取并验证状态指纹；检测到状态变化时拒绝覆盖。复验和文件系统修改之间仍有极窄竞态，按第 11 节的限制处理。

## 6. HTTP API

继续使用已有 task 级路由，不增加 Assistant Transport 命令。

### 查询

`GET /tasks/{task_id}/changes`

响应返回 task_id 和已收口的 pending 文件组：

```json
{
  "task_id": 42,
  "files": [
    {
      "change_id": "chg_...",
      "paths": ["apps/backend/app.py"],
      "action": "modified",
      "status": "pending",
      "last_run_id": 77,
      "operation_count": 2,
      "net_diff": {
        "state": "verified",
        "additions": 7,
        "deletions": 3,
        "patch": "...",
        "truncated": false,
        "has_unrendered_changes": false
      }
    }
  ]
}
```

`net_diff` 是最近一次 Keep 基线到当前 Agent 最终状态的一份聚合 Diff，不是逐操作 Diff 的拼接或 additions/deletions 求和。`operation_count` 按不同 operation id 计数（一个递归删除覆盖多条路径仍计作一次操作）；`last_run_id` 仅作摘要，task 面板不返回每步操作 patch。`has_unrendered_changes=true` 表示文件组相对基线确有变化，但变化里至少有一部分无法用文本行 Diff 表达（如二进制内容、目录、符号链接、空文件或纯移动）；此时 UI 应说明有文件变化，不能显示“与基线无差异”。ADD→UPDATE→DELETE 等净状态等于基线的历史则标记 `false`，展示“与基线无差异”。整条 pending 链或链尾状态未通过校验时，`net_diff.state` 返回 `conflict` / `unverifiable` 且不附 patch。净 Diff 超出预算时用明确的 `truncated` 标记，不截成看似完整的补丁。
预写中或正在 Revert 的快照保持隐藏；启动对账成功后才重新进入 ChangeSet 查询。

### Keep

`POST /tasks/{task_id}/changes/keep`

请求统一使用 `{ "change_ids": ["chg_..."] }`；单项也是单元素数组。服务将当前 pending 尾段在一个 DB 事务内收口为 kept，不触碰 workspace 文件。响应逐项返回 `kept` / `already_kept` / `stale_change_id` / `failed` 结果，单项失败不隐去其他结果。

### Revert

`POST /tasks/{task_id}/changes/revert`

请求统一使用 `{ "change_ids": ["chg_..."] }`；单项或批量都通过该数组表达。对每个 change id 独立执行回退，返回 HTTP 200 和逐项结果；请求级参数错误仍返回 4xx，task 闸门繁忙返回 409。

```json
{
  "task_id": 42,
  "results": [
    { "change_id": "chg_a", "outcome": "reverted" },
    {
      "change_id": "chg_b",
      "outcome": "conflict",
      "reason_code": "manual_change_conflict",
      "message": "文件在 Agent 修改后发生变化，本次未回退"
    }
  ],
  "change_set": { "task_id": 42, "files": [] }
}
```

`outcome` 至少支持 `reverted`、`already_reverted`、`stale_change_id`、`conflict`、`snapshot_unverifiable` 和 `failed`。`reverted` 表示整组已恢复到基线，不表示只撤销最后一条工具操作。逐项消息使用受控文本；不返回磁盘绝对路径、堆栈或未经筛选的异常正文。每次响应附上操作后的完整 ChangeSet，前端以该响应更新 canonical domain view。

## 7. 前端接线与 UI

### 7.1 模块边界

```text
apps/desktop/lib/api/changes.ts
  └─ ChangeSet DTO、list/keep/revert API；通过 lib/http/client.ts

apps/desktop/components/task-changes/
  ├─ task-scoped loader/action state
  ├─ changes panel and file-group rows
  └─ baseline-to-current final Diff rendering

apps/desktop/components/task-page.tsx / workspace-shell.tsx
  └─ 提供 taskId、workspace/run 状态并装配面板
```

- taskId 切换时丢弃旧请求结果，查询使用 `AbortSignal` 和 generation 防止旧 task 响应覆盖新 task。
- 在 task 打开时加载；Run 进入终态后刷新；Keep/Revert 使用 mutation 响应中的 ChangeSet 更新状态。初版不轮询，不新增 SSE 事件。
- active Run 期间仍可查看已有变更和当前工具调用的 Diff，但禁止 Keep/Revert。Run 完成或取消后刷新面板。
- 展示数据从 ChangeSet domain API 来；工具调用自己的 Diff 仍由既有 `ToolPart`/`DiffTool` 渲染。可以在工具行提供“在文件变更中查看”导航，但不能把回退能力实现成模型工具。

### 7.2 推荐草图

```text
┌──────────────┬────────────────────────┬──────────────────────────────┐
│ 工作区 / 任务 │ Assistant 对话          │ 文件变更          2 个待处理 │
│              │                         │ [撤销全部待处理] [刷新]        │
│ 当前任务     │ 修改了 2 个文件          │                              │
│              │ [在文件变更中查看]       │ ▾ apps/backend/app.py         │
│              │                         │   待处理 · 净变化 +12  −4     │
│              │                         │   最终 Diff：基线 → 当前       │
│              │                         │   [保留当前状态] [回退到基线]   │
│              │                         │                              │
│              │                         │ ▸ apps/desktop/app.tsx 冲突   │
│              │                         │   文件已发生变化，本次未回退   │
└──────────────┴────────────────────────┴──────────────────────────────┘
```

- “文件变更”放在 task 会话内的右侧面板或等价 task-scoped 工作区区域，入口显示待处理文件数。不能放在 workspace 全局，因为 ChangeSet 的事实和回退边界属于 task。
- 第一阶段始终显示覆盖范围提示：“当前追踪文件工具修改；终端命令可能产生未记录的文件变更。”提示不依赖检测某次 Run 是否调用终端；终端文件写入没有接入 ChangeSet 捕获范围，不能因列表为空就暗示 task 没有其他 Agent 修改。
- 一条文件组对应一行，无论该文件在一个或多个 Run 中被 Agent 操作多少次。行内显示路径/移动路径、处理状态、真实操作次数，以及**最近 Keep 基线到当前最终状态**的一份净 Diff。多路径目录组显示根路径和路径数；只有移动组将源和目标用箭头连接。多次操作的 patch 不逐条展示，净 additions/deletions 也不对每步统计求和。
- 只有当前文件状态匹配最新 after-state 且整条 pending 链验证通过时才显示最终 Diff。若链尾状态不匹配或中间链断开，隐藏净 Diff 并显示冲突提示；不把可能包含人工修改的当前磁盘内容标作 Agent Diff。
- `保留当前状态` 将当前状态设为新基线；`回退到基线` 统一撤销该文件组最近处理边界之后的全部操作。没有单步/逐操作回退按钮。批量 `撤销全部待处理` 先确认待处理文件组数，执行后显示每组结果。
- 若链首和链尾相同，例如基线不存在、Agent 新建后修改并删除，显示“新增后已删除 · 3 次操作 · 当前不存在 · 净变化 0”，Diff 区显示“与基线无差异”。该行仍保留以便用户收口 pending 状态；回退只标记整段历史 reverted，不写 workspace 文件。链不连续或当前状态冲突时显示“本次未回退”，不提供 force overwrite。
- 多次修改时，展开行只显示最终净 Diff：

  ```text
  ▾ src/app.ts   待处理 · 修改 3 次
    最终 Diff（基线 → 当前）：+4 −2
    <一份聚合后的 unified diff>
    [保留当前状态] [回退到基线]
  ```

- `operation_busy` 显示“任务仍在运行，请完成后再操作”；`snapshot_unverifiable` 说明旧快照不足以安全验证，不假装成普通人工冲突。
- - 保持 loading、空状态、加载失败和部分成功状态可辨认。切换 task 后不复用前一个 task 的面板数据。

### 7.3 assistant-ui 官方文档对照

现有 `Thread` 使用 `MessagePrimitive.GroupedParts` 把 routine tool calls 收入工具 trace，并通过 `presentation.surface` 决定独立展示。ChangeSet 面板是持久的 task 级操作界面，不是某一条模型工具调用的结果，所以应由 task 页面装配，不注册为可被模型调用的工具。

如果保留工具消息上的导航链接或调整工具行位置，沿用项目现有 `ToolPart` 和 `GroupedParts` 接线，并参考 assistant-ui 官方 [Tool UI](https://assistant-ui.com/docs/tools/tool-ui)、[Message Part Grouping](https://assistant-ui.com/docs/guides/part-grouping) 与 [Tool call element](https://assistant-ui.com/elements/tool-call)。实现时对照当前 `@assistant-ui/react` 版本 `^0.15.17`，不为 ChangeSet 引入另一套工具 schema 或 Transport。

## 8. 错误处理、启动对账与日志

- 对外返回稳定 `reason_code`：人工改动、快照无法验证、任务仍运行、文件读写失败、文件组操作失败。前端文案映射稳定 code，不展示 Python 异常内容。
- 后端记录结构化事件，例如 `file_mutation_snapshot_prepare_failed`、`file_snapshot_reconcile_incomplete`、`task_change_set_revert_interrupted`，字段包括 `task_id`、`run_id`、`change_id` 和受控失败类别。不记录 Diff 正文、文件全文或凭据。
- 写入前采集、before-image 暂存或预写快照持久化失败时，文件工具不得执行 workspace 写入；Agent Run 可继续执行其他步骤。
- 后端启动先将遗留 active Run 收敛为 cancelled，再扫描 `prepared` / `reverting` 快照行。Agent 操作按当前磁盘状态生成收口快照；未产生变化则删除预写行。Revert 根据当前磁盘状态标记已到 baseline 的快照或更新未完成路径的 after-state。启动对账不执行恢复性文件写入；无法读取的记录保留隐藏状态，供下次启动重试并阻止路径重叠写入。
- Run 到 success/failed/cancelled 任一终态都完成快照 lifecycle 收口；恢复后遗留 active Run 按项目既有语义标记 cancelled。不要因 `stable=0` 永久隐藏已终态 Run 的有效操作。
- 文件已写入而 snapshot 收口失败时保留 `prepared` 行，后端启动后据实对账；不能重放 Agent Run，也不能把实际文件恢复到旧版本。
- SQLite 不保存第二套 ChangeSet 状态镜像；`file_snapshots` 和工作区的实际状态共同驱动领域查询，UI 状态只是缓存。

## 9. 实施切片

### Slice A：后端语义与安全写入

1. 引入 `FileMutationService`、before-image 暂存与 `file_snapshots` 预写行；只有持久化 prepared snapshot 后才改 workspace 文件，启动时按实际状态对账。
2. 定义文件组、Keep/Revert 决策边界和状态迁移；为同一路径多次修改实现完整 pending 尾段回退。
3. 引入可读旧记录的版本化快照元数据，包含 after-state 指纹、文件身份和精确 before-image；对无法精确验证/恢复的旧记录采取保守拒绝。
4. 为成功和部分失败的结构化文件工具按实际 before/after 落盘快照；保证预写快照序号与工具调用顺序一致。另评估终端写入如何捕获或限制，解决前明确列为能力边界。
5. 完整链预检、提交前指纹复验、可恢复的文件组提交、CAS 更新处理状态。
6. task operation 闸门与 workspace/path 写锁协调；active Run 时拒绝写操作。
7. Run 所有终态的快照 lifecycle 收口和旧记录行为。

### Slice B：查询与逐文件批量结果

1. ChangeSet 查询返回按路径/移动链聚合的文件组、操作次数及最近基线到当前已验证状态的一份最终净 Diff。
2. Keep 将 pending 尾段统一标记为 kept。
3. Revert API 逐文件组收集结果；失败不会丢失其他文件的成功信息；响应附完整 ChangeSet。
4. 请求和响应 schema、OpenAPI 与服务错误映射保持一致。

### Slice C：桌面 UI

1. 增加 `lib/api/changes.ts` 和 task-scoped loader/mutation 状态。
2. 在 TaskPage/workspace shell 提供 task 级“文件变更”入口、待处理计数和侧面板。
3. 展示单文件/移动组最终净 Diff、统一回退到基线、Keep、批量回退确认和逐文件组结果。
4. 运行期间禁用写操作；Run 终态和 mutation 完成后刷新；继续使用现有 tool Diff renderer。

## 10. 验收标准

### 后端

- 同一文件在一个或多个 Run 中连续修改后，Revert 回到最近一次 Keep/Revert 边界的精确文件状态。
- 结构化文件工具在任何 workspace 写入前都先持久化隐藏的 prepared snapshot 和 before-image；准备失败时不修改文件。
- prepared/reverting snapshot 不出现在 ChangeSet 查询；文件操作结束后只按实际落盘状态收口已应用快照，未发生变化时删除预写行。
- Agent 写入后进程崩溃，重启对账保留当时磁盘状态并创建对应 ChangeSet，不将文件恢复到 before-image；外部编辑同样按磁盘实况记录。
- Revert 中断后重启不恢复文件：到达 baseline 的行标记 reverted，未到达的行更新 after-state 并保留 pending，可再次 Revert。
- 同 task 同路径并发工具写入按共享路径锁串行；不同路径并行时预写快照序号唯一。
- 多路径 MOVE 与递归删除按真实前后状态成组审阅；recursive delete 在实际修改前复验完整树清单，外部新增内容不得被误删。
- Keep/Revert 清理不再需要的 before-image 引用；其他 pending 或 prepared snapshot 仍引用的 blob 不得被 GC 删除，staging 与对象 GC 隔离。
- 多文件 Revert 中一个文件冲突时，其他文件仍可成功；冲突路径不得被写入或标记 reverted。
- active Run 时 Keep/Revert 被后端拒绝且无文件/数据库副作用；Run 终态及崩溃收敛为 cancelled 后，已收口快照可正常查询。
- 日志使用 `task_id`、`run_id` 和 `change_id` 关联，不记录文件正文或 Diff 正文。

### 前端

- task 打开、Run 终态、Keep/Revert 后显示最新 ChangeSet；task 切换时旧响应不污染新面板。
- 待处理数按文件组统计，不重复计算同一文件多次工具操作。
- active Run 禁用操作；状态忙碌/快照不可验证/人工冲突文案互相区分。
- 批量操作即使部分失败，也能同时显示成功文件和失败文件，且冲突文件继续显示为待处理。
- ChangeSet UI 使用本机领域 API；不读写 Assistant Transport state、SQLite 或 workspace 文件。
- 工具调用 Diff 继续按既有只读工具 UI 展示，unknown tool/data fallback 不受影响。

### 验证边界

后端定向验收：ChangeSet service 与 API contract 45 passed、1 skipped。前端 ChangeSet API/panel Vitest 13 passed；TypeScript `tsc --noEmit` 与后端 Ruff 检查通过。当前定向用例覆盖快照预写与启动对账、partial write、Revert 中断、before-image GC、同路径/不同路径并发、递归删除、MOVE、Keep/Revert 和 ChangeSet 面板契约。Windows 当前测试账户没有创建符号链接的权限，真实 symlink 集成用例跳过；字段编码与恢复调用通过隔离测试验证。现有 Playwright E2E 使用 Vite 与独立测试服务，不覆盖真实 Tauri IPC、动态端口或 BackendSupervisor 生命周期。

## 11. 主要风险与明确限制

1. **采集范围**：ChangeSet 仅覆盖结构化文件编辑工具；`execute_terminal` 可能写入 workspace，但按需求不纳入采集和回退范围，UI 应清楚披露此范围。
2. **快照信息不足**：缺少精确状态指纹或字节级恢复材料时不能安全验证。方案保守拒绝不完整快照，而不覆盖用户修改。
3. **文件系统与 SQLite 不共享事务**：预写快照可在启动后据实记录已落盘状态，但不提供跨多路径原子可见性或自动文件恢复。Revert 中断后可能保留部分已回退结果，未到 baseline 的路径继续显示为 pending。
4. **外部编辑器不遵守进程内锁**：普通文件恢复会在内容暂存完成后、原子替换前再校验一次状态；删除、目录和符号链接恢复也会在修改目录项前复验。它们都能拒绝大部分已发生的并发人工修改，但复验与替换/删除/创建之间仍有极窄竞态。跨平台普通文件系统接口不提供按内容条件替换或删除的原语，因此保证是尽最大可能检测并保护外部修改，不能承诺与不遵守进程内锁的编辑器实现严格原子互斥。
