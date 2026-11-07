这是 64k 行的后端。我会并行派出多个只读调查方向，同时自己深读核心链路。
现在我自己核验关键论断，先看调度器与消息队列。
审查完成。我按「内核链路 → 传输 → 工具 → 生命周期」读了全部 6 万行，并对每条结论做了代码复核。下面是**已核实**的逻辑缺陷，按严重度排列。

---

## P0：会让 Task 永久卡死或状态错乱

### 1. `TaskActor._loop` 的阻塞读在异常保护之外，且整个 loop 无人监督

```155:171:apps/backend/app/core/task_actor.py
        while True:
            pending = await self._queue.pending(self.task_id, self._watermark)   # 在 try 之外
            fresh = [message for message in pending if cast(int, message.id) > self._watermark]
            self._watermark = max(cast(int, message.id) for message in pending)
            ...
            try:
                await self._advance(pending, fresh)
            except Exception:
                log.exception("task_actor_advance_failed", ...)   # 只保住 _advance
```

`pending()` 内部要 `TaskMessage.of_model`（`task_message.py:104-106`，未知 `kind` 直接抛），DB 抖动同样抛。异常冲出 `_loop` 后：

- `create_task(self._loop())`（`task_actor.py:118-122`）没有 done_callback，异常被 asyncio 静默吞掉（只在 GC 时打 "Task exception was never retrieved"）；
- `start()` 里 `if self._loop_task is None` 永不重建，该 Task 从此不再调度任何 Run，**只有重启进程能恢复**；
- 更严重的是 `child_run_job.py:121` 用 `wait_quiescent()`（**无超时**）等子 Agent，`_wait_for` 在 `deadline is None` 时 `await changed.wait()` 无限等（`task_actor.py:348-350`）。loop 死了就没人置位 `state_changed` → **父 Agent 的工具调用永久挂起**，用户只能手动停止。

置信度：高。这条同时解释了「日志里只看到一条 advance_failed，之后这个 Task 再也不动了」这类现象。

### 2. `_advance` 失败后水位与 `_handled` 一起前移：失败的消息不会被重试

```156:171:apps/backend/app/core/task_actor.py
            self._watermark = max(cast(int, message.id) for message in pending)   # 先移水位
            ...
            except Exception:
                # 单轮失败只记录，不杀死 loop：消息仍在外部队列，下一次推进重读事实源。
                log.exception("task_actor_advance_failed", ...)
            self._handled = self._watermark      # 失败也标记为已推进
```

`pending()` 只在存在 `id > after_id` 的行时才返回（`task_message_queue.py:84`）。水位已前移、`_advance` 又失败，**没有任何「下一次推进」**——那条消息要等到用户再发一条新消息才会被一起重读。注释承诺的「下一次推进重读事实源」不成立，日志主动误导排查。

附带后果：`_quiescent` 用 `message.id > self._handled` 判断（`task_actor.py:332-335`），失败已把 `_handled` 推过去；若上一条 Run 是 `cancelled`/`failed`（`continues` 为 False），`wait_quiescent` 会**在还有未推进的唤醒消息时返回 True**，委派方会把上一轮的旧 Run 当成本次结论读走。

置信度：高（不重试）/ 中（谎报静止，取决于上一条 Run 的终态）。

### 3. `TaskExecution._drive` 缺 `try/finally`：取消信号残留会毒化下一次 Run

```119:133:apps/backend/app/core/task_execution.py
    async def _drive(self, run: ConversationRunRecord) -> None:
        await RunDriver().drive(run)                     # 可能抛：settle 自身失败会 re-raise
        if cancellation_registry.is_cancelled(self.task_id):
            await self._cancellation.cancel_background_task()
        self._task = self._run = None
        reason = cancellation_registry.consume(self.task_id)   # 唯一的信号清理点
        await self._on_released(run, reason)
```

`RunDriver._settle_failure` 在落库自身失败时**重新抛出**（`run_driver.py:121-134`，注释也承认「Run 仍停在 running」）；构造 `RunDriver()` 也可能抛。此时：

- `_task` / `_run` 不清空、`consume` 不执行 → **取消信号留在 registry 里，下一次 Run 一启动就在第一个检查点被取消**——正是模块 docstring `task_execution.py:6-9` 明确要避免的情形；
- `_on_released` 不执行 → 剩余排队消息不接续、状态不发布；
- 后台 task 异常无人消费（`task_execution.py:78` 没有 done_callback）。

置信度：高。

### 4. `Subscriber.offer` 在 control 帧挂起期间**丢弃比 full 帧更新的 mutation**

```45:55:apps/backend/app/assistant_transport/stream/subscriber.py
        if frame.kind != "mutation":
            # full/control 帧是一个新的投递边界。排队的 mutation 产生于它之前，不能在 full
            # 状态之后被重放。
            self._mutation_queue.clear()
            self._control_frame = frame
            self._wake.set()
            return
        if self._control_frame is not None:
            return                                    # ← 更新的 mutation 被吞
```

注释的理由只对「已在队列里、产生于 full 之前的 mutation」成立。full 帧从被 `call_soon_threadsafe` 投递到被消费之间还有一段时间，这段时间到达的 mutation **产生于 full 之后**，却被无条件丢弃且无日志、无 `resync_required`。客户端此后永久缺这一条变更，直到下一次 full 或重连。

置信度：高（代码路径确定）。

### 5. `rerun` 的重建发布绕过了「Run 在跑不卸载」这条自己写下的规则

```178:209:apps/backend/app/assistant_transport/service/transport_assistant_service.py
        space = await asyncio.to_thread(task_runtime_spaces.get_or_create, task_id)
        async with space.async_operation():
            ...
            await asyncio.to_thread(self._replace_latest_input, task_id, message_id, message)
        await asyncio.to_thread(self._republish, task_id)      # 出闸门、且晚于入队才重建
```

`_replace_latest_input` 内部 `enqueue` 提交就会唤醒 actor 开新 Run；随后 `_republish → rebuild_state` **无条件** `unload_snapshot()`（`conversation_task_state_service.py:304-308`）并从库重建。而 `_unload_if_unwatched` 的注释明确写着：

```351:354:apps/backend/app/assistant_transport/service/conversation_task_state_service.py
        工作副本只是投影缓存：没人看、也没有流式输出在往里写时留着它只占内存……
        Run 还在运行时不卸载：流式正文没落库，重建会丢掉正在输出的内容。
```

`_republish` 走的是另一条路径，绕过了这个判断。窗口内已流式输出但未落库的正文会从 working copy 消失，新 Run 的增量也可能被随后的 full 帧覆盖。

置信度：中高（机制确定；实际可见度取决于时序）。

---

## P1：功能性错误

### 6. `.fileignore` 被自动创建为空 → 内置默认忽略目录**永不生效**（已决策：删除默认集合，不算缺陷）

```130:146:apps/backend/app/tools/tool_handler/search/ignore_rules.py
def render_default_content() -> str:
    ...
    return ""            # workspace 默认不额外忽略任何路径
```

```186:192:apps/backend/app/tools/tool_handler/search/ignore_rules.py
    text = _read_ignore_text(path)      # 文件缺失 → 按上面的空串创建
    ...
    if not lines:
        # 缺失文件会被创建为空文件；空文件和显式空文件都表示 workspace 不额外忽略路径。
        return _empty_matcher(root)
```

`load_search_ignore_rules` 只组合 `.fileignore` + `.gitignore`（`:227-231`），**没有叠加** `default_ignore_rules()`；`default_ignore_rules` 只在 `rules is None` 时才有机会（`file_walker.py:55`），而 `SearchScope.iter_files` 永远显式传入组合规则（`scope.py:91-96`）。

后果：在没有 `.gitignore` 的 workspace（或 `.gitignore` 不含 `node_modules` 的仓），每次 `search_content` / `find_files` 都会递归遍历 `.git/objects`、`node_modules`、`dist`——`DEFAULT_IGNORED_DIR_NAMES` 成为死代码，5 秒软超时后返回 `SearchTimedOut`，模型一页结果都拿不到。

**决策（已实现）**：忽略策略只由 workspace 的 `.fileignore` + `.gitignore` 决定，内置默认集合删除。理由：`.fileignore` 首次创建即为空、显式空文件也表达「不忽略」，默认集合在任何路径上都到不了；保留它只会让「搜索跳过了什么」有两个事实源，用户也无法通过改配置看到它。空文件、缺失、读取失败、解析失败一律等于「不额外忽略任何路径」，并保留 WARNING 日志。`iter_files` 的 `rules` 参数改为必填，避免调用方漏注入时静默遍历全盘。

### 7. 模型调用未注册的工具时，调用被静默丢弃，Run 仍以「正常完成」收尾

```51:56:apps/backend/app/core/llm_provider/message_codec.py
    for call in message.tool_calls or []:
        call_id = call.id or ""
        function = getattr(call, "function", None)
        tool_name = getattr(function, "name", None) or ""
        if not call_id.strip() or tool_name not in registered_tool_names:
            continue
```

```155:169:apps/backend/app/core/workflow/nodes/model_node.py
    calls = [...]                       # 被过滤后为空
    if not calls and finish_reason is None:      # finish_reason 是 "tool_calls"，跳过
        ...
    # 落到最终回复分支
```

工具名未注册（MCP 热注销、工具组变更、模型幻觉）时，`calls` 为空但 `finish_reason == "tool_calls"`，于是跳过「无完成原因 → 续写」，进入 `model_node.py:208-233` 的最终回复分支，以 `COMPLETE` 落定、`final_output` 通常为空串。模型永远收不到 unknown tool 的观察，用户看到一条空回复，失败被彻底抹平。`ToolAccessGate` 里的 `unknown tool` 拒绝文案在这条路径上不可达。

置信度：高。

### 8. 并行执行路径丢失 HITL `decision`：用户批准被静默吞掉

```62:68:apps/backend/app/core/workflow/node_helper/tool_batch_runner.py
    for index, call in serial:
        observations[index] = await asyncio.to_thread(
            _execute_call, rc, call, step_id, batch, loop, decision      # 串行带 decision
        )
    if parallel:
        results = await _run_parallel(rc, [call for _, call in parallel], step_id, batch, loop)
```

`_run_parallel → _execute_call` 不传 `decision`（默认 `None`），于是 HITL 工具会走「首次调用」分支，重新登记待决并再写一条 waiting 占位——用户决定既没生效也没报错。`_reject_mixed_decision_batch` 只拦「HITL + 其他调用同批」，单个 `parallel_mode == "parallel"` 的 HITL 工具会直接命中这条路径。当前 `agent_team_run` 是 serial（延迟暴露），任何新增并行 HITL 工具即触发。

置信度：高（代码事实）。

### 9. 关闭阶段无逐步隔离：任一步抛异常，后面全部不执行，bootstate 停在 `ready`

```223:242:apps/backend/app/lifespan.py
    finally:
        try:
            HookInterceptor.safe_fire(HookContext(event=HookEvent.SESSION_END))
            await asyncio.to_thread(background_registry.close)
            await get_agent_team_coordinator().aclose()
            ...
            await task_runtime_spaces.aclose()
            await asyncio.to_thread(close_service_dependencies)
            _mark_boot_stopped()          # 只有全部成功才写
```

`AgentTeamCoordinator.aclose()` 的 `gather` 没有 `return_exceptions`（`team_coordinator.py:545-550`），一条 Team 收尾时 `background_registry.complete` 抛 DB 错误就会掀翻整个 finally：actor 不停、终端不关、MCP stdio 子进程与 SQLite 引擎不关（`close_service_dependencies` 是唯一关闭点），且 `_mark_boot_stopped()` 跳过 → supervisor 轮询到 `ready` 会误判后端仍健康。

置信度：高。

### 10. `POST /tasks/{id}/tool-calls/{cid}/cancel` 不映射 `KeyError`，Task 不存在时返回 500

```293:296:apps/backend/app/assistant_transport/assistant_api.py
    if not await asyncio.to_thread(service.cancel_tool_call, task_id, tool_call_id):
        raise HTTPException(
            status_code=409, detail="task has no running execution or call is already cancelled"
        )
```

该端点没有走 `_accept`（`:50-105`，统一把 `KeyError → 404`），而 service 第一句就是 `self._tasks.get_task(task_id)` 并**明确声明抛 `KeyError`**（`transport_assistant_service.py:240-243`）。同文件其他命令全是 404 契约，这一处不一致。

置信度：高。

### 11. `list_directory` 的路径归一化是自赋值（注释承诺的行为不存在）

```117:120:apps/backend/app/tools/tool_handler/list_directory.py
        # 模型常把根目录误解为空字符串；这里安全归一化为 "."，既兼容 LLM 输入，
        # 又不影响安全校验（PathResolver 随后仍会解析 "."）。
        if path == "":
            path = ""
```

`PathResolver.resolve_without_boundary("")` 返回 `"path must be a non-empty string"`，模型按注释语义传空串拿不到工作区根目录清单。

置信度：高。

### 12. 搜索链路没有总大小/总匹配上限，单行内的正则回溯不可中断

```42:58:apps/backend/app/tools/tool_handler/search/content_engine.py
    for file_path in scope.iter_files(file_glob):
        scope.check_cancel_and_timeout()
        try:
            lines = _read_lines(file_path,scope)        # 整个文件读成 list[str]
        ...
        for index, line in enumerate(lines, start=1):
            scope.check_cancel_and_timeout()
            if regex.search(line):                      # 单行内不可中断
```

- `MAX_SEARCH_LINE_BYTES` 只限单行（1MB），**不限文件总大小**，也不限 `rows` 总量（`limit` 只在最后切片 `:76`）；
- `search_content` 声明 `execution_mode="thread"`，而 thread 路径**没有硬超时强杀**（`tool_handler_runner.py:251-261` 明确写「`timeout_seconds` 在此仅作元数据」）；全仓 0 处使用 `"process"` 模式，`_force_kill` / windows job object 是死路径。

后果：`pattern="(a+)+$"` 撞上一行压缩过的 minified JS，需要重启进程才能恢复；终端的 `_OutputCollector._chunks` 同样无上限（`output_collector.py:110-116`）。

置信度：高（机制）/ 中（触发频率）。

### 13. `_enqueue_node` 的「节点绑定与调度输入同事务」不成立，Team 已停止时留下孤儿节点 Task

```452:485:apps/backend/app/core/tool/multi_agent/agent_team/team_coordinator.py
        if node_execution is None:
            task = self._task_service.get_or_create_task(...)      # 自己开事务并提交
            node_execution = state.create_execution_node(...)
        ...
        with main_session_factory().begin() as session:
            if not self._persist_running(row.id, state, session=session):
                return None                                        # Task 已落库，state 未落库
```

`_persist_running` 落空（Team 已被取消/失败/重启收敛）时，节点 Task 已经在库里的，但不在 `state.node_executions` 中——重启恢复拿不到它的 id、不会被取消，也没有 Run，UI 出现一个永不结束的空任务。

置信度：中高。

---

## P2：正确性与可观测性瑕疵

| 位置 | 缺陷 | 证据 |
|---|---|---|
| `conversation_run_crud.py:79-86` | `save_execution` 无条件 UPDATE、不检查 `rowcount`，Run 行已删时整轮 ReAct 进度静默写丢（同文件 `:465`、`task_crud.py:326` 都检查了） | 对照即可 |
| `conversation_task_state_service.py:382-393` | `publish_state` 的 `validate + deepcopy` 在锁外，取锁后直接替换 working copy → 窗口内的投影被旧帧覆盖 | 中高 |
| `model_node.py:140-172` / `run_driver.py:113-120` | 取消/失败路径不累加本次调用的用量，`transition(CANCEL)` 用的是**上一步**的 `rc.usage_stats`；FAIL 根本不传 | 高 |
| `team_coordinator.py:216, 291-296` | `state.current_node_id` 在驱动起点就赋值，`TEAM_START_FAILED` 基本不可达，首个节点启动失败也被标成 `next_node_start_failed` | 高 |
| `patch_apply.py:56-69` + `patch_parser.py:434-441` | 纯插入 hunk 在文件末尾无条件补 `\n`（`removesuffix` 是死代码）；CRLF 补丁的 hunk 行保留 `\r`，exact 匹配永不命中、必然降级到模糊策略 | 高 |
| `ignore_rules.py` / `file_walker.py` | 搜索根在工作区外时 `relative_to` 失败 → `match_*` 恒 False，**全部忽略规则静默失效** | 高 |
| `mcp_configuration_service.py:106-121` | 测试回写是 read→write 无锁（窗口只有几行，不是 20 秒），并发保存仍可能被旧 `servers` 覆盖；同仓 `ConfigurationFileStore.locked()` 未被使用 | 低中 |
| `runtime_message_context_manager.py:114, 232-243` | 序号是进程内计数器、只在 `begin_run` 对齐一次；第二个写入者（HITL 结算/后台结论）并发追加会撞 `(task_id, sequence)` 唯一约束，整条消息写不进 | 待验证（当前"单一驱动者"假设兜着） |

---

## 复核过、确认**没有**问题（避免重复排查）

- `RunTransition` 迁移表：终态不是任何迁移的来源状态，`update_status_if_in` 把状态白名单放进 SQL `WHERE` + `rowcount` 判断，是真正的原子条件迁移。
- `TaskMessageQueue.pending` 的唤醒时序（先登记 waiter → `clear()` → 读库 → `wait()`），不丢唤醒；`ack` 用「条件删除返回行内容」而不是 mailbox 副本，不会重复并入。
- `RunMailbox` 的堆操作与 `_seen` 去重；`prepare_input` 的「取消 → 挂起 → 步数上限」顺序与 `pending_nowait` 兜底复查。
- `StreamingPartStateMachine` 的 part 顺序与收口：`_close()` 是唯一出口，`finish()` 在 `finally`，不丢片。
- `max_steps` 边界（`step_count > step_limit` 严格大于），无 off-by-one，且有可见结论。
- `ToolDecisionService.submit` 用 `BEGIN IMMEDIATE` + 双闸门，重复提交返回 `DECISION_ALREADY_SUBMITTED`，不会重复入队。
- 路径越界防护（`PathResolver` 的 `relative_to` + 分量比较）、`atomic_write` 的 fsync/replace、补丁解析的拒绝面、多命中歧义直接报错。
- 存储层的 WAL / `foreign_keys=ON` / `pool_reset_on_return="rollback"`、`sqlite_autoincrement`（有测试守护）、分页游标无 off-by-one。
- `parent_watch`、`bootstate`、MCP 池的空闲回收（跳过 `in_flight`）与指纹比对。

---

## 建议的修复路线（按第零铁律排）

1. **先修 1/2/3 这一组**——它们同一个根因：`_loop` 与 `_drive` 都是「没人管的裸 task」。结构化做法是给两者各加一层显式收尾（`_drive` 用 `try/finally` 把注销+`consume`+`_on_released` 收进 finally；`_loop` 把阻塞读也纳入 `try`，并对 loop task 挂 done_callback 记录 + 不自动重启但在 `start()` 放宽为「done 则重建」）。同时把 `child_run_job` 的 `wait_quiescent()` 从「无限等」改成带上限。
2. **水位只在推进成功后前移**：`_advance` 返回成功才更新 `_watermark`/`_handled`，失败时保留并在下一轮 `pending` 重读（否则日志承诺与实际不符）。
3. **传输层把「full 之后的新 mutation」和「full 之前的旧 mutation」区分开**：`offer` 在 `_control_frame` 非空时只 `clear` 队列里已有的旧帧，新帧照常入队。
4. **搜索忽略规则删除内置默认集合**（已实现）：删除 `default_ignore_rules()` 与 `DEFAULT_IGNORED_DIR_NAMES`，空/缺失/读写失败一律等于不额外忽略；策略只由 `.fileignore` + `.gitignore` 决定。
5. **`rerun` 的 rebuild+publish 挪回闸门内、入队之前**，或让 `rebuild_state` 复用 `_unload_if_unwatched` 的「Run 在跑不卸载」判断。

需要我直接动手改哪一组，告诉我即可；我建议从 1、2 开始（都是 `task_actor.py` / `task_execution.py` 的局部改动，可单独验证）。