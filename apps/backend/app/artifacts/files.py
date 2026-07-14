"""运行产物文件系统写入。"""

from hashlib import sha256
from pathlib import Path
from uuid import uuid4


class ArtifactFileStore:
    """将 artifact 内容写入受控目录。"""

    def __init__(self, root_dir: Path) -> None:
        """初始化 artifact 文件存储。

        参数:
            root_dir: artifact 根目录。

        返回:
            无。

        异常:
            OSError: 如果目录无法创建。

        副作用:
            创建 artifact 根目录。
        """

        self._root_dir = root_dir
        self._root_dir.mkdir(parents=True, exist_ok=True)

    def write_text(self, kind: str, content: str) -> tuple[str, int, str]:
        """写入文本 artifact。

        参数:
            kind: artifact 类型，用作子目录名称。
            content: 待写入文本。

        返回:
            相对路径、字节数和 SHA-256。

        异常:
            OSError: 如果写入失败。

        副作用:
            向 artifact 目录写入一个 UTF-8 文本文件。
        """

        data = content.encode("utf-8")
        digest = sha256(data).hexdigest()
        directory = self._root_dir / kind
        directory.mkdir(parents=True, exist_ok=True)
        relative_path = Path(kind) / f"{uuid4()}.txt"
        path = self._root_dir / relative_path
        path.write_bytes(data)
        return str(relative_path), len(data), digest
