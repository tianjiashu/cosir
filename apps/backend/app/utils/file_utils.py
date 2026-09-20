"""文件 I/O 纯工具函数与常量。

单一职责：提供不依赖项目类型/模型的纯文件读写工具与文件相关常量。
不负责：路径解析、权限判定、业务逻辑。

模块级常量（如 ``IMAGE_EXTENSIONS``）属于跨层共享的公共契约事实，放在
leaf 层可被 ``api`` / ``core`` / ``service`` 任意层无循环依赖地引用，避免
在 ``conversation_run_state_service`` 与 ``vision_content_blocks`` 之间重复定义同一份扩展名集合。
"""

from pathlib import Path


def read_text_file(path: str | Path) -> str:
    """读取文本文件的全部内容。

    参数:
        path: 文件路径（字符串或 Path 对象）。

    返回:
        文件的 UTF-8 文本内容。

    异常:
        FileNotFoundError: 文件不存在。
        PermissionError: 无权读取文件。
        OSError: 其他 I/O 错误（如磁盘满、路径非法）。

    副作用:
        无。
    """
    return Path(path).read_text(encoding="utf-8")
