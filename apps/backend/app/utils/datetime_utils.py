"""通用工具函数集合。

单一职责：提供不依赖任何项目类型/模型的纯工具函数。
"""

from datetime import UTC, datetime

# Task titles are also rendered in the narrow workspace sidebar. Keep the
# persisted preview bounded so newly-created tasks cannot push sidebar actions
# out of their usable area.
TASK_TITLE_LIMIT = 10


def utc_now() -> datetime:
    """Return the current UTC datetime."""
    return datetime.now(UTC)


def to_text(value: datetime) -> str:
    """Serialize a datetime to ISO-8601 text."""
    return value.isoformat()


def from_text(value: str) -> datetime:
    """Parse ISO-8601 datetime text."""
    return datetime.fromisoformat(value)


def preview(value: str, limit: int = 80) -> str:
    """Return a single-line preview suitable for titles and sidebars.

    Args:
        value: Raw user input or message text.
        limit: Maximum character count for the preview (default 80).

    Returns:
        Whitespace-normalized single-line text whose length never exceeds
        ``limit``; truncated with ``...`` when exceeding the limit.
    """

    normalized = " ".join(value.strip().split())
    if len(normalized) <= limit:
        return normalized
    if limit <= 3:
        return "." * limit
    return f"{normalized[: limit - 3]}..."
