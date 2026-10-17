"""重复读取仅约束观察行为；命令结果、心跳输出与上下文隔离不受误判。"""

import pytest

from app.service.terminal.errors import TerminalRepeatedReadError
from app.service.terminal.terminal_observation import TerminalObservation
from app.service.terminal.terminal_read_guard import TerminalReadGuard


def test_four_unchanged_reads_warn_then_fifth_errors_and_new_information_recovers():
    guard = TerminalReadGuard()
    assert guard.observe(1, (1, "running")) is None
    for count in range(1, 5):
        warning = guard.observe(1, (1, "running"))
        assert set(warning) == {"code", "unchanged_reads", "message"}
        assert warning["code"] == "TERMINAL_READ_UNCHANGED"
        assert warning["unchanged_reads"] == count
        assert f"information: {count}." in warning["message"]
        assert "increase wait_ms" in warning["message"]
    for count in (5, 6):
        with pytest.raises(TerminalRepeatedReadError) as caught:
            guard.observe(1, (1, "running"))
        assert caught.value.retryable
        assert f"{count} consecutive reads" in str(caught.value)
        assert "does not stop, fail, or otherwise affect" in str(caught.value)
    assert guard.observe(1, (2, "running")) is None
    assert guard.observe(1, (2, "running"))["unchanged_reads"] == 1
    assert guard.observe(2, (2, "running")) is None
    assert guard.observe(2, (2, "running"))["unchanged_reads"] == 1
    assert guard.observe(2, (2, "exited")) is None


def test_new_information_never_warns_and_resets_an_existing_streak():
    guard = TerminalReadGuard()
    for version in range(1, 10):
        assert guard.observe(1, (version, "running")) is None
        for count in range(1, 5):
            assert guard.observe(1, (version, "running"))["unchanged_reads"] == count


def test_identical_new_history_counts_as_progress_but_cursor_blink_does_not():
    observation = TerminalObservation("g", 1000, "off")
    observation.initialize_screen({"rows": 24, "columns": 80})
    guard = TerminalReadGuard()

    def read():
        return guard.observe(1, (observation.information_version, "running"))

    assert read() is None
    for _ in range(8):
        observation.update_screen(observation.screen, ["heartbeat\n"])
        assert read() is None
    for count in range(1, 5):
        observation.update_screen(
            {
                **observation.screen,
                "revision": count,
                "cursor": {**observation.screen["cursor"], "visible": count % 2 == 0},
            },
            [],
        )
        assert read()["unchanged_reads"] == count
    observation.shell_event({"kind": "command_started", "command": "sleep 10"})
    assert read() is None
    observation.shell_event({"kind": "command_returned", "result": {"success": True}})
    assert read() is None
