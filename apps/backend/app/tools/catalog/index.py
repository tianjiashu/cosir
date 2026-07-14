"""工具目录的内存索引。"""

from typing import Iterable

from app.tools.catalog.records import ToolCatalogRecord


class ToolCatalogIndex:
    """按工具名保存目录记录并防止重复。"""

    def __init__(self, records: Iterable[ToolCatalogRecord] = ()) -> None:
        """初始化目录索引。

        参数:
            records: 初始目录记录。

        返回:
            无。

        异常:
            ValueError: 当存在重复工具名时抛出。

        副作用:
            在内存中保存记录。
        """

        self._records: dict[str, ToolCatalogRecord] = {}
        for record in records:
            self.upsert(record)

    def upsert(self, record: ToolCatalogRecord) -> None:
        """新增或替换一个工具目录记录。

        参数:
            record: 需要保存的目录记录。

        返回:
            无。

        异常:
            ValueError: 当工具名为空时抛出。

        副作用:
            更新内存索引。
        """

        if not record.name.strip():
            raise ValueError("tool catalog name must not be blank")
        self._records[record.name] = record

    def list_records(self) -> list[ToolCatalogRecord]:
        """按名称返回目录记录。

        参数:
            无。

        返回:
            稳定排序后的记录列表。

        异常:
            无。

        副作用:
            无。
        """

        return [self._records[name] for name in sorted(self._records)]
