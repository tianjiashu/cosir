"""读截断标记的测试辅助：完整原文的落盘路径只写在模型可见正文的截断标记里（``ToolOutputBudget``）。"""

import re

_LOCATION = re.compile(r"full output: (.+?)\] \.\.\.")


def truncated_artifact_path(content: str | None) -> str:
    """截断标记里完整原文的落盘路径；没截断或未落盘（``full output unavailable``）时为空串。"""

    match = _LOCATION.search(content or "")
    return match.group(1) if match else ""
