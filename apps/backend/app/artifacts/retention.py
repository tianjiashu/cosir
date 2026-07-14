"""运行产物保留策略。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ArtifactRetentionPolicy:
    """描述 artifact 保留策略。

    参数:
        max_age_days: 最大保留天数。
        max_total_bytes: 最大总字节数。

    返回:
        不可变保留策略。

    异常:
        无。

    副作用:
        无。
    """

    max_age_days: int = 30
    max_total_bytes: int = 1024 * 1024 * 500
