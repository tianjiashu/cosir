"""Assistant Transport wire schema。

本模块只描述桌面端与后端之间的 Assistant UI request 请求结构，不依赖领域模型、
LangGraph 或 storage。wire 字段遵循 assistant-ui 的 camelCase 约定；进入 service 层
后由 API 边界映射为后端 snake_case 领域参数。
"""

import json
from hashlib import sha256
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.assistant_transport.request.command.add_message_command import AddMessageCommand
from app.assistant_transport.request.command.custom_command import CustomCommand

AssistantCommand = Annotated[
    AddMessageCommand | CustomCommand,
    Field(discriminator="type"),
]


class AssistantTransportRequest(BaseModel):
    """校验 Assistant Transport 请求。

    命令数组由服务端按顺序幂等处理；对话历史由 canonical facts 重建。
    """

    # Transport 请求只接受当前契约字段；旧的状态游标不得再被静默吞掉。
    model_config = ConfigDict(extra="forbid")

    commands: list[AssistantCommand] = Field(min_length=0, max_length=32)
    # 已有 task 使用 ``task-{taskId}``；新对话尚无 task/thread 身份，使用
    # workspaceId 作为创建目标，响应头返回新 task id 后再进入同一 runtime。
    threadId: str = Field(pattern=r"^task-[1-9][0-9]*$")
    taskId: int = Field(ge=1)
    workspaceId: int | None = Field(default=None, ge=1)
    providerId: int | None = Field(default=None, ge=1)
    modelName: str | None = None
    reasoningEffort: str | None = Field(default=None)
    # 空 commands 用于恢复指定 run；编辑重跑的 add-message 也必须携带当前 runId。
    runId: int | None = Field(default=None)

    # Assistant UI 会在请求根部附带这些通用 runtime 字段。它们属于兼容性 envelope：
    # 当前后端不使用、不持久化，但显式声明后可以安全接收未来版本的客户端请求；
    # ``extra=forbid`` 仍会拦截真正未知的字段。命令内部的 parentId 则是消息关系，
    # 由 Assistant UI 命令协议保留，不能与这里的根级 parentId 混淆。
    parentId: str | None = None
    state: object | None = None
    system: str | None = None
    tools: dict[str, object] | None = None
    callSettings: dict[str, object] | None = None
    config: dict[str, object] | None = None

    @model_validator(mode="after")
    def validate_transport_constraints(self) -> "AssistantTransportRequest":
        """在模型构造后自动校验全部纯 wire 契约约束，不涉及领域持久化状态。

        本校验器由 Pydantic 在请求解析阶段自动触发，覆盖所有可在解析期判定的结构
        性约束，与领域 service 的运行期校验（task 是否存在、command 幂等冲突、run
        占用状态）严格分离：

        - ``reasoningEffort`` 必须在允许集合 ``{low, high, max}`` 内；
        - ``commands`` 内 ``commandId`` 必须唯一；
        - ``threadId`` 必须与 ``task-{taskId}`` 一致，二者是同一领域身份的两种表达；
        - 一次请求最多包含一个 ``add-message`` 命令（首版运行模型不支持批量消息）；
        - ``custom`` 命令尚未绑定领域处理器，直接拒绝；
        - 空命令必须携带 ``runId`` 用于恢复已有 run；add-message 是否重放只由
          ``runId`` 是否存在决定，不能由 ``sourceId`` 推导；
        - 含 ``add-message`` 时 ``providerId`` 与 ``modelName`` 必填且 ``modelName``
          非空（启动对话必须确定执行上下文，原 service 内的同等校验已前移至此）；
        - 新建对话（``taskId is None``）必须提供 ``workspaceId`` 作为创建目标。

        参数:
            无；约束字段直接取自当前请求模型。

        返回:
            当前校验通过的 ``AssistantTransportRequest`` 实例（满足 Pydantic
            ``model_validator(mode="after")`` 的契约要求）。

        异常:
            TransportRequestError: 当上述任一纯 wire 约束不满足时抛出，携带稳定的
                机器码、面向用户的安全提示与可重试标记；由全局 exception_handler
                映射为统一的 Assistant Transport HTTP 错误体。

        副作用:
            无；仅做只读断言，不触碰 storage 或 service。
        """
        allowed_effort = {"low", "high", "max"}
        if self.reasoningEffort is not None and self.reasoningEffort not in allowed_effort:
            raise TransportRequestError(
                status_code=400,
                code="REASONING_EFFORT_INVALID",
                message=f"reasoningEffort 必须是 {sorted(allowed_effort)} 之一",
                retryable=False,
            )
        if len({command.commandId for command in self.commands}) != len(self.commands):
            raise TransportRequestError(
                status_code=400,
                code="DUPLICATE_COMMAND_ID",
                message="commands 内 commandId 必须唯一",
                retryable=False,
            )
        if self.threadId != f"task-{self.taskId}":
            raise TransportRequestError(
                status_code=409,
                code="THREAD_TASK_MISMATCH",
                message="threadId 与 taskId 不一致",
                retryable=False,
            )
        message_count = sum(
            1 for command in self.commands if isinstance(command, AddMessageCommand)
        )
        if message_count > 1:
            raise TransportRequestError(
                status_code=400,
                code="MULTIPLE_ADD_MESSAGES_UNSUPPORTED",
                message="一次请求最多包含一个 add-message 命令",
                retryable=False,
            )
        if any(command.type == "custom" for command in self.commands):
            raise TransportRequestError(
                status_code=400,
                code="CUSTOM_COMMAND_UNSUPPORTED",
                message="custom 命令尚未绑定领域处理器",
                retryable=False,
            )
        # 首版运行模型在启动对话时必须同时确定厂商与模型，二者构成执行上下文；
        # 缺失其一会让 Turn 无法绑定执行器，属纯 wire 契约约束，前移至此。
        has_message = any(isinstance(command, AddMessageCommand) for command in self.commands)
        if not has_message and self.runId is None:
            raise TransportRequestError(
                status_code=400,
                code="RUN_ID_REQUIRED",
                message="没有新消息时必须提供 runId 以继续已有运行",
                retryable=False,
            )
        if has_message and (
            self.providerId is None or self.modelName is None or not self.modelName.strip()
        ):
            raise TransportRequestError(
                status_code=400,
                code="MODEL_SELECTION_REQUIRED",
                message="启动对话必须同时提供 providerId 与 modelName",
                retryable=False,
            )
        return self

    def payload_hash(self) -> str:
        """计算当前 add-message 请求的稳定业务载荷指纹。

        参数:
            无；载荷字段直接取自当前请求模型。

        返回:
            SHA-256 十六进制字符串。

        异常:
            无；请求字段已由 Pydantic 完成校验。

        副作用:
            无。

        说明:
            ``commandId`` 是幂等身份，``taskId`` / ``workspaceId`` 是路由身份，``threadId`` 是
            Transport 元数据，不参与载荷 hash。消息、run 操作身份和模型选择会改变实际
            执行语义，必须参与 hash；Assistant UI 的 ``parentId``/``sourceId`` 只属于
            编辑元数据，不参与领域幂等指纹。
        """
        payload = {
            "commands": [
                command.model_dump(
                    mode="json", exclude={"commandId", "parentId", "sourceId"}
                )
                for command in self.commands
            ],
            "providerId": self.providerId,
            "modelName": self.modelName,
            "reasoningEffort": self.reasoningEffort,
            "runId": self.runId,
        }
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return sha256(serialized.encode("utf-8")).hexdigest()


class TransportRequestError(Exception):
    """纯 wire 契约校验失败的结构化异常。

    由 ``AssistantTransportRequest.validate_transport_constraints``（``model_validator``）
    在请求解析阶段自动抛出，仅描述与领域持久化无关的结构性约束违反，覆盖：推理深度取值、
    commandId 唯一性、thread/task 一致性、add-message 数量上限与未支持命令类型。
    API 适配层（``app/app.py`` 的全局 exception_handler）负责把它映射为统一的 Assistant
    Transport HTTP 错误体，保持与 ``_raise_transport_error`` 相同的 ``code`` / ``message`` /
    ``retryable`` 形状，使前端 ``TransportError`` 契约零改动。
    """

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        retryable: bool,
    ) -> None:
        """构造结构化请求校验异常。

        参数:
            status_code: 映射后的 HTTP 状态码。
            code: 稳定的机器可读错误码，与后端错误体 ``error.code`` 对齐。
            message: 面向用户的安全提示，不含密钥或异常堆栈。
            retryable: 客户端是否可在修正条件后重试。

        返回:
            无。

        异常:
            无。

        副作用:
            无；仅初始化不可变错误上下文。
        """
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable
