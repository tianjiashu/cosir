"""行号污染检测：识别把 ``read_file`` 输出的 ``N| `` 行号前缀整体回写的内容。

供 write/edit/apply 落盘前拒绝；与原子写分开，因为它依赖 ``read_file`` 的输出格式，
属于文件工具自身的约定。
"""

import re


def looks_like_line_numbered(content: str, threshold: float = 0.5) -> bool:
    """判断内容是否像把 ``read_file`` 的带行号输出整体回写（行号污染）。

    参数:
        content: 待检测文本。
        threshold: 命中行号前缀的行占比阈值，超过则判定为行号污染。

    返回:
        True 表示内容疑似行号污染，应当拒绝落盘。

    异常:
        无。

    副作用:
        无。
    """

    if not content:
        return False
    pattern = re.compile(r"^\d+\| ")
    lines = content.split("\n")
    if not lines:
        return False
    counted = sum(1 for line in lines if pattern.match(line))
    return (counted / len(lines)) >= threshold
