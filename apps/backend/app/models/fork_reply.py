"""手动分叉选中的那条回复。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ForkReply:
    """回复的领域身份，wire id 只在 Transport 边界解码成它（见 ``message_identity``）。

    回复要么跟在一条输入后面，要么在 Run 开头、前面没有输入（唤醒、结果续跑），二者恰有一个。
    """

    input_message_id: int | None = None
    run_id: int | None = None
