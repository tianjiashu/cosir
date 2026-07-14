"""通用人工输入请求与响应记录。"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class HumanInputRequestRecord:
    """表示一次等待用户补充信息的请求。

    参数:
        request_id: 人工输入请求标识符。
        run_id: 所属运行标识符。
        step_id: 关联步骤标识符。
        prompt: 展示给用户的问题或说明。
        schema: 期望响应结构。
        status: 请求状态。
        created_at: 创建时间。
        responded_at: 响应时间。

    返回:
        不可变人工输入请求记录。

    异常:
        无。

    副作用:
        无。
    """

    request_id: str
    run_id: str
    step_id: Optional[str]
    prompt: str
    schema: Dict[str, Any]
    status: str
    created_at: datetime
    responded_at: Optional[datetime]

    def to_dict(self) -> Dict[str, Any]:
        """转换为 API 可序列化字典。

        参数:
            无。

        返回:
            人工输入请求字典。

        异常:
            无。

        副作用:
            无。
        """

        return {
            "request_id": self.request_id,
            "run_id": self.run_id,
            "step_id": self.step_id,
            "prompt": self.prompt,
            "schema": self.schema,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "responded_at": self.responded_at.isoformat() if self.responded_at else None,
        }


@dataclass(frozen=True)
class HumanInputResponseRecord:
    """表示用户对人工输入请求的响应。

    参数:
        response_id: 响应标识符。
        request_id: 关联请求标识符。
        response: 用户响应载荷。
        idempotency_key: 幂等键。
        created_at: 响应创建时间。

    返回:
        不可变人工输入响应记录。

    异常:
        无。

    副作用:
        无。
    """

    response_id: str
    request_id: str
    response: Dict[str, Any]
    idempotency_key: str
    created_at: datetime
