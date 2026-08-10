# Langfuse Agent 执行观测修复计划

> 状态：方案待实施  
> 范围：`apps/backend/app/core/observability/langfuse_tracing.py`、`apps/backend/app/core/observability/langfuse_tool_trace_recorder.py` 及其装配、测试  
> 目标：让 Langfuse 观测能力服务于长期稳定迭代，而不是成为 Agent 执行链路的新故障源。

## 一、最零铁律下的判断

第零铁律要求所有改动以“方便项目长期稳定迭代”为最终目标。套到 Langfuse 观测链路上，优先级不是“尽快把 trace 发出去”，而是：

1. 可观测性失败绝不能中断 turn、工具执行或事件流。
2. 观测数据必须能支持复盘一次 Agent 执行的关键链路。
3. 任何上报到 Langfuse 的数据都必须经过统一脱敏边界。
4. 尽量复用 Langfuse SDK、OpenTelemetry、Python `contextvars` 与项目既有 redaction/logging 能力，不手写通用追踪框架。
5. 修复应通过单元测试和必要的集成测试锁定，不依赖人工记忆。

当前实现的方向基本正确：Langfuse 耦合收口在 `core/observability`，service 层通过 `ToolTraceRecorder` 协议依赖倒置，Langfuse import 惰性加载。但现有代码仍有几个不符合第零铁律的实现风险：异常边界不够硬、工具 span contextmanager 协议有 bug、脱敏不完整、完整执行链观测不足。

本文档中的“脱敏”覆盖所有发往 Langfuse 的外部观测 payload，不只覆盖工具结果。LangChain `CallbackHandler` 自动捕获的 LLM generation 输入/输出也在边界内。

## 二、当前问题清单

### P1：工具 span contextmanager 会破坏工具异常语义

位置：`app/core/observability/langfuse_tool_trace_recorder.py`

`LangfuseToolTraceRecorder.span()` 用一个大 `try/except` 包住了 `yield adapter`。在 `@contextmanager` 语义里，调用方 `with` 块内部抛出的异常会被重新抛回到 `yield` 处。当前实现会把工具执行异常误判为 Langfuse 异常，并在 `except` 中再次 `yield _NullToolSpan()`，可能触发 `RuntimeError: generator didn't stop after throw()`，或掩盖真实工具执行错误。

修复原则：

- `try/except` 只包住 Langfuse span 创建阶段。
- `yield` 后不能再 yield。
- `adapter.finalize()` 和 Langfuse context exit 失败只能记日志，不得覆盖工具执行异常。
- 工具执行异常仍交给 `ToolExecutionService` 的既有收口逻辑处理。

### P1：turn trace 退出阶段可能把成功 turn 判失败

位置：`app/core/observability/langfuse_tracing.py`

`turn_trace()` 在 `yield` 后直接调用 `client.flush()`，并直接执行 `attr_cm.__exit__()` / `root_span_cm.__exit__()`。这些操作属于观测侧副作用。如果它们抛异常，会向上传播到 `AgentRuntime.run_agent()`，使原本已经完成的 Agent turn 被误判失败。

修复原则：

- `flush()` 失败只写 `log.exception`。
- context exit 失败只写 `log.exception`。
- 如果 workflow 本身已经抛异常，观测异常不得覆盖原异常。
- 如果 workflow 正常结束，观测异常不得制造新的业务异常。

### P1：工具 recorder 构造没有安全降级

位置：`app/core/runtime/runner.py`、`app/core/observability/langfuse_tool_trace_recorder.py`

`runner.py` 在进入 `turn_trace()` 前直接执行 `LangfuseToolTraceRecorder()`。如果 Langfuse client 初始化失败，turn 会在 workflow 执行前失败。这违反“可观测性失败不影响主流程”。

修复原则：

- 新增安全工厂，例如 `build_tool_trace_recorder()`。
- 工厂内部先判断 `tracing_enabled()`，再尝试构造 recorder。
- 构造失败时记录日志并返回 `None` 或 `_NullToolTraceRecorder`。
- `runner.py` 不直接调用 Langfuse recorder 构造函数。

### P2：脱敏边界不完整

位置：`app/core/observability/langfuse_tool_trace_recorder.py`

当前只对 `ToolObservation.content` 调用 `redact_terminal_output()`，但 `observation.data`、`observation.error`、`status_message`、工具输入 arguments 仍可能原样上报。工具输出可能包含命令行、路径、文件内容、API key、token 或异常详情，不能只脱敏 content。

同时，`langfuse_tracing.py` 通过 LangChain `CallbackHandler` 自动捕获 LLM generation。generation 的 input 可能包含用户提示、工具观察、文件片段、错误信息和路径；generation 的 output 也可能回显敏感内容。因此 LLM generation 原文捕获同样必须纳入脱敏或禁用策略，不能只处理手动 tool span。

修复原则：

- 新增 Langfuse 上报专用 sanitizer，职责是“把将要上报到外部观测系统的数据递归脱敏”。
- sanitizer 应复用项目既有 `redact_terminal_output()` 或 redaction 原语。
- 覆盖字段：tool input arguments、output.content、output.data、output.error、status_message。
- 覆盖字段：LLM generation input、generation output、metadata 中可能携带的用户输入或工具观察。
- 若 Langfuse SDK 支持 masking / redaction hook，优先接入 SDK 官方 masking 能力；若不能稳定覆盖 LangChain `CallbackHandler` 的自动 input/output，则第一阶段禁用 LLM 原文 capture，仅保留 model、usage、latency、trace metadata，后续再用包装 CallbackHandler 或 SDK hook 恢复脱敏后的原文。
- 不修改 `ToolObservation` 本身，避免影响模型消息和前端事件语义。

### P2：当前还不能完整观测 Agent 执行

当前可观测范围：

- turn 根 span。
- LangChain `CallbackHandler` 自动捕获的模型 generation。
- 手动记录的工具 tool observation。

缺失范围：

- workflow 节点生命周期。
- step started / model requested / observation added 等 runtime event。
- 审批中断与恢复。
- checkpoint 写入与恢复。
- context compaction。
- cancel / disconnect / failed 的精确阶段。
- SSE 事件投递链路。

修复原则：

- 先确保现有 LLM + tool trace 稳定、脱敏、安全降级。
- 再补充轻量 runtime event 到 Langfuse event/span 的映射。
- 不在第一步重写 workflow，也不引入自研 tracing 框架。

### P2：跨 `asyncio.to_thread` 的 trace 归属缺少测试锁定

工具执行经 `asyncio.to_thread` 进入工作线程。Python `asyncio.to_thread` 通常会复制 `contextvars.Context`，OpenTelemetry 默认也基于 contextvars，但这属于关键观测假设，必须有测试锁定。

修复原则：

- 不使用 Java 式 `ThreadLocal` 作为主方案。
- Python async 链路优先使用 `contextvars.ContextVar`。
- 对于 Langfuse trace 归属，优先依赖 OTel context propagation，并用测试证明。
- 如果测试证明 tool span 无法挂到当前 turn trace，再显式传递 `trace_id` / parent observation 上下文。

## 三、推荐技术方案

### 3.1 收紧 Langfuse 工具 span 生命周期

目标结构：

```python
@contextmanager
def span(self, call: ToolCall, step_id: str) -> Iterator[ToolCallSpan]:
    span_cm = None
    adapter = _NullToolSpan()
    try:
        span_cm = self._client.start_as_current_observation(...)
        langfuse_span = span_cm.__enter__()
        adapter = _LangfuseToolSpan(langfuse_span)
    except Exception:
        log.exception(...)
        yield _NullToolSpan()
        return

    try:
        yield adapter
    finally:
        try:
            adapter.finalize()
        except Exception:
            log.exception(...)
        try:
            span_cm.__exit__(None, None, None)
        except Exception:
            log.exception(...)
```

实现时必须捕获 `sys.exc_info()` 并把真实异常传给 `span_cm.__exit__(*exc_info)`。这样工具执行异常既能保持原始传播语义，也能让 Langfuse context manager 看到异常上下文。Langfuse exit 自身失败只能记录日志，不能覆盖原始工具异常。

### 3.2 收紧 turn trace 生命周期

`turn_trace()` 应拆成四段：

1. 前置构造：import、client、trace_id、root span cm、attributes cm、CallbackHandler。
2. 进入上下文：进入 root span 与 attributes。若部分进入失败，必须清理已经进入的上下文。
3. 执行业务：`yield TurnTraceResult(...)`，业务异常原样传播。
4. 观测清理：flush 与 context exit 均 best-effort，不影响业务结果。

关键要求：

- 如果 root span 已进入但 attributes 进入失败，要退出 root span。
- 如果 root span / attributes 已进入但 `CallbackHandler` 构造失败，要退出已经进入的 context，并降级为 `TurnTraceResult([], None)`。
- `client.flush()` 失败不得抛出。
- `__exit__()` 失败不得抛出。
- 日志必须带 `task_id`、`turn_id`，但不能带 secret。

### 3.3 新增安全 recorder 工厂

建议放在 `langfuse_tool_trace_recorder.py` 或新文件 `langfuse_recorder_factory.py`。如果只服务 Langfuse 工具 recorder，可先放在现有文件，避免过早拆分。

接口建议：

```python
def build_tool_trace_recorder() -> ToolTraceRecorder:
    """按当前 Settings 构造 Langfuse 工具追踪 recorder，失败时安全降级。"""
```

`runner.py` 改为：

```python
recorder = build_tool_trace_recorder()
```

这样 `runner.py` 不需要知道 Langfuse 构造细节。工厂返回类型固定为 `ToolTraceRecorder`：未启用、缺 key、构造失败时返回 `_NullToolTraceRecorder`，避免 runner 出现 `None` 分支，也避免未来 recorder 实现的 `flush()` 行为差异泄漏到主流程。

### 3.4 新增 Langfuse 上报 sanitizer

建议新增 `app/core/observability/langfuse_payload_sanitizer.py`，职责单一：把将要发送到 Langfuse 的 payload 做递归脱敏。

初版能力：

- `sanitize_langfuse_payload(value: Any) -> Any`
- dict/list/tuple 递归处理。
- str 走 `redact_terminal_output()`。
- 非 JSON 基础类型转为安全字符串或原样保留简单标量。
- 对 key 名含 `key`、`token`、`secret`、`password`、`cookie`、`authorization` 的字段强制替换为 `[REDACTED]`。
- 对大小写混合 key 同样生效。
- 对超长文本沿用现有输出预算或增加 Langfuse 专用长度上限，避免外部观测 payload 无界膨胀。

注意：这不是重新发明完整 DLP，而是复用既有 redaction 能力，补齐外部观测出口的统一边界。

### 3.5 LLM generation 脱敏策略

LLM generation 由 LangChain `CallbackHandler` 自动上报，不能直接套用 tool span 的 `output` 映射。实施时按以下顺序选择：

1. 优先检查 Langfuse SDK v4 是否提供官方 masking / redaction hook，并把 `sanitize_langfuse_payload()` 接入该 hook，统一处理 generation input/output 与 tool payload。
2. 如果 SDK hook 能覆盖 LangChain `CallbackHandler` 的自动上报，则保留 generation 原文，但必须先经 sanitizer。
3. 如果 SDK hook 不能稳定覆盖，第一阶段禁用 generation input/output 原文 capture，仅保留 model、usage、latency、trace_id、task_id、turn_id、agent_id 等非敏感元数据。
4. 后续如确需展示脱敏后的 prompt/completion，再实现包装 CallbackHandler，把原文 capture 关闭，改由包装层写入脱敏后的 input/output。

这一路线优先保证安全和稳定，再逐步增加观测细节，符合第零铁律。

### 3.6 是否使用 ThreadLocal

不建议用 Java 式 `ThreadLocal` 作为主机制。

原因：

- Agent runtime 是 async workflow，`threading.local()` 不能隔离同线程内的多个 async task。
- 工具执行会经过 `asyncio.to_thread`，`ContextVar` 更符合 Python 对 async 上下文传播的标准语义。
- Langfuse v4 基于 OpenTelemetry，优先使用 OTel 当前上下文，不应自建一套平行追踪上下文。

推荐策略：

1. 主链路使用 Langfuse / OTel 当前上下文。
2. 必要时用 `contextvars.ContextVar` 保存当前 `trace_id` 作为兜底。
3. 对关键关联字段继续显式传递，例如已有的 `langfuse_trace_id`。
4. 只有在同步线程池内部确实存在独立线程局部状态需求时，才局部使用 `threading.local()`。

## 四、分阶段实施计划

### 阶段 1：先保证观测失败不影响执行

改动：

- 修复 `LangfuseToolTraceRecorder.span()` 的 contextmanager 异常边界。
- 修复 `turn_trace()` 的 flush / exit 异常边界。
- 修复 root span 部分进入失败时的清理。
- 修复 `CallbackHandler` 构造失败时的上下文清理。
- 新增 `build_tool_trace_recorder()`，`runner.py` 改用安全工厂。
- runner 侧对 `recorder.flush()` 做最终兜底，即使未来 recorder 实现不小心抛异常，也不得影响成功 turn。
- `core/observability/__init__.py` 导出安全工厂，runner 不再直接 import 具体 Langfuse recorder 类。

验收：

- Langfuse span 创建失败，工具仍执行。
- 工具执行自身抛异常时，不产生 `generator didn't stop after throw()`。
- 工具执行自身抛异常时，Langfuse span exit 能收到原异常上下文。
- `client.flush()` 抛异常时，turn 不被标记 failed。
- `recorder.flush()` 抛异常时，turn 不被标记 failed。
- context exit 抛异常时，turn 不被标记 failed。
- `CallbackHandler` 构造失败时，已进入的 Langfuse context 被清理，turn 继续执行。

### 阶段 2：补齐脱敏边界

改动：

- 新增 `langfuse_payload_sanitizer.py`。
- 工具输入 arguments 上报前脱敏。
- 工具 output 全字段脱敏。
- error 与 status_message 脱敏。
- LLM generation input/output 接入 SDK masking；若无法确认覆盖，则禁用原文 capture，只保留非敏感元数据。

验收：

- `api_key`、`token`、`password`、`Authorization`、`Cookie` 等字段不会进入 Langfuse payload。
- 嵌套 dict/list 中的敏感字段也会被脱敏。
- 非敏感结构化字段仍保留，便于排查。
- LLM prompt/completion 中的敏感字段不会以原文进入 Langfuse；如果无法脱敏，则 Langfuse 不捕获原文 prompt/completion。

### 阶段 3：锁定 trace 归属

改动：

- 增加测试覆盖 `asyncio.to_thread` 下 tool span 是否挂到当前 turn trace。
- 如果测试失败，再引入显式 trace context 传递，不先做假设性大改。

验收：

- 一个 turn 内的 LLM generation 与 tool observation 能归入同一个 trace。
- tool observation 至少能通过 `metadata.step_id`、`tool_call_id` 与 runtime event 对齐。

### 阶段 4：扩展完整 Agent 执行观测

改动：

- 增加 runtime event 到 Langfuse 的轻量映射层。
- 优先记录关键状态事件：run_started、step_started、model_requested、tool_call_started、tool_call_finished、human_input_required、run_cancelled、run_failed、final_response。
- checkpoint、context compaction 可作为后续细化，不阻塞前面稳定性修复。
- Langfuse runtime event 投影必须独立于本地 `runtime_events` 落库与 SSE 投递，投影失败只能记录日志。

验收：

- Langfuse trace 能按时间线复盘一次 turn 的主要阶段。
- 本地 `runtime_events` 仍是事实源，Langfuse 只是外部观测投影。
- Langfuse 上报失败不影响 `runtime_events` 落库与 SSE。

## 五、测试计划

### 单元测试

新增或扩展：

- `tests/test_langfuse_tracing_trace_id_format.py`
- `tests/test_langfuse_tool_trace_recorder.py`
- `tests/test_langfuse_payload_sanitizer.py`

测试项：

1. tracing disabled 时不 import Langfuse，不产生 callbacks。
2. 缺 key 时降级，warning 不重复刷屏。
3. `turn_trace()` 构造失败时 yield 空结果。
4. root span 进入成功、attributes 进入失败时会清理 root span。
5. root span / attributes 进入成功、`CallbackHandler` 构造失败时会清理已进入 context 并降级。
6. `turn_trace()` flush 失败不向上传播。
7. `turn_trace()` exit 失败不向上传播。
8. 工具 span 创建失败时返回 Null span，工具执行继续。
9. 工具执行抛异常时异常语义不被 recorder 改写，且 span exit 收到原异常上下文。
10. `adapter.finalize()` 抛异常时只记录日志。
11. `recorder.flush()` 抛异常时 runner 不把成功 turn 标记 failed。
12. Langfuse payload sanitizer 递归脱敏。
13. sanitizer 覆盖大小写混合 key、嵌套 list/dict、非 JSON 类型、异常对象字符串化和超长内容。
14. LLM generation input/output 要么经 sanitizer 后上报，要么被禁用原文捕获。

### 集成测试

测试项：

1. `ToolExecutionService` 注入 fake recorder，断言每个 call 有 span/record。
2. `asyncio.to_thread` 工具执行路径下，fake Langfuse/OTel 上下文能读到同一 trace。
3. `AgentRuntime.run_agent()` 中 recorder 构造失败不会导致 RUN_FAILED。
4. `AgentRuntime.run_agent()` 中 recorder flush 失败不会导致 RUN_FAILED。
5. Langfuse runtime event 投影失败时，`runtime_events` 落库和 SSE 投递仍正常。

### 手工验收

在配置真实 Langfuse 后跑一个包含模型调用、读文件、命令执行、写文件的 turn，检查：

- Langfuse UI 中存在同一个 turn trace。
- generation 和 tool observation 可关联到同一 turn。
- tool output 不含敏感字段原文。
- 断网或 Langfuse 服务不可达时，Agent turn 正常完成，本地日志记录上报失败。

## 六、完成标准

修复完成必须同时满足：

1. 所有新增/受影响测试通过。
2. `ruff` / `mypy` 按项目现有命令通过。
3. Langfuse 不可用时，Agent 执行行为与未集成前一致。
4. Langfuse 可用时，至少能观测 turn、LLM generation、tool observation。
5. 外部观测 payload 经过统一脱敏。
6. 审查 Agent 确认文档方案与第零铁律一致，且没有明显遗漏的高风险点。

## 七、暂不做事项

- 不自研完整 tracing 框架。
- 不用 `threading.local()` 替代 OTel/contextvars。
- 不把 Langfuse 依赖扩散到 service、tools、api 业务代码。
- 不在第一阶段强行把 tool span 嵌到对应 generation span 下；先用 `step_id` 和 `tool_call_id` 建立可追踪关联。
- 不把 Langfuse 当作事实源；事实源仍是本地 `runtime_events`、日志和数据库。
