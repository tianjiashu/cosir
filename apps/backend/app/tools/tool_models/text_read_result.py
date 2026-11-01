from dataclasses import dataclass


@dataclass(frozen=True)
class TextReadResult:
    """read_file 内部文本读取结果。

    字段:
        content: 已加页头、行号与页尾，可直接返回给模型的文本。
        total_lines: 文件总行数（读取扫到文件末尾，不是已扫描行数）。
        end_line: 本页实际返回的最后一行号；没有返回内容时为 None。
        file_size: 文件字节大小，读取失败时为 0。
        next_offset: 如果还有后续内容，下一次读取建议使用的 offset；读到文件尾为 None。
        truncated: 本页是否因字符预算而提前停止；分页到 limit 不算截断。
        error: 面向模型的失败文本（错误事实 + 修正建议），原样作为 ``tool_error`` 的
            ``content``；非空表示读取失败。
    """

    content: str = ""
    total_lines: int = 0
    end_line: int | None = None
    file_size: int = 0
    next_offset: int | None = None
    truncated: bool = False
    error: str = ""
