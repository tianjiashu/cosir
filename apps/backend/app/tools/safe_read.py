"""限定在配置好的项目根目录内的安全只读工具。"""

from pathlib import Path
from typing import List

from app.tools.types import ToolDefinition


class SafeReadTools:
    """为项目根目录创建安全的只读文件工具。"""

    def __init__(self, project_root: Path) -> None:
        """使用根目录初始化安全读取工具。

        参数:
            project_root: 所有文件操作都必须停留于其内的目录。

        返回:
            无。

        异常:
            OSError: 如果项目根目录无法被解析。

        副作用:
            解析项目根目录路径。
        """

        self._project_root = project_root.resolve()

    def definitions(self) -> List[ToolDefinition]:
        """返回内置的安全只读工具定义。

        参数:
            无。

        返回:
            用于 read_file、list_directory 和 search_text 的工具定义。

        异常:
            无。

        副作用:
            无。
        """

        return [
            ToolDefinition(
                name="read_file",
                description="Read a UTF-8 text file inside the project root.",
                permission="safe_read",
                required_params=("path",),
                handler=self.read_file,
                parameters_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
            ),
            ToolDefinition(
                name="list_directory",
                description="List entries in a directory inside the project root.",
                permission="safe_read",
                required_params=("path",),
                handler=self.list_directory,
                parameters_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
            ),
            ToolDefinition(
                name="search_text",
                description="Search for text in UTF-8 files inside the project root.",
                permission="safe_read",
                required_params=("query",),
                handler=self.search_text,
                parameters_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
            ),
        ]

    def read_file(self, path: str) -> str:
        """读取项目根目录内的一个 UTF-8 文本文件。

        参数:
            path: 项目根目录内某文件的相对路径。

        返回:
            文件内容。

        异常:
            ValueError: 如果解析后的路径逃逸出项目根目录。
            IsADirectoryError: 如果路径指向一个目录。
            OSError: 如果文件无法被读取。

        副作用:
            从磁盘读取一个文件。
        """

        file_path = self._resolve_inside_root(path)
        if file_path.is_dir():
            raise IsADirectoryError(path)
        return file_path.read_text(encoding="utf-8")

    def list_directory(self, path: str = ".") -> str:
        """列出项目根目录内某目录的直接条目。

        参数:
            path: 项目根目录内的相对目录路径。

        返回:
            以换行分隔的、已排序的目录条目。

        异常:
            ValueError: 如果解析后的路径逃逸出项目根目录。
            NotADirectoryError: 如果路径不是一个目录。
            OSError: 如果目录无法被读取。

        副作用:
            从磁盘读取目录元数据。
        """

        directory = self._resolve_inside_root(path)
        if not directory.is_dir():
            raise NotADirectoryError(path)
        return "\n".join(sorted(child.name for child in directory.iterdir()))

    def search_text(self, query: str) -> str:
        """在项目根目录下的 UTF-8 文件中搜索文本。

        参数:
            query: 待搜索的文本查询。

        返回:
            以 ``path:line`` 格式、最多 50 行呈现的、以换行分隔的匹配结果。

        异常:
            ValueError: 如果查询为空。

        副作用:
            从磁盘读取文本文件。
        """

        if not query:
            raise ValueError("query must not be blank")

        matches: List[str] = []
        for file_path in self._project_root.rglob("*"):
            if len(matches) >= 50:
                break
            if not file_path.is_file() or ".git" in file_path.parts:
                continue
            try:
                for line_number, line in enumerate(
                    file_path.read_text(encoding="utf-8").splitlines(),
                    start=1,
                ):
                    if query in line:
                        relative = file_path.relative_to(self._project_root)
                        matches.append(f"{relative}:{line_number}: {line}")
                        break
            except UnicodeDecodeError:
                continue
        return "\n".join(matches)

    def _resolve_inside_root(self, path: str) -> Path:
        """解析相对路径并确保其停留在项目根目录内。

        参数:
            path: 用户或模型提供的相对路径。

        返回:
            项目根目录内的、解析后的绝对路径。

        异常:
            ValueError: 如果解析后的路径逃逸出项目根目录。

        副作用:
            解析文件系统路径元数据。
        """

        resolved = (self._project_root / path).resolve()
        if self._project_root not in (resolved, *resolved.parents):
            raise ValueError("path escapes project root")
        return resolved
