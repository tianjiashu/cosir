"""终端卡片输出的裁剪规则：卡片只保留原始输出的最近一段。

后端收集器（``app/tools``）与 snapshot 投影（``assistant_transport``）都要用同一份规则，所以放在
两者都能依赖的 ``core/tool``。完整输出在命令的日志文件里；卡片与实时预览只带最近一段，避免重连
首帧与数据库行随长时运行的输出无限增长。
"""

from __future__ import annotations

from app.config.constant import Constant


def trim_output_window(text: str) -> tuple[str, int]:
    """把卡片输出裁到最近一段，返回 (保留的文本, 这次裁掉的行数)。

    未超过窗口的 1.25 倍时原样返回 ``(text, 0)``：多留四分之一是为了不在每个输出块都触发裁剪，
    每次裁剪都要整段重发卡片。裁剪优先落在整行边界上；最后一段是一整行超长输出时从窗口起点硬切，
    硬切可能切在控制序列中间，终端模拟器能容忍。
    """
    window = Constant.Terminal.DISPLAY_OUTPUT_WINDOW_CHARS
    if len(text) <= window + window // 4:
        return text, 0
    cut = len(text) - window
    newline = text.find("\n", cut)
    if 0 <= newline < len(text) - 1:
        dropped = text[: newline + 1]
        return text[newline + 1 :], dropped.count("\n")
    dropped = text[:cut]
    return text[cut:], dropped.count("\n")
