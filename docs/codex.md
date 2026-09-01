二、高危缺陷明细(需优先处理)
🔴 H1 — Checkpoint 重放导致 add_message 重复落库(维度 2)
根因:LangGraph interrupt() + Command(resume=) 恢复时重放节点 body。model_node(L325-327)和 tools_node 的 _persist_tool_observations 在节点函数体内调用 RuntimeContextManager.add_message,这是真实落库副作用,重放会重复执行 → turn_messages 表出现重复行 + 内存 messages 重复。 文件:model_node.py L325-327、tools_node.py L278-297 / L81。 修复方向:节点 body 必须幂等——落库前按 step_id/tool_call_id 判重跳过;或将「落库」从节点 body 移出,改由编排层在节点返回后统一持久化。

🔴 H2 — bind_turn 未覆盖 resume 重放路径(维度 2,与 H1 同源)
根因:bind_turn → _reset_message_sequence(清理 turn 残留)只在 run() 入口(L216)调一次。resume 重试复用同一 turn_id + 同一 manager 实例,未再次调 bind_turn。设计意图「同 turn_id resume 重试清理残留」实际未接入 resume 路径 → 结合 H1,残留持续累积。 文件:workflow.py L206-216(入口)、L255-325(while 循环 resume 分支无 bind_turn)。 修复方向:在 while 循环 resume 分支(Command(resume=) 之前)显式再调 bind_turn(turn);且清理必须在 graph 重放落库之前完成。

🔴 H3 — resume 值类型契约错配(维度 3)
根因:approval_resolver 类型签名是 Callable[[list[ToolCall]], list[ToolCall]],但 workflow.py L318-324 实际传 dict 列表(interrupt 载荷是 dict),tools_node L257 又对返回值调 ToolCall.from_dict(item) 当 dict 消费。契约靠「双方心照不宣用 dict」维持,类型签名是谎言;若 resolver 真返回 ToolCall 对象,from_dict 会因不可 .get 抛 AttributeError。 文件:workflow.py L318-324、runtime_config.py L52、tools_node.py L257。 修复方向:统一 approval_resolver 类型为 Callable[[list[dict]], list[dict]],或在 workflow/tools_node 之间明确一方负责 ToolCall ↔ dict 转换并加断言/校验。

🔴 H4 — SSE 断开兜底僵尸 turn 风险(维度 4)
根因:workflow.run() 主循环(L255-294)无 try/finally 在客户端断开时标 failed。producer.cancel() 级联取消 graph.astream 后,终态落定依赖更外层 runner.run_turn 兜底——但审查未读到 runner.run_turn 的 finally 实现,无法确认 running 中断开必然落 client_disconnected。若缺失,留下 running 僵尸 turn。 文件:workflow.py L255-294;runner.run_turn(待确认)。 修复方向:确认 runner.run_turn 在 CancelledError 路径有 try/finally 调 fail_turn_if_running(turn_id, end_reason="client_disconnected");或直接在 workflow.run() 主循环外层补 try/finally 双保险。

🔴 H5 — error_code 归一缺口(维度 6)
根因:RunFailedPayload 无 error_code 字段;runner.py L433-437 异常路径 RunFailedPayload(error=str(exc), end_reason=None) 裸异常字符串透传前端;各节点 RUN_FAILED 用手写自由字符串("invalid_model_output" 等)而非 ErrorKind 枚举,与已定义的 ErrorKind 体系不归一。 文件:run_failed_payload.py L29-42、runner.py L391-441、model_node.py L459、observation_node.py L205、finalize_max_steps.py L93。 修复方向:RunFailedPayload 增 error_code: str | None;引入 model_error_mapper 把异常归一为 ErrorKind 写入 error_code;各节点改用 ErrorKind 成员值。

🔴 H6 — 上下文压缩无兜底(维度 7)
根因:maybe_compact 的 compressor 默认 None 且全代码 0 处调用(search maybe_compact() 命中 0)。task 级 messages 跨 turn 只追加不裁剪,长期多 turn 任务内存无限增长。生产实际处于失效状态。 文件:runtime_context_manager.py L304-320。 修复方向:即使依赖压缩器,也应在 compressor is None 时补最小兜底(按 total_tokens 软上限裁剪最旧历史,保留 SystemMessage + 最近 N 轮)。

三、中/低危缺陷(摘要)
维度	问题	严重	文件/位置
2	tools_node 落库缺 call_id 幂等防护、resume 消费缺 call_id 完整性校验	中	tools_node.py L210/L257/L81
2	checkpoint state 与内存 RuntimeContextManager 重建不一致	中	workflow.py L238 / manager L151
3	字段残缺 dict 经 from_dict 兜底成空工具调用静默放行	中	tool_call.py L39-43 + tools_node.py L257
3	resume 缺 call_id 去重/幂等,重复 call_id 会重复执行	中	tools_node.py L257
3	审批被拒返回空列表,无拒绝原因反馈模型	中	tools_node.py L257-325
3	多工具 instruction 仅取 [0],被拒项 instruction 丢失	低	tools_node.py L262/L315
4	is_current_turn_cancelled 死代码(None 检查在解引用后,不可达)	中	runtime_operations.py L157-177
4	bind_turn 写 current_turn_id/total_tokens 未持 self.lock,与 add_message 跨锁	低	manager L195-218
6	continuation_error_data 在 terminal_state() 未置 None,可能残留穿透	低	common.py L202-242
7	model_node 无界 chunks 列表缓冲(长输出内存峰值)	中	model_node.py L169/L182-200
四、关键洞察
H1+H2 是同一根因的两个面:LangGraph 重放模型 + 节点 body 含副作用(落库)+ bind_turn 不覆盖 resume。修复必须同时处理「节点幂等」和「resume 路径清理」,否则单修一个无效。
维度 5(消息通道边界)符合——说明我们前几轮对 bind_turn / WeakValueDictionary / write_event 的修复是正确且稳固的;H2 是 bind_turn 的覆盖范围问题,不是 bind_turn 本身逻辑错。
维度 1(State 一致性)符合——state 契约、条件边时序、REPAIR 对称均无问题,这是 workflow 设计最稳固的部分。
H3/H5 属「契约/类型谎言」类缺陷——类型签名与实际运行期结构不符,短期不崩但长期是维护陷阱(违规第零铁律「长期稳定迭代」)。
五、建议的修复优先级
code
复制
插入
新建文件
保存
应用
P0(阻断性,先修):
  H1 + H2 — Checkpoint 重放幂等(节点 body 落库幂等 + resume 路径 bind_turn)
  H4     — SSE 断开僵尸 turn(确认/补 runner finally 兜底)

P1(高优先,正确性/可维护性):
  H3 — approval_resolver 类型契约归一
  H5 — error_code 归一(RunFailedPayload + ErrorKind + model_error_mapper)
  H6 — 上下文压缩最小兜底

P2(中低,逐步清理):
  各类 call_id 幂等/校验、审批被拒反馈、is_current_turn_cancelled 死代码、
  无界 chunks 缓冲、continuation_error_data 残留
六、待你决策
是否同意上述优先级?尤其 P0 的 H1+H2,修复涉及「把落库移出节点 body」或「按 call_id/step_id 判重」,这是架构级改动(第零铁律允许为长期可维护性做结构性改动),需要你拍板方向。
需要我先就 P0 的 H1+H2 出一份具体改造方案(不动码,只给设计),还是直接开独立开发 Agent 实施某个具体项?
H4 需要我先读 runner.run_turn 确认僵尸 turn 风险是否真实存在(审查未读到该实现),再决定是否修?
按规则,任何修复实施后都需重新启动独立审查 + 测试 Agent 闭环。需要我继续推进哪一步?