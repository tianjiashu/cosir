"""``tool_output_delta`` 事件 payload 值对象。"""

from app.models.payload.runtime_event_payload import RuntimeEventPayload


class ToolOutputDeltaPayload(RuntimeEventPayload):
    """工具执行过程中产生的输出增量片段，用于客户端实时滚动展示。

    当前仅 ``execute_terminal`` 产出：命令在子进程运行期间，采集侧按行把
    stdout/stderr 合并流经跨进程队列回传父进程，父进程轮询 drain 后经
    ``RuntimeEventBus`` 广播。

    与 ``FILE_CHANGE_UPDATED`` 同属**不持久化**的运行中实时广播通道：不写入
    ``runtime_events`` 表（逐行落库会放大写入量，且回放时与终态输出重复），
    因此刷新/重连后不重建滚动过程，最终完整输出以 ``tool_call_finished`` 为准。

    片段在**采集侧**已完成脱敏与输出预算截断，父进程只做转发，不再二次加工。

    参数:
        tool_call_id: 所属工具调用标识，前端据此把片段归并到对应工具卡片。
        step_id: 产生该工具调用的步骤标识。
        text: 本次增量的输出文本片段（已脱敏、已受预算约束）。
        truncated: 是否因触达输出预算而不再继续回传后续片段。
    """

    tool_call_id: str
    step_id: str
    text: str
    truncated: bool = False
