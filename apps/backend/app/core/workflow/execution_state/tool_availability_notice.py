"""本轮工具准入说明：与上次告诉模型的禁用集合比较，只在必要时生成通知文本。

通知消费后是 canonical 历史的固定行，每个 Run 都重复全量说明会让 context 持续膨胀；因此按上次
已告知的集合决定发什么。工具名一律排序，避免同一集合跨进程产生不同文本（前缀缓存）。
"""

from __future__ import annotations

_PREFIX = "Tool availability notice:"


def _names(tools: set[str] | frozenset[str]) -> str:
    return ", ".join(sorted(tools))


def tool_availability_notice(
    announced: frozenset[str] | None,
    banned: frozenset[str],
    allowed: frozenset[str],
) -> str | None:
    """返回本轮应投递的准入通知文本；``None`` 表示无需通知。

    - ``announced`` 为 ``None``（模型此前被告知了什么未知）：完整说明。
    - 集合有变化：说明新禁用 / 恢复的工具，并附当前完整禁用列表，使单条通知自洽。
    - 未变化但仍有禁用：简短提醒，防止模型在长对话里遗忘。
    - 未变化且全部可用：不通知。
    """

    if announced is None:
        return _full_notice(banned, allowed)
    if banned == announced:
        if not banned:
            return None
        return f"{_PREFIX} still disabled for this round, calls to them will be rejected: {_names(banned)}."
    changes = []
    if newly_banned := banned - announced:
        changes.append(f"newly disabled: {_names(newly_banned)}")
    if re_enabled := announced - banned:
        changes.append(f"re-enabled: {_names(re_enabled)}")
    if not banned:
        current = "all tools are available now."
    elif not allowed:
        current = "all tools are disabled now."
    else:
        current = f"currently disabled, calls to them will be rejected: {_names(banned)}."
    return f"{_PREFIX} changed since last round ({'; '.join(changes)}); {current}"


def _full_notice(banned: frozenset[str], allowed: frozenset[str]) -> str:
    if not banned:
        return f"{_PREFIX} all tools are available for this round."
    if not allowed:
        return f"{_PREFIX} all tools are not available for this round."
    return (
        f"{_PREFIX} the following tools are disabled for this round and calls "
        f"to them will be rejected: {_names(banned)}. "
        f"{_names(allowed)} tools available."
    )
