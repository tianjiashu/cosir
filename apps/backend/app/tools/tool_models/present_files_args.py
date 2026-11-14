"""present_files 参数校验模型；字段 description 会进入模型可见的工具 schema。"""

from pydantic import BaseModel, ConfigDict, Field

# 一次最多呈现几个：卡片是「成果」的清单，多了就退化成文件操作记录。
PRESENT_FILES_MAX = 5


class PresentedFile(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    path: str = Field(
        min_length=1,
        description=(
            "Path of the file to show, relative to the workspace root, or absolute for a file "
            "outside it; '~' is not expanded."
        ),
    )
    caption: str | None = Field(
        default=None,
        description=(
            'One line of context shown under the file name, e.g. "the failing case is row 42". '
            "Pass null if the file speaks for itself."
        ),
    )


class PresentFilesArgs(BaseModel):
    """定义 present_files 的参数约束；字段说明面向调用该工具的 Agent。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    files: list[PresentedFile] = Field(
        min_length=1,
        max_length=PRESENT_FILES_MAX,
        description=f"Files to show to the user (at most {PRESENT_FILES_MAX}).",
    )
