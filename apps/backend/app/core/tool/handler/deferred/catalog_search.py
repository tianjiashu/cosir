"""``tool_search`` 的查询语义：在延迟工具目录里按名字精确选取，或按关键词打分排序。

三种写法（与工具描述一致）：

- ``select:a,b``：按名字精确选取，大小写敏感；目录里没有的名字静默丢弃，已加载的照样返回（要不要重给定义由
  ``tool_search`` 判断）；按写下的顺序返回、去重，不受 ``max_results`` 限制——模型点名要的就是这几个。
- ``+词 其余词``：名字里必须含 ``+`` 后面的词（不区分大小写），再按其余词打分；只有必含词时按名字排序。
- 其余：关键词按名字与描述打分，取前 ``max_results`` 个；一个词都没命中的工具不返回，不为凑数硬塞。

打分只求「名字命中优先于描述命中」这一个序关系，不做模糊匹配与词干还原：目录是几十个工具的量级，模型
看到的是名字目录，关键词多半直接来自名字。
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from app.core.tool.handler.deferred.deferred_catalog import DeferredEntry

SELECT_PREFIX = "select:"
_REQUIRED_PREFIX = "+"
# 名字拆词：服务前缀与工具名之间的 ``__``、蛇形与连字符，以及驼峰。
_NAME_SPLIT = re.compile(r"[_\-\s.]+|(?<=[a-z0-9])(?=[A-Z])")
_WORD = re.compile(r"[a-z0-9]+")

_NAME_PART_SCORE = 10
_NAME_SUBSTRING_SCORE = 5
_DESCRIPTION_SCORE = 2


def search_catalog(
    catalog: Sequence[DeferredEntry], query: str, max_results: int
) -> list[DeferredEntry]:
    """按 ``query`` 从目录里取工具；没有命中时返回空列表（调用方据此回「没有匹配」，不算错误）。"""
    query = query.strip()
    if query.startswith(SELECT_PREFIX):
        return _select(catalog, query[len(SELECT_PREFIX) :])
    terms = query.lower().split()
    required = [term[1:] for term in terms if term.startswith(_REQUIRED_PREFIX) and len(term) > 1]
    ranked = [term for term in terms if not term.startswith(_REQUIRED_PREFIX)]
    candidates = [d for d in catalog if all(term in d.name.lower() for term in required)]
    if not ranked:
        return sorted(candidates, key=lambda d: d.name)[:max_results] if required else []
    scored = [(score, d) for d in candidates if (score := _score(d, ranked)) > 0]
    scored.sort(key=lambda item: (-item[0], item[1].name))
    return [d for _, d in scored[:max_results]]


def _select(catalog: Sequence[DeferredEntry], names: str) -> list[DeferredEntry]:
    by_name = {d.name: d for d in catalog}
    picked: dict[str, DeferredEntry] = {}
    for name in (part.strip() for part in names.split(",")):
        if name in by_name:
            picked.setdefault(name, by_name[name])
    return list(picked.values())


def _score(definition: DeferredEntry, terms: Sequence[str]) -> int:
    name = definition.name.lower()
    name_parts = {part.lower() for part in _NAME_SPLIT.split(definition.name) if part}
    description_words = set(_WORD.findall(definition.description.lower()))
    score = 0
    for term in terms:
        if term in name_parts:
            score += _NAME_PART_SCORE
        elif term in name:
            score += _NAME_SUBSTRING_SCORE
        if term in description_words:
            score += _DESCRIPTION_SCORE
    return score
