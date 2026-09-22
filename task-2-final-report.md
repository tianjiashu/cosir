# Task 2 final lifecycle report

## Scope

- 仅修改 backend 代码与 backend tests；未修改 frontend/Tauri。
- 未创建 Git commit。
- 修复前先读取了当前工作区最新报告 `task-2-fix-report.md`，并对其“已修复”结论逐项复核。

## P1/P2 closure

1. Child launch/follow-up 不再在 `threading.RLock` 内跨 `await`。锁只保护 process-local 状态；canonical create/claim/settle 在异步路径通过 `asyncio.to_thread` 执行。每次 canonical await 后都会重新检查 session generation/closing。`ConversationRunExecutor.start_registered()` 是无 await 的同步注册临界区，close 不能插入到“检查 → 注册”之间；close 竞争时会 cancel/settle，避免遗留 active Run。
2. Run 创建失败会显式回收刚创建的 delegation child Task；创建成功但启动失败仍会 canonical settle、取消执行 task、移除 session index 并清理取消信号。
3. Finalization observer 只向专用 worker 调度；Task/Run/SQLite 读取、child finalization 和 parent cleanup 均在 worker 中执行。`ConversationRunExecutor.cancel()` 以及 execution-finally 的 terminal close 都通过 `to_thread` 执行。
4. lifespan startup failure 现在按已初始化资源逆序清理 executor/child session、wait coordinator、terminal 和 service/storage dependency，并记录 cleanup 失败而保留原始 startup 异常。
5. done callback 对 `CancelledError` 单独忽略；其它 `BaseException` 均写结构化错误日志，不再静默吞掉。同步 dependency cache reset 提供 `close_sync()` 确定性契约：fence child session、投递 task cancel、清理 executor registry、关闭 terminal，并清空 bounded closed-session tombstone/index。
6. 关闭 session 会移除 `_sessions`/`_by_child_task` 索引；仅保留最多 1024 个有界幂等 tombstone，避免长期 registry 增长。follow-up mailbox 失败保留并执行有界 retry/sweep。

## TDD / race evidence

修复前新增回归测试出现预期失败：

- close-vs-claim race 因锁跨 await 导致 close 超时；
- Run 创建失败直接向外冒泡且 child Task 残留；
- finalization observer 在阻塞 canonical Task 读取时同步阻塞；
- close 后 session index 仍残留。

新增测试使用无 barrier timeout 的 worker 阻塞，测试本身只对整个 close 操作设置安全上限；若锁跨 await，失败表现是明确的 close timeout，而不是 worker barrier 自行超时放行。

## Final verification

```text
./.venv/bin/pytest -q tests/test_task1_async_tools_and_child_wait.py tests/test_task2_child_agent_sessions.py
37 passed in 1.34s

./.venv/bin/pytest -q tests/test_task2_lifecycle_cleanup.py tests/test_conversation_run_executor_cancel.py tests/test_run_zombie_convergence.py tests/test_child_agent_claim.py tests/test_child_agent_claim_adversarial.py
53 passed in 0.94s

./.venv/bin/ruff check <changed backend files>
All checks passed!

./.venv/bin/python -m compileall -q app
exit_code=0

git diff --check
exit_code=0
```

另外运行了全 backend `ruff check app tests`。它仍报告 118 条既有问题，集中在本次未修改的旧文件；本次涉及文件的 targeted ruff 已通过，未为清理全仓既有 lint 扩大改动范围。
