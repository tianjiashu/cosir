from pydantic import BaseModel

from app.models.turn_record import TurnRecord
from app.utils.datetime_utils import to_text


class TurnResponse(BaseModel):
    """校验并序列化轮次状态响应（与 ``TurnRecord`` 字段对齐）。

    参数:
        turn_id: 轮次标识。
        task_id: 所属任务标识。
        input_text: 本轮用户输入。
        status: 轮次状态。
        end_reason: 终态原因，可能为 None。
        response_text: Agent 回复文本，可能为 None。
        agent_id: 驱动该轮次的 agent 标识，可能为 None。
        created_at: 创建时间文本。
        updated_at: 更新时间文本。

    返回:
        Pydantic 响应模型。

    异常:
        无。

    副作用:
        无。
    """

    turn_id: str
    task_id: str
    input_text: str
    status: str
    end_reason: str | None = None
    response_text: str | None = None
    agent_id: str | None = None
    created_at: str
    updated_at: str

    @classmethod
    def from_record(cls, record: TurnRecord) -> "TurnResponse":
        """从 ``TurnRecord`` 值对象构造响应模型。

        把领域值对象前向映射为 API 响应模型，避免 ``**to_dict()`` 因字典值类型
        被推断为 ``str | None`` 而与必填 ``str`` 字段冲突（mypy 报错），同时消除
        重复的字段拆解逻辑。

        参数:
            record: 待转换的轮次记录。

        返回:
            与记录字段对齐的 ``TurnResponse`` 实例。

        异常:
            无。

        副作用:
            无。
        """

        return cls(
            turn_id=record.turn_id,
            task_id=record.task_id,
            input_text=record.input_text,
            status=record.status,
            end_reason=record.end_reason,
            response_text=record.response_text,
            agent_id=record.agent_id,
            created_at=to_text(record.created_at),
            updated_at=to_text(record.updated_at),
        )
