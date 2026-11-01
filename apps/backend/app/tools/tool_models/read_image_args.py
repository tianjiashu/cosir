"""read_image 参数校验模型；字段 description 会进入模型可见的工具 schema。"""

from pydantic import BaseModel, ConfigDict, Field

# 一次调用最多读几张：每张都会随结果进入下一次模型请求，数量直接决定请求体积。
READ_IMAGE_MAX_PATHS = 10


class ReadImageArgs(BaseModel):
    """定义 read_image 的参数约束；字段说明面向调用该工具的 Agent。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    paths: list[str] = Field(
        min_length=1,
        max_length=READ_IMAGE_MAX_PATHS,
        description=(
            f"Image files to look at (at most {READ_IMAGE_MAX_PATHS}). Pass paths relative to the "
            "workspace root, or absolute paths for files outside it; '~' is not expanded."
        ),
    )
