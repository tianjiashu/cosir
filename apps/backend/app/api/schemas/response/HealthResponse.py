"""后端健康状态响应模型。"""

from pydantic import BaseModel


class HealthResponse(BaseModel):
    """后端存活探针响应。

    当前为最小占位实现：仅携带 ``status`` 字段标识进程存活，不探测子系统就绪态。
    真实探针（聚合 storage / 模型配置中心）为后续 TODO，
    届时将扩展字段并产出不含 secret 明文的健康摘要。

    参数:
        status: 健康状态标识，占位实现固定为 ``"health"``。

    返回:
        Pydantic 响应模型。

    异常:
        无。

    副作用:
        无。
    """

    status: str
