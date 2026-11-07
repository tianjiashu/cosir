"""工具呈现类别的无依赖契约；执行准入与呈现方式相互独立。"""

from typing import Literal

ToolPresentation = Literal["activity", "standalone", "quiet"]
