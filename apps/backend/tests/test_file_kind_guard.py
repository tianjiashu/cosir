"""文件类型词表守卫：后端 ``FileKind`` 与前端 ``FILE_KINDS`` 是同一份词表，两边手写，漏改一边就在这里失败。

前端按 ``kind`` 分发预览器（``lib/workspace-files/file-preview-mode.ts`` 以 ``Record<FileKind, …>`` 穷举），
后端多出一种前端不认识的类型会落到错误的预览器上，而 TS 编译查不出来。
"""

import re
from pathlib import Path
from typing import get_args

from app.models.enums.file_kind import FileKind

_FRONTEND = Path("../desktop/lib/api/workspace-files.ts")


def test_frontend_file_kinds_match_backend() -> None:
    match = re.search(
        r"export const FILE_KINDS = \[([^\]]*)\] as const;", _FRONTEND.read_text(encoding="utf-8")
    )
    assert match, "前端 FILE_KINDS 的写法变了，守卫需要同步"
    frontend = re.findall(r'"([a-z]+)"', match.group(1))
    assert frontend == list(get_args(FileKind))
