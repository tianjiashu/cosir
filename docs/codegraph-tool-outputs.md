# CodeGraph 6 工具原始输出（真实 Kernel 调用）

- project: `/Users/woaigugu/Documents/tianjiashu/coding-agent`

- 来源：后端 `CodeGraphKernelClient.query(method, params)` 的真实返回，

  与 agent 通过 `codegraph_query.py` 实际收到的 `QueryResult.content` 一致。

- 每个工具输出即前端当前收到的原始文本（后端 `codegraph_query.py` 直接 join 成文本）。


## codegraph_explore

**params**: `{'query': 'CodegraphQueryTool 如何把 6 个工具分发到 Kernel', 'max_files': 5}`

**is_error**: `False`

**content blocks**: 1


### block[0] type=text

```text
**Exploration: CodegraphQueryTool 如何把 6 个工具分发到 Kernel**

Found 43 symbols across 2 files.

**Blast radius — what depends on these (update/verify before editing)**

- `CodeGraph` (third_party/codegraph/src/index.ts:135) — 113 callers in `third_party/codegraph/src/agent-kernel/lifecycle-service.ts`, `third_party/codegraph/src/mcp/engine.ts`, `third_party/codegraph/src/mcp/tools.ts`, `third_party/codegraph/src/index.ts`; tests: `third_party/codegraph/__tests__/arkts-resolution.test.ts`, `third_party/codegraph/__tests__/c-fnptr-synthesizer.test.ts`, `third_party/codegraph/__tests__/celery-dispatch-synthesizer.test.ts`, `third_party/codegraph/__tests__/cfml-inheritance-resolution.test.ts` +76
- `query` (apps/backend/app/storage/crud/log_crud.py:79) — 1 caller in `apps/backend/app/tools/tool_handler/codegraph_query.py`; ⚠️ no covering tests found
- `query` (apps/backend/app/codegraph/kernel_client.py:187) — 1 caller in `apps/backend/app/service/log_query_service.py`; ⚠️ no covering tests found
- `CodeGraphKernelClient` (apps/backend/app/codegraph/kernel_client.py:46) — 7 callers in `apps/backend/app/codegraph/__init__.py`, `apps/backend/app/api/app.py`, `apps/backend/app/codegraph/supervisor.py`, `apps/backend/app/service/codegraph_lifecycle_service.py` +2 more; ⚠️ no covering tests found

**Relationships**

**extends:**
- CodegraphQueryTool → HandlerBase
- ExecuteTerminalTool → HandlerBase
- WebExtractTool → HandlerBase
- WebSearchTool → HandlerBase
- DeleteTool → HandlerBase

**calls:**
- build_codegraph_explore_definition → for_tool
- build_codegraph_search_definition → for_tool
- build_codegraph_node_definition → for_tool
- build_codegraph_callers_definition → for_tool
- build_codegraph_callees_definition → for_tool
- build_codegraph_impact_definition → for_tool
- execute → tool_error
- execute → get
- execute → query
- execute → tool_success
- ... and 108 more

**instantiates:**
- to_definition → ToolDefinition
- to_definition → ToolDisplayHints
- init → CodeGraph
- initSync → CodeGraph
- open → CodeGraph
- recreate → CodeGraph
- openSync → CodeGraph
- _add_sqlite_handler_or_warn → LogStore
- get_log_store → LogStore
- _entry_from_model → LogEntryRecord
- ... and 19 more

**references:**
- to_definition → execute
- init → CodeGraph
- initSync → CodeGraph
- open → CodeGraph
- recreate → CodeGraph
- openSync → CodeGraph
- setQueryPool → QueryPool
- constructor → WORKER_FILE
- constructor → MAX_POOL_SIZE
- constructor → QueryPoolOptions
- ... and 21 more

**Source Code**

> The code below is the **verbatim, current on-disk source** of these files — re-read from disk on this call and line-numbered, byte-for-byte identical to what the Read tool returns. It is NOT a summary, outline, or stale cache. Treat each block as a Read you have already performed: do not Read a file shown here.

**`apps/backend/app/codegraph/kernel_client.py`** — CodeGraphProtocolIncompatibleError(instantiates), call(calls), instantiates(instantiates), CodeGraphKernelUnavailableError(instantiates), calls(calls), __init__(method), start(calls), call(method), _write_line(calls), CodeGraphKernelTimeoutError(instantiates), +22 more

```python
46	class CodeGraphKernelClient:
47	    """Kernel 子进程的 RPC 客户端（不管理进程生命周期）。"""
48	
49	    def __init__(self, proc: Popen, timeout_seconds: float = 30.0) -> None:
50	        """构造客户端并启动 stdout 读取线程。
51	
52	        参数:
53	            proc: 已启动的 Kernel 子进程（stdin/stdout 须为管道）。
54	            timeout_seconds: 单次请求默认超时（秒）。
55	
56	        返回:
57	            无。
58	
59	        异常:
60	            无。
61	
62	        副作用:
63	            启动 reader 线程消费 ``proc.stdout``；注册进程退出监听标记。
64	        """
65	        self._proc = proc
66	        self._timeout_seconds = timeout_seconds
67	        self._pending: dict[str, Future[Any]] = {}
68	        self._pending_lock = threading.Lock()
69	        self._write_lock = threading.Lock()
70	        self._stop_reader = threading.Event()
71	        self._reader = threading.Thread(
72	            target=self._read_loop, name="workspace_payload-kernel-reader", daemon=True
73	        )
74	        self._reader.start()
75	
76	    # ------------------------------------------------------------------
77	    # Public API
78	    # ------------------------------------------------------------------
79	
80	    def call(
81	        self,
82	        method: str,
83	        params: dict[str, Any] | None = None,
84	        timeout: float | None = None,
85	    ) -> Any:
86	        """发送一次 RPC 请求并等待响应。
87	
88	        参数:
89	            method: 方法名（如 ``kernel.hello`` / ``codegraph_explore``）。
90	            params: 请求参数；查询方法须含 ``workspace_path``。
91	            timeout: 本次超时（秒）；省略用构造默认值。
92	
93	        返回:
94	            成功响应的 ``result`` 字段（HelloResult/PingResult/QueryResult 等，
95	            由调用方按 method 转型）；查询方法返回 ``QueryResult``。
96	
97	        异常:
98	            CodeGraphKernelTimeoutError: 超时未收到响应。
99	            CodeGraphKernelUnavailableError: 进程已退出或管道断裂。
100	            CodeGraphKernelError（子类）: 协议层返回 error（经 ``error_from_code`` 映射）。
101	
102	        副作用:
103	            写一行 JSON 到 stdin；在读线程投递前阻塞。
104	        """
105	        if self._stop_reader.is_set() or self._proc.poll() is not None:
106	            raise CodeGraphKernelUnavailableError("kernel process is not available for requests")
107	
108	        wait = timeout if timeout is not None else self._timeout_seconds
109	        request_id = uuid.uuid4().hex
110	        future: Future[Any] = Future()
111	        with self._pending_lock:
112	            self._pending[request_id] = future
113	        try:
114	            self._write_line({"id": request_id, "method": method, "params": params or {}})
115	            return future.result(timeout=wait)
116	        except CodeGraphKernelError:
117	            raise
118	        except FuturesTimeoutError as exc:
119	            future.cancel()
120	            raise CodeGraphKernelTimeoutError(
121	                f"kernel request {method} timed out after {wait}s"
122	            ) from exc
123	        finally:
124	            with self._pending_lock:
125	                self._pending.pop(request_id, None)
126	
127	    def hello(self) -> HelloResult:
128	        """发起 kernel.hello 握手并校验协议版本兼容。
129	
130	        对 Kernel 返回做字段显式取值，字段缺失/多余时抛
131	        ``CodeGraphProtocolIncompatibleError``（归为协议层错误，而非裸 TypeError 逃逸）。
132	        """
133	        result = self.call(METHOD_HELLO)
134	        if not isinstance(result, dict):
135	            raise CodeGraphProtocolIncompatibleError("kernel.hello returned a non-object payload")
136	        try:
137	            hello = HelloResult(
138	                protocol_version=str(result["protocol_version"]),
139	                kernel_version=str(result["kernel_version"]),
140	                codegraph_version=str(result["codegraph_version"]),
141	                capabilities=[str(c) for c in result["capabilities"]],
142	                platform=str(result["platform"]),
143	            )
144	        except (KeyError, TypeError, ValueError) as exc:
145	            raise CodeGraphProtocolIncompatibleError(
146	                f"kernel.hello payload missing/invalid field: {exc}"
147	            ) from exc
148	        if hello.protocol_version != PROTOCOL_VERSION:
149	            raise CodeGraphProtocolIncompatibleError(
150	                f"kernel protocol {hello.protocol_version} != expected {PROTOCOL_VERSION}"
151	            )
152	        return hello
153	
154	    def ping(self, timeout: float | None = None) -> PingResult:
155	        """发起 kernel.ping 健康检查（显式取值，字段异常归为协议错误）。
156	
157	        参数:
158	            timeout: RPC 超时（秒）；None 时沿用客户端构造时设定的 ``timeout_seconds``。
159	                用于调用方做短超时健康快检（如 prepare 前 5s 探活）。
160	
161	        返回:
162	            PingResult：Kernel 存活状态与活跃 workspace 数。
163	
164	        异常:
165	            CodeGraphProtocolIncompatibleError: 当返回体不是对象或字段缺失/类型不符。
166	            CodeGraphKernelUnavailableError: 当 Kernel 进程不可达。
167	            CodeGraphKernelTimeoutError: 当 RPC 超过给定或默认的 ``timeout_seconds``。
168	
169	        副作用:
170	            无（不修改 Kernel 状态）。
171	        """
172	
173	        result = self.call(METHOD_PING, timeout=timeout)
174	        if not isinstance(result, dict):
175	            raise CodeGraphProtocolIncompatibleError("kernel.ping returned a non-object payload")
176	        try:
177	            return PingResult(
178	                ok=bool(result["ok"]),
179	                uptime_ms=int(result["uptime_ms"]),
180	                active_workspaces=int(result["active_workspaces"]),
181	            )
182	        except (KeyError, TypeError, ValueError) as exc:
183	            raise CodeGraphProtocolIncompatibleError(
184	                f"kernel.ping payload missing/invalid field: {exc}"
185	            ) from exc
186	
187	    def query(
188	        self,
189	        method: str,
190	        params: dict[str, Any],
191	        timeout: float | None = None,
192	    ) -> QueryResult:
193	        """发起一次查询工具调用，返回归一化 QueryResult。"""
194	        result = self.call(method, params, timeout)
195	        return QueryResult(
196	            content=result.get("content", []),
197	            is_error=result.get("is_error", False),
198	        )
199	
200	    def index_status(
201	        self,
202	        workspace_path: str,
203	        timeout: float | None = None,
204	    ) -> IndexStatusResult:
205	        """查询 workspace 索引状态（codegraph_status）。"""
206	        result = self.call(METHOD_INDEX_STATUS, {"workspace_path": workspace_path}, timeout)
207	        return self._index_status_result(result)
208	
209	    def index_init(
210	        self,
211	        workspace_path: str,
212	        timeout: float | None = None,
213	    ) -> IndexInitResult:
214	        """创建 workspace 索引（codegraph_init，写入型，可长超时）。"""
215	        result = self.call(METHOD_INDEX_INIT, {"workspace_path": workspace_path}, timeout)
216	        return self._index_init_result(result)
217	
218	    def index_sync(
219	        self,
220	        workspace_path: str,
221	        timeout: float | None = None,
222	    ) -> IndexSyncResult:
223	        """增量同步 workspace 索引（codegraph_sync，写入型）。"""
224	        result = self.call(METHOD_INDEX_SYNC, {"workspace_path": workspace_path}, timeout)
225	        return self._index_sync_result(result)
226	
227	    @staticmethod
228	    def _index_status_result(result: Any) -> IndexStatusResult:
229	        """解析 codegraph_status 响应（字段缺失/类型错归为协议错误，对齐 hello 范式）。"""
230	        if not isinstance(result, dict):
231	            raise CodeGraphProtocolIncompatibleError(
232	                "codegraph_status returned a non-object payload"
233	            )
234	        try:
235	            last_indexed = result["last_indexed_at"]
236	            return IndexStatusResult(
237	                state=str(result["state"]),
238	                last_indexed_at=int(last_indexed) if last_indexed is not None else None,
239	            )
240	        except (KeyError, TypeError, ValueError) as exc:
241	            raise CodeGraphProtocolIncompatibleError(
242	                f"codegraph_status payload missing/invalid field: {exc}"
243	            ) from exc
244	
245	    @staticmethod
246	    def _index_init_result(result: Any) -> IndexInitResult:
247	        """解析 codegraph_init 响应（畸形 payload 归为协议错误，保证上层不逃逸裸异常）。"""
248	        if not isinstance(result, dict):
249	            raise CodeGraphProtocolIncompatibleError("codegraph_init returned a non-object payload")
250	        try:
251	            return IndexInitResult(
252	                state=str(result["state"]),
253	                files_indexed=int(result["files_indexed"]),
254	                duration_ms=int(result["duration_ms"]),
255	            )
256	        except (KeyError, TypeError, ValueError) as exc:
257	            raise CodeGraphProtocolIncompatibleError(
258	                f"codegraph_init payload missing/invalid field: {exc}"
259	            ) from exc
260	
261	    @staticmethod
262	    def _index_sync_result(result: Any) -> IndexSyncResult:
263	        """解析 codegraph_sync 响应（畸形 payload 归为协议错误，保证上层不逃逸裸异常）。"""
264	        if not isinstance(result, dict):
265	            raise CodeGraphProtocolIncompatibleError("codegraph_sync returned a non-object payload")
266	        try:
267	            return IndexSyncResult(
268	                state=str(result["state"]),
269	                files_added=int(result["files_added"]),
270	                files_modified=int(result["files_modified"]),
271	                files_removed=int(result["files_removed"]),
272	                duration_ms=int(result["duration_ms"]),
273	            )
274	        except (KeyError, TypeError, ValueError) as exc:
275	            raise CodeGraphProtocolIncompatibleError(
276	                f"codegraph_sync payload missing/invalid field: {exc}"
277	            ) from exc
278	
279	    def shutdown(self) -> None:
280	        """请求 Kernel 优雅关闭（不阻塞等待进程退出）。"""
281	        try:
282	            self.call(METHOD_SHUTDOWN)
283	        except CodeGraphKernelError as exc:
284	            log.warning(
285	                "codegraph_kernel_shutdown_failed",
286	                extra={"msg": "kernel.shutdown 调用失败", "data": {"error": str(exc)}},
287	            )
288	
289	    def stop(self) -> None:
290	        """停止客户端：标记 reader 停止并等待线程退出。"""
291	        self._stop_reader.set()
292	        if self._reader.is_alive():
293	            self._reader.join(timeout=2.0)
294	
295	    # ------------------------------------------------------------------
296	    # Internal: IO
297	    # ------------------------------------------------------------------
298	
299	    def _write_line(self, obj: dict[str, Any]) -> None:
300	        """线程安全地写一行 JSON 到 stdin；管道断裂转 Kernel 不可用。"""
301	        line = json.dumps(obj, ensure_ascii=False) + "\n"
302	        with self._write_lock:
303	            stdin = self._proc.stdin
304	            if stdin is None:
305	                raise CodeGraphKernelUnavailableError("kernel stdin is not available")
306	            try:
307	                stdin.write(line)
308	                stdin.flush()
309	            except (OSError, BrokenPipeError) as exc:
310	                raise CodeGraphKernelUnavailableError(f"kernel stdin write failed: {exc}") from exc
311	
312	    def _read_loop(self) -> None:
313	        """消费 stdout 的 JSON-line，按 id 投递给等待中的 Future。"""
314	        stdout = self._proc.stdout
315	        if stdout is None:
316	            return
317	        try:
318	            for raw in stdout:
319	                if self._stop_reader.is_set():
320	                    break
321	                self._dispatch_line(raw)
322	        except (ValueError, OSError):
323	            # 进程退出 / 管道断裂：把所有 pending 标记为不可用（finally 兜底）。
324	            pass
325	        finally:
326	            # 无论正常停止还是管道断裂，统一把所有仍在等待的请求失败化，
327	            # 避免调用方无限挂起。stop() 后本就不应有新请求。
328	            self._fail_all_pending()
329	
330	    def _dispatch_line(self, raw: str) -> None:
331	        """解析单行并投递；非法 JSON 或缺少 id 的响应被忽略。"""
332	        line = raw.strip()
333	        if not line:
334	            return
335	        try:
336	            msg = json.loads(line)
337	        except json.JSONDecodeError:
338	            return
339	        response_id = msg.get("id")
340	        if not isinstance(response_id, str):
341	            return
342	        with self._pending_lock:
343	            future = self._pending.get(response_id)
344	            if future is None or future.done():
345	                return
346	        if "error" in msg and msg["error"] is not None:
347	            err = msg["error"]
348	            try:
349	                code = KernelErrorCode(err.get("code", "INTERNAL"))
350	            except ValueError:
351	                code = KernelErrorCode.INTERNAL
352	            future.set_exception(
353	                error_from_code(
354	                    code,
355	                    err.get("message", "kernel error"),
356	                    bool(err.get("retryable", False)),
357	                )
358	            )
359	        else:
360	            future.set_result(msg.get("result"))
361	
362	    def _fail_all_pending(self) -> None:
363	        """进程不可用时把所有等待中的请求标记为 Kernel 不可用（线程安全快照）。"""
364	        with self._pending_lock:
365	            snapshots = list(self._pending.items())
366	        for _request_id, future in snapshots:
367	            if not future.done():
368	                future.set_exception(
369	                    CodeGraphKernelUnavailableError("kernel process exited or pipe broke")
370	                )
371	
```

**`third_party/codegraph/src/index.ts`** — calls(calls), instantiates(instantiates), references(references), CodeGraph(references), CodeGraph(instantiates), wireLayers(calls), constructor(method), wireLayers(method), reopenIfReplaced(method), init(method), +4 more

```typescript
154	  // File watcher for auto-sync on file changes
155	  private watcher: FileWatcher | null = null;
156	
157	  private constructor(
158	    db: DatabaseConnection,
159	    queries: QueryBuilder,
160	    projectRoot: string
161	  ) {
162	    this.db = db;
163	    this.queries = queries;
164	    this.projectRoot = projectRoot;
165	    this.fileLock = new FileLock(
166	      path.join(getCodeGraphDir(projectRoot), 'workspace_payload.lock')
167	    );
168	    this.wireLayers();
169	  }
170	
171	  /**
172	   * (Re)build the query/extraction/graph layers over the current `this.queries`
173	   * (which wraps `this.db`). Factored out of the constructor so `reopenIfReplaced`
174	   * can rebuild them against a fresh connection without duplicating the wiring.
175	   * The path-based `fileLock` is independent of the DB handle, so it stays put.
176	   */
177	  private wireLayers(): void {
178	    // Down-weight the project name as a query term in search ranking — it names
179	    // the whole repo, not a symbol, so it has no discriminative value (#720).
180	    try {
181	      this.queries.setProjectNameTokens(deriveProjectNameTokens(this.projectRoot));
182	    } catch {
183	      // Best-effort: ranking still works without it.
184	    }
185	    this.orchestrator = new ExtractionOrchestrator(this.projectRoot, this.queries);
186	    this.resolver = createResolver(this.projectRoot, this.queries);
187	    this.graphManager = new GraphQueryManager(this.queries);
188	    this.traverser = new GraphTraverser(this.queries);
189	    this.contextBuilder = createContextBuilder(
190	      this.projectRoot,
191	      this.queries,
192	      this.traverser
193	    );
194	  }
195	
196	  /**
197	   * Heal a stale database handle in place. If `.workspace_payload/` was removed and

... (gap) ...

207	   * POSIX-only in practice: `isReplacedOnDisk` never fires on Windows (an open
208	   * file can't be unlinked there, and st_ino is unreliable).
209	   */
210	  reopenIfReplaced(): boolean {
211	    if (!this.db.isReplacedOnDisk()) return false;
212	    const dbPath = this.db.getPath();
213	    // Open the live file FIRST — if that throws (e.g. mid-recreate), the old
214	    // handle stays in place and the caller retries on the next query, rather
215	    // than leaving this instance with no connection at all.
216	    const fresh = DatabaseConnection.open(dbPath);
217	    const stale = this.db;
218	    this.db = fresh;
219	    this.queries = new QueryBuilder(fresh.getDb());
220	    this.wireLayers();
221	    // Releasing the dead handle also frees the leaked db/-wal/-shm fds that were
222	    // pinning the unlinked inode (#925).
223	    try { stale.close(); } catch { /* the old inode is gone; closing just frees fds */ }
224	    return true;
225	  }
226	
227	  // ===========================================================================
228	  // Lifecycle Methods

... (gap) ...

237	   * @param options - Initialization options
238	   * @returns A new CodeGraph instance
239	   */
240	  static async init(projectRoot: string, options: InitOptions = {}): Promise<CodeGraph> {
241	    await initGrammars();
242	    const resolvedRoot = path.resolve(projectRoot);
243	
244	    // Check if already initialized
245	    if (isInitialized(resolvedRoot)) {
246	      throw new Error(`CodeGraph already initialized in ${resolvedRoot}`);
247	    }
248	
249	    // Create directory structure
250	    createDirectory(resolvedRoot);
251	
252	    // Initialize database
253	    const dbPath = getDatabasePath(resolvedRoot);
254	    const db = DatabaseConnection.initialize(dbPath);
255	    const queries = new QueryBuilder(db.getDb());
256	
257	    const instance = new CodeGraph(db, queries, resolvedRoot);
258	
259	    // Run initial indexing if requested
260	    if (options.index) {
261	      await instance.indexAll({ onProgress: options.onProgress });
262	    }
263	
264	    return instance;
265	  }
266	
267	  /**
268	   * Initialize synchronously (without indexing)
269	   */
270	  static initSync(projectRoot: string): CodeGraph {
271	    const resolvedRoot = path.resolve(projectRoot);
272	
273	    // Check if already initialized
274	    if (isInitialized(resolvedRoot)) {
275	      throw new Error(`CodeGraph already initialized in ${resolvedRoot}`);
276	    }
277	
278	    // Create directory structure
279	    createDirectory(resolvedRoot);
280	
281	    // Initialize database
282	    const dbPath = getDatabasePath(resolvedRoot);
283	    const db = DatabaseConnection.initialize(dbPath);
284	    const queries = new QueryBuilder(db.getDb());
285	
286	    return new CodeGraph(db, queries, resolvedRoot);
287	  }
288	
289	  /**
290	   * Open an existing CodeGraph project
291	   *
292	   * @param projectRoot - Path to the project root directory
293	   * @param options - Open options
294	   * @returns A CodeGraph instance
295	   */
296	  static async open(projectRoot: string, options: OpenOptions = {}): Promise<CodeGraph> {
297	    await initGrammars();
298	    const resolvedRoot = path.resolve(projectRoot);
299	
300	    // Check if initialized
301	    if (!isInitialized(resolvedRoot)) {
302	      throw new Error(`CodeGraph not initialized in ${resolvedRoot}. Run init() first.`);
303	    }
304	
305	    // Validate directory structure
306	    const validation = validateDirectory(resolvedRoot);
307	    if (!validation.valid) {
308	      throw new Error(`Invalid CodeGraph directory: ${validation.errors.join(', ')}`);
309	    }
310	
311	    // Open database
312	    const dbPath = getDatabasePath(resolvedRoot);
313	    const db = DatabaseConnection.open(dbPath);
314	    const queries = new QueryBuilder(db.getDb());
315	
316	    const instance = new CodeGraph(db, queries, resolvedRoot);
317	
318	    // Sync if requested
319	    if (options.sync) {
320	      await instance.sync();
321	    }
322	
323	    return instance;
324	  }
325	
326	  /**
327	   * Rebuild the project's database from scratch and return a fresh, empty
```


... (output truncated to budget; the source above is complete and verbatim — treat it as already Read. For any area not covered, run another codegraph_explore with the specific names — do NOT Read these files.)
```


## codegraph_search

**params**: `{'query': 'resolve_node_binary', 'limit': 10}`

**is_error**: `False`

**content blocks**: 1


### block[0] type=text

```text
**Search Results (3 found)**

**resolve_node_binary** (function)
apps/backend/app/codegraph/node_resolver.py:48
`() -> Path`

**app.codegraph.node_resolver** (import)
apps/backend/app/codegraph/__init__.py:21
`from app.codegraph.node_resolver import resolve_node_binary`

**app.codegraph.node_resolver** (import)
apps/backend/app/codegraph/supervisor.py:29
`from app.codegraph.node_resolver import resolve_node_binary`

```


## codegraph_node

**params**: `{'symbol': 'resolve_node_binary', 'include_code': True}`

**is_error**: `False`

**content blocks**: 1


### block[0] type=text

```text
**resolve_node_binary** (function)

**Location:** apps/backend/app/codegraph/node_resolver.py:48
**Signature:** `() -> Path`
**Trail — codegraph_node any of these to follow it (no Read needed)**
**Calls →** get (apps/backend/app/storage/crud/task_crud.py:139), resolve (apps/backend/app/core/agents/agent_profile_registry.py:43), _fixed_node_candidates (apps/backend/app/codegraph/node_resolver.py:26), CodeGraphNodeMissingError (apps/backend/app/codegraph/exceptions.py:102), NODE_DIR_ENV (apps/backend/app/codegraph/node_resolver.py:23)
**Called by ←** _spawn (apps/backend/app/codegraph/supervisor.py:238), __init__.py (apps/backend/app/codegraph/__init__.py:1), supervisor.py (apps/backend/app/codegraph/supervisor.py:1)
```


## codegraph_callers

**params**: `{'symbol': 'resolve_node_binary', 'limit': 20}`

**is_error**: `False`

**content blocks**: 1


### block[0] type=text

```text
**Callers of resolve_node_binary (3 found)**

- _spawn (method) - apps/backend/app/codegraph/supervisor.py:238
- __init__.py (file) - apps/backend/app/codegraph/__init__.py:1 — via import
- supervisor.py (file) - apps/backend/app/codegraph/supervisor.py:1 — via import
```


## codegraph_callees

**params**: `{'symbol': 'resolve_node_binary', 'limit': 20}`

**is_error**: `False`

**content blocks**: 1


### block[0] type=text

```text
**Callees of resolve_node_binary (5 found)**

- get (method) - apps/backend/app/storage/crud/task_crud.py:139
- resolve (method) - apps/backend/app/core/agents/agent_profile_registry.py:43
- _fixed_node_candidates (function) - apps/backend/app/codegraph/node_resolver.py:26
- CodeGraphNodeMissingError (class) - apps/backend/app/codegraph/exceptions.py:102 — via instantiation
- NODE_DIR_ENV (variable) - apps/backend/app/codegraph/node_resolver.py:23 — via reference
```


## codegraph_impact

**params**: `{'symbol': 'resolve_node_binary', 'limit': 20}`

**is_error**: `False`

**content blocks**: 1


### block[0] type=text

```text
**Impact: "resolve_node_binary" affects 9 symbols**

**apps/backend/app/codegraph/node_resolver.py:**
resolve_node_binary:48

**apps/backend/app/codegraph/supervisor.py:**
_spawn:238, start:118, supervisor.py:1

**apps/backend/app/codegraph/__init__.py:**
__init__.py:1

**apps/backend/app/api/app.py:**
app.py:1

**apps/backend/app/service/codegraph_lifecycle_service.py:**
codegraph_lifecycle_service.py:1

**apps/backend/app/tools/tool_handler/codegraph_query.py:**
codegraph_query.py:1

**apps/backend/app/tools/tool_system.py:**
tool_system.py:1

```

