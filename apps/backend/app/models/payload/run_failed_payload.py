"""Payload model for run_failed events."""

from typing import Any, Literal

from app.models.payload.runtime_event_payload import RuntimeEventPayload


class RunFailedPayload(RuntimeEventPayload):
    """运行失败事件 payload。

    失败路径必须让前端 / 排查侧能拿到本轮已消耗的 token（符合「可排查日志」铁律：
    异常路径须带上下文）。token 以扁平字段表示：

    - ``input_tokens`` / ``output_tokens`` / ``total_tokens`` / ``cache_hit_tokens`` /
      ``cache_miss_tokens`` / ``reasoning_tokens``：直接供前端 ``StatusBadge`` 渲染，
      与 :class:`RunFinishedPayload` / :class:`RunCancelledPayload` 字段对齐，
      保证完成 / 失败 / 取消三态渲染逻辑统一。

    不使用嵌套 ``usage`` 字段：三态 payload 统一为扁平字段，避免前端消费判断歧义与
    数据结构重复；后端排查侧直接从 ``rc.usage_stats.to_dict()`` 取精确聚合，不依赖 payload。

    并非所有失败分支都能拿到 ``usage_stats``（如 ``runner.py`` / ``turns_api.py`` 等无上下文的
    进程级异常、客户端断开），故扁平字段允许为默认值 0。

    取消分支**不发** ``RUN_FAILED`` 终态事件（避免与取消流的其它信号重复），其 token 改由
    ``RunCancelledPayload`` 携带。
    """

    error: str
    status: Literal["failed"] | None = None
    end_reason: str | None = None
    # end_reason 为语义化枚举码，用于前端区分失败性质：
    #   - "client_disconnected"：SSE 客户端断开导致 run 中止（turn_stream_service 兜底路径）。
    #   - None（默认）：真执行失败，无特定语义原因（runner 异常分支）。
    # 注意：不要把自由文本异常塞进本字段，否则前端分支判断不可枚举、排查困难。
    message: str | None = None
    step_id: str | None = None
    requested_agent_id: str | None = None
    task_agent_id: str | None = None
    tool_name: str | None = None
    langfuse_trace_id: str | None = None
    data: dict[str, Any] | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    reasoning_tokens: int = 0
    # 错误细分码（设计文档阶段 1.5 引入，与 ``ErrorKind`` 枚举对齐）。
    # ``None`` 表示未细分（普通 RUN_FAILED）；``"MODEL_NOT_CONFIGURED"`` 表示
    # 模型未配置（``agent.model_name`` 为 None 或解析失败），前端据此显示
    # 引导文案与「前往配置」入口。本期仅此一项细分，其余细分见阶段 4。
    error_code: str | None = None
    # 面向用户的修复指引文案（与 ``error_code`` 配对，由异常 mapper 注入）。
    # 为 ``None`` 表示无指引文案（普通 RUN_FAILED 路径）。
    guidance: str | None = None
