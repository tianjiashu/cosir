#!/usr/bin/env python3
"""SQLite 列值归一化原语（log-triage skill 内置）。

单一职责：把「同一个逻辑值在不同 SQLite 存储形态下」的真实取值归一为 Python 值或等价 SQL
表达式，供各查询脚本共用，避免各处自行判断导致口径不一致。

存在理由：布尔列在本项目中由 ORM 写入 INTEGER ``0``/``1``，但手工修复、导入或其它方言下可能
是 TEXT ``'0'``/``'1'``/``'false'``；此时直接用 Python 真值判断（``bool('0') is True``）或在
SQL 里裸比较（``is_streaming = 0`` / ``= 1``）都会把 false 静默判成 true 或整行漏掉。
Python 侧归一口径由 :func:`as_bool` 提供，SQL 侧等价判定由 :func:`bool_true_sql` 提供，两者
共享同一份「假值文本」常量，禁止任何调用方再自写判断。

职责边界：
- 负责：标量值的布尔归一，以及与之语义一致的 SQL 判定片段。
- 不负责：任何 SQL 访问与业务语义。
"""

from __future__ import annotations

from typing import Any

# 「假」值的文本形态（已 strip + lower）；Python 侧与 SQL 侧共享这一份事实来源。
BOOL_FALSE_TEXTS: tuple[str, ...] = ("", "0", "false", "no", "off", "none", "null")
_FALSE_TEXTS = frozenset(BOOL_FALSE_TEXTS)


def as_bool(value: Any) -> bool:
    """把 SQLite 列值归一为布尔。

    参数:
        value: 列值，可能是 ``None`` / ``bool`` / ``int``（0/1）/ ``str``（``"0"``、``"false"``…）。

    返回:
        归一后的布尔值：``None`` 与空串、``0``、``"0"``、``"false"``（大小写不敏感、容忍两侧
        空白）为 ``False``，其余为 ``True``。

    异常:
        无。

    副作用:
        无。
    """

    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() not in _FALSE_TEXTS
    return bool(value)


def bool_true_sql(column: str) -> str:
    """返回与 :func:`as_bool` 语义一致的 SQL「为真」判定表达式。

    用于必须在 SQL 层过滤布尔列的场景（保持 ``LIMIT`` 下推，不做 Python 侧后置过滤），
    避免调用方自写 ``列 = 1`` 而漏掉 TEXT 形态。

    判定「假」的两条分支（与 :func:`as_bool` 一一对应）：
    1. 文本形态（先 ``CAST`` 再 ``lower``/``trim``）落入 :data:`BOOL_FALSE_TEXTS`；``NULL`` 经
       ``COALESCE`` 归一为空串后同属假值，避免 ``NULL NOT IN (...)`` 求值为 ``NULL`` 而丢行。
    2. 列值本身是数值（``typeof`` 为 ``integer``/``real``）且等于 0。这一条不可省略：REAL 形态的
       ``0.0`` CAST 成文本是 ``'0.0'``，并不在假值文本集合里，只靠第 1 条会被判成「真」，与
       ``as_bool(0.0) is False`` 分叉，导致同一行「显示为非流式」却被「--exclude-streaming」当
       流式过滤掉。

    参数:
        column: 列引用片段（如 ``"is_streaming"`` 或 ``"c.is_streaming"``）；必须来自本 skill
            内部常量，**不得**拼接外部输入。

    返回:
        可直接嵌入 ``WHERE`` 的布尔表达式字符串。

    异常:
        无。

    副作用:
        无。
    """

    literals = ", ".join(f"'{item}'" for item in BOOL_FALSE_TEXTS)
    return (
        "NOT ("
        f"COALESCE(lower(trim(CAST({column} AS TEXT))), '') IN ({literals})"
        f" OR (typeof({column}) IN ('integer', 'real') AND CAST({column} AS REAL) = 0)"
        ")"
    )
