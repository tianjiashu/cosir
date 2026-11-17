"""文件类型词表：文件面板的 ``FileInfo.kind`` 与交付记录的 ``kind`` 共用这一份。

判定规则只在 ``service/workspace_files/file_classification``；词表放在 models 层，是因为交付记录
（持久化契约）也要引用它，而 models 不依赖 service。新增类型只改这里、判定处与前端 ``FILE_KINDS``
（两边一致由 ``tests/test_file_kind_guard.py`` 守着）。
"""

from typing import Literal

FileKind = Literal[
    "text", "markdown", "table", "mermaid", "image", "svg", "pdf", "audio", "video", "binary"
]
