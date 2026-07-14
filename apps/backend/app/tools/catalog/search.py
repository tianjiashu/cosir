"""工具目录关键字检索。"""

from app.tools.catalog.index import ToolCatalogIndex
from app.tools.catalog.records import ToolCatalogRecord


class ToolCatalogSearch:
    """按名称、描述和标签检索工具目录。"""

    def search(self, query: str, index: ToolCatalogIndex, limit: int = 8) -> list[ToolCatalogRecord]:
        """返回与查询最相关的目录记录。

        参数:
            query: 用户意图或工作流阶段关键词。
            index: 待检索目录索引。
            limit: 最大结果数。

        返回:
            按简单文本评分和名称排序的目录记录。

        异常:
            ValueError: 当 limit 小于 1 时抛出。

        副作用:
            无。
        """

        if limit < 1:
            raise ValueError("limit must be greater than zero")
        terms = {term for term in query.lower().split() if term}
        scored = []
        for record in index.list_records():
            haystack = " ".join((record.name, record.namespace, record.short_description, *record.tags)).lower()
            score = sum(term in haystack for term in terms)
            if score or not terms:
                scored.append((score, record))
        return [record for _, record in sorted(scored, key=lambda item: (-item[0], item[1].name))[:limit]]
