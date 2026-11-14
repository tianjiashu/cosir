from app.utils.datetime_utils import SESSION_TITLE_LIMIT, preview


def test_session_title_limit_keeps_preview_within_sidebar_budget() -> None:
    result = preview("标题 " * 100, limit=SESSION_TITLE_LIMIT)

    assert len(result) == SESSION_TITLE_LIMIT
    assert result.endswith("...")


def test_preview_handles_limits_shorter_than_ellipsis() -> None:
    assert preview("abcdef", limit=3) == "..."
    assert preview("abcdef", limit=2) == ".."
