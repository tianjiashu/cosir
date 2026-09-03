from dataclasses import dataclass


@dataclass(frozen=True)
class TextReadResult:
    """read_file 内部文本读取结果。

    字段:
        content: 已加行号、可直接返回给模型的文本。
        total_lines: 已扫描到的行数。因为读取是流式的，提前停止时它表示已扫描行数。
        file_size: 文件字节大小，读取失败时为 0。
        next_offset: 如果还有后续内容，下一次读取建议使用的 offset。
        hint: 面向模型的继续读取提示。
        error: 错误文本，非空表示读取失败。
        reason: 面向模型的失败说明富文本（根因 + 可操作修正建议），与
            :class:`ToolObservation.reason` 同源语义；非空表示读取失败。
        retryable: 错误是否适合稍后重试。
    """

    content: str = ""
    total_lines: int = 0
    file_size: int = 0
    next_offset: int | None = None
    hint: str = ""
    error: str = ""
    reason: str = ""
    retryable: bool = False
